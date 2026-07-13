import { DatabaseSync } from "node:sqlite";
import { copyFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, join } from "node:path";

const applyChanges = process.argv.includes("--apply");
const apiKey = process.env.GEMINI_API_KEY;
if (!apiKey) {
  throw new Error("GEMINI_API_KEY is not set");
}

const configDirectory = join(homedir(), ".cc-switch");
const databasePath = join(configDirectory, "cc-switch.db");
const backupDirectory = join(configDirectory, "backups");
const claudeDirectory = join(homedir(), ".claude");
const claudeSettingsPath = join(claudeDirectory, "settings.json");
const providerId = "fanvpn-gemini-native";
const bridgeBaseUrl = "http://127.0.0.1:18888/gemini";
const model = "gemini-3.5-flash";

if (!existsSync(databasePath)) {
  throw new Error(`CC Switch database does not exist: ${databasePath}`);
}

const settingsConfig = {
  env: {
    ANTHROPIC_BASE_URL: bridgeBaseUrl,
    ANTHROPIC_API_KEY: apiKey,
    ANTHROPIC_MODEL: model,
    ANTHROPIC_DEFAULT_HAIKU_MODEL: model,
    ANTHROPIC_DEFAULT_SONNET_MODEL: model,
    ANTHROPIC_DEFAULT_OPUS_MODEL: model,
  },
};
const meta = {
  apiFormat: "gemini_native",
  commonConfigEnabled: false,
  endpointAutoSelect: false,
};

const database = new DatabaseSync(databasePath);
try {
  const existing = database
    .prepare("SELECT name, is_current FROM providers WHERE id = ? AND app_type = 'claude'")
    .get(providerId);

  if (!applyChanges) {
    console.log(
      JSON.stringify(
        {
          mode: "dry-run",
          databasePath,
          providerId,
          providerExists: Boolean(existing),
          bridgeBaseUrl,
          model,
          apiKeyPresent: true,
          apiKeyLength: apiKey.length,
          actions: [
            ...(existsSync(claudeSettingsPath)
              ? []
              : ["create the missing Claude Code settings file"]),
            "back up the CC Switch database",
            "upsert the Gemini Native provider for Claude Code",
            "make it the current Claude provider",
            "enable Claude proxy takeover on 127.0.0.1:15721",
          ],
        },
        null,
        2,
      ),
    );
    process.exitCode = 0;
  } else {
    if (!existsSync(claudeSettingsPath)) {
      mkdirSync(claudeDirectory, { recursive: true });
      writeFileSync(claudeSettingsPath, "{}\n", { encoding: "utf8", flag: "wx" });
    }
    mkdirSync(backupDirectory, { recursive: true });
    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const backupPath = join(
      backupDirectory,
      `${basename(databasePath)}.before-fanvpn-${timestamp}.bak`,
    );
    copyFileSync(databasePath, backupPath);

    database.exec("BEGIN IMMEDIATE");
    try {
      const nextSortIndex = Number(
        database
          .prepare(
            "SELECT COALESCE(MAX(sort_index), -1) + 1 AS value FROM providers WHERE app_type = 'claude'",
          )
          .get().value,
      );
      database
        .prepare("UPDATE providers SET is_current = 0 WHERE app_type = 'claude'")
        .run();
      database
        .prepare(
          `INSERT INTO providers (
             id, app_type, name, settings_config, website_url, category,
             created_at, sort_index, notes, icon, icon_color, meta,
             is_current, in_failover_queue, cost_multiplier
           ) VALUES (?, 'claude', ?, ?, ?, 'third_party', ?, ?, ?, 'gemini', ?, ?, 1, 0, '1.0')
           ON CONFLICT(id, app_type) DO UPDATE SET
             name = excluded.name,
             settings_config = excluded.settings_config,
             website_url = excluded.website_url,
             category = excluded.category,
             notes = excluded.notes,
             icon = excluded.icon,
             icon_color = excluded.icon_color,
             meta = excluded.meta,
             is_current = 1`,
        )
        .run(
          providerId,
          "Gemini Native via FanVPN",
          JSON.stringify(settingsConfig),
          "https://ai.google.dev/gemini-api",
          Date.now(),
          nextSortIndex,
          "Claude Code → CC Switch → FanVPN Bridge → Chrome/FanVPN → Google Gemini",
          "#4285F4",
          JSON.stringify(meta),
        );
      database
        .prepare(
          `INSERT INTO provider_endpoints (provider_id, app_type, url, added_at)
           SELECT ?, 'claude', ?, ?
           WHERE NOT EXISTS (
             SELECT 1 FROM provider_endpoints
             WHERE provider_id = ? AND app_type = 'claude' AND url = ?
           )`,
        )
        .run(providerId, bridgeBaseUrl, Date.now(), providerId, bridgeBaseUrl);
      database
        .prepare(
          `UPDATE proxy_config
           SET proxy_enabled = 1,
               enabled = 1,
               listen_address = '127.0.0.1',
               listen_port = 15721,
               updated_at = datetime('now')
           WHERE app_type = 'claude'`,
        )
        .run();
      database.exec("COMMIT");
    } catch (error) {
      database.exec("ROLLBACK");
      throw error;
    }

    console.log(
      JSON.stringify(
        {
          mode: "applied",
          backupPath,
          providerId,
          bridgeBaseUrl,
          model,
          apiKeyStored: true,
          apiKeyPrinted: false,
          proxy: "http://127.0.0.1:15721",
        },
        null,
        2,
      ),
    );
  }
} finally {
  database.close();
}

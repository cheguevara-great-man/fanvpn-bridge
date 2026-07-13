import { DatabaseSync } from "node:sqlite";
import { homedir } from "node:os";
import { join } from "node:path";

const databasePath = join(homedir(), ".cc-switch", "cc-switch.db");
const database = new DatabaseSync(databasePath, { readOnly: true });
const includeSchema = !process.argv.includes("--summary");

try {
  const objects = includeSchema
    ? database
        .prepare(
          "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name",
        )
        .all()
    : undefined;

  const redactSecrets = (rawJson) => {
    const redact = (value, parentKey = "") => {
      if (/(KEY|TOKEN|SECRET|PASSWORD)/i.test(parentKey) && value != null) {
        return "<redacted>";
      }
      if (Array.isArray(value)) return value.map((entry) => redact(entry));
      if (value && typeof value === "object") {
        return Object.fromEntries(
          Object.entries(value).map(([key, entry]) => [key, redact(entry, key)]),
        );
      }
      return value;
    };
    return redact(JSON.parse(rawJson));
  };

  const providers = database
    .prepare(
      "SELECT id, app_type, name, settings_config, category, meta, is_current FROM providers ORDER BY app_type, sort_index, name",
    )
    .all()
    .map((provider) => ({
      ...provider,
      settings_config: redactSecrets(provider.settings_config),
      meta: JSON.parse(provider.meta),
    }));
  const proxyConfig = database
    .prepare("SELECT * FROM proxy_config ORDER BY app_type")
    .all();
  const settings = database
    .prepare("SELECT key, value FROM settings ORDER BY key")
    .all()
    .map(({ key, value }) =>
      includeSchema
        ? { key, value: /key|token|secret|password/i.test(key) ? "<redacted>" : value }
        : { key },
    );

  console.log(
    JSON.stringify({ databasePath, objects, providers, proxyConfig, settings }, null, 2),
  );
} finally {
  database.close();
}

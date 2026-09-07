import { expect, test } from "bun:test";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { defaultConfig } from "../src/config";
import { getCodexConfigPath, getCodexModelsCachePath, getCodexHome } from "../src/codex-integration-shared";
import { startServer } from "../src/server";

test("Bridge-managed setup owns only shadow config and preserves real rollout home", () => {
  const keys = ["BRIDGE_WEB_MANAGED", "CODEX_CHATGPT_WEB_HOME", "CODEX_HOME"] as const;
  const previous = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  const root = join(process.cwd(), "build", "managed-test");
  try {
    process.env.BRIDGE_WEB_MANAGED = "1";
    process.env.CODEX_CHATGPT_WEB_HOME = join(root, "web");
    process.env.CODEX_HOME = join(root, "real-codex");
    expect(getCodexConfigPath()).toBe(join(root, "web", "integration", "config.toml"));
    expect(getCodexModelsCachePath()).toBe(join(root, "web", "integration", "models_cache.json"));
    expect(getCodexHome()).toBe(join(root, "real-codex"));
    expect(defaultConfig().subagentProtocol).toBe("native");
  } finally {
    for (const key of keys) {
      if (previous[key] === undefined) delete process.env[key];
      else process.env[key] = previous[key];
    }
  }
});

test("Bridge model export satisfies the Launcher catalog readiness proof", async () => {
  const keys = ["BRIDGE_WEB_MANAGED", "CODEX_CHATGPT_WEB_HOME"] as const;
  const previous = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  const root = join(process.cwd(), "build", "managed-catalog-test");
  let server: ReturnType<typeof startServer> | undefined;
  try {
    process.env.BRIDGE_WEB_MANAGED = "1";
    process.env.CODEX_CHATGPT_WEB_HOME = join(root, "web");
    mkdirSync(process.env.CODEX_CHATGPT_WEB_HOME, { recursive: true });
    writeFileSync(join(process.env.CODEX_CHATGPT_WEB_HOME, "native-model-template.json"), JSON.stringify({
      models: [{
        slug: "gpt-5.6-sol",
        display_name: "GPT-5.6 Sol",
        visibility: "list",
        supported_in_api: true,
        supported_reasoning_levels: ["medium"],
        tool_mode: "code_mode_only",
      }],
    }));
    const config = { ...defaultConfig("browser-only"), port: 0 };
    server = startServer(config);
    const endpoint = `http://127.0.0.1:${server.port}`;
    const headers = { "x-bridge-web-token": config.controlToken };
    const models = await fetch(`${endpoint}/bridge/models`, { headers });
    expect(models.status).toBe(200);
    const body = await models.json() as { models: Array<{ slug?: unknown }> };
    expect(body.models.some(model => model.slug === "chatgpt-web/light")).toBe(true);
    const health = await (await fetch(`${endpoint}/bridge/health`, { headers })).json() as Record<string, unknown>;
    expect(health.successful_model_catalog_requests).toBe(1);
    expect(typeof health.last_successful_model_catalog_request_at).toBe("string");
  } finally {
    if (server) await server.stop(true);
    rmSync(root, { recursive: true, force: true });
    for (const key of keys) {
      if (previous[key] === undefined) delete process.env[key];
      else process.env[key] = previous[key];
    }
  }
});

import { expect, test } from "bun:test";
import { join } from "node:path";
import { defaultConfig } from "../src/config";
import { getCodexConfigPath, getCodexModelsCachePath, getCodexHome } from "../src/codex-integration-shared";

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

import { expect, test } from "bun:test";
import { resolve } from "node:path";
import { parseRequest } from "../src/responses/parser";
import { extractChatGptTurnEnvironment } from "../src/adapters/chatgpt-web/environment";

test("Bridge reasoning normalization preserves Codex environment provenance on follow-ups", () => {
  const cwd = resolve(process.cwd());
  const body = {
    model: "chatgpt-web/high", stream: true,
    client_metadata: { "x-codex-turn-metadata": JSON.stringify({
      thread_id: "thread_test", turn_id: "turn_second", sandbox: "none", workspaces: { [cwd]: {} },
    }) },
    input: [
      { type: "message", id: "context_native", role: "user", content: [{type: "input_text", text:
        `<environment_context><cwd>${cwd}</cwd><sandbox_mode>danger-full-access</sandbox_mode></environment_context>`}] },
      { type: "message", id: "user_native", role: "user", content: [{type: "input_text", text: "Continue"}] },
      { type: "reasoning", id: "rs_0123456789abcdef0123456789abcdef", summary: [{type: "summary_text", text: "Planning"}] },
    ],
  };
  expect(extractChatGptTurnEnvironment(parseRequest(body)).cwd).toBe(cwd);
  const oldBroken = { ...body, input: body.input.map(({id, ...item}) => item) };
  expect(() => extractChatGptTurnEnvironment(parseRequest(oldBroken))).toThrow("missing cwd");
  const result = Bun.spawnSync(["python", "-c",
    "import json,sys; from fanvpn_bridge.web_harness import clean_web_history; print(json.dumps(clean_web_history(json.load(sys.stdin))))"], {
      cwd: resolve(cwd, "../.."),
      env: { ...process.env, PYTHONPATH: resolve(cwd, "../../native-host") },
      stdin: Buffer.from(JSON.stringify(body)),
  });
  expect(result.exitCode).toBe(0);
  const forwarded = JSON.parse(result.stdout.toString());
  expect(forwarded.input[0].id).toBe("context_native");
  expect(extractChatGptTurnEnvironment(parseRequest(forwarded)).cwd).toBe(cwd);
});

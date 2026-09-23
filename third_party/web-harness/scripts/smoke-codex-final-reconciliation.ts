import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { createInterface } from "node:readline";
import { bridgeToResponsesSSE } from "../src/bridge";
import { defaultConfig } from "../src/config";
import { augmentNativeModelCatalog } from "../src/model-catalog";
import type { AdapterEvent } from "../src/types";

const codex = resolve(process.argv[2] ?? "");
if (!existsSync(codex)) throw new Error("Pass the native Codex executable as the first argument");

const bundled = spawnSync(codex, ["debug", "models", "--bundled"], {
  encoding: "utf8",
  stdio: ["ignore", "pipe", "pipe"],
  timeout: 15_000,
});
if (bundled.status !== 0) throw new Error(`Could not read bundled Codex models: ${bundled.stderr}`);
const config = defaultConfig("browser-only");
const catalog = augmentNativeModelCatalog(JSON.parse(bundled.stdout), config);
const root = mkdtempSync(join(tmpdir(), "webharness-final-reconciliation-"));
const probeCodexHome = join(root, "codex");
mkdirSync(probeCodexHome);
writeFileSync(join(root, "models.json"), `${JSON.stringify(catalog)}\n`);

const draft = "DRAFT_ONLY_7f2a";
const final = "FINAL_ONLY_9c4b";
const second = "SECOND_TURN_COMPLETE";
const requests: unknown[] = [];
let nextResponseDraft = false;

async function* answer(text: string, finalText?: string): AsyncGenerator<AdapterEvent> {
  yield { type: "text_delta", text, phase: "final_answer" };
  yield { type: "done", endTurn: true, ...(finalText === undefined ? {} : { finalText }) };
}

const server = Bun.serve({
  hostname: "127.0.0.1",
  port: 0,
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/v1/models") return Response.json(catalog);
    if (url.pathname !== "/v1/responses" || request.method !== "POST") return new Response("Not found", { status: 404 });
    requests.push(await request.json());
    const first = requests.length === 1 || nextResponseDraft;
    nextResponseDraft = false;
    const text = await new Response(bridgeToResponsesSSE(answer(first ? draft : second, first ? final : undefined), "chatgpt-web/high")).text();
    const frames = text.split("\n\n");
    const body = new ReadableStream<Uint8Array>({
      async start(controller) {
        const encoder = new TextEncoder();
        for (const frame of frames) {
          if (!frame) continue;
          controller.enqueue(encoder.encode(`${frame}\n\n`));
          if (first && frame.startsWith("event: response.output_text.delta")) await Bun.sleep(1000);
        }
        controller.close();
      },
    });
    return new Response(body, { headers: { "content-type": "text/event-stream", "cache-control": "no-cache" } });
  },
});

writeFileSync(join(probeCodexHome, "config.toml"), [
  'model = "chatgpt-web/high"',
  'model_provider = "final-reconciliation-probe"',
  `model_catalog_json = ${JSON.stringify(join(root, "models.json"))}`,
  "",
  "[model_providers.final-reconciliation-probe]",
  'name = "Local final reconciliation probe"',
  `base_url = "http://127.0.0.1:${server.port}/v1"`,
  'env_key = "OPENAI_API_KEY"',
  'wire_api = "responses"',
  "supports_websockets = false",
  "",
].join("\n"));

async function runCodex(args: string[]): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  const child = Bun.spawn([codex, ...args], {
    cwd: root,
    env: { ...process.env, CODEX_HOME: probeCodexHome, OPENAI_API_KEY: "local-final-probe" },
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  const timeout = setTimeout(() => child.kill(), 25_000);
  try {
    const [exitCode, stdout, stderr] = await Promise.all([
      child.exited,
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
    ]);
    return { exitCode, stdout, stderr };
  } finally {
    clearTimeout(timeout);
  }
}

async function runAppServer(): Promise<{
  streamedDraft: boolean;
  completedFinal: boolean;
  turnCompleted: boolean;
  followupHistoryFinal: boolean;
  followupHistoryDraft: boolean;
  error?: string;
}> {
  const child = spawn(codex, ["app-server"], {
    cwd: root,
    env: { ...process.env, CODEX_HOME: probeCodexHome, OPENAI_API_KEY: "local-final-probe" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const messages: Array<Record<string, any>> = [];
  let wake: (() => void) | undefined;
  const lineReader = createInterface({ input: child.stdout! });
  lineReader.on("line", line => {
    try {
      messages.push(JSON.parse(line));
      wake?.();
      wake = undefined;
    } catch { /* Ignore non-protocol diagnostics. */ }
  });
  let stderr = "";
  child.stderr?.on("data", chunk => { stderr += String(chunk).slice(0, 1000); });
  const timeout = setTimeout(() => child.kill(), 30_000);
  const send = (message: unknown) => child.stdin!.write(`${JSON.stringify(message)}\n`);
  const next = async (): Promise<Record<string, any>> => {
    while (messages.length === 0) {
      if (child.exitCode !== null) throw new Error(`App server exited: ${stderr.slice(0, 500)}`);
      await new Promise<void>(resolveWait => { wake = resolveWait; });
    }
    return messages.shift()!;
  };
  const until = async (predicate: (message: Record<string, any>) => boolean): Promise<Record<string, any>> => {
    for (;;) {
      const message = await next();
      if (message.error) throw new Error(`App server protocol error: ${JSON.stringify(message.error)}`);
      if (predicate(message)) return message;
    }
  };
  try {
    send({ method: "initialize", id: 0, params: { clientInfo: { name: "webharness_final_probe", title: "WebHarness Final Probe", version: "0.1.0" } } });
    await until(message => message.id === 0);
    send({ method: "initialized", params: {} });
    send({ method: "thread/start", id: 1, params: { model: "chatgpt-web/high", cwd: root, approvalPolicy: "never", sandbox: "read-only" } });
    const thread = await until(message => message.id === 1);
    const threadId = thread.result?.thread?.id;
    if (typeof threadId !== "string") throw new Error("App server did not return a thread id");
    send({ method: "turn/start", id: 2, params: { threadId, input: [{ type: "text", text: "Reply using the local probe response." }] } });
    const observed: Array<Record<string, any>> = [];
    for (;;) {
      const message = await next();
      if (message.error) throw new Error(`App server turn error: ${JSON.stringify(message.error)}`);
      observed.push(message);
      if (message.method === "turn/completed") break;
    }
    send({ method: "turn/start", id: 3, params: { threadId, input: [{ type: "text", text: "Reply again using the local probe response." }] } });
    await until(message => message.method === "turn/completed");
    const followupRequest = JSON.stringify(requests.at(-1) ?? {});
    return {
      streamedDraft: observed.some(message => message.method === "item/agentMessage/delta" && JSON.stringify(message.params).includes(draft)),
      completedFinal: observed.some(message => message.method === "item/completed" && JSON.stringify(message.params).includes(final)),
      turnCompleted: observed.some(message => message.method === "turn/completed" && message.params?.turn?.status === "completed"),
      followupHistoryFinal: followupRequest.includes(final),
      followupHistoryDraft: followupRequest.includes(draft),
    };
  } catch (error) {
    return { streamedDraft: false, completedFinal: false, turnCompleted: false,
      followupHistoryFinal: false, followupHistoryDraft: false,
      error: error instanceof Error ? error.message : String(error) };
  } finally {
    clearTimeout(timeout);
    child.kill();
    if (child.exitCode === null) await new Promise<void>(resolveExit => child.once("close", () => resolveExit()));
    lineReader.close();
  }
}

try {
  const first = await runCodex([
    "exec", "--skip-git-repo-check", "--json", "--sandbox", "read-only",
    "--model", "chatgpt-web/high", "Reply using the local probe response.",
  ]);
  const followup = first.exitCode === 0 ? await runCodex([
    "exec", "resume", "--last", "--skip-git-repo-check", "--json",
    "Reply using the local probe response again.",
  ]) : undefined;
  const sessionRoot = join(probeCodexHome, "sessions");
  const sessionFiles = existsSync(sessionRoot)
    ? readdirSync(sessionRoot, { recursive: true }).filter(name => String(name).endsWith(".jsonl"))
    : [];
  const recorded = sessionFiles.map(name => readFileSync(join(sessionRoot, String(name)), "utf8")).join("\n");
  nextResponseDraft = true;
  const appServer = first.exitCode === 0 && followup?.exitCode === 0 && requests.length === 2
    ? await runAppServer()
    : undefined;
  const result = {
    firstExitCode: first.exitCode,
    followupExitCode: followup?.exitCode,
    requests: requests.length,
    firstStdoutDraft: first.stdout.includes(draft),
    firstStdoutFinal: first.stdout.includes(final),
    rolloutDraft: recorded.includes(draft),
    rolloutFinal: recorded.includes(final),
    followupRequestDraft: JSON.stringify(requests[1] ?? {}).includes(draft),
    followupRequestFinal: JSON.stringify(requests[1] ?? {}).includes(final),
    appServer,
    stderr: [first.stderr, followup?.stderr].filter(Boolean).join("\n").slice(0, 1200),
  };
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  if (first.exitCode !== 0 || followup?.exitCode !== 0 || requests.length !== 4
    || !appServer?.streamedDraft || !appServer.completedFinal || !appServer.turnCompleted
    || !appServer.followupHistoryFinal || appServer.followupHistoryDraft) process.exitCode = 1;
} finally {
  await server.stop(true);
  const resolved = realpathSync(root);
  if (dirname(resolved) !== realpathSync(tmpdir()) || !basename(resolved).startsWith("webharness-final-reconciliation-")) {
    throw new Error(`Refusing to remove unexpected test directory: ${resolved}`);
  }
  rmSync(resolved, { recursive: true, force: true, maxRetries: 8, retryDelay: 200 });
}

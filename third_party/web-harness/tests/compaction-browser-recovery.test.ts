import { expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ChatGptBrowserWorker, type BrowserTurn } from "../src/adapters/chatgpt-web/browser-worker";
import { createChatGptWebAdapter } from "../src/adapters/chatgpt-web/index";
import { resolveChatGptWebModelMode } from "../src/adapters/chatgpt-web/model";
import { ChatGptExternalTurnProgress } from "../src/adapters/chatgpt-web/turn-progress";
import type { AdapterEvent, CodexParsedRequest, CodexProviderConfig } from "../src/types";

test.each([[true, false, true], [false, false, true], [true, true, true], [true, false, false]])("browser turns preserve recovery, ordering and final-only tools (owned=%s, tools=%s, multipart=%s)", async (owned, tools, multipart) => {
  const diagnostics = mkdtempSync(join(tmpdir(), "compaction-observation-"));
  const finalResponse = new Error("fixture reached final response observation");
  const capabilities = { localToolsEnabled: tools, solAvailable: true, proAvailable: true };
  const progress = tools ? new ChatGptExternalTurnProgress() : undefined;
  const recoveryCallbacks: unknown[] = [];
  const actions: string[] = [];
  const sendBudgets: number[] = [];
  let stage = "";
  let released = false;
  let sendActivated = 0;
  let submitted = 0;
  const page = {
    evaluate: async () => ({}),
    isClosed: () => false,
    on: () => {},
    off: () => {},
  };
  const worker = Object.assign(Object.create(ChatGptBrowserWorker.prototype), {
    config: { appName: "Codex Native2", browserDiagnosticsPath: diagnostics, ...(owned ? { browserHostDescriptorPath: "owned-descriptor" } : {}) },
    runStage: async (_trace: string, name: string, timeout: number, action: (signal: AbortSignal) => Promise<unknown>) => {
      stage = name;
      if (name === "send" || name.endsWith("_send")) sendBudgets.push(timeout);
      return action(new AbortController().signal);
    },
    prepareChatSurface: async () => {},
    selectModelAndEffort: async (_page: unknown, model: string, effort: string) => {
      actions.push(`effort:${effort}`);
      return resolveChatGptWebModelMode(model, effort, capabilities);
    },
    captureSubmissionBaseline: async () => ({}),
    attachPrompt: async (_page: unknown, _text: string, localTools: boolean) => {
      expect(localTools).toBe(false);
      actions.push("attach:plain");
    },
    attachPromptWithCompactionRetry: async (_page: unknown, _text: string, localTools: boolean) => {
      expect(localTools).toBe(tools);
      actions.push(localTools ? "attach:tools" : "attach:plain");
    },
    attachFiles: async () => { actions.push("files"); },
    sendAttachedPrompt: async (...args: unknown[]) => {
      // Context ingestion cannot mistake tool activity for acknowledgement of a part.
      expect(args[4]).toBe(stage === "send" ? progress : undefined);
      const lifecycle = args[5] as { onSendActivated(): void; onSubmitted(): void };
      expect(typeof lifecycle.onSendActivated).toBe("function");
      lifecycle.onSendActivated();
      lifecycle.onSubmitted();
      recoveryCallbacks.push(args[7]);
      actions.push("send");
      return "user_turn";
    },
    waitForNewAssistantTurn: async (...args: unknown[]) => {
      expect(args[4]).toBe(stage === "send" ? progress : undefined);
      recoveryCallbacks.push(args[7]);
      actions.push("observe");
      if (stage === "send") throw finalResponse;
      return {};
    },
    waitForMultipartAcknowledgement: async () => { actions.push("ack"); },
  });
  try {
    await expect(worker.runBrowserTurn({
      traceId: "compaction_recovery_fixture",
      modelId: "gpt-5.6-sol",
      reasoning: "high",
      capabilities,
      compaction: !tools,
      onSendActivated: () => { sendActivated++; },
      onSubmitted: () => { submitted++; },
      externalProgress: progress,
      completionFence: tools ? {
        begin: async () => { throw new Error("fixture must stop before completion"); },
        commit: async () => { throw new Error("fixture must stop before completion"); },
      } : undefined,
      prepare: async () => ({ text: "Summarize the context", images: [], multipart: multipart ? { parts: ['{"part":1}', '{"part":2}', '{"part":3}'], commit: "Summarize" } : undefined, release: () => { released = true; } }),
    }, owned ? "owned-surface" : undefined, page)).rejects.toBe(finalResponse);
    expect(recoveryCallbacks.map(callback => typeof callback)).toEqual(
      Array(multipart ? 6 : 2).fill(owned ? "function" : "undefined"),
    );
    expect(actions).toEqual([
      "effort:high",
      ...(multipart ? [
        "attach:plain", "send", "observe", "ack",
        "attach:plain", "send", "observe", "ack",
      ] : []),
      tools ? "attach:tools" : "attach:plain", "files", "send", "observe",
    ]);
    expect(sendBudgets).toEqual(multipart ? [180_000, 180_000, 180_000] : [20_000]);
    expect(sendActivated).toBe(multipart ? 3 : 1);
    expect(submitted).toBe(multipart ? 3 : 1);
    expect(released).toBe(true);
  } finally {
    rmSync(diagnostics, { recursive: true, force: true });
  }
});

test.each([false, true])("a first multipart send failure cannot start another browser turn on reconnect (accepted=%s)", async accepted => {
  const diagnostics = mkdtempSync(join(tmpdir(), "multipart-send-reconnect-"));
  const capabilities = { localToolsEnabled: false, solAvailable: true, proAvailable: true };
  const provider: CodexProviderConfig = {
    adapter: "chatgpt-web",
    baseUrl: `browser://multipart-send-reconnect-${diagnostics}`,
    chatgptWeb: capabilities,
  };
  const worker = ChatGptBrowserWorker.forProvider(provider);
  const originalRun = worker.run;
  let browserStarts = 0;
  let sends = 0;
  let released = false;
  const page = { evaluate: async () => ({}), isClosed: () => false, on: () => {}, off: () => {} };
  const duplicateIdentity = new Error("ChatGPT exposed duplicate conversation turn identities");
  const fixture = Object.assign(Object.create(ChatGptBrowserWorker.prototype), {
    config: { appName: "Codex Native2", browserDiagnosticsPath: diagnostics },
    runStage: async (_trace: string, _name: string, _timeout: number, action: (signal: AbortSignal) => Promise<unknown>) => (
      action(new AbortController().signal)
    ),
    prepareChatSurface: async () => {},
    selectModelAndEffort: async (_page: unknown, model: string, effort: string) => (
      resolveChatGptWebModelMode(model, effort, capabilities)
    ),
    captureSubmissionBaseline: async () => ({}),
    attachPrompt: async () => {},
    sendAttachedPrompt: async (...args: unknown[]) => {
      sends++;
      const lifecycle = args[5] as BrowserTurn | undefined;
      await lifecycle?.onSendActivated?.();
      if (!accepted) throw duplicateIdentity;
      await lifecycle?.onSubmitted?.();
      return "user_turn";
    },
    waitForNewAssistantTurn: async () => { throw duplicateIdentity; },
  });
  worker.run = async turn => {
    browserStarts++;
    return fixture.runBrowserTurn({
      ...turn,
      prepare: async () => ({
        text: "Read the context",
        images: [],
        multipart: { parts: ['{"part":1}', '{"part":2}'], commit: "Answer" },
        release: () => { released = true; },
      }),
    }, undefined, page);
  };
  try {
    const adapter = createChatGptWebAdapter(provider);
    const request: CodexParsedRequest = {
      modelId: "gpt-5.6-sol",
      stream: true,
      context: { messages: [{ role: "user", content: "Read the context", timestamp: 1 }], tools: [] },
      options: {},
      _rawBody: {
        client_metadata: {
          "x-codex-turn-metadata": JSON.stringify({ thread_id: diagnostics, turn_id: "first-part" }),
        },
        input: [{
          type: "message", role: "user", content: [{ type: "input_text", text: "Read the context" }],
          internal_chat_message_metadata_passthrough: { turn_id: "first-part" },
        }],
      },
    };
    for (let attempt = 0; attempt < 2; attempt++) {
      const events: AdapterEvent[] = [];
      await adapter.runTurn!(request, { headers: new Headers() }, event => events.push(event));
      expect(events.at(-1)).toMatchObject({
        type: "error",
        code: accepted ? "chatgpt_submitted_turn_failed" : "chatgpt_submission_ambiguous",
        retryable: false,
      });
    }
    expect(browserStarts).toBe(1);
    expect(sends).toBe(1);
    expect(released).toBe(true);
  } finally {
    worker.run = originalRun;
    rmSync(diagnostics, { recursive: true, force: true });
  }
});

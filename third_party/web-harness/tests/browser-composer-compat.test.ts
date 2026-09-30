import { expect, test } from "bun:test";
import { ChatGptBrowserWorker, clearChatGptComposerText, CHATGPT_COMPOSER_SELECT_ALL_KEY } from "../src/adapters/chatgpt-web/browser-worker";
import { CHATGPT_CONTEXT_MENU_ROW_SELECTOR, CHATGPT_SELECTED_CONNECTOR_SELECTOR } from "../src/chatgpt-session";
import type { Locator } from "playwright-core";
import { createContext, runInContext } from "node:vm";

function markdownConnectorFixture(choiceCount = 1) {
  const { createDocument } = require("@mixmark-io/domino") as { createDocument(html: string): Document };
  const document = createDocument('<span app-mention-path="app://test" app-mention-display-name="Codex Native2">Codex Native2</span>');
  const calls: string[] = [];
  let selected = false;
  const pill = {
    filter() { return this; },
    evaluateAll: async (read: (elements: Element[]) => unknown) => read(selected ? [document.querySelector("span")!] : []),
    waitFor: async () => { expect(selected).toBeTrue(); },
  };
  const choice = {
    waitFor: async () => {
      if (!choiceCount) { const error = new Error("missing"); error.name = "TimeoutError"; throw error; }
    },
    count: async () => choiceCount,
    getAttribute: async (name: string) => name === "aria-current" ? "true" : null,
  };
  const rows = {
    filter: (options: { visible?: boolean }) => options.visible ? rows : choice,
    allInnerTexts: async () => choiceCount ? ["Codex Native2"] : [],
  };
  const composer = {
    getAttribute: async (name: string) => name === "data-composer-markdown" ? "" : null,
    focus: async () => {},
    pressSequentially: async (text: string) => { expect(text).toBe("@codex"); calls.push("mention"); },
    press: async (key: string) => {
      if (key === "Backspace") { selected = false; calls.push("clear"); }
      else if (key === "Enter") { selected = true; calls.push("select-exact-app"); }
      else expect(key).toBe(CHATGPT_COMPOSER_SELECT_ALL_KEY);
    },
    locator: (selector: string) => { expect(selector).toBe(CHATGPT_SELECTED_CONNECTOR_SELECTOR); return pill; },
  };
  const page = {
    getByRole: (_role: string, options: { name: RegExp }) => ({
      filter: () => ({ count: async () => options.name.test("Personalized") ? 1 : 0 }),
    }),
    getByText: (name: string, options: { exact: boolean }) => {
      expect(name).toBe("Codex Native2"); expect(options.exact).toBeTrue(); return {};
    },
    locator: (selector: string) => { expect(selector).toBe(CHATGPT_CONTEXT_MENU_ROW_SELECTOR); return rows; },
  };
  const worker = Object.assign(Object.create(ChatGptBrowserWorker.prototype), {
    config: { appName: "Codex Native2" },
    activeComposer: async () => composer,
    clearChatGptComposerState: async () => { selected = false; calls.push("cleanup"); },
  }) as {
    selectConnector(page: unknown): Promise<unknown>;
    connectorIsSelected(composer: unknown): Promise<boolean>;
  };
  return { worker, page, composer, calls };
}

test("Markdown composer uses @codex and the current keyboard highlight, then reuses the proven pill", async () => {
  const { worker, page, composer, calls } = markdownConnectorFixture();
  await expect(worker.selectConnector(page)).resolves.toBe(composer);
  expect(calls).toEqual(["clear", "clear", "mention", "select-exact-app"]);
  expect(await worker.connectorIsSelected(composer)).toBeTrue();
  await expect(worker.selectConnector(page)).resolves.toBe(composer);
  expect(calls).toEqual(["clear", "clear", "mention", "select-exact-app"]);
});

test.each([0, 2])("Markdown connector selection rejects missing or duplicate exact rows (%s) and clears the draft", async count => {
  const { worker, page, composer, calls } = markdownConnectorFixture(count);
  await expect(worker.selectConnector(page)).rejects.toMatchObject({ code: "connector_not_found" });
  expect(calls.at(-1)).toBe("cleanup");
  expect(calls.filter(call => call === "mention")).toHaveLength(count ? 1 : 3);
  expect(await worker.connectorIsSelected(composer)).toBeFalse();
});

test("ProseMirror clears its previous document through native editing before the next prompt", async () => {
  let draft = "previous unsent prompt";
  const calls: string[] = [];
  const signal = new AbortController().signal;
  const composer = {
    getAttribute: async () => "",
    fill: async () => { throw new Error("fill must not be used for ProseMirror"); },
    focus: async (options: { signal?: AbortSignal }) => { expect(options.signal).toBe(signal); calls.push("focus"); },
    press: async (key: string, options: { signal?: AbortSignal }) => {
      expect(options.signal).toBe(signal); calls.push(key);
      if (key === "Backspace") draft = "";
    },
  } as unknown as Locator;
  await clearChatGptComposerText(composer, signal);
  expect(draft).toBe("");
  expect(calls).toEqual(["focus", CHATGPT_COMPOSER_SELECT_ALL_KEY, "Backspace"]);
});

test("legacy composer keeps its existing fill-based clearing", async () => {
  let draft = "previous draft";
  const composer = {
    getAttribute: async () => null,
    fill: async (value: string) => { draft = value; },
  } as unknown as Locator;
  await clearChatGptComposerText(composer);
  expect(draft).toBe("");
});

test("current conversation layout uses stable turn keys for send evidence and ignores display renumbering", async () => {
  let turns = [{ id: "old-user-message-id", displayKey: "fallback-turn-0", answered: true }];
  const observers: (() => void)[] = [];
  const element = (turn: typeof turns[number]) => ({
    getAttribute: (name: string) => name === "data-turn-key" ? turn.id : null,
  });
  const context = createContext({
    performance: { timeOrigin: 1 },
    document: {
      documentElement: {},
      querySelectorAll: (selector: string) => {
        if (selector === "[data-turn-id-container]") return [];
        if (selector === "[data-turn-key]") return turns.map(element);
        if (selector.includes(':assistant')) return turns.filter(turn => turn.answered).map(element);
        if (selector.includes(':user')) return turns.map(element);
        return [];
      },
    },
    MutationObserver: class {
      constructor(callback: () => void) { observers.push(callback); }
      observe() {}
    },
  });
  const page = {
    evaluate: async (callback: Function, options: unknown) => runInContext(`(${callback.toString()})`, context)(options),
    locator: () => ({}),
  };
  const worker = Object.create(ChatGptBrowserWorker.prototype) as {
    captureSubmissionBaseline(page: unknown): Promise<{ initialTurnIdentities: string[]; domCache: unknown }>;
    currentSubmissionEvidence(page: unknown, baseline: unknown): Promise<string | undefined>;
    submissionDomState(page: unknown): Promise<{ responseIdentities: string[] }>;
  };
  const baseline = await worker.captureSubmissionBaseline(page);
  expect([...baseline.initialTurnIdentities]).toEqual(["old-user-message-id"]);
  turns[0]!.displayKey = "fallback-turn-8";
  observers.forEach(notify => notify());
  expect(await worker.currentSubmissionEvidence(page, baseline)).toBeUndefined();
  turns.push({ id: "new-user-message-id", displayKey: "fallback-turn-9", answered: false });
  observers.forEach(notify => notify());
  expect(await worker.currentSubmissionEvidence(page, baseline)).toBe("user_turn");
  turns[1]!.answered = true;
  expect([...(await worker.submissionDomState(page)).responseIdentities])
    .toEqual(["old-user-message-id", "new-user-message-id"]);
  turns.push(turns[1]!);
  await expect(worker.submissionDomState(page)).rejects.toThrow("duplicate");
});

test("prompt readback excludes the app mention without changing Markdown, Chinese, or quotes", async () => {
  const { createDocument } = require("@mixmark-io/domino") as { createDocument(html: string): Document };
  const document = createDocument('<div><p><span app-mention-path="app://test" app-mention-display-name="Codex Native2">Codex Native2</span> # 中文 "引号"</p><p>\\path and \`code\`</p></div>');
  const composer = { evaluate: async (read: (element: Element) => unknown) => read(document.querySelector("div")!) };
  const worker = Object.create(ChatGptBrowserWorker.prototype) as { attachedPromptText(page: unknown, signal: unknown, composer: unknown): Promise<string> };
  expect(await worker.attachedPromptText({}, undefined, composer)).toBe('# 中文 "引号"\n\\path and \`code\`');
});

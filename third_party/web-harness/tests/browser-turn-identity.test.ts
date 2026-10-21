import { expect, test } from "bun:test";
import { createContext, runInContext } from "node:vm";
import type { Page } from "playwright-core";
import { ChatGptBrowserWorker } from "../src/adapters/chatgpt-web/browser-worker";

type TurnFixture = { identity: string; units: string[] };
type SubmissionState = {
  turnIdentities: string[];
  userIdentities: string[];
  responseIdentities: string[];
  promotedTurnIdentities: Record<string, string>;
};

// Execute the production DOM reader, not a separate implementation of its alias rules.
async function submissionState(turns: TurnFixture[]): Promise<SubmissionState> {
  const containers = turns.map(turn => {
    const container = {
      getAttribute: (name: string) => name === "data-turn-key" ? turn.identity : null,
      querySelectorAll: (_selector: string) => turn.units.map(key => ({
        getAttribute: () => key,
        closest: () => container,
      })),
    };
    return container;
  });
  const context = createContext({
    performance: { timeOrigin: 1 },
    document: {
      documentElement: {},
      querySelectorAll: (selector: string) => {
        if (selector === "[data-turn-key]") return containers;
        const role = selector.includes('="assistant"') ? "assistant"
          : selector.includes('="user"') ? "user" : undefined;
        return role ? containers.filter((_, index) => turns[index]!.units.some(key => key.endsWith(`:${role}`))) : [];
      },
    },
    MutationObserver: class { observe() {} },
  });
  const page = {
    evaluate: async (callback: Function, options: unknown) =>
      runInContext(`(${callback.toString()})`, context)(options),
  } as unknown as Page;
  const worker = Object.create(ChatGptBrowserWorker.prototype) as {
    submissionDomState(page: Page): Promise<SubmissionState>;
  };
  return worker.submissionDomState(page);
}

test("the completed renderer proves fallback-to-message-ID promotion using both search-unit roles", async () => {
  const state = await submissionState([{
    identity: "user-message-id",
    units: ["fallback-turn-0:0:user", "fallback-turn-0:2:assistant"],
  }]);
  expect(state.promotedTurnIdentities).toEqual({ "fallback-turn-0": "user-message-id" });
  expect(state.userIdentities).toEqual(["user-message-id"]);
  expect(state.responseIdentities).toEqual(["user-message-id"]);
});

test("promotion requires matching role keys and cannot be inferred from a display index alone", async () => {
  for (const units of [
    ["fallback-turn-0:2:assistant"],
    ["fallback-turn-0:0:user"],
    ["fallback-turn-0:0:user", "fallback-turn-1:2:assistant"],
    ["conversation-turn-0:0:user", "conversation-turn-0:2:assistant"],
  ]) {
    const state = await submissionState([{ identity: "user-message-id", units }]);
    expect(state.promotedTurnIdentities).toEqual({});
  }
  const provisional = await submissionState([{
    identity: "fallback-turn-0",
    units: ["fallback-turn-0:0:user", "fallback-turn-0:2:assistant"],
  }]);
  expect(provisional.promotedTurnIdentities).toEqual({});
});

test("two canonical containers cannot claim the same provisional turn", async () => {
  await expect(submissionState(["message-a", "message-b"].map(identity => ({
    identity,
    units: ["fallback-turn-0:0:user", "fallback-turn-0:2:assistant"],
  })))).rejects.toThrow("ambiguous promoted");
});

test("a promoted completed answer rebinds without accepting a different user submission", async () => {
  let state = await submissionState([{
    identity: "user-message-id",
    units: ["fallback-turn-0:0:user", "fallback-turn-0:2:assistant"],
  }]);
  const selectors: string[] = [];
  const locator = { count: async () => 1 };
  const page = {
    locator: (selector: string) => { selectors.push(selector); return locator; },
  } as unknown as Page;
  const worker = Object.assign(Object.create(ChatGptBrowserWorker.prototype), {
    submissionDomState: async () => state,
  }) as {
    reconcileAssistantTurnBinding(page: Page, baseline: unknown, binding: unknown): Promise<{
      identity: string; locator: unknown; acceptedTurnIdentities: string[];
    }>;
  };
  const baseline = { initialTurnIdentities: [], domCache: {} };
  const binding = {
    identity: "fallback-turn-0",
    locator: { count: async () => 0 },
    acceptedTurnIdentities: ["fallback-turn-0"],
  };
  const rebound = await worker.reconcileAssistantTurnBinding(page, baseline, binding);
  expect(rebound.identity).toBe("user-message-id");
  expect(rebound.locator).toBe(locator);
  expect(rebound.acceptedTurnIdentities).toEqual(["user-message-id"]);
  expect(selectors).toEqual(['[data-turn-id="user-message-id"], [data-turn-key="user-message-id"]']);

  state = { ...state, userIdentities: ["user-message-id", "another-user"] };
  await expect(worker.reconcileAssistantTurnBinding(page, baseline, binding)).rejects.toThrow("another user turn");

  state = await submissionState([{
    identity: "another-user",
    units: ["fallback-turn-1:0:user", "fallback-turn-1:2:assistant"],
  }]);
  await expect(worker.reconcileAssistantTurnBinding(page, baseline, binding)).rejects.toThrow("another user turn");

  state = { ...state, promotedTurnIdentities: {} };
  await expect(worker.reconcileAssistantTurnBinding(page, baseline, binding)).rejects.toThrow("another user turn");
  expect(selectors).toHaveLength(1);
});

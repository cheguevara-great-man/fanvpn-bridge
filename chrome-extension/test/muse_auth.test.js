import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";
import { museBrowserTicket } from "../src/muse_auth.js";

const ticket = { vm_id: "vm_test-1", access_token: "test-access", hatch_token: "test-hatch" };
const source = await readFile(new URL("../src/muse_login.js", import.meta.url), "utf8");

function page(replies, { origin = "https://muse.ai", iframe = false } = {}) {
  const listeners = [], calls = [];
  const window = {};
  window.top = iframe ? {} : window;
  const context = vm.createContext({
    window, location: { origin }, URL, AbortController, setTimeout, clearTimeout,
    chrome: { runtime: { id: "test-extension", onMessage: { addListener: (listener) => listeners.push(listener) } } },
    fetch: async (url, options) => {
      calls.push({ url, options });
      const reply = replies.shift();
      if (reply instanceof Error) throw reply;
      const status = reply?.status || 200;
      return { status, ok: status === 200, text: async () => JSON.stringify(reply?.body ?? reply) };
    },
  });
  vm.runInContext(source, context);
  return {
    listeners, calls,
    request: () => new Promise((resolve) => listeners[0]({ kind: "muse-auth:get" }, { id: "test-extension" }, resolve)),
  };
}

function authReplies() {
  return [{ vm_id: ticket.vm_id }, { access_token: ticket.access_token }, { token: ticket.hatch_token }];
}

test("page obtains a fresh ticket using only fixed same-origin credentialed endpoints", async () => {
  const muse = page([...authReplies(), ...authReplies()]);
  for (let index = 0; index < 2; index++) {
    assert.equal(JSON.stringify(await muse.request()), JSON.stringify({ ok: true, ticket }));
  }
  assert.equal(muse.calls.length, 6);
  assert.deepEqual(muse.calls.slice(0, 3).map(({ url, options }) => [url, options.method]), [
    ["https://muse.ai/api/session", "GET"],
    ["https://muse.ai/api/auth/check", "POST"],
    ["https://muse.ai/api/hatch/token", "POST"],
  ]);
  for (const { options } of muse.calls) {
    assert.equal(options.credentials, "include");
    assert.equal(options.redirect, "error");
    assert.equal(options.cache, "no-store");
    assert.equal(Object.keys(options.headers).some((key) => key.toLowerCase() === "cookie"), false);
  }
  assert.equal(muse.calls[1].options.body, "");
  assert.equal(muse.calls[2].options.headers.Authorization, "Bearer test-access");
  assert.deepEqual(JSON.parse(muse.calls[2].options.body), {
    vmAddress: "wss://vm_test-1.metaaivm.com/", vmName: "vm_test-1",
  });
});

test("page accepts only its extension and top-level exact origin", () => {
  assert.equal(page([], { iframe: true }).listeners.length, 0);
  assert.equal(page([], { origin: "https://muse.ai.evil.test" }).listeners.length, 0);
  const muse = page([]);
  assert.equal(muse.listeners[0]({ kind: "muse-auth:get" }, { id: "other-extension" }, () => assert.fail()), false);
  assert.equal(muse.listeners[0]({ kind: "other" }, { id: "test-extension" }, () => assert.fail()), false);
  assert.equal(muse.calls.length, 0);
});

test("page returns bounded safe errors, never raw auth replies or exceptions", async () => {
  const cases = [
    [[{ status: 401, body: "SECRET" }], "muse_web_login_required"],
    [[{ status: 403 }], "muse_web_login_required"],
    [[{ status: 429 }], "muse_web_rate_limited"],
    [[{ status: 500, body: "SECRET" }], "muse_web_browser_unavailable"],
    [[new Error("SECRET token")], "muse_web_browser_unavailable"],
    [[{ status: "unavailable" }], "muse_web_browser_unavailable"],
    [[{}], "muse_web_vm_unavailable"],
    [[{ vm_id: "../evil.test" }], "muse_web_vm_unavailable"],
    [[{ vm_id: "ok" }, { access_token: "line\nbreak" }], "muse_web_ticket_invalid"],
    [[{ vm_id: "ok" }, { access_token: "a" }, { token: "" }], "muse_web_ticket_invalid"],
    [[{ vm_id: "a".repeat(140000) }], "muse_web_ticket_invalid"],
  ];
  for (const [replies, code] of cases) {
    assert.equal(JSON.stringify(await page(replies).request()), JSON.stringify({ ok: false, error_code: code }));
  }
});

test("broker selects one existing signed-in candidate without account fallback or tab creation", async () => {
  const sent = [];
  const browser = { tabs: {
    query: async () => [
      { id: 1, url: "https://muse.ai/", incognito: true, active: true },
      { id: 2, url: "https://evil.test/", active: true },
      { id: 3, url: "https://muse.ai/", lastAccessed: 100 },
      { id: 4, url: "https://muse.ai/chat", active: true },
    ],
    sendMessage: async (id, message) => { sent.push({ id, message }); return { ok: true, ticket }; },
  } };
  assert.deepEqual(await museBrowserTicket(browser), { ok: true, ticket });
  assert.deepEqual(sent, [{ id: 4, message: { kind: "muse-auth:get" } }]);
  browser.tabs.sendMessage = async (id) => {
    assert.equal(id, 4);
    return { ok: false, error_code: "muse_web_login_required" };
  };
  assert.deepEqual(await museBrowserTicket(browser), { ok: false, error_code: "muse_web_login_required" });
  browser.tabs.sendMessage = async () => { throw new Error("SECRET"); };
  assert.equal((await museBrowserTicket(browser)).error_code, "muse_web_tab_required");
});

test("broker validates tickets and sanitizes arbitrary failures", async () => {
  const browser = { tabs: {
    query: async () => [{ id: 1, url: "https://muse.ai/" }],
    sendMessage: async () => ({ ok: true, ticket: { ...ticket, extra: "SECRET" } }),
  } };
  assert.equal((await museBrowserTicket(browser)).error_code, "muse_web_ticket_invalid");
  browser.tabs.sendMessage = async () => ({ ok: false, error_code: "SECRET" });
  assert.equal((await museBrowserTicket(browser)).error_code, "muse_web_browser_unavailable");
  browser.tabs.query = async () => [];
  assert.equal((await museBrowserTicket(browser)).error_code, "muse_web_tab_required");
});

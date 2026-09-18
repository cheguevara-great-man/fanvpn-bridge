import assert from "node:assert/strict";
import test from "node:test";

function eventTarget() {
  const listeners = [];
  return {
    listeners,
    addListener(listener) { listeners.push(listener); },
    emit(...args) { return listeners.map((listener) => listener(...args)); },
  };
}

async function waitFor(predicate, message) {
  const deadline = Date.now() + 2000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(message);
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
}

test("injects DeepSeek page auth only into DeepSeek API request heads", async () => {
  const nativeMessages = eventTarget();
  const nativeOutbound = [];
  const offscreenInbound = [];
  const port = {
    onMessage: nativeMessages,
    onDisconnect: eventTarget(),
    postMessage(message) { nativeOutbound.push(message); },
    disconnect() {},
  };
  const originalChrome = globalThis.chrome;
  globalThis.chrome = {
    runtime: {
      lastError: null,
      connectNative() { return port; },
      getManifest() { return { version: "test-version" }; },
      getURL(path) { return `chrome-extension://test/${path}`; },
      async getContexts() { return [{ contextType: "OFFSCREEN_DOCUMENT" }]; },
      async sendMessage(message) {
        offscreenInbound.push(message);
        return { ok: true };
      },
      onMessage: eventTarget(),
      onMessageExternal: eventTarget(),
      onInstalled: eventTarget(),
      onStartup: eventTarget(),
    },
    offscreen: { async createDocument() {} },
    tabs: {
      async query() { return [{ id: 7, url: "https://chat.deepseek.com/a/chat" }]; },
      async sendMessage() {
        return {
          ok: true,
          auth: { token: "page-token", locale: "zh-CN", timezoneOffset: 28800 },
        };
      },
    },
  };

  try {
    await import(`../src/background.js?deepseek-test=${Date.now()}`);
    nativeMessages.emit({
      v: 1,
      type: "hello",
      host_version: "test-host",
      max_chunk_bytes: 256 * 1024,
      max_in_flight: 4,
    });
    await waitFor(
      () => nativeOutbound.some((message) => message.type === "hello_ack"),
      "native handshake did not finish",
    );

    nativeMessages.emit({
      v: 1,
      type: "request.head",
      id: "deepseek_request_0001",
      method: "POST",
      url: "https://chat.deepseek.com/api/v0/chat_session/create",
      headers: [["authorization", "Bearer untrusted-caller"], ["content-type", "application/json"]],
    });
    await waitFor(
      () => offscreenInbound.some((message) => message.envelope?.id === "deepseek_request_0001"),
      "DeepSeek request head was not forwarded",
    );
    const forwarded = offscreenInbound.find(
      (message) => message.envelope?.id === "deepseek_request_0001",
    ).envelope;
    const headers = Object.fromEntries(forwarded.headers.map(([name, value]) => [name.toLowerCase(), value]));
    assert.equal(headers.authorization, "Bearer page-token");
    assert.equal(headers["x-client-locale"], "zh-CN");
    assert.equal(headers["x-client-timezone-offset"], "28800");
    assert.equal(headers["x-client-platform"], "web");

    nativeMessages.emit({
      v: 1,
      type: "request.head",
      id: "ordinary_request_0001",
      method: "GET",
      url: "https://api.example.test/v1/models",
      headers: [["authorization", "Bearer caller-owned"]],
    });
    await waitFor(
      () => offscreenInbound.some((message) => message.envelope?.id === "ordinary_request_0001"),
      "ordinary request head was not forwarded",
    );
    const ordinary = offscreenInbound.find(
      (message) => message.envelope?.id === "ordinary_request_0001",
    ).envelope;
    assert.deepEqual(ordinary.headers, [["authorization", "Bearer caller-owned"]]);
  } finally {
    globalThis.chrome = originalChrome;
  }
});

import assert from "node:assert/strict";
import test from "node:test";

function eventTarget() {
  const listeners = [];
  return {
    listeners,
    addListener(listener) {
      listeners.push(listener);
    },
    emit(...args) {
      return listeners.map((listener) => listener(...args));
    },
  };
}

async function waitFor(predicate, message) {
  const deadline = Date.now() + 2000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(message);
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
}

test("serializes native messages, caches the offscreen context, retries, and resets", async () => {
  const nativeMessages = eventTarget();
  const nativeDisconnect = eventTarget();
  const nativeOutbound = [];
  const offscreenInbound = [];
  let contextQueries = 0;
  let documentCreates = 0;
  let failNextSend = false;

  const port = {
    onMessage: nativeMessages,
    onDisconnect: nativeDisconnect,
    postMessage(message) {
      nativeOutbound.push(message);
    },
  };

  const originalChrome = globalThis.chrome;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const scheduledTimers = [];

  globalThis.chrome = {
    runtime: {
      lastError: null,
      connectNative() {
        return port;
      },
      getManifest() {
        return { version: "test-version" };
      },
      getURL(path) {
        return `chrome-extension://test/${path}`;
      },
      async getContexts() {
        contextQueries += 1;
        return documentCreates > 0 ? [{ contextType: "OFFSCREEN_DOCUMENT" }] : [];
      },
      async sendMessage(message) {
        if (failNextSend) {
          failNextSend = false;
          throw new Error("Receiving end does not exist");
        }
        offscreenInbound.push(message);
        return { ok: true };
      },
      onMessage: eventTarget(),
      onInstalled: eventTarget(),
      onStartup: eventTarget(),
    },
    offscreen: {
      async createDocument() {
        documentCreates += 1;
      },
    },
  };

  try {
    await import(`../src/background.js?test=${Date.now()}`);
    nativeMessages.emit({
      v: 1,
      type: "hello",
      host_version: "test-host",
      max_chunk_bytes: 64 * 1024,
      max_in_flight: 2,
    });
    nativeMessages.emit({
      v: 1,
      type: "request.head",
      id: "ordered_request_0001",
      method: "POST",
      url: "https://api.example.test/v1/responses",
      headers: [],
    });
    nativeMessages.emit({
      v: 1,
      type: "request.body",
      id: "ordered_request_0001",
      seq: 0,
      data: "",
      end: true,
    });

    await waitFor(() => offscreenInbound.length === 3, "ordered messages were not forwarded");
    assert.equal(documentCreates, 1);
    assert.equal(contextQueries, 1);
    assert.deepEqual(
      offscreenInbound.map((message) => message.kind || message.envelope.type),
      ["configure", "request.head", "request.body"],
    );
    assert.deepEqual(
      {
        maxChunkBytes: offscreenInbound[0].maxChunkBytes,
        maxInFlight: offscreenInbound[0].maxInFlight,
      },
      { maxChunkBytes: 64 * 1024, maxInFlight: 2 },
    );
    assert.equal(nativeOutbound[0].type, "hello_ack");

    const modeResponse = new Promise((resolve) => {
      const handled = chrome.runtime.onMessage.emit(
        { target: "background", kind: "codex-mode:get" },
        {},
        resolve,
      );
      assert.deepEqual(handled, [true]);
    });
    await waitFor(
      () => nativeOutbound.some((message) => message.type === "control.mode.get"),
      "mode control request was not sent",
    );
    const modeRequest = nativeOutbound.find((message) => message.type === "control.mode.get");
    nativeMessages.emit({
      v: 1,
      type: "control.mode.result",
      id: modeRequest.id,
      ok: true,
      mode: "browser_full",
      restart_vscode_required: false,
    });
    assert.equal((await modeResponse).mode, "browser_full");

    failNextSend = true;
    nativeMessages.emit({
      v: 1,
      type: "request.head",
      id: "retry_request_0002",
      method: "GET",
      url: "https://api.example.test/v1/models",
      headers: [],
    });
    await waitFor(
      () => offscreenInbound.some((message) => message.envelope?.id === "retry_request_0002"),
      "failed offscreen send was not retried",
    );
    assert.equal(contextQueries, 2);
    assert.equal(documentCreates, 1);

    globalThis.setTimeout = (callback, delay) => {
      scheduledTimers.push({ callback, delay });
      return scheduledTimers.length;
    };
    globalThis.clearTimeout = () => {};
    nativeDisconnect.emit();
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
    await waitFor(
      () => offscreenInbound.some((message) => message.kind === "reset"),
      "native disconnect did not reset offscreen requests",
    );
    const reset = offscreenInbound.find((message) => message.kind === "reset");
    assert.equal(reset.reason, "native_host_disconnected");
    assert.equal(scheduledTimers.length, 1);
    assert.equal(scheduledTimers[0].delay, 1000);
  } finally {
    globalThis.chrome = originalChrome;
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
});

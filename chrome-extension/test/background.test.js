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
  const externalMessages = eventTarget();
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
    disconnect() {},
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
      onMessageExternal: externalMessages,
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

    const serverRouteResponse = new Promise((resolve) => {
      const handled = chrome.runtime.onMessage.emit(
        { target: "background", kind: "server-executor:get" },
        {},
        resolve,
      );
      assert.deepEqual(handled, [true]);
    });
    await waitFor(
      () => nativeOutbound.some((message) => message.type === "control.server_executor.get"),
      "server executor control request was not sent",
    );
    const serverRouteRequest = nativeOutbound.find(
      (message) => message.type === "control.server_executor.get",
    );
    nativeMessages.emit({
      v: 1,
      type: "control.server_executor.result",
      id: serverRouteRequest.id,
      ok: true,
      state: { mode: "browser_chain", configured: true, client_running: false },
    });
    assert.equal((await serverRouteResponse).state.mode, "browser_chain");

    const antigravityResponse = new Promise((resolve) => {
      const handled = chrome.runtime.onMessage.emit(
        { target: "background", kind: "antigravity-setup:get" },
        {},
        resolve,
      );
      assert.deepEqual(handled, [true]);
    });
    await waitFor(
      () => nativeOutbound.some((message) => message.type === "control.antigravity.get"),
      "Antigravity setup status request was not sent",
    );
    const antigravityRequest = nativeOutbound.find(
      (message) => message.type === "control.antigravity.get",
    );
    nativeMessages.emit({
      v: 1,
      type: "control.antigravity.result",
      id: antigravityRequest.id,
      ok: true,
      state: { ready: true, restart_vscode_required: false },
    });
    assert.equal((await antigravityResponse).state.ready, true);

    const deviceResponse = new Promise((resolve) => {
      const handled = externalMessages.emit(
        {
          kind: "device-config:apply",
          config: {
            machineId: "11111111-1111-4111-8111-111111111111",
            machineName: "公司电脑-03",
            reportToken: "device-token-secret-1234567890",
            collectorUrl: "https://203.0.113.10:9443/v1/usage/events",
            dashboardUrl: "https://203.0.113.10:9443/dashboard",
          },
        },
        { id: "gjhcbooefgfcjbcdkjbbaljkoceghnkg" },
        resolve,
      );
      assert.deepEqual(handled, [true]);
    });
    await waitFor(
      () => nativeOutbound.some((message) => message.type === "control.device.apply"),
      "device configuration request was not sent",
    );
    const deviceRequest = nativeOutbound.find((message) => message.type === "control.device.apply");
    assert.equal(deviceRequest.config.machine_name, "公司电脑-03");
    nativeMessages.emit({
      v: 1,
      type: "control.device.result",
      id: deviceRequest.id,
      ok: true,
      state: { configured: true, restart_required: false },
    });
    assert.equal((await deviceResponse).ok, true);

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

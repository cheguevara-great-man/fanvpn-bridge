import assert from "node:assert/strict";
import test from "node:test";

async function waitFor(predicate, message) {
  const deadline = Date.now() + 2000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(message);
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
}

test("uses negotiated limits and aborts a response blocked on flow control", async () => {
  let listener;
  const outbound = [];
  const fetchCalls = [];
  const responseBytes = new Uint8Array(20 * 1024);
  responseBytes.fill(7);

  const originalChrome = globalThis.chrome;
  const originalFetch = globalThis.fetch;
  globalThis.chrome = {
    runtime: {
      onMessage: {
        addListener(value) {
          listener = value;
        },
      },
      async sendMessage(value) {
        outbound.push(value.envelope);
        return { ok: true };
      },
    },
  };
  globalThis.fetch = async (url, options) => {
    fetchCalls.push({ url, options });
    return new Response(responseBytes, {
      status: 200,
      headers: { "content-type": "application/octet-stream" },
    });
  };

  try {
    await import(`../src/offscreen.js?test=${Date.now()}`);
    assert.equal(typeof listener, "function");

    const sendControl = (message) => new Promise((resolve, reject) => {
      const keepAlive = listener(
        { target: "offscreen", ...message },
        {},
        (response) => (response?.ok ? resolve(response) : reject(new Error(response?.error))),
      );
      assert.equal(keepAlive, false);
    });
    const sendEnvelope = (envelope) => new Promise((resolve, reject) => {
      const keepAlive = listener(
        { target: "offscreen", envelope },
        {},
        (response) => (response?.ok ? resolve(response) : reject(new Error("envelope rejected"))),
      );
      assert.equal(keepAlive, true);
    });

    await sendControl({ kind: "configure", maxChunkBytes: 16 * 1024, maxInFlight: 1 });

    const protocol = await import("../src/protocol.js");
    const id = "negotiated_request_0001";
    await sendEnvelope(protocol.envelope(protocol.MessageType.REQUEST_HEAD, {
      id,
      method: "POST",
      url: "https://api.example.test/v1/responses",
      headers: [],
    }));
    await sendEnvelope(protocol.envelope(protocol.MessageType.REQUEST_BODY, {
      id,
      seq: 0,
      data: protocol.bytesToBase64(new TextEncoder().encode("request-body")),
      end: true,
    }));

    await waitFor(
      () => outbound.some((message) => message.type === protocol.MessageType.RESPONSE_BODY),
      "first response frame was not emitted",
    );
    assert.equal(fetchCalls.length, 1);
    assert.equal(fetchCalls[0].options.body instanceof Blob, true);
    assert.equal(fetchCalls[0].options.redirect, "error");
    assert.equal(
      new TextDecoder().decode(await fetchCalls[0].options.body.arrayBuffer()),
      "request-body",
    );

    const responseFrames = () => outbound.filter(
      (message) => message.type === protocol.MessageType.RESPONSE_BODY,
    );
    assert.equal(responseFrames().length, 1);
    assert.equal(protocol.base64ToBytes(responseFrames()[0].data).byteLength, 16 * 1024);

    // maxInFlight=1 means sequence 1 is waiting for ACK 0. Reset must reject
    // that waiter so executeRequest can finish instead of leaking its state.
    await sendControl({ kind: "reset", reason: "test_disconnect" });
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(responseFrames().length, 1);

    // Reusing the id proves reset synchronously removed the old request state.
    await sendEnvelope(protocol.envelope(protocol.MessageType.REQUEST_HEAD, {
      id,
      method: "GET",
      url: "https://api.example.test/v1/models",
      headers: [],
    }));
    assert.equal(
      outbound.some(
        (message) => message.type === protocol.MessageType.ERROR && /Duplicate/.test(message.message),
      ),
      false,
    );
    await sendEnvelope(protocol.envelope(protocol.MessageType.REQUEST_ABORT, {
      id,
      reason: "client_cancelled",
    }));
  } finally {
    globalThis.chrome = originalChrome;
    globalThis.fetch = originalFetch;
  }
});

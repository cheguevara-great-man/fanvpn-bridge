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
  const backgroundControls = [];
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
        if (value?.kind === "antigravity-user-agent:set") {
          backgroundControls.push(value);
          return { ok: true };
        }
        outbound.push(value.envelope);
        return { ok: true };
      },
    },
  };
  globalThis.fetch = async (url, options) => {
    fetchCalls.push({ url, options });
    if (url === "https://www.googleapis.com/oauth2/v2/userinfo") {
      return Response.json({
        email: "person@example.test",
        picture: "https://lh3.googleusercontent.com/a/example=s96-c",
      });
    }
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
    const responseHead = outbound.find(
      (message) => message.type === protocol.MessageType.RESPONSE_HEAD,
    );
    assert.equal(responseHead.timing.attempts, 1);
    assert.equal(responseHead.timing.preemptions, 0);
    assert.ok(responseHead.timing.executor_queue_ms >= 0);
    assert.ok(responseHead.timing.fetch_head_ms >= 0);

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

    const antigravityId = "antigravity_request_0001";
    await sendEnvelope(protocol.envelope(protocol.MessageType.REQUEST_HEAD, {
      id: antigravityId,
      method: "POST",
      url: "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
      headers: [["User-Agent", "antigravity-cli/test windows/amd64"]],
    }));
    await sendEnvelope(protocol.envelope(protocol.MessageType.REQUEST_BODY, {
      id: antigravityId,
      seq: 0,
      data: "",
      end: true,
    }));
    await waitFor(() => fetchCalls.length === 2, "Antigravity request was not fetched");
    assert.equal(backgroundControls.length, 1);
    assert.equal(backgroundControls[0].userAgent, "antigravity-cli/test windows/amd64");

    const userInfoId = "antigravity_userinfo_0001";
    await sendEnvelope(protocol.envelope(protocol.MessageType.REQUEST_HEAD, {
      id: userInfoId,
      method: "GET",
      url: "https://www.googleapis.com/oauth2/v2/userinfo",
      headers: [],
    }));
    await sendEnvelope(protocol.envelope(protocol.MessageType.REQUEST_BODY, {
      id: userInfoId,
      seq: 0,
      data: "",
      end: true,
    }));
    await waitFor(
      () => outbound.some(
        (message) => message.type === protocol.MessageType.RESPONSE_BODY && message.id === userInfoId,
      ),
      "rewritten user-info response was not emitted",
    );
    const userInfoFrame = outbound.find(
      (message) => message.type === protocol.MessageType.RESPONSE_BODY && message.id === userInfoId,
    );
    const userInfo = JSON.parse(
      new TextDecoder().decode(protocol.base64ToBytes(userInfoFrame.data)),
    );
    assert.equal(
      userInfo.picture,
      "http://127.0.0.1:18888/antigravity-avatar/a/example=s96-c",
    );
  } finally {
    globalThis.chrome = originalChrome;
    globalThis.fetch = originalFetch;
  }
});

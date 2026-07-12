import assert from "node:assert/strict";

let listener;
const outbound = [];
let observedFetch = null;

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
  observedFetch = {
    url,
    authorization: options.headers.get("authorization"),
    body: new Uint8Array(options.body || new ArrayBuffer()),
  };
  return new Response(new TextEncoder().encode("data: hello\n\n"), {
    status: 200,
    headers: { "content-type": "text/event-stream", "content-length": "13" },
  });
};

const protocol = await import("../chrome-extension/src/protocol.js");
await import("../chrome-extension/src/offscreen.js");
assert.equal(typeof listener, "function");

async function dispatch(envelope) {
  return new Promise((resolve, reject) => {
    const keepAlive = listener(
      { target: "offscreen", envelope },
      {},
      (response) => (response?.ok ? resolve(response) : reject(new Error("offscreen rejected"))),
    );
    assert.equal(keepAlive, true);
  });
}

const id = "extension_check_0001";
await dispatch(
  protocol.envelope(protocol.MessageType.REQUEST_HEAD, {
    id,
    method: "POST",
    url: "https://api.example.test/v1/responses",
    headers: [["authorization", "Bearer extension-test"]],
  }),
);
await dispatch(
  protocol.envelope(protocol.MessageType.REQUEST_BODY, {
    id,
    seq: 0,
    data: protocol.bytesToBase64(new TextEncoder().encode("request-body")),
    end: true,
  }),
);

const deadline = Date.now() + 2000;
while (!outbound.some((message) => message.type === protocol.MessageType.RESPONSE_BODY)) {
  if (Date.now() > deadline) throw new Error("offscreen response timed out");
  await new Promise((resolve) => setTimeout(resolve, 5));
}

assert.equal(observedFetch.url, "https://api.example.test/v1/responses");
assert.equal(observedFetch.authorization, "Bearer extension-test");
assert.equal(new TextDecoder().decode(observedFetch.body), "request-body");
assert.equal(outbound[0].type, protocol.MessageType.FLOW_ACK);
const head = outbound.find((message) => message.type === protocol.MessageType.RESPONSE_HEAD);
const body = outbound.find((message) => message.type === protocol.MessageType.RESPONSE_BODY);
assert.equal(head.status, 200);
assert.equal(head.headers.some(([name]) => name === "content-length"), false);
assert.equal(new TextDecoder().decode(protocol.base64ToBytes(body.data)), "data: hello\n\n");
assert.equal(body.end, true);

console.log("offscreen executor: OK");

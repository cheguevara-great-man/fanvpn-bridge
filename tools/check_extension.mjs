import assert from "node:assert/strict";
import {
  MAX_CHUNK_BYTES,
  PROTOCOL_VERSION,
  base64ToBytes,
  bytesToBase64,
  envelope,
  isProtocolEnvelope,
} from "../chrome-extension/src/protocol.js";

const bytes = new Uint8Array(MAX_CHUNK_BYTES);
for (let index = 0; index < bytes.length; index += 1) bytes[index] = index % 251;
const encoded = bytesToBase64(bytes);
assert.deepEqual(base64ToBytes(encoded), bytes);

const ping = envelope("ping", { nonce: "extension-check" });
assert.equal(ping.v, PROTOCOL_VERSION);
assert.equal(isProtocolEnvelope(ping), true);
assert.equal(isProtocolEnvelope({ v: 999, type: "ping" }), false);

console.log("extension protocol: OK");

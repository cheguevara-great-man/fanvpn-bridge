/**
 * Stable browser-side constants for Native Messaging protocol v1.
 *
 * The JSON Schema in ../../contracts/native-messaging-v1.schema.json is the
 * source of truth. This module keeps runtime code free from magic limits.
 */

export const PROTOCOL_VERSION = 1;
export const MAX_CHUNK_BYTES = 256 * 1024;
export const MAX_IN_FLIGHT = 16;
export const MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024;

export const MessageType = Object.freeze({
  HELLO: "hello",
  HELLO_ACK: "hello_ack",
  REQUEST_HEAD: "request.head",
  REQUEST_BODY: "request.body",
  REQUEST_ABORT: "request.abort",
  RESPONSE_HEAD: "response.head",
  RESPONSE_BODY: "response.body",
  FLOW_ACK: "flow.ack",
  ERROR: "error",
  PING: "ping",
  PONG: "pong",
  CONTROL_MODE_GET: "control.mode.get",
  CONTROL_MODE_SET: "control.mode.set",
  CONTROL_MODE_RESULT: "control.mode.result",
});

export const ErrorCode = Object.freeze({
  NATIVE_CHANNEL_UNAVAILABLE: "NATIVE_CHANNEL_UNAVAILABLE",
  PROTOCOL_MISMATCH: "PROTOCOL_MISMATCH",
  PROTOCOL_VIOLATION: "PROTOCOL_VIOLATION",
  MESSAGE_TOO_LARGE: "MESSAGE_TOO_LARGE",
  EGRESS_UNAVAILABLE: "EGRESS_UNAVAILABLE",
  PROXY_CONNECTION_FAILED: "PROXY_CONNECTION_FAILED",
  UPSTREAM_CONNECTION_FAILED: "UPSTREAM_CONNECTION_FAILED",
  CLIENT_CANCELLED: "CLIENT_CANCELLED",
  REQUEST_TIMEOUT: "REQUEST_TIMEOUT",
  INTERNAL_ERROR: "INTERNAL_ERROR",
});

/** @param {unknown} value */
export function isProtocolEnvelope(value) {
  return Boolean(
    value &&
      typeof value === "object" &&
      value.v === PROTOCOL_VERSION &&
      typeof value.type === "string",
  );
}

/** @param {Uint8Array} bytes */
export function bytesToBase64(bytes) {
  let binary = "";
  const stride = 0x8000;
  for (let offset = 0; offset < bytes.byteLength; offset += stride) {
    const slice = bytes.subarray(offset, Math.min(offset + stride, bytes.byteLength));
    binary += String.fromCharCode(...slice);
  }
  return btoa(binary);
}

/** @param {string} value */
export function base64ToBytes(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

/** @param {string} type @param {Record<string, unknown>} fields */
export function envelope(type, fields = {}) {
  return { v: PROTOCOL_VERSION, type, ...fields };
}

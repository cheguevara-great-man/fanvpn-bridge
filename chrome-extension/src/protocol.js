/**
 * Stable browser-side constants for Native Messaging protocol v1.
 *
 * The JSON Schema in ../../contracts/native-messaging-v1.schema.json is the
 * source of truth. This module keeps runtime code free from magic limits.
 */

export const PROTOCOL_VERSION = 1;
export const MAX_CHUNK_BYTES = 256 * 1024;
export const MAX_IN_FLIGHT = 4;

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
});

export const ErrorCode = Object.freeze({
  NATIVE_CHANNEL_UNAVAILABLE: "NATIVE_CHANNEL_UNAVAILABLE",
  PROTOCOL_MISMATCH: "PROTOCOL_MISMATCH",
  PROTOCOL_VIOLATION: "PROTOCOL_VIOLATION",
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

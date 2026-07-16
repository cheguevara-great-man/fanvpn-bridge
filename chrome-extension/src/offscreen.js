import {
  ErrorCode,
  MAX_CHUNK_BYTES,
  MAX_IN_FLIGHT,
  MAX_REQUEST_BODY_BYTES,
  MessageType,
  base64ToBytes,
  bytesToBase64,
  envelope,
  isProtocolEnvelope,
} from "./protocol.js";
import { pumpResponseBody } from "./stream.js";
import { resilientFetch } from "./resilient_fetch.js";

const requests = new Map();
const METHODS_WITHOUT_BODY = new Set(["GET", "HEAD"]);
const BROWSER_DECODED_HEADERS = new Set([
  "content-length",
  "content-encoding",
  "transfer-encoding",
]);

let negotiatedMaxChunkBytes = MAX_CHUNK_BYTES;
let negotiatedMaxInFlight = MAX_IN_FLIGHT;

function postBackground(message) {
  return chrome.runtime.sendMessage({ target: "background", envelope: message });
}

function protocolError(id, message) {
  return postBackground(
    envelope(MessageType.ERROR, {
      id,
      code: ErrorCode.PROTOCOL_VIOLATION,
      message,
      retryable: false,
    }),
  );
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.target !== "offscreen") {
    return false;
  }
  if (message.kind === "configure" || message.kind === "reset") {
    try {
      handleControlMessage(message);
      sendResponse({ ok: true });
    } catch (error) {
      sendResponse({ ok: false, error: error?.message || String(error) });
    }
    return false;
  }
  if (!isProtocolEnvelope(message.envelope)) return false;
  handleEnvelope(message.envelope)
    .then(() => sendResponse({ ok: true }))
    .catch(async (error) => {
      await postBackground(
        envelope(MessageType.ERROR, {
          id: message.envelope.id,
          code: error.code || ErrorCode.INTERNAL_ERROR,
          message: error.message || String(error),
          retryable: Boolean(error.retryable),
        }),
      );
      sendResponse({ ok: false });
    });
  return true;
});

async function handleEnvelope(message) {
  if (message.type === MessageType.REQUEST_HEAD) {
    if (requests.has(message.id)) {
      await protocolError(message.id, "Duplicate request id");
      return;
    }
    requests.set(message.id, {
      head: message,
      requestChunks: [],
      requestBytes: 0,
      expectedRequestSeq: 0,
      controller: new AbortController(),
      maxChunkBytes: negotiatedMaxChunkBytes,
      maxInFlight: negotiatedMaxInFlight,
      responseAcked: -1,
      responseWaiters: [],
    });
    return;
  }
  if (message.type === MessageType.REQUEST_BODY) {
    const state = requests.get(message.id);
    if (!state || message.seq !== state.expectedRequestSeq) {
      if (state) {
        requests.delete(message.id);
        abortRequestState(state, "request_body_sequence_mismatch");
      }
      await protocolError(message.id, "Request body sequence mismatch");
      return;
    }
    const bytes = base64ToBytes(message.data);
    if (bytes.byteLength > state.maxChunkBytes) {
      requests.delete(message.id);
      abortRequestState(state, "request_chunk_too_large");
      await protocolError(message.id, "Request body chunk exceeds negotiated size");
      return;
    }
    state.requestBytes += bytes.byteLength;
    if (state.requestBytes > MAX_REQUEST_BODY_BYTES) {
      requests.delete(message.id);
      abortRequestState(state, "request_body_too_large");
      await postBackground(
        envelope(MessageType.ERROR, {
          id: message.id,
          code: ErrorCode.MESSAGE_TOO_LARGE,
          message: "Request body exceeds the 32 MiB browser buffer limit",
          retryable: false,
        }),
      );
      return;
    }
    state.requestChunks.push(bytes);
    state.expectedRequestSeq += 1;
    await postBackground(
      envelope(MessageType.FLOW_ACK, {
        id: message.id,
        stream: "request",
        seq: message.seq,
      }),
    );
    if (message.end) void executeRequest(message.id, state);
    return;
  }
  if (message.type === MessageType.FLOW_ACK && message.stream === "response") {
    const state = requests.get(message.id);
    if (!state) return;
    state.responseAcked = Math.max(state.responseAcked, message.seq);
    const waiters = state.responseWaiters.splice(0);
    for (const waiter of waiters) waiter.resolve();
    return;
  }
  if (message.type === MessageType.REQUEST_ABORT) {
    const state = requests.get(message.id);
    if (state) {
      requests.delete(message.id);
      abortRequestState(state, message.reason || "request_aborted");
    }
    return;
  }
  await protocolError(message.id, `Unexpected message type ${message.type}`);
}

async function executeRequest(id, state) {
  try {
    const headers = new Headers();
    for (const pair of state.head.headers || []) {
      if (Array.isArray(pair) && pair.length === 2) headers.append(pair[0], pair[1]);
    }
    const options = {
      method: state.head.method,
      headers,
      // Routes are an origin allowlist. Automatic redirects could escape the
      // configured upstream and replay credentials or request bodies there.
      redirect: "error",
      cache: "no-store",
    };
    if (!METHODS_WITHOUT_BODY.has(state.head.method)) {
      options.body = new Blob(state.requestChunks);
    }
    state.requestChunks.length = 0;
    const response = await resilientFetch(state.head.url, options, {
      parentSignal: state.controller.signal,
    });
    const responseHeaders = [];
    for (const [name, value] of response.headers.entries()) {
      if (!BROWSER_DECODED_HEADERS.has(name.toLowerCase())) {
        responseHeaders.push([name, value]);
      }
    }
    await postBackground(
      envelope(MessageType.RESPONSE_HEAD, {
        id,
        status: response.status,
        headers: responseHeaders,
      }),
    );
    await pumpResponseBody(response.body, {
      maxChunkBytes: state.maxChunkBytes,
      sendFrame: (sequence, bytes, end) =>
        sendResponseFrame(id, state, sequence, bytes, end),
    });
  } catch (error) {
    if (state.controller.signal.aborted) return;
    const failure = classifyFetchError(error);
    await postBackground(
      envelope(MessageType.ERROR, {
        id,
        code: failure.code,
        message: failure.message,
        retryable: failure.retryable,
      }),
    );
  } finally {
    state.requestChunks.length = 0;
    if (requests.get(id) === state) requests.delete(id);
  }
}

async function sendResponseFrame(id, state, sequence, bytes, end) {
  while (sequence - state.responseAcked > state.maxInFlight) {
    throwIfAborted(state);
    await new Promise((resolve, reject) => state.responseWaiters.push({ resolve, reject }));
  }
  throwIfAborted(state);
  await postBackground(
    envelope(MessageType.RESPONSE_BODY, {
      id,
      seq: sequence,
      data: bytesToBase64(bytes),
      end,
    }),
  );
}

function handleControlMessage(message) {
  if (message.kind === "reset") {
    abortAllRequests(message.reason || "native_session_reset");
    return;
  }
  if (
    !Number.isInteger(message.maxChunkBytes) ||
    message.maxChunkBytes < 1 ||
    message.maxChunkBytes > MAX_CHUNK_BYTES ||
    !Number.isInteger(message.maxInFlight) ||
    message.maxInFlight < 1 ||
    message.maxInFlight > MAX_IN_FLIGHT
  ) {
    throw new Error("Invalid negotiated protocol limits");
  }
  abortAllRequests("native_session_reconfigured");
  negotiatedMaxChunkBytes = message.maxChunkBytes;
  negotiatedMaxInFlight = message.maxInFlight;
}

function abortAllRequests(reason) {
  const active = [...requests.values()];
  requests.clear();
  for (const state of active) abortRequestState(state, reason);
}

function abortRequestState(state, reason) {
  const error = abortError(reason);
  if (!state.controller.signal.aborted) state.controller.abort(reason);
  state.requestChunks.length = 0;
  const waiters = state.responseWaiters.splice(0);
  for (const waiter of waiters) waiter.reject(error);
}

function throwIfAborted(state) {
  if (state.controller.signal.aborted) {
    throw abortError(state.controller.signal.reason || "request_aborted");
  }
}

function abortError(reason) {
  const error = new Error(typeof reason === "string" ? reason : "Request aborted");
  error.name = "AbortError";
  return error;
}

function classifyFetchError(error) {
  const message = error?.message || String(error);
  if (error?.name === "AbortError") {
    return { code: ErrorCode.CLIENT_CANCELLED, message: "Request aborted", retryable: false };
  }
  return {
    code: ErrorCode.UPSTREAM_CONNECTION_FAILED,
    message: message === "Failed to fetch" ? "Browser could not reach the upstream API" : message,
    retryable: true,
  };
}

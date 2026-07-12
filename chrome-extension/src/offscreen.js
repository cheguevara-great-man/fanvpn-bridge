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

const requests = new Map();

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
  if (message?.target !== "offscreen" || !isProtocolEnvelope(message.envelope)) {
    return false;
  }
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
      responseAcked: -1,
      responseWaiters: [],
    });
    return;
  }
  if (message.type === MessageType.REQUEST_BODY) {
    const state = requests.get(message.id);
    if (!state || message.seq !== state.expectedRequestSeq) {
      await protocolError(message.id, "Request body sequence mismatch");
      return;
    }
    const bytes = base64ToBytes(message.data);
    if (bytes.byteLength > MAX_CHUNK_BYTES) {
      await protocolError(message.id, "Request body chunk exceeds negotiated size");
      return;
    }
    state.requestBytes += bytes.byteLength;
    if (state.requestBytes > MAX_REQUEST_BODY_BYTES) {
      requests.delete(message.id);
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
    for (const resolve of waiters) resolve();
    return;
  }
  if (message.type === MessageType.REQUEST_ABORT) {
    const state = requests.get(message.id);
    if (state) {
      state.controller.abort(message.reason || "request aborted");
      requests.delete(message.id);
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
      redirect: "follow",
      signal: state.controller.signal,
      cache: "no-store",
    };
    if (!new Set(["GET", "HEAD"]).has(state.head.method)) {
      options.body = await new Blob(state.requestChunks).arrayBuffer();
    }
    const response = await fetch(state.head.url, options);
    const responseHeaders = [];
    for (const [name, value] of response.headers.entries()) {
      if (!new Set(["content-length", "content-encoding", "transfer-encoding"]).has(name.toLowerCase())) {
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
    await pumpResponse(id, state, response.body);
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
    requests.delete(id);
  }
}

async function pumpResponse(id, state, body) {
  if (!body) {
    await sendResponseFrame(id, state, 0, new Uint8Array(), true);
    return;
  }
  const reader = body.getReader();
  let pending = null;
  let sequence = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    for (let offset = 0; offset < value.byteLength; offset += MAX_CHUNK_BYTES) {
      const piece = value.slice(offset, Math.min(offset + MAX_CHUNK_BYTES, value.byteLength));
      if (pending) {
        await sendResponseFrame(id, state, sequence, pending, false);
        sequence += 1;
      }
      pending = piece;
    }
  }
  await sendResponseFrame(id, state, sequence, pending || new Uint8Array(), true);
}

async function sendResponseFrame(id, state, sequence, bytes, end) {
  while (sequence - state.responseAcked > MAX_IN_FLIGHT) {
    await new Promise((resolve) => state.responseWaiters.push(resolve));
  }
  await postBackground(
    envelope(MessageType.RESPONSE_BODY, {
      id,
      seq: sequence,
      data: bytesToBase64(bytes),
      end,
    }),
  );
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

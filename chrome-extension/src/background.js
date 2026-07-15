import {
  ErrorCode,
  MAX_CHUNK_BYTES,
  MAX_IN_FLIGHT,
  MessageType,
  PROTOCOL_VERSION,
  envelope,
  isProtocolEnvelope,
} from "./protocol.js";

const NATIVE_HOST_NAME = "com.fanvpn.bridge";
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const OFFSCREEN_PATH = "offscreen.html";

let nativePort = null;
let reconnectTimer = null;
let reconnectDelay = RECONNECT_MIN_MS;
let offscreenCreation = null;
let offscreenReady = false;
let lastError = null;
let handshakeComplete = false;
let negotiatedLimits = null;

function setError(code, message) {
  lastError = { code, message, at: new Date().toISOString() };
  console.error(`[FanVPN Bridge] ${code}: ${message}`);
}

function clearError() {
  lastError = null;
}

function connectNative() {
  if (nativePort) return;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  try {
    const port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
    nativePort = port;
    handshakeComplete = false;
    negotiatedLimits = null;

    // Chrome does not await async Port listeners. Keep one promise chain per
    // connection so request.head and its body frames cannot overtake each
    // other while the offscreen document is being created or recovered.
    let messageChain = Promise.resolve();
    port.onMessage.addListener((message) => {
      messageChain = messageChain
        .then(() => {
          if (nativePort !== port) return undefined;
          return handleNativeMessage(message, port);
        })
        .catch((error) => {
          if (nativePort !== port) return;
          const detail = error?.message || String(error);
          setError(ErrorCode.INTERNAL_ERROR, detail);
          postNative(
            envelope(MessageType.ERROR, {
              id: message?.id,
              code: ErrorCode.INTERNAL_ERROR,
              message: "Native message processing failed",
              retryable: true,
            }),
            port,
          );
        });
    });
    port.onDisconnect.addListener(() => {
      const message = chrome.runtime.lastError?.message || "Native Host disconnected";
      if (nativePort !== port) return;
      nativePort = null;
      handshakeComplete = false;
      negotiatedLimits = null;
      setError(ErrorCode.NATIVE_CHANNEL_UNAVAILABLE, message);
      void resetOffscreenRequests("native_host_disconnected");
      scheduleReconnect();
    });
  } catch (error) {
    nativePort = null;
    setError(ErrorCode.NATIVE_CHANNEL_UNAVAILABLE, error.message || String(error));
    scheduleReconnect();
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectNative();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
}

function postNative(message, port = nativePort) {
  if (!port || nativePort !== port) {
    setError(ErrorCode.NATIVE_CHANNEL_UNAVAILABLE, "Native Host is not connected");
    return false;
  }
  try {
    port.postMessage(message);
    return true;
  } catch (error) {
    setError(ErrorCode.NATIVE_CHANNEL_UNAVAILABLE, error.message || String(error));
    return false;
  }
}

async function handleNativeMessage(message, port) {
  if (!isProtocolEnvelope(message)) {
    postNative(
      envelope(MessageType.ERROR, {
        code: ErrorCode.PROTOCOL_MISMATCH,
        message: "Unsupported or malformed protocol envelope",
        retryable: false,
      }),
      port,
    );
    return;
  }
  if (message.type === MessageType.HELLO) {
    const limits = negotiatedProtocolLimits(message);
    if (!limits) {
      postNative(
        envelope(MessageType.ERROR, {
          code: ErrorCode.PROTOCOL_MISMATCH,
          message: "Host protocol limits are incompatible with this extension",
          retryable: false,
        }),
        port,
      );
      return;
    }
    try {
      await sendOffscreenMessage(
        {
          target: "offscreen",
          kind: "configure",
          maxChunkBytes: limits.maxChunkBytes,
          maxInFlight: limits.maxInFlight,
        },
        { requireOk: true },
      );
      if (nativePort !== port) return;
      negotiatedLimits = limits;
      handshakeComplete = true;
      reconnectDelay = RECONNECT_MIN_MS;
      clearError();
      postNative(
        envelope(MessageType.HELLO_ACK, {
          extension_version: chrome.runtime.getManifest().version,
          executor: "offscreen",
        }),
        port,
      );
    } catch (error) {
      setError(ErrorCode.EGRESS_UNAVAILABLE, error.message || String(error));
      postNative(
        envelope(MessageType.ERROR, {
          code: ErrorCode.EGRESS_UNAVAILABLE,
          message: "Offscreen executor could not be created",
          retryable: true,
        }),
        port,
      );
    }
    return;
  }
  if (message.type === MessageType.PING) {
    postNative(envelope(MessageType.PONG, { nonce: message.nonce }), port);
    return;
  }
  if (!handshakeComplete) {
    postNative(
      envelope(MessageType.ERROR, {
        id: message.id,
        code: ErrorCode.PROTOCOL_MISMATCH,
        message: "Protocol handshake is not complete",
        retryable: true,
      }),
      port,
    );
    return;
  }
  try {
    await sendOffscreenMessage({ target: "offscreen", envelope: message });
  } catch (error) {
    setError(ErrorCode.EGRESS_UNAVAILABLE, error.message || String(error));
    postNative(
      envelope(MessageType.ERROR, {
        id: message.id,
        code: ErrorCode.EGRESS_UNAVAILABLE,
        message: "Offscreen executor is unavailable",
        retryable: true,
      }),
      port,
    );
  }
}

async function ensureOffscreenDocument() {
  if (offscreenReady) return;
  if (!offscreenCreation) {
    offscreenCreation = (async () => {
      const offscreenUrl = chrome.runtime.getURL(OFFSCREEN_PATH);
      const contexts = await chrome.runtime.getContexts({
        contextTypes: ["OFFSCREEN_DOCUMENT"],
        documentUrls: [offscreenUrl],
      });
      if (contexts.length === 0) {
        await chrome.offscreen.createDocument({
          url: OFFSCREEN_PATH,
          reasons: ["DOM_SCRAPING"],
          justification: "Execute allowlisted cross-origin API requests through Chrome network settings",
        });
      }
      offscreenReady = true;
    })()
      .finally(() => {
        offscreenCreation = null;
      });
  }
  await offscreenCreation;
}

async function sendOffscreenMessage(message, { requireOk = false } = {}) {
  let lastFailure;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    await ensureOffscreenDocument();
    try {
      const response = await chrome.runtime.sendMessage(message);
      if (requireOk && response?.ok !== true) {
        throw new Error(response?.error || "Offscreen executor rejected configuration");
      }
      return response;
    } catch (error) {
      lastFailure = error;
      offscreenReady = false;
    }
  }
  throw lastFailure;
}

async function resetOffscreenRequests(reason) {
  if (!offscreenReady) return;
  try {
    const response = await chrome.runtime.sendMessage({
      target: "offscreen",
      kind: "reset",
      reason,
    });
    if (response?.ok !== true) throw new Error(response?.error || "Offscreen reset failed");
  } catch (_error) {
    // Do not recreate an executor solely to clean it up. The next HELLO sends
    // configure, which also resets any state left by the previous connection.
    offscreenReady = false;
  }
}

function negotiatedProtocolLimits(message) {
  if (
    message.v !== PROTOCOL_VERSION ||
    !Number.isInteger(message.max_chunk_bytes) ||
    message.max_chunk_bytes < 1 ||
    message.max_chunk_bytes > MAX_CHUNK_BYTES ||
    !Number.isInteger(message.max_in_flight) ||
    message.max_in_flight < 1 ||
    message.max_in_flight > MAX_IN_FLIGHT
  ) {
    return null;
  }
  return {
    maxChunkBytes: message.max_chunk_bytes,
    maxInFlight: message.max_in_flight,
  };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.target === "background" && isProtocolEnvelope(message.envelope)) {
    postNative(message.envelope);
    sendResponse({ ok: true });
    return false;
  }
  if (message?.target === "background" && message.kind === "status") {
    sendResponse({
      nativeConnected: Boolean(nativePort),
      handshakeComplete,
      executor: handshakeComplete ? "offscreen" : null,
      negotiatedLimits,
      lastError,
      version: chrome.runtime.getManifest().version,
    });
    return false;
  }
  return false;
});

chrome.runtime.onInstalled.addListener(connectNative);
chrome.runtime.onStartup.addListener(connectNative);
connectNative();

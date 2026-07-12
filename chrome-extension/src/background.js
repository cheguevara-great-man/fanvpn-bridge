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
let lastError = null;
let handshakeComplete = false;

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
    port.onMessage.addListener(handleNativeMessage);
    port.onDisconnect.addListener(() => {
      const message = chrome.runtime.lastError?.message || "Native Host disconnected";
      if (nativePort === port) nativePort = null;
      handshakeComplete = false;
      setError(ErrorCode.NATIVE_CHANNEL_UNAVAILABLE, message);
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

function postNative(message) {
  if (!nativePort) {
    setError(ErrorCode.NATIVE_CHANNEL_UNAVAILABLE, "Native Host is not connected");
    return false;
  }
  try {
    nativePort.postMessage(message);
    return true;
  } catch (error) {
    setError(ErrorCode.NATIVE_CHANNEL_UNAVAILABLE, error.message || String(error));
    return false;
  }
}

async function handleNativeMessage(message) {
  if (!isProtocolEnvelope(message)) {
    postNative(
      envelope(MessageType.ERROR, {
        code: ErrorCode.PROTOCOL_MISMATCH,
        message: "Unsupported or malformed protocol envelope",
        retryable: false,
      }),
    );
    return;
  }
  if (message.type === MessageType.HELLO) {
    if (
      message.v !== PROTOCOL_VERSION ||
      message.max_chunk_bytes > MAX_CHUNK_BYTES ||
      message.max_in_flight > MAX_IN_FLIGHT
    ) {
      postNative(
        envelope(MessageType.ERROR, {
          code: ErrorCode.PROTOCOL_MISMATCH,
          message: "Host protocol limits are incompatible with this extension",
          retryable: false,
        }),
      );
      return;
    }
    try {
      await ensureOffscreenDocument();
      handshakeComplete = true;
      reconnectDelay = RECONNECT_MIN_MS;
      clearError();
      postNative(
        envelope(MessageType.HELLO_ACK, {
          extension_version: chrome.runtime.getManifest().version,
          executor: "offscreen",
        }),
      );
    } catch (error) {
      setError(ErrorCode.EGRESS_UNAVAILABLE, error.message || String(error));
      postNative(
        envelope(MessageType.ERROR, {
          code: ErrorCode.EGRESS_UNAVAILABLE,
          message: "Offscreen executor could not be created",
          retryable: true,
        }),
      );
    }
    return;
  }
  if (message.type === MessageType.PING) {
    postNative(envelope(MessageType.PONG, { nonce: message.nonce }));
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
    );
    return;
  }
  try {
    await ensureOffscreenDocument();
    await chrome.runtime.sendMessage({ target: "offscreen", envelope: message });
  } catch (error) {
    setError(ErrorCode.EGRESS_UNAVAILABLE, error.message || String(error));
    postNative(
      envelope(MessageType.ERROR, {
        id: message.id,
        code: ErrorCode.EGRESS_UNAVAILABLE,
        message: "Offscreen executor is unavailable",
        retryable: true,
      }),
    );
  }
}

async function ensureOffscreenDocument() {
  const offscreenUrl = chrome.runtime.getURL(OFFSCREEN_PATH);
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [offscreenUrl],
  });
  if (contexts.length > 0) return;
  if (!offscreenCreation) {
    offscreenCreation = chrome.offscreen
      .createDocument({
        url: OFFSCREEN_PATH,
        reasons: ["DOM_SCRAPING"],
        justification: "Execute allowlisted cross-origin API requests through Chrome network settings",
      })
      .finally(() => {
        offscreenCreation = null;
      });
  }
  await offscreenCreation;
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

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
const ANTIGRAVITY_HOST = "daily-cloudcode-pa.googleapis.com";
const ANTIGRAVITY_USER_AGENT_RULE_ID = 1001;
const CONTROL_HANDSHAKE_TIMEOUT_MS = 5000;
const CONTROL_TIMEOUT_MS = 60000;

let nativePort = null;
let reconnectTimer = null;
let reconnectDelay = RECONNECT_MIN_MS;
let offscreenCreation = null;
let offscreenReady = false;
let lastError = null;
let handshakeComplete = false;
let negotiatedLimits = null;
const pendingControls = new Map();

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
      rejectPendingControls(message);
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
  if (
    message.type === MessageType.CONTROL_MODE_RESULT ||
    message.type === MessageType.CONTROL_ANTIGRAVITY_RESULT
  ) {
    const pending = pendingControls.get(message.id);
    if (!pending) return;
    pendingControls.delete(message.id);
    clearTimeout(pending.timeout);
    pending.resolve(message);
    return;
  }
  if (message.type === MessageType.ERROR && pendingControls.has(message.id)) {
    const pending = pendingControls.get(message.id);
    pendingControls.delete(message.id);
    clearTimeout(pending.timeout);
    pending.reject(new Error(message.message || "Native Host 拒绝了模式操作"));
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

async function requestModeControl(kind, mode) {
  await waitForNativeHandshake();
  const id = crypto.randomUUID().replaceAll("-", "");
  const type = kind === "set" ? MessageType.CONTROL_MODE_SET : MessageType.CONTROL_MODE_GET;
  const message = envelope(type, kind === "set" ? { id, mode } : { id });
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pendingControls.delete(id);
      reject(new Error("模式切换超时"));
    }, CONTROL_TIMEOUT_MS);
    pendingControls.set(id, { resolve, reject, timeout });
    if (!postNative(message)) {
      pendingControls.delete(id);
      clearTimeout(timeout);
      reject(new Error("Native Host 当前不可用"));
    }
  });
}

async function requestAntigravityControl(kind) {
  await waitForNativeHandshake();
  const id = crypto.randomUUID().replaceAll("-", "");
  const type =
    kind === "setup"
      ? MessageType.CONTROL_ANTIGRAVITY_SETUP
      : MessageType.CONTROL_ANTIGRAVITY_GET;
  const message = envelope(type, { id });
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pendingControls.delete(id);
      reject(new Error("Antigravity 配置超时"));
    }, kind === "setup" ? 15 * 60 * 1000 : CONTROL_TIMEOUT_MS);
    pendingControls.set(id, { resolve, reject, timeout });
    if (!postNative(message)) {
      pendingControls.delete(id);
      clearTimeout(timeout);
      reject(new Error("Native Host 当前不可用"));
    }
  });
}

async function waitForNativeHandshake() {
  connectNative();
  const deadline = Date.now() + CONTROL_HANDSHAKE_TIMEOUT_MS;
  while (!nativePort || !handshakeComplete) {
    if (Date.now() >= deadline) throw new Error("Native Host 尚未连接完成");
    await new Promise((resolve) => setTimeout(resolve, 100));
    connectNative();
  }
}

function rejectPendingControls(message) {
  for (const pending of pendingControls.values()) {
    clearTimeout(pending.timeout);
    pending.reject(new Error(message));
  }
  pendingControls.clear();
}

async function setAntigravityUserAgentRule(userAgent) {
  if (typeof userAgent !== "string" || userAgent.length === 0 || userAgent.length > 512) {
    throw new Error("Invalid Antigravity User-Agent");
  }
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [ANTIGRAVITY_USER_AGENT_RULE_ID],
    addRules: [
      {
        id: ANTIGRAVITY_USER_AGENT_RULE_ID,
        priority: 2,
        action: {
          type: "modifyHeaders",
          requestHeaders: [{ header: "user-agent", operation: "set", value: userAgent }],
        },
        condition: {
          urlFilter: `||${ANTIGRAVITY_HOST}/`,
          resourceTypes: ["xmlhttprequest", "other"],
        },
      },
    ],
  });
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
  if (message?.target === "background" && message.kind === "antigravity-user-agent:set") {
    setAntigravityUserAgentRule(message.userAgent)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, message: error?.message || String(error) }));
    return true;
  }
  if (message?.target === "background" && message.kind === "codex-mode:get") {
    requestModeControl("get")
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, mode: "unmanaged", message: error.message }));
    return true;
  }
  if (message?.target === "background" && message.kind === "codex-mode:set") {
    if (!["direct", "browser_lean", "browser_full"].includes(message.mode)) {
      sendResponse({ ok: false, mode: "unmanaged", message: "不支持的模式" });
      return false;
    }
    requestModeControl("set", message.mode)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, mode: "unmanaged", message: error.message }));
    return true;
  }
  if (
    message?.target === "background" &&
    ["antigravity-setup:get", "antigravity-setup:run"].includes(message.kind)
  ) {
    requestAntigravityControl(message.kind.endsWith(":run") ? "setup" : "get")
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, message: error.message }));
    return true;
  }
  return false;
});

chrome.runtime.onInstalled.addListener(connectNative);
chrome.runtime.onStartup.addListener(connectNative);
connectNative();

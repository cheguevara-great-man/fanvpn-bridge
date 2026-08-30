import {
  ErrorCode,
  MAX_CHUNK_BYTES,
  MAX_IN_FLIGHT,
  MessageType,
  PROTOCOL_VERSION,
  bytesToBase64,
  envelope,
  isProtocolEnvelope,
} from "./protocol.js";

const NATIVE_HOST_NAME = "com.fanvpn.bridge";
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const OFFSCREEN_PATH = "offscreen.html";
const BROWSER_GATEWAY_EXTENSION_ID = "gjhcbooefgfcjbcdkjbbaljkoceghnkg";
const ANTIGRAVITY_HOST = "daily-cloudcode-pa.googleapis.com";
const ANTIGRAVITY_USER_AGENT_RULE_ID = 1001;
const CONTROL_HANDSHAKE_TIMEOUT_MS = 5000;
const CONTROL_TIMEOUT_MS = 60000;
const SERVER_EXECUTOR_CONTROL_TIMEOUT_MS = 8000;
const UPDATE_TIMEOUT_MS = 25 * 60 * 1000;
const UPDATE_CHUNK_BYTES = 192 * 1024;
const UPDATE_PROJECTS = Object.freeze({
  "fanvpn-bridge": "cheguevara-great-man/fanvpn-bridge",
  "browser-gateway": "cheguevara-great-man/browser-gateway",
});

let nativePort = null;
let reconnectTimer = null;
let reconnectDelay = RECONNECT_MIN_MS;
let offscreenCreation = null;
let offscreenReady = false;
let lastError = null;
let handshakeComplete = false;
let negotiatedLimits = null;
const pendingControls = new Map();
const pendingUpdates = new Map();

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
    message.type === MessageType.CONTROL_SERVER_EXECUTOR_RESULT ||
    message.type === MessageType.CONTROL_ANTIGRAVITY_RESULT ||
    message.type === MessageType.CONTROL_DEVICE_RESULT ||
    message.type === MessageType.CONTROL_SUBAGENTS_RESULT ||
    message.type === MessageType.CONTROL_GEMINI_QUOTA_RESULT
  ) {
    const pending = pendingControls.get(message.id);
    if (!pending) return;
    pendingControls.delete(message.id);
    clearTimeout(pending.timeout);
    pending.resolve(message);
    return;
  }
  if (message.type === MessageType.CONTROL_UPDATE_READY) {
    const pending = pendingUpdates.get(message.id);
    if (pending) pending.ready.resolve();
    return;
  }
  if (message.type === MessageType.CONTROL_UPDATE_RESULT) {
    const pending = pendingUpdates.get(message.id);
    if (pending) {
      pendingUpdates.delete(message.id);
      clearTimeout(pending.timeout);
      if (message.ok === true) pending.resolve(message);
      else pending.reject(new Error(message.message || "软件更新失败"));
      return;
    }
    const status = pendingControls.get(message.id);
    if (!status) return;
    pendingControls.delete(message.id);
    clearTimeout(status.timeout);
    status.resolve(message);
    return;
  }
  if (message.type === MessageType.ERROR && pendingControls.has(message.id)) {
    const pending = pendingControls.get(message.id);
    pendingControls.delete(message.id);
    clearTimeout(pending.timeout);
    pending.reject(new Error(message.message || "Native Host 拒绝了模式操作"));
    return;
  }
  if (message.type === MessageType.ERROR && pendingUpdates.has(message.id)) {
    const pending = pendingUpdates.get(message.id);
    pendingUpdates.delete(message.id);
    clearTimeout(pending.timeout);
    const error = new Error(message.message || "Native Host 拒绝了软件更新");
    pending.ready.reject(error);
    pending.reject(error);
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

async function requestServerExecutorControl(kind, mode) {
  await waitForNativeHandshake();
  const id = crypto.randomUUID().replaceAll("-", "");
  const type = kind === "set"
    ? MessageType.CONTROL_SERVER_EXECUTOR_SET
    : MessageType.CONTROL_SERVER_EXECUTOR_GET;
  const message = envelope(type, kind === "set" ? { id, mode } : { id });
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pendingControls.delete(id);
      reject(new Error("链路控制无响应；请更新或重启 AI Bridge Host"));
    }, SERVER_EXECUTOR_CONTROL_TIMEOUT_MS);
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

async function requestDeviceControl(kind, config = null) {
  await waitForNativeHandshake();
  const id = crypto.randomUUID().replaceAll("-", "");
  const type = kind === "apply" ? MessageType.CONTROL_DEVICE_APPLY : MessageType.CONTROL_DEVICE_GET;
  const fields = kind === "apply" ? { id, config } : { id };
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pendingControls.delete(id);
      reject(new Error("设备配置超时"));
    }, CONTROL_TIMEOUT_MS);
    pendingControls.set(id, { resolve, reject, timeout });
    if (!postNative(envelope(type, fields))) {
      pendingControls.delete(id);
      clearTimeout(timeout);
      reject(new Error("Native Host 当前不可用"));
    }
  });
}

async function requestSubagentControl(kind, config = null) {
  await waitForNativeHandshake();
  const id = crypto.randomUUID().replaceAll("-", "");
  const type = kind === "apply"
    ? MessageType.CONTROL_SUBAGENTS_APPLY
    : MessageType.CONTROL_SUBAGENTS_GET;
  const fields = kind === "apply" ? { id, config } : { id };
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pendingControls.delete(id);
      reject(new Error("子 Agent 配置超时"));
    }, CONTROL_TIMEOUT_MS);
    pendingControls.set(id, { resolve, reject, timeout });
    if (!postNative(envelope(type, fields))) {
      pendingControls.delete(id);
      clearTimeout(timeout);
      reject(new Error("Native Host 当前不可用"));
    }
  });
}

async function requestGeminiQuotaControl() {
  await waitForNativeHandshake();
  const id = crypto.randomUUID().replaceAll("-", "");
  const message = envelope(MessageType.CONTROL_GEMINI_QUOTA_GET, { id });
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pendingControls.delete(id);
      reject(new Error("Gemini 额度查询超时"));
    }, CONTROL_TIMEOUT_MS);
    pendingControls.set(id, { resolve, reject, timeout });
    if (!postNative(message)) {
      pendingControls.delete(id);
      clearTimeout(timeout);
      reject(new Error("Native Host 当前不可用"));
    }
  });
}

async function requestUpdateStatus() {
  await waitForNativeHandshake();
  const id = crypto.randomUUID().replaceAll("-", "");
  const message = envelope(MessageType.CONTROL_UPDATE_STATUS, { id });
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pendingControls.delete(id);
      reject(new Error("读取安装状态超时"));
    }, CONTROL_TIMEOUT_MS);
    pendingControls.set(id, { resolve, reject, timeout });
    if (!postNative(message)) {
      pendingControls.delete(id);
      clearTimeout(timeout);
      reject(new Error("Native Host 当前不可用"));
    }
  });
}

async function requestSoftwareUpdate(project, installRoot = "") {
  const repository = UPDATE_PROJECTS[project];
  if (!repository) throw new Error("不支持的软件更新项目");
  await waitForNativeHandshake();
  const archive = await downloadUpdateArchive(repository);
  const id = crypto.randomUUID().replaceAll("-", "");
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pendingUpdates.delete(id);
      reject(new Error("软件更新超时；请稍后重新打开扩展检查状态"));
    }, UPDATE_TIMEOUT_MS);
    const ready = deferred();
    pendingUpdates.set(id, { resolve, reject, ready, timeout });
    if (!postNative(envelope(MessageType.CONTROL_UPDATE_START, {
      id, project, commit: archive.commit, install_root: String(installRoot || "").trim(),
    }))) {
      pendingUpdates.delete(id);
      clearTimeout(timeout);
      reject(new Error("Native Host 当前不可用"));
      return;
    }
    ready.promise
      .then(() => sendUpdateArchive(id, archive.bytes))
      .catch((error) => {
        const pending = pendingUpdates.get(id);
        if (pending) {
          pendingUpdates.delete(id);
          clearTimeout(timeout);
          reject(error);
        }
      });
  });
}

async function downloadUpdateArchive(repository) {
  const metadata = await fetch(`https://api.github.com/repos/${repository}/commits/master`, {
    cache: "no-store", headers: { accept: "application/vnd.github+json" },
  });
  if (!metadata.ok) throw new Error(`无法检查更新：GitHub 返回 HTTP ${metadata.status}`);
  const commit = String((await metadata.json())?.sha || "").toLowerCase();
  if (!/^[0-9a-f]{40}$/.test(commit)) throw new Error("GitHub 更新版本无效");
  const response = await fetch(`https://github.com/${repository}/archive/${commit}.zip`, { cache: "no-store" });
  if (!response.ok) throw new Error(`无法下载更新包：GitHub 返回 HTTP ${response.status}`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.byteLength === 0 || bytes.byteLength > 64 * 1024 * 1024) throw new Error("更新包大小异常");
  return { commit, bytes };
}

async function sendUpdateArchive(id, bytes) {
  for (let offset = 0, seq = 0; offset < bytes.byteLength; offset += UPDATE_CHUNK_BYTES, seq += 1) {
    const data = bytes.subarray(offset, Math.min(offset + UPDATE_CHUNK_BYTES, bytes.byteLength));
    if (!postNative(envelope(MessageType.CONTROL_UPDATE_BODY, { id, seq, data: bytesToBase64(data) }))) {
      throw new Error("Native Host 在传输更新包时断开");
    }
  }
  if (!postNative(envelope(MessageType.CONTROL_UPDATE_FINISH, { id }))) {
    throw new Error("Native Host 在开始更新时断开");
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((onResolve, onReject) => { resolve = onResolve; reject = onReject; });
  return { promise, resolve, reject };
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
  for (const pending of pendingUpdates.values()) {
    clearTimeout(pending.timeout);
    pending.ready.reject(new Error(message));
    pending.reject(new Error(message));
  }
  pendingUpdates.clear();
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
    if (!["direct", "browser_lean", "browser_full", "gemini_account", "hybrid_force", "hybrid_configured", "hybrid_native"].includes(message.mode)) {
      sendResponse({ ok: false, mode: "unmanaged", message: "不支持的模式" });
      return false;
    }
    requestModeControl("set", message.mode)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, mode: "unmanaged", message: error.message }));
    return true;
  }
  if (message?.target === "background" && message.kind === "server-executor:get") {
    requestServerExecutorControl("get")
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, state: { mode: "browser_chain" }, message: error.message }));
    return true;
  }
  if (message?.target === "background" && message.kind === "server-executor:set") {
    if (!["browser_chain", "server_center"].includes(message.mode)) {
      sendResponse({ ok: false, state: { mode: "browser_chain" }, message: "不支持的服务器中心链路" });
      return false;
    }
    requestServerExecutorControl("set", message.mode)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, state: { mode: "browser_chain" }, message: error.message }));
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
  if (message?.target === "background" && message.kind === "gemini-quota:get") {
    requestGeminiQuotaControl()
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, message: error.message }));
    return true;
  }
  if (message?.target === "background" && message.kind === "software-update:status") {
    requestUpdateStatus()
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, message: error.message }));
    return true;
  }
  if (message?.target === "background" && message.kind === "software-update:run") {
    requestSoftwareUpdate(message.project, message.installRoot)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, message: error.message }));
    return true;
  }
  if (
    message?.target === "background" &&
    ["subagents:get", "subagents:apply"].includes(message.kind)
  ) {
    requestSubagentControl(message.kind.endsWith(":apply") ? "apply" : "get", message.config)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, message: error.message }));
    return true;
  }
  return false;
});

chrome.runtime.onMessageExternal?.addListener((message, sender, sendResponse) => {
  if (sender.id !== BROWSER_GATEWAY_EXTENSION_ID) {
    sendResponse({ ok: false, message: "不受信任的扩展" });
    return false;
  }
  if (message?.kind === "device-config:get") {
    requestDeviceControl("get")
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, message: error.message }));
    return true;
  }
  if (message?.kind === "device-config:apply") {
    const value = message.config ?? {};
    requestDeviceControl("apply", {
      machine_id: value.machineId,
      machine_name: value.machineName,
      report_token: value.reportToken,
      collector_url: value.collectorUrl,
      dashboard_url: value.dashboardUrl,
    })
      .then((result) => {
        sendResponse(result);
        if (result?.ok === true && result?.state?.restart_required) {
          setTimeout(() => nativePort?.disconnect(), 250);
        }
      })
      .catch((error) => sendResponse({ ok: false, message: error.message }));
    return true;
  }
  if (message?.kind === "software-update:run" && message.project === "browser-gateway") {
    requestSoftwareUpdate("browser-gateway", message.installRoot)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, message: error.message }));
    return true;
  }
  sendResponse({ ok: false, message: "不支持的外部操作" });
  return false;
});

chrome.runtime.onInstalled.addListener(connectNative);
chrome.runtime.onStartup.addListener(connectNative);
connectNative();

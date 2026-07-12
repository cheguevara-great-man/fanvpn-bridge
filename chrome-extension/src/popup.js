const native = document.getElementById("native");
const handshake = document.getElementById("handshake");
const executor = document.getElementById("executor");
const version = document.getElementById("version");
const errorBox = document.getElementById("error");

try {
  const status = await chrome.runtime.sendMessage({ target: "background", kind: "status" });
  setState(native, status.nativeConnected, status.nativeConnected ? "已连接" : "未连接");
  setState(handshake, status.handshakeComplete, status.handshakeComplete ? "完成" : "未完成");
  executor.textContent = status.executor || "-";
  version.textContent = status.version || "-";
  if (status.lastError) {
    errorBox.hidden = false;
    errorBox.textContent = `${status.lastError.code}: ${status.lastError.message}`;
  }
} catch (error) {
  setState(native, false, "不可用");
  setState(handshake, false, "不可用");
  errorBox.hidden = false;
  errorBox.textContent = error.message || String(error);
}

function setState(element, ok, text) {
  element.textContent = text;
  element.className = ok ? "ok" : "bad";
}

const native = document.getElementById("native");
const handshake = document.getElementById("handshake");
const executor = document.getElementById("executor");
const siteAccess = document.getElementById("site-access");
const version = document.getElementById("version");
const errorBox = document.getElementById("error");

try {
  const chatGptAccess = await chrome.permissions.contains({
    origins: ["https://chatgpt.com/*"],
  });
  setState(siteAccess, chatGptAccess, chatGptAccess ? "已授权" : "被 Chrome 扣留");
  const status = await chrome.runtime.sendMessage({ target: "background", kind: "status" });
  setState(native, status.nativeConnected, status.nativeConnected ? "已连接" : "未连接");
  setState(handshake, status.handshakeComplete, status.handshakeComplete ? "完成" : "未完成");
  executor.textContent = status.executor || "-";
  version.textContent = status.version || "-";
  if (status.lastError) {
    errorBox.hidden = false;
    errorBox.textContent = `${status.lastError.code}: ${status.lastError.message}`;
  } else if (!chatGptAccess) {
    errorBox.hidden = false;
    errorBox.textContent = "请在扩展详情中将网站访问权限设为“在所有网站上”。";
  }
} catch (error) {
  setState(siteAccess, false, "不可用");
  setState(native, false, "不可用");
  setState(handshake, false, "不可用");
  errorBox.hidden = false;
  errorBox.textContent = error.message || String(error);
}

function setState(element, ok, text) {
  element.textContent = text;
  element.className = ok ? "ok" : "bad";
}

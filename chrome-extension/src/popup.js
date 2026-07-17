const native = document.getElementById("native");
const handshake = document.getElementById("handshake");
const executor = document.getElementById("executor");
const siteAccess = document.getElementById("site-access");
const version = document.getElementById("version");
const modeNote = document.getElementById("mode-note");
const noticeBox = document.getElementById("notice");
const errorBox = document.getElementById("error");
const modeButtons = [...document.querySelectorAll("button[data-mode]")];

const MODE_LABELS = {
  direct: "服务器直连",
  browser_lean: "浏览器精简",
  browser_full: "浏览器完整",
  unmanaged: "未由 Bridge 管理",
};

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
  if (status.lastError) showError(`${status.lastError.code}: ${status.lastError.message}`);
  else if (!chatGptAccess) showError("请在扩展详情中将网站访问权限设为“在所有网站上”。");
} catch (error) {
  setState(siteAccess, false, "不可用");
  setState(native, false, "不可用");
  setState(handshake, false, "不可用");
  showError(error.message || String(error));
}

try {
  await refreshMode();
} catch (error) {
  modeNote.textContent = "上次托管配置：读取失败";
  showError(error.message || String(error));
}

for (const button of modeButtons) {
  button.addEventListener("click", async () => {
    setBusy(true);
    hideMessages();
    try {
      const result = await chrome.runtime.sendMessage({
        target: "background",
        kind: "codex-mode:set",
        mode: button.dataset.mode,
      });
      if (result?.ok !== true) throw new Error(result?.message || "模式切换失败");
      renderMode(result.mode);
      noticeBox.hidden = false;
      noticeBox.textContent = "切换成功，VS Code 已按所选模式启动。";
    } catch (error) {
      showError(error.message || String(error));
    } finally {
      setBusy(false);
    }
  });
}

async function refreshMode() {
  const result = await chrome.runtime.sendMessage({ target: "background", kind: "codex-mode:get" });
  if (result?.ok !== true) throw new Error(result?.message || "无法读取 Codex 模式");
  renderMode(result.mode);
}

function renderMode(mode) {
  modeNote.textContent = `上次托管配置：${MODE_LABELS[mode] || mode}`;
  for (const button of modeButtons) button.classList.toggle("active", button.dataset.mode === mode);
}

function setBusy(busy) {
  for (const button of modeButtons) button.disabled = busy;
}

function hideMessages() {
  noticeBox.hidden = true;
  errorBox.hidden = true;
}

function showError(message) {
  noticeBox.hidden = true;
  errorBox.hidden = false;
  errorBox.textContent = message;
}

function setState(element, ok, text) {
  element.textContent = text;
  element.className = ok ? "ok" : "bad";
}

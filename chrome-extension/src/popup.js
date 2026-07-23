const native = document.getElementById("native");
const handshake = document.getElementById("handshake");
const executor = document.getElementById("executor");
const siteAccess = document.getElementById("site-access");
const version = document.getElementById("version");
const modeNote = document.getElementById("mode-note");
const antigravityButton = document.getElementById("antigravity-setup");
const antigravityNote = document.getElementById("antigravity-note");
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
  const chatGptAccess = await chrome.permissions.contains({ origins: ["https://chatgpt.com/*"] });
  setState(siteAccess, chatGptAccess, chatGptAccess ? "已授权" : "被 Chrome 扣留");
  const status = await chrome.runtime.sendMessage({ target: "background", kind: "status" });
  setState(native, status.nativeConnected, status.nativeConnected ? "已连接" : "未连接");
  setState(handshake, status.handshakeComplete, status.handshakeComplete ? "完成" : "未完成");
  executor.textContent = status.executor || "-";
  version.textContent = status.version || "-";
  if (status.lastError) showError(`${status.lastError.code}: ${status.lastError.message}`);
  else if (!chatGptAccess) showError("请在扩展详情中把网站访问权限设为“在所有网站上”。");
} catch (error) {
  setState(siteAccess, false, "不可用");
  setState(native, false, "不可用");
  setState(handshake, false, "不可用");
  showError(error.message || String(error));
}

try { await refreshMode(); } catch (error) {
  modeNote.textContent = "上次托管配置：读取失败";
  showError(error.message || String(error));
}
try { await refreshAntigravity(); } catch (error) {
  renderAntigravity(null);
  showError(error.message || String(error));
}

for (const button of modeButtons) {
  button.addEventListener("click", async () => {
    setBusy(true);
    hideMessages();
    try {
      const result = await chrome.runtime.sendMessage({
        target: "background", kind: "codex-mode:set", mode: button.dataset.mode,
      });
      if (result?.ok !== true) throw new Error(result?.message || "模式切换失败");
      renderMode(result.mode);
      showNotice("切换成功，VS Code 已按所选模式启动。");
    } catch (error) { showError(error.message || String(error)); }
    finally { setBusy(false); }
  });
}

antigravityButton.addEventListener("click", async () => {
  setBusy(true);
  hideMessages();
  antigravityButton.textContent = "正在通过 Chrome 下载并配置……";
  try {
    const result = await chrome.runtime.sendMessage({
      target: "background", kind: "antigravity-setup:run",
    });
    if (result?.ok !== true) throw new Error(result?.message || "Antigravity 配置失败");
    renderAntigravity(result.state);
    showNotice("配置完成。完全退出并重新打开 VS Code 后即可使用。");
  } catch (error) { showError(error.message || String(error)); }
  finally { setBusy(false); }
});

async function refreshMode() {
  const result = await chrome.runtime.sendMessage({ target: "background", kind: "codex-mode:get" });
  if (result?.ok !== true) throw new Error(result?.message || "无法读取 Codex 模式");
  renderMode(result.mode);
}

async function refreshAntigravity() {
  const result = await chrome.runtime.sendMessage({
    target: "background", kind: "antigravity-setup:get",
  });
  if (result?.ok !== true) throw new Error(result?.message || "无法读取 Antigravity 状态");
  renderAntigravity(result.state);
}

function renderMode(mode) {
  modeNote.textContent = `上次托管配置：${MODE_LABELS[mode] || mode}`;
  for (const button of modeButtons) button.classList.toggle("active", button.dataset.mode === mode);
}

function renderAntigravity(state) {
  const ready = state?.ready === true;
  antigravityButton.classList.toggle("ready", ready);
  antigravityButton.textContent = ready ? "已配置 · 点击检查更新" : "一键配置 Antigravity";
  antigravityNote.textContent = ready
    ? "CLI 与 VS Code 插件均已就绪；会话打开时自动运行。"
    : "自动安装 CLI、VS Code 插件并配置浏览器链路。";
}

function setBusy(busy) {
  for (const button of [...modeButtons, antigravityButton]) button.disabled = busy;
}
function hideMessages() { noticeBox.hidden = true; errorBox.hidden = true; }
function showNotice(message) { errorBox.hidden = true; noticeBox.hidden = false; noticeBox.textContent = message; }
function showError(message) { noticeBox.hidden = true; errorBox.hidden = false; errorBox.textContent = message; }
function setState(element, ok, text) { element.textContent = text; element.className = ok ? "ok" : "bad"; }

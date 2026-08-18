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
const subagentModel = document.getElementById("subagent-model");
const subagentEffort = document.getElementById("subagent-effort");
const subagentRoles = document.getElementById("subagent-roles");
const addRoleButton = document.getElementById("add-role");
const saveSubagentsButton = document.getElementById("save-subagents");
const quotaRefreshButton = document.getElementById("quota-refresh");
const quotaGroupLabel = document.getElementById("quota-group");
const quota5hValue = document.getElementById("quota-5h-value");
const quota5hFill = document.getElementById("quota-5h-fill");
const quota5hNote = document.getElementById("quota-5h-note");
const quotaWeeklyValue = document.getElementById("quota-weekly-value");
const quotaWeeklyFill = document.getElementById("quota-weekly-fill");
const quotaWeeklyNote = document.getElementById("quota-weekly-note");
let availableModels = [];

const MODE_LABELS = {
  gemini_account: "仅 Gemini 账号",
  hybrid_force: "Hybrid · 子 Agent 固定 Gemini 3.7 Flash High",
  hybrid_configured: "Hybrid · 子 Agent 默认 Gemini（Codex 可覆盖）",
  hybrid_native: "Hybrid · Codex 原生子 Agent 决策",
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
try { await refreshSubagents(); } catch (_error) {
  document.getElementById("subagent-settings").open = false;
}
try { await refreshGeminiQuota(); } catch (error) {
  renderGeminiQuotaError(error.message || String(error));
}

quotaRefreshButton.addEventListener("click", async () => {
  quotaRefreshButton.disabled = true;
  try { await refreshGeminiQuota(); } catch (error) {
    renderGeminiQuotaError(error.message || String(error));
  } finally { quotaRefreshButton.disabled = false; }
});

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
      if (result.mode === "hybrid_configured") await refreshSubagents();
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

addRoleButton.addEventListener("click", () => addRole({
  name: "", description: "", developer_instructions: "",
  model: subagentModel.value || "gemini-3.7-flash",
  model_reasoning_effort: subagentEffort.value || "high",
}));

saveSubagentsButton.addEventListener("click", async () => {
  setBusy(true);
  hideMessages();
  try {
    const roles = [...subagentRoles.querySelectorAll(".role")].map(readRole);
    const result = await chrome.runtime.sendMessage({
      target: "background",
      kind: "subagents:apply",
      config: {
        default_model: subagentModel.value,
        default_reasoning_effort: subagentEffort.value,
        roles,
      },
    });
    if (result?.ok !== true) throw new Error(result?.message || "子 Agent 配置保存失败");
    renderSubagents(result.state);
    showNotice("子 Agent 配置已保存；新启动的 Agent 将使用新设置。");
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

async function refreshSubagents() {
  const result = await chrome.runtime.sendMessage({ target: "background", kind: "subagents:get" });
  if (result?.ok !== true) throw new Error(result?.message || "无法读取子 Agent 配置");
  renderSubagents(result.state);
}

function renderSubagents(state) {
  availableModels = Array.isArray(state?.models) ? state.models : [];
  fillModelSelect(subagentModel, state?.default_model || "gemini-3.7-flash");
  fillEffortSelect(subagentEffort, state?.default_reasoning_effort || "high", selectedModel());
  subagentModel.onchange = () => fillEffortSelect(subagentEffort, subagentEffort.value, selectedModel());
  subagentRoles.replaceChildren();
  for (const role of state?.roles || []) addRole(role);
}

function selectedModel(id = subagentModel.value) {
  return availableModels.find((model) => model.id === id);
}

function fillModelSelect(select, selected) {
  select.replaceChildren();
  const models = availableModels.length ? availableModels : [
    { id: "gemini-3.7-flash", name: "Gemini 3.7 Flash", efforts: ["low", "medium", "high"] },
  ];
  for (const model of models) {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = model.name || model.id;
    option.selected = model.id === selected;
    select.append(option);
  }
}

function fillEffortSelect(select, selected, model) {
  const efforts = model?.efforts?.length ? model.efforts : ["low", "medium", "high"];
  select.replaceChildren();
  for (const effort of efforts) {
    const option = document.createElement("option");
    option.value = effort;
    option.textContent = ({ low: "低", medium: "中", high: "高", xhigh: "超高", max: "Max", ultra: "Ultra" })[effort] || effort;
    option.selected = effort === selected;
    select.append(option);
  }
  if (!select.value) select.value = efforts.includes("high") ? "high" : efforts[0];
}

function addRole(role) {
  const container = document.createElement("div");
  container.className = "role";
  container.innerHTML = `
    <div class="role-head"><strong>自定义角色</strong><button type="button" data-remove>删除</button></div>
    <label>角色名<input data-key="name" placeholder="例如 reviewer" /></label>
    <label>用途描述<input data-key="description" placeholder="Codex 根据这段描述决定何时使用" /></label>
    <label>角色指令<textarea data-key="developer_instructions" placeholder="这个角色应如何工作"></textarea></label>
    <label>模型<select data-key="model"></select></label>
    <label>推理强度<select data-key="model_reasoning_effort"></select></label>`;
  for (const key of ["name", "description", "developer_instructions"]) {
    container.querySelector(`[data-key="${key}"]`).value = role[key] || "";
  }
  const modelSelect = container.querySelector('[data-key="model"]');
  const effortSelect = container.querySelector('[data-key="model_reasoning_effort"]');
  fillModelSelect(modelSelect, role.model || subagentModel.value);
  fillEffortSelect(effortSelect, role.model_reasoning_effort || "high", selectedModel(modelSelect.value));
  modelSelect.onchange = () => fillEffortSelect(effortSelect, effortSelect.value, selectedModel(modelSelect.value));
  container.querySelector("[data-remove]").onclick = () => container.remove();
  subagentRoles.append(container);
}

function readRole(container) {
  return Object.fromEntries(["name", "description", "developer_instructions", "model", "model_reasoning_effort"]
    .map((key) => [key, container.querySelector(`[data-key="${key}"]`).value]));
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
  for (const button of [...modeButtons, antigravityButton, addRoleButton, saveSubagentsButton, quotaRefreshButton]) button.disabled = busy;
}
function hideMessages() { noticeBox.hidden = true; errorBox.hidden = true; }
function showNotice(message) { errorBox.hidden = true; noticeBox.hidden = false; noticeBox.textContent = message; }
function showError(message) { noticeBox.hidden = true; errorBox.hidden = false; errorBox.textContent = message; }
function setState(element, ok, text) { element.textContent = text; element.className = ok ? "ok" : "bad"; }

async function refreshGeminiQuota() {
  const result = await chrome.runtime.sendMessage({ target: "background", kind: "gemini-quota:get" });
  if (result?.ok !== true) throw new Error(result?.message || "Gemini 额度查询失败");
  renderGeminiQuota(result.state);
}

function renderGeminiQuota(state) {
  const quota = state?.quota;
  const groups = Array.isArray(quota?.groups) ? quota.groups : [];
  const group = groups.find((item) => String(item?.displayName || "").toLowerCase().includes("gemini")) || groups[0];
  if (!group || !Array.isArray(group.buckets) || group.buckets.length === 0) {
    renderGeminiQuotaError("Google 账号未返回额度信息");
    return;
  }
  quotaGroupLabel.textContent = group.displayName || "Gemini 模型";
  applyQuotaBucket(group.buckets.find((bucket) => bucket?.window === "5h"), quota5hValue, quota5hFill, quota5hNote);
  applyQuotaBucket(group.buckets.find((bucket) => bucket?.window === "weekly"), quotaWeeklyValue, quotaWeeklyFill, quotaWeeklyNote);
}

function applyQuotaBucket(bucket, valueElement, fillElement, noteElement) {
  if (!bucket) {
    valueElement.textContent = "-";
    valueElement.className = "";
    fillElement.style.width = "0%";
    noteElement.textContent = "无数据";
    return;
  }
  const fraction = Math.max(0, Math.min(1, Number(bucket.remainingFraction) || 0));
  const percent = Math.round(fraction * 100);
  valueElement.textContent = `剩余 ${percent}%`;
  valueElement.className = percent <= 20 ? "bad" : "ok";
  fillElement.style.width = `${percent}%`;
  fillElement.className = `quota-fill${percent <= 20 ? " crit" : percent <= 50 ? " warn" : ""}`;
  const parts = [];
  if (bucket.description) parts.push(bucket.description);
  const reset = formatResetTime(bucket.resetTime);
  if (reset) parts.push(`重置时间：${reset}`);
  noteElement.textContent = parts.join(" · ") || "无数据";
}

function formatResetTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function renderGeminiQuotaError(message) {
  quotaGroupLabel.textContent = "Gemini 模型";
  quota5hValue.textContent = "-";
  quota5hValue.className = "bad";
  quota5hFill.style.width = "0%";
  quota5hNote.textContent = message;
  quotaWeeklyValue.textContent = "-";
  quotaWeeklyValue.className = "bad";
  quotaWeeklyFill.style.width = "0%";
  quotaWeeklyNote.textContent = message;
}

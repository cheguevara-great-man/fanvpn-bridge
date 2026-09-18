const USER_TOKEN_STORAGE_KEY = "userToken";

function firstString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

function readDeepSeekToken() {
  try {
    const raw = localStorage.getItem(USER_TOKEN_STORAGE_KEY);
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw);
      if (typeof parsed === "string") return parsed.trim() || null;
      if (parsed && typeof parsed === "object") {
        return firstString(parsed.token, parsed.value, parsed.accessToken);
      }
    } catch (_error) {
      // Older DeepSeek clients stored the token as a plain string.
    }
    return raw.trim() && raw.trim() !== "null" ? raw.trim() : null;
  } catch (_error) {
    return null;
  }
}

function authSnapshot() {
  const token = readDeepSeekToken();
  if (!token) return null;
  return {
    token,
    locale: document.documentElement.lang || navigator.language || "en-US",
    timezoneOffset: -new Date().getTimezoneOffset() * 60,
  };
}

function publishAuth() {
  const auth = authSnapshot();
  if (!auth) return;
  void chrome.runtime.sendMessage({ kind: "deepseek-auth:update", auth }).catch(() => {});
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.kind !== "deepseek-auth:get") return false;
  const auth = authSnapshot();
  sendResponse(auth ? { ok: true, auth } : { ok: false });
  return false;
});

publishAuth();
window.addEventListener("focus", publishAuth);
window.addEventListener("storage", (event) => {
  if (event.key === USER_TOKEN_STORAGE_KEY) publishAuth();
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") publishAuth();
});

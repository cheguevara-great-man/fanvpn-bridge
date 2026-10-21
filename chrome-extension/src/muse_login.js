// Runs only in a top-level Muse page. Cookies stay in Chrome's cookie jar.
(() => {
  if (location.origin !== "https://muse.ai" || window.top !== window) return;
  const errors = new Set([
    "muse_web_login_required", "muse_web_vm_unavailable", "muse_web_rate_limited",
    "muse_web_auth_timeout", "muse_web_ticket_invalid",
  ]);
  function token(value) {
    return typeof value === "string" && /^[\x21-\x7e]{1,16384}$/.test(value);
  }
  async function ticket() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 25000);
    async function request(path, method, body, accessToken) {
      const headers = { Accept: "application/json", "Content-Type": "application/json" };
      if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
      const response = await fetch(new URL(path, location.origin).href, {
        method, headers, credentials: "include", cache: "no-store", redirect: "error",
        signal: controller.signal,
        ...(method === "POST" ? { body: body === undefined ? "" : JSON.stringify(body) } : {}),
      });
      if (response.status === 401 || response.status === 403) throw new Error("muse_web_login_required");
      if (response.status === 429) throw new Error("muse_web_rate_limited");
      if (!response.ok) throw new Error("muse_web_browser_unavailable");
      const text = await response.text();
      if (text.length > 128 * 1024) throw new Error("muse_web_ticket_invalid");
      const value = JSON.parse(text);
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("muse_web_ticket_invalid");
      return value;
    }
    try {
      const session = await request("/api/session", "GET");
      if (typeof session.vm_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(session.vm_id)) {
        throw new Error("muse_web_vm_unavailable");
      }
      const auth = await request("/api/auth/check", "POST");
      if (!token(auth.access_token)) throw new Error("muse_web_ticket_invalid");
      const hatch = await request("/api/hatch/token", "POST", {
        vmAddress: `wss://${session.vm_id}.metaaivm.com/`, vmName: session.vm_id,
      }, auth.access_token);
      if (!token(hatch.token)) throw new Error("muse_web_ticket_invalid");
      return { ok: true, ticket: { vm_id: session.vm_id, access_token: auth.access_token, hatch_token: hatch.token } };
    } catch (error) {
      // No response bodies, raw exceptions, Cookie values or tokens in errors.
      const code = controller.signal.aborted ? "muse_web_auth_timeout" : error?.message;
      return { ok: false, error_code: errors.has(code) ? code : "muse_web_browser_unavailable" };
    } finally {
      clearTimeout(timer);
    }
  }
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender?.id !== chrome.runtime.id || message?.kind !== "muse-auth:get") return false;
    void ticket().then(respond);
    return true;
  });
})();

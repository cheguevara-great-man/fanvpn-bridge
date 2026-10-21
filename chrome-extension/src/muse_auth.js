// Browser-side broker. No Cookie permission, storage, tab creation or page edits.
export async function museBrowserTicket(browser = chrome) {
  const tabs = await browser.tabs.query({ url: "https://muse.ai/*" });
  const candidates = tabs.filter((tab) => {
    try {
      return Number.isInteger(tab.id) && !tab.incognito && new URL(tab.url).origin === "https://muse.ai";
    } catch { return false; }
  }).sort((a, b) => Number(Boolean(b.active)) - Number(Boolean(a.active)) || (b.lastAccessed || 0) - (a.lastAccessed || 0));
  if (!candidates.length) return { ok: false, error_code: "muse_web_tab_required" };
  // Do not combine credentials across tabs/accounts or silently switch accounts.
  try {
    const result = await browser.tabs.sendMessage(candidates[0].id, { kind: "muse-auth:get" });
    if (result?.ok === true) {
      const value = result.ticket;
      const token = (raw) => typeof raw === "string" && /^[\x21-\x7e]{1,16384}$/.test(raw);
      if (value && Object.keys(value).length === 3 && typeof value.vm_id === "string"
          && /^[A-Za-z0-9_-]{1,128}$/.test(value.vm_id) && token(value.access_token) && token(value.hatch_token)) {
        return { ok: true, ticket: { vm_id: value.vm_id, access_token: value.access_token, hatch_token: value.hatch_token } };
      }
      return { ok: false, error_code: "muse_web_ticket_invalid" };
    }
    const allowed = new Set(["muse_web_login_required", "muse_web_vm_unavailable", "muse_web_rate_limited",
      "muse_web_auth_timeout", "muse_web_ticket_invalid"]);
    return { ok: false, error_code: allowed.has(result?.error_code) ? result.error_code : "muse_web_browser_unavailable" };
  } catch {
    // A pre-update tab may lack the script. Do not inject into or reload it.
    return { ok: false, error_code: "muse_web_tab_required" };
  }
}

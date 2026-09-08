// Explicit network configuration for the Bridge-managed Electron partition.
// Credentials never appear in command lines or renderer state.
const fs = require("node:fs");
const path = require("node:path");

function prepareBridgeNetwork({ app, coreHome }) {
  if (process.env.BRIDGE_WEB_MANAGED !== "1") return;
  let value;
  try { value = JSON.parse(fs.readFileSync(path.join(coreHome, "network.json"), "utf8")); }
  catch (error) { if (error.code === "ENOENT") return; throw error; }
  const tunnelProxyUrl = value.tunnelProxyUrl;
  const browserProxyUrl = value.browserProxyUrl;
  if (tunnelProxyUrl !== undefined && tunnelProxyUrl !== "http://127.0.0.1:18889") {
    throw new Error("Invalid WebHarness proxy configuration");
  }
  // The native Tunnel client is not an Electron webContents and does not
  // inherit Chromium's proxy. Give only this launcher and its children the
  // existing loopback forward proxy; Windows and Codex remain untouched.
  if (tunnelProxyUrl) {
    process.env.HTTPS_PROXY = tunnelProxyUrl;
    process.env.HTTP_PROXY = tunnelProxyUrl;
    process.env.ALL_PROXY = tunnelProxyUrl;
    process.env.https_proxy = tunnelProxyUrl;
    process.env.http_proxy = tunnelProxyUrl;
    process.env.all_proxy = tunnelProxyUrl;
  }
  process.env.NO_PROXY = "127.0.0.1,localhost,::1";
  process.env.no_proxy = process.env.NO_PROXY;
  if (value.mode === "gateway") {
    if (browserProxyUrl !== "http://127.0.0.1:18889") {
      throw new Error("Invalid WebHarness browser proxy configuration");
    }
    return { browserProxyUrl };
  }
  return {};
}

async function applyBridgeBrowserNetwork({ session, partition, network, timeoutMs = 5_000 }) {
  if (!network?.browserProxyUrl) return;
  const configure = session.fromPartition(partition).setProxy({
    mode: "fixed_servers",
    proxyRules: "127.0.0.1:18889",
    // Electron expects a comma-separated bypass list. Keeping loopback out of
    // the proxy is mandatory because the browser helper, CDP, Responses
    // listener and Tunnel diagnostics all communicate locally.
    proxyBypassRules: "<local>,localhost,127.0.0.1,[::1]",
  });
  let timeout;
  try {
    await Promise.race([
      configure,
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error("WebHarness browser proxy configuration timed out")), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

module.exports = { applyBridgeBrowserNetwork, prepareBridgeNetwork };

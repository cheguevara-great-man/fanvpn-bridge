// Explicit network configuration for the Bridge-managed Electron partition.
// Credentials never appear in command lines or renderer state.
const fs = require("node:fs");
const path = require("node:path");

async function configureBridgeNetwork({ app, session, coreHome, partition }) {
  if (process.env.BRIDGE_WEB_MANAGED !== "1") return;
  let value;
  try { value = JSON.parse(fs.readFileSync(path.join(coreHome, "network.json"), "utf8")); }
  catch (error) { if (error.code === "ENOENT") return; throw error; }
  if (value.mode === "system") return;
  const host = value.host;
  if (typeof host !== "string" || !/^[a-zA-Z0-9.-]+$/.test(host)
      || !Number.isInteger(value.port) || value.port < 1 || value.port > 65535
      || typeof value.username !== "string" || typeof value.password !== "string"
      || /[\r\n\0]/.test(value.username + value.password)) {
    throw new Error("Invalid WebHarness proxy configuration");
  }
  // The secure MCP tunnel is a child process, not an Electron webContents.
  // Scope its standard proxy environment to this launcher and its children.
  const proxy = new URL(`https://${host}:${value.port}`);
  proxy.username = value.username;
  proxy.password = value.password;
  process.env.HTTPS_PROXY = proxy.href;
  process.env.HTTP_PROXY = proxy.href;
  process.env.https_proxy = proxy.href;
  process.env.http_proxy = proxy.href;
  process.env.NO_PROXY = "127.0.0.1,localhost,::1";
  process.env.no_proxy = process.env.NO_PROXY;
  await session.fromPartition(partition).setProxy({
    mode: "fixed_servers", proxyRules: `https://${host}:${value.port}`,
    proxyBypassRules: "localhost;127.0.0.1;[::1]",
  });
  app.on("login", (event, contents, details, auth, callback) => {
    if (!auth.isProxy || auth.host.toLowerCase() !== host.toLowerCase() || auth.port !== value.port
        || contents?.session !== session.fromPartition(partition)) return;
    event.preventDefault();
    callback(value.username, value.password);
  });
}
module.exports = { configureBridgeNetwork };

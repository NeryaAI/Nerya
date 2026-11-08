"use strict";
const http = require("node:http");
const crypto = require("node:crypto");
const net = require("node:net");
const { readdir } = require("node:fs/promises");
const path = require("node:path");
const PROOF = "x-nerya-local-peer";
function loopback(address) {
  const value = String(address || "").replace(/^::ffff:/, "");
  return value === "::1" || (net.isIP(value) === 4 && value.startsWith("127."));
}
function markPeer(req, key) {
  // Never trust a client-provided assertion. Decide before Next adds its own
  // x-forwarded-for; public tunnels carry an incoming forwarding chain.
  delete req.headers[PROOF];
  if (loopback(req.socket.remoteAddress) && !req.headers["x-forwarded-for"] && !req.headers["x-real-ip"] && !req.headers.forwarded) {
    req.headers[PROOF] = key;
  }
}
async function start() {
  const dir = process.cwd();
  const dev = process.env.NODE_ENV !== "production";
  // Next's route matcher scans these even when native watching is disabled.
  // Fail before prepare instead of entering a repeated scandir/500 cycle.
  for (const routeDir of dev ? ["pages", "app", "src/pages", "src/app"] : []) {
    const target = path.join(dir, routeDir);
    try {
      await readdir(target);
    } catch (error) {
      if (error.code === "ENOENT") continue;
      if (error.code !== "EPERM" && error.code !== "EACCES") throw error;
      throw new Error(`Dashboard cannot scan ${target} (${error.code}). Check directory permissions and macOS Privacy & Security > Files and Folders for the application launching Node. Polling cannot bypass directory read permissions.`, { cause: error });
    }
  }
  const key = crypto.randomBytes(32).toString("hex");
  process.env.NERYA_LOCAL_PEER_KEY = key;
  const port = Number(process.env.PORT || 3000);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid dashboard port");
  // Keep the long-lived local server separate from CLI builds and test cleanup.
  if (process.env.NODE_ENV !== "production" && !process.env.NERYA_UI_DIST_DIR) {
    process.env.NERYA_UI_DIST_DIR = `.next-local-${port}`;
  }
  const hostname = process.env.NERYA_DASHBOARD_HOST || "127.0.0.1";
  // Skip Next's telemetry-gated npm version check during offline startup.
  process.env.NEXT_TELEMETRY_DISABLED ??= "1";
  const app = require("next")({ dev, dir, hostname, port });
  await app.prepare();
  const handle = app.getRequestHandler();
  const server = http.createServer((req, res) => { markPeer(req, key); handle(req, res); });
  // Next's custom-server request handler installs its own WebSocket dispatcher
  // on the first HTTP request. Only sanitize the peer header here. Calling the
  // inherited getUpgradeHandler() as well creates a separate Next server on
  // reconnect (Next 14). Never attach a second upgrade dispatcher.
  server.on("upgrade", (req) => { markPeer(req, key); });
  server.requestTimeout = 0; // Application proxy/Agent deadlines remain authoritative.
  server.headersTimeout = 60000;
  server.listen(port, hostname, () => console.log(`[nerya] dashboard ready at http://${hostname}:${port} (Node ${process.version})`));
  let stopping = false;
  const stop = () => { if (stopping) return; stopping = true; server.close(); Promise.resolve(app.close()).finally(() => process.exit(0)); };
  process.on("SIGTERM", stop); process.on("SIGINT", stop);
}
module.exports = { start, loopback, markPeer, PROOF };
if (require.main === module) start().catch((error) => { console.error(error); process.exitCode = 1; });

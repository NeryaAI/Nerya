"use strict";
const http = require("node:http");
const crypto = require("node:crypto");
const net = require("node:net");
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
  const key = crypto.randomBytes(32).toString("hex");
  process.env.NERYA_LOCAL_PEER_KEY = key;
  const port = Number(process.env.PORT || 3000);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid dashboard port");
  // Keep the long-lived local server separate from CLI builds and test cleanup.
  if (process.env.NODE_ENV !== "production" && !process.env.NERYA_UI_DIST_DIR) {
    process.env.NERYA_UI_DIST_DIR = `.next-local-${port}`;
  }
  const hostname = process.env.NERYA_DASHBOARD_HOST || "127.0.0.1";
  const app = require("next")({ dev: process.env.NODE_ENV !== "production", dir: process.cwd(), hostname, port });
  await app.prepare();
  const handle = app.getRequestHandler();
  const server = http.createServer((req, res) => { markPeer(req, key); handle(req, res); });
  const upgrade = app.getUpgradeHandler();
  server.on("upgrade", (req, socket, head) => { markPeer(req, key); upgrade(req, socket, head); });
  server.requestTimeout = 0; // Application proxy/Agent deadlines remain authoritative.
  server.headersTimeout = 60000;
  server.listen(port, hostname, () => console.log(`[nerya] dashboard ready at http://${hostname}:${port}`));
  let stopping = false;
  const stop = () => { if (stopping) return; stopping = true; server.close(); Promise.resolve(app.close()).finally(() => process.exit(0)); };
  process.on("SIGTERM", stop); process.on("SIGINT", stop);
}
module.exports = { loopback, markPeer, PROOF };
if (require.main === module) start().catch((error) => { console.error(error); process.exitCode = 1; });

import { NextRequest, NextResponse } from "next/server";
import http from "node:http";
import https from "node:https";

// the dashboard proxy must forward authentication
// material to the Nerya backend so per-actor route scoping
// (`nerya.api.auth`) works end-to-end. We:
//
//  * forward the user's `Authorization` and `X-Nerya-Token` headers as-is,
//  * fall back to a server-side token (`NERYA_API_TOKEN`) when the
//    client did not send one — useful when the dashboard is the trusted
//    client behind an SSO,
//  * preserve `cookie`, content-type, and request method nuances,
//  * strip hop-by-hop / dangerous headers (`host`, `connection`, ...),
//  * support all common HTTP verbs, not just GET/POST.
//
// The runtime' dashboard performs the same forwarding via its Next.js proxy;
// without this, the Nerya dashboard would silently lose auth context.

const BASE = process.env.NERYA_API || "http://127.0.0.1:18317";
const SERVER_TOKEN = process.env.NERYA_API_TOKEN || "";
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"]);
const DEFAULT_PROXY_TIMEOUT_MS = 120_000;
const LONG_PROXY_TIMEOUT_MS = 30 * 60 * 1000;
const SAFE_RETRY_METHODS = new Set(["GET", "HEAD"]);

const HOP_BY_HOP = new Set([
  "host",
  "connection",
  "content-length",
  "transfer-encoding",
  "keep-alive",
  "te",
  "upgrade",
  "proxy-connection",
  "proxy-authorization",
]);
const PROXY_CLIENT_HEADERS = new Set(["x-forwarded-for", "x-real-ip"]);
const RETRYABLE_PROXY_ERROR_CODES = new Set([
  "ECONNABORTED",
  "ECONNRESET",
  "ECONNREFUSED",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "EPIPE",
  "ETIMEDOUT",
  "PROXY_TIMEOUT",
  "UND_ERR_HEADERS_TIMEOUT",
]);

function buildForwardHeaders(req: NextRequest): Headers {
  const headers = new Headers();
  // Pass through everything except hop-by-hop and the rewriting host header.
  req.headers.forEach((value, key) => {
    const lower = key.toLowerCase();
    if (HOP_BY_HOP.has(lower)) return;
    if (PROXY_CLIENT_HEADERS.has(lower)) return;
    headers.set(key, value);
  });
  if (!headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  headers.set("accept-encoding", "identity");
  // Hint to the backend who originated the call (handy for audit logs).
  const reqWithIp = req as NextRequest & { ip?: string };
  const fwd = isLocalDashboardRequest(req)
    ? "127.0.0.1"
    : req.headers.get("x-forwarded-for") || req.headers.get("x-real-ip") || reqWithIp.ip || "";
  if (fwd) headers.set("X-Forwarded-For", fwd);
  headers.set("X-Forwarded-Proto", req.nextUrl.protocol.replace(":", ""));
  headers.set("X-Forwarded-Host", req.nextUrl.host);
  return headers;
}

function normaliseHost(raw: string): string {
  const host = (raw || "").trim().toLowerCase();
  if (!host) return "";
  if (host.startsWith("[") && host.includes("]")) return host.slice(1, host.indexOf("]"));
  if (host === "::1") return host;
  if (host.indexOf(":") === host.lastIndexOf(":")) return host.split(":")[0];
  return host.split(":")[0];
}

function isLocalDashboardRequest(req: NextRequest): boolean {
  const host = normaliseHost(req.headers.get("x-forwarded-host") || req.headers.get("host") || req.nextUrl.hostname);
  return !host || LOCAL_HOSTS.has(host) || host.startsWith("127.");
}

function isAnonymousProxyPath(joined: string): boolean {
  return joined === "health" || joined === "auth/status" || joined === "auth/login";
}

function isLongRunningProxyPath(joined: string): boolean {
  return joined.startsWith("agent/run_turn") || joined.startsWith("strategy/");
}

function proxyTimeoutMs(joined: string): number {
  return isLongRunningProxyPath(joined) ? LONG_PROXY_TIMEOUT_MS : DEFAULT_PROXY_TIMEOUT_MS;
}

function isSSEPath(joined: string): boolean {
  // Server-Sent Events endpoints. The proxy must pipe upstream chunks
  // straight to the client instead of buffering — otherwise the
  // dashboard's EventSource sees nothing until the upstream connection
  // closes (which for /gateway/events/stream is 30 minutes later).
  return joined === "gateway/events/stream";
}

type ProxyError = Error & { code?: string; cause?: unknown; attempts?: number };

type ProxyResponse = {
  status: number;
  headers: http.IncomingHttpHeaders;
  body: string;
};

function errorCode(err: unknown): string {
  const e = err as ProxyError | undefined;
  if (e?.code) return e.code;
  const c = e?.cause as { code?: string } | undefined;
  return c?.code || "";
}

function retryableProxyError(err: unknown): boolean {
  const code = errorCode(err);
  if (code && RETRYABLE_PROXY_ERROR_CODES.has(code)) return true;
  const message = err instanceof Error ? err.message : String(err);
  return message.includes("Headers Timeout") || message.includes("fetch failed");
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function headersToNode(headers: Headers, body: string | null): Record<string, string> {
  const out: Record<string, string> = {};
  headers.forEach((value, key) => {
    out[key] = value;
  });
  if (body !== null) {
    out["content-length"] = String(Buffer.byteLength(body));
  }
  return out;
}

function nodeForward(
  url: string,
  init: { method: string; headers: Headers; body: string | null; timeoutMs: number },
): Promise<ProxyResponse> {
  // Avoid global fetch here: Node/undici caps response headers at 5 minutes,
  // while Nerya agent turns can legitimately run longer before sending JSON.
  const target = new URL(url);
  const transport = target.protocol === "https:" ? https : http;
  return new Promise((resolve, reject) => {
    const req = transport.request(
      target,
      {
        method: init.method,
        headers: headersToNode(init.headers, init.body),
        timeout: init.timeoutMs,
      },
      (res) => {
        res.setEncoding("utf8");
        const chunks: string[] = [];
        res.on("data", (chunk: string) => {
          chunks.push(chunk);
        });
        res.on("end", () => {
          resolve({
            status: res.statusCode || 502,
            headers: res.headers,
            body: chunks.join(""),
          });
        });
      },
    );
    req.on("timeout", () => {
      const err = new Error(`proxy timeout after ${init.timeoutMs}ms`) as ProxyError;
      err.code = "PROXY_TIMEOUT";
      req.destroy(err);
    });
    req.on("error", reject);
    if (init.body !== null) {
      req.write(init.body);
    }
    req.end();
  });
}

async function nodeForwardWithRetry(
  url: string,
  init: { method: string; headers: Headers; body: string | null; timeoutMs: number },
): Promise<{ response: ProxyResponse; attempts: number }> {
  // Never blindly retry mutating POSTs like /agent/run_turn; the backend may
  // still be running and a retry would create a duplicate turn or side effect.
  const maxAttempts = SAFE_RETRY_METHODS.has(init.method) ? 3 : 1;
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return {
        response: await nodeForward(url, init),
        attempts: attempt,
      };
    } catch (err) {
      lastErr = err;
      if (attempt >= maxAttempts || !retryableProxyError(err)) {
        const e = err as ProxyError;
        if (e && typeof e === "object") e.attempts = attempt;
        throw e;
      }
      await sleep(200 * attempt);
    }
  }
  const e = lastErr as ProxyError;
  if (e && typeof e === "object") e.attempts = maxAttempts;
  throw lastErr;
}

function streamForward(
  url: string,
  init: { method: string; headers: Headers; timeoutMs: number },
): Promise<NextResponse> {
  // SSE pipe: keep the upstream connection open and push every chunk
  // into a ReadableStream the browser's EventSource consumes directly.
  const target = new URL(url);
  const transport = target.protocol === "https:" ? https : http;
  return new Promise((resolve, reject) => {
    const req = transport.request(
      target,
      {
        method: init.method,
        headers: headersToNode(init.headers, null),
      },
      (res) => {
        const status = res.statusCode || 502;
        const respHeaders = new Headers();
        Object.entries(res.headers).forEach(([key, value]) => {
          const lower = key.toLowerCase();
          if (HOP_BY_HOP.has(lower)) return;
          if (Array.isArray(value)) {
            value.forEach((v) => respHeaders.append(key, v));
          } else if (value !== undefined) {
            respHeaders.set(key, String(value));
          }
        });
        respHeaders.set("Cache-Control", "no-cache, no-transform");
        respHeaders.set("Connection", "keep-alive");
        respHeaders.set("X-Accel-Buffering", "no");
        if (!respHeaders.has("Content-Type")) {
          respHeaders.set("Content-Type", "text/event-stream");
        }
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            res.on("data", (chunk: Buffer) => {
              try {
                controller.enqueue(new Uint8Array(chunk));
              } catch {
                // controller already closed
              }
            });
            res.on("end", () => {
              try {
                controller.close();
              } catch {
                // already closed
              }
            });
            res.on("error", (err) => {
              try {
                controller.error(err);
              } catch {
                // already closed
              }
            });
          },
          cancel() {
            try {
              res.destroy();
            } catch {
              // best effort
            }
            try {
              req.destroy();
            } catch {
              // best effort
            }
          },
        });
        resolve(new NextResponse(stream, { status, headers: respHeaders }));
      },
    );
    // No idle timeout: SSE connections are by definition long-running.
    // The Python side caps each connection at 30 minutes via ``deadline``.
    req.setTimeout(0);
    req.on("error", reject);
    req.end();
  });
}

async function forward(req: NextRequest, path: string[], method: string) {
  const url = `${BASE}/${path.join("/")}${req.nextUrl.search || ""}`;
  const headers = buildForwardHeaders(req);
  const joined = path.join("/");
  if (
    !isLocalDashboardRequest(req) &&
    !isAnonymousProxyPath(joined) &&
    !headers.has("authorization") &&
    !headers.has("x-nerya-token")
  ) {
    return NextResponse.json(
      { error: "unauthorized", reason: "remote_dashboard_missing_token" },
      { status: 401 }
    );
  }
  // Inject the server-side token only after the remote dashboard password/JWT
  // check. This preserves trusted local/SSO proxy deployments without letting
  // public dashboard calls bypass the admin-login requirement.
  if (SERVER_TOKEN && !headers.has("authorization") && !headers.has("x-nerya-token")) {
    headers.set("Authorization", `Bearer ${SERVER_TOKEN}`);
  }
  if (method === "GET" && isSSEPath(joined)) {
    try {
      return await streamForward(url, { method, headers, timeoutMs: 0 });
    } catch (err) {
      const e = err as { message?: string; code?: string; stack?: string };
      return NextResponse.json(
        {
          error: "upstream_unreachable",
          target: url,
          detail: [e?.code, e?.message ?? String(err)].filter(Boolean).join(" | "),
          trace: e?.stack || undefined,
        },
        { status: 502 },
      );
    }
  }
  let body: string | null = null;
  if (method !== "GET" && method !== "HEAD") {
    const text = await req.text();
    body = text && text.length > 0 ? text : "";
  }
  let upstream: ProxyResponse;
  let attempts = 1;
  try {
    const result = await nodeForwardWithRetry(url, {
      method,
      headers,
      body,
      timeoutMs: proxyTimeoutMs(joined),
    });
    upstream = result.response;
    attempts = result.attempts;
  } catch (err) {
    // Bubble up *everything* the runtime gave us so the chat error card
    // can show the real cause (ECONNREFUSED, ETIMEDOUT, EAI_AGAIN, ...)
    // instead of the opaque ``upstream_unreachable`` label.
    //
    // ``cause`` on Node fetch errors typically holds the underlying
    // ``Error: connect ECONNREFUSED 127.0.0.1:18317`` — exactly what we
    // need to tell the operator the backend died vs. the network is
    // down. ``stack`` lets us debug timing issues without tailing the
    // dashboard process.
    const e = err as { message?: string; code?: string; cause?: unknown; stack?: string };
    const failedAttempts = typeof (e as ProxyError).attempts === "number"
      ? (e as ProxyError).attempts
      : attempts;
    const causeMsg = (() => {
      const c = e?.cause;
      if (!c) return "";
      if (typeof c === "string") return c;
      if (typeof c === "object" && c !== null) {
        const co = c as { message?: string; code?: string };
        return [co.code, co.message].filter(Boolean).join(": ");
      }
      return String(c);
    })();
    const detailParts = [
      e?.code,
      e?.message ?? String(err),
      causeMsg,
    ].filter(Boolean);
    return NextResponse.json(
      {
        error: "upstream_unreachable",
        target: url,
        attempts: failedAttempts,
        retryable: SAFE_RETRY_METHODS.has(method),
        detail: detailParts.join(" | "),
        trace: e?.stack || undefined,
      },
      { status: 502 }
    );
  }
  const respHeaders = new Headers();
  Object.entries(upstream.headers).forEach(([key, value]) => {
    const lower = key.toLowerCase();
    if (HOP_BY_HOP.has(lower)) return;
    if (Array.isArray(value)) {
      value.forEach((v) => respHeaders.append(key, v));
    } else if (value !== undefined) {
      respHeaders.set(key, String(value));
    }
  });
  if (!respHeaders.has("content-type")) {
    respHeaders.set("content-type", "application/json");
  }
  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: respHeaders,
  });
}

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
// Next.js route.ts caps each request at 10s by default in serverless-style
// runtimes. The Nerya dashboard runs as a long-lived dev / standalone
// server so this cap really only matters for hosted deployments — bump
// to 30 min so platforms like Vercel don't synthesise their own 502.
export const maxDuration = 1800;

export async function GET(req: NextRequest, ctx: { params: { path: string[] } }) {
  return forward(req, ctx.params.path, "GET");
}
export async function POST(req: NextRequest, ctx: { params: { path: string[] } }) {
  return forward(req, ctx.params.path, "POST");
}
export async function PUT(req: NextRequest, ctx: { params: { path: string[] } }) {
  return forward(req, ctx.params.path, "PUT");
}
export async function PATCH(req: NextRequest, ctx: { params: { path: string[] } }) {
  return forward(req, ctx.params.path, "PATCH");
}
export async function DELETE(req: NextRequest, ctx: { params: { path: string[] } }) {
  return forward(req, ctx.params.path, "DELETE");
}
export async function HEAD(req: NextRequest, ctx: { params: { path: string[] } }) {
  return forward(req, ctx.params.path, "HEAD");
}

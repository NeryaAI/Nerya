import { NextRequest, NextResponse } from "next/server";

// Plan 11 P2 — the dashboard proxy must forward authentication
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
// Hermes' dashboard performs the same forwarding via its Next.js proxy;
// without this, the Nerya dashboard would silently lose auth context.

const BASE = process.env.NERYA_API || "http://127.0.0.1:8787";
const SERVER_TOKEN = process.env.NERYA_API_TOKEN || "";

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

function buildForwardHeaders(req: NextRequest): Headers {
  const headers = new Headers();
  // Pass through everything except hop-by-hop and the rewriting host header.
  req.headers.forEach((value, key) => {
    const lower = key.toLowerCase();
    if (HOP_BY_HOP.has(lower)) return;
    headers.set(key, value);
  });
  if (!headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  // Inject the server-side token only when the user did not provide one.
  if (SERVER_TOKEN && !headers.has("authorization") && !headers.has("x-nerya-token")) {
    headers.set("Authorization", `Bearer ${SERVER_TOKEN}`);
  }
  // Hint to the backend who originated the call (handy for audit logs).
  const fwd = req.headers.get("x-forwarded-for");
  if (fwd) headers.set("X-Forwarded-For", fwd);
  headers.set("X-Forwarded-Proto", req.nextUrl.protocol.replace(":", ""));
  headers.set("X-Forwarded-Host", req.nextUrl.host);
  return headers;
}

// undici (Node fetch) defaults to a 5-minute headersTimeout / bodyTimeout
// which kills long-running ``/agent/run_turn`` POSTs that take >5min for
// real coding tasks (e.g. authoring a new connector + strategy proposal).
// We extend it to 30 minutes so the dashboard proxy doesn't synthesise an
// ``upstream_unreachable`` while the backend is still happily working.
//
// Imported lazily because undici is a transitive dep of next/server and
// not always exposed at the top level — falling back gracefully when the
// dispatcher API isn't available keeps the proxy working in environments
// (e.g. edge runtime) where the dispatcher knob doesn't exist.
let _longTimeoutDispatcher: unknown = null;
async function longTimeoutDispatcher(): Promise<unknown> {
  if (_longTimeoutDispatcher !== null) return _longTimeoutDispatcher;
  try {
    // Use eval-based require to bypass webpack's static analysis — undici is
    // a transitive dep of Next.js itself and is always present at runtime,
    // but bundling it in App Router routes triggers
    // ``Module not found: Can't resolve 'undici'`` because Next 14 doesn't
    // auto-externalise transitive deps for route handlers.
    const req = eval("require") as (mod: string) => unknown;
    const mod = req("undici") as { Agent?: new (opts: object) => unknown };
    const Agent = mod?.Agent;
    if (!Agent) throw new Error("undici.Agent not available");
    _longTimeoutDispatcher = new Agent({
      headersTimeout: 30 * 60 * 1000, // 30 minutes
      bodyTimeout: 30 * 60 * 1000,    // 30 minutes
      keepAliveTimeout: 60_000,
      keepAliveMaxTimeout: 600_000,
    });
  } catch {
    _longTimeoutDispatcher = false;  // sentinel — don't retry on failure
  }
  return _longTimeoutDispatcher;
}

async function forward(req: NextRequest, path: string[], method: string) {
  const url = `${BASE}/${path.join("/")}${req.nextUrl.search || ""}`;
  const headers = buildForwardHeaders(req);
  const init: RequestInit & { dispatcher?: unknown; duplex?: string } = {
    method,
    headers,
  };
  // Apply the long-timeout dispatcher only for the chat / agent routes
  // that legitimately block for minutes; quick read paths keep the
  // default so a hung backend on /workspace doesn't waste threads.
  const joined = path.join("/");
  if (
    joined.startsWith("agent/run_turn") ||
    joined.startsWith("strategy/")
  ) {
    const d = await longTimeoutDispatcher();
    if (d && d !== false) init.dispatcher = d;
  }
  if (method !== "GET" && method !== "HEAD") {
    const body = await req.text();
    if (body && body.length > 0) {
      init.body = body;
    }
  }
  let res: Response;
  try {
    res = await fetch(url, init);
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
        detail: detailParts.join(" | "),
        trace: e?.stack || undefined,
      },
      { status: 502 }
    );
  }
  const respHeaders = new Headers();
  res.headers.forEach((value, key) => {
    const lower = key.toLowerCase();
    if (HOP_BY_HOP.has(lower)) return;
    respHeaders.set(key, value);
  });
  if (!respHeaders.has("content-type")) {
    respHeaders.set("content-type", "application/json");
  }
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: respHeaders,
  });
}

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

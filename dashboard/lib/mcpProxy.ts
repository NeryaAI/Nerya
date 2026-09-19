/** Public MCP/OAuth proxy. Never reuse the dashboard's privileged REST proxy. */
const paths = new Set([
  "/mcp", "/mcp/oauth/authorize", "/mcp/oauth/token", "/mcp/oauth/register",
  "/mcp/oauth/revoke", "/mcp/oauth/login", "/.well-known/oauth-authorization-server",
  "/.well-known/oauth-authorization-server/mcp/oauth", "/.well-known/oauth-protected-resource/mcp",
]);

async function readBody(request: Request): Promise<Uint8Array | undefined> {
  if (request.method === "GET" || request.method === "HEAD" || !request.body) return undefined;
  const reader = request.body.getReader();
  const parts: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 1048576) { await reader.cancel(); throw new RangeError("request_too_large"); }
    parts.push(value);
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) { body.set(part, offset); offset += part.byteLength; }
  return body;
}

export async function proxyMcp(request: Request): Promise<Response> {
  const url = new URL(request.url);
  if (!paths.has(url.pathname)) return Response.json({ error: "not_found" }, { status: 404 });
  const api = (process.env.NERYA_API || "http://127.0.0.1:18317").replace(/\/$/, "");
  const headers = new Headers();
  // OAuth cookies are needed for consent, but dashboard JWTs are never synthesized here.
  for (const name of ["authorization", "cookie", "content-type", "accept", "origin", "mcp-protocol-version", "mcp-session-id", "last-event-id"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("host", request.headers.get("host") || url.host);
  headers.set("accept-encoding", "identity");
  const abort = new AbortController();
  const cancel = () => abort.abort();
  request.signal.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(cancel, 1800000);
  try {
    const body = await readBody(request);
    if (body) headers.set("content-length", String(body.byteLength));
    const response = await fetch(api + url.pathname + url.search, {
      method: request.method, headers, body: body as BodyInit | undefined,
      redirect: "manual", cache: "no-store", signal: abort.signal,
    });
    const forwarded = new Headers();
    for (const [key, value] of response.headers) {
      if (!["connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "set-cookie"].includes(key.toLowerCase())) forwarded.set(key, value);
    }
    const cookies = (response.headers as Headers & { getSetCookie?: () => string[] }).getSetCookie?.()
      ?? (response.headers.get("set-cookie") ? [response.headers.get("set-cookie")!] : []);
    for (const cookie of cookies) forwarded.append("set-cookie", cookie);
    forwarded.set("cache-control", "no-store");
    // MCP is configured as stateless JSON; buffering also keeps the timeout attached to the body.
    const result = request.method === "HEAD" || [204, 304].includes(response.status) ? null : await response.arrayBuffer();
    return new Response(result, { status: response.status, headers: forwarded });
  } catch (error) {
    return Response.json({ error: error instanceof RangeError ? "request_too_large" : "mcp_upstream_unavailable" },
      { status: error instanceof RangeError ? 413 : 502 });
  } finally {
    clearTimeout(timer);
    request.signal.removeEventListener("abort", cancel);
  }
}

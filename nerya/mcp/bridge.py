"""Mount the optional ASGI MCP app on the existing stdlib API HTTP listener.

No second public listener, no injected dashboard credential. The ASGI lifespan
lives on one event loop; stdlib request threads submit work to that loop.
The MCP app uses stateless JSON responses, not persistent GET/SSE streams.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from urllib.parse import urlsplit

from ..core.config import load_config
from .public import PUBLIC_PATHS, public_origin

_RUNNERS = {}
_LOCK = threading.RLock()
MAX_BODY = 1_048_576


class AppRunner:
    def __init__(self, config, port):
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.error = None
        self.config, self.port = config, port
        self.thread = threading.Thread(target=self._run, daemon=True, name="nerya-mcp-asgi")
        self.thread.start()
        if not self.ready.wait(20) or self.error:
            self.close()
            raise RuntimeError("MCP initialization failed; install the MCP extra and check administrator settings")

    def _run(self):
        async def lifetime():
            from .server import create_http_app, create_server
            from .settings import server_settings
            from .tools import NeryaTools
            from ..sdk.internal_client import InternalClient
            self.stop = asyncio.Event()
            settings = server_settings(self.config, transport="http", host="127.0.0.1", port=self.port)
            self.app = create_http_app(create_server(NeryaTools(InternalClient.from_config(self.config))),
                                       settings, config=self.config)
            async with self.app.lifespan_app.router.lifespan_context(self.app.lifespan_app):
                self.ready.set()
                await self.stop.wait()
        try:
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(lifetime())
        except Exception as exc:
            self.error = type(exc).__name__
            self.ready.set()
        finally:
            self.loop.close()

    def close(self):
        if not self.loop.is_closed() and hasattr(self, "stop"):
            self.loop.call_soon_threadsafe(self.stop.set)
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    async def request(self, scope, body):
        messages = []
        received = False
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()
        async def send(message):
            messages.append(message)
        await self.app(scope, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        return start["status"], start.get("headers", []), b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body")

    def dispatch(self, scope, body):
        future = asyncio.run_coroutine_threadsafe(self.request(scope, body), self.loop)
        try:
            return future.result(timeout=1800)
        except TimeoutError:
            # No retry: a disconnected/expired write may still have completed.
            future.cancel()
            raise


def close_workspace(root):
    with _LOCK:
        old = _RUNNERS.pop(str(root.resolve()), None)
    if old:
        old[1].close()


def _runner(config, port):
    cfg = load_config(config.paths.root) if config.paths.config.exists() else config
    if cfg.get("mcp.enabled") is not True:
        return None
    if cfg.get("mcp.auth_mode", "oauth2") != "oauth2":
        raise ValueError("The integrated public MCP endpoint requires OAuth2")
    # Runtime public URLs are derived from trusted tunnel state, never request Host.
    cfg.data["mcp"]["public_url"] = public_origin(cfg)
    signature = hashlib.sha256(json.dumps([cfg.get("mcp"), cfg.get("runtime.auth.admin_password_hash")],
                                          sort_keys=True).encode()).hexdigest()
    key = str(cfg.paths.root.resolve())
    with _LOCK:
        old = _RUNNERS.get(key)
        if old and old[0] == signature and old[1].thread.is_alive():
            return old[1]
        if old:
            old[1].close()
        runner = AppRunner(cfg, port)
        _RUNNERS[key] = (signature, runner)
        return runner


def _read_body(handler):
    lengths = handler.headers.get_all("Content-Length", [])
    transfer = handler.headers.get("Transfer-Encoding", "").lower()
    if len(lengths) > 1 or (lengths and transfer):
        raise ValueError("Ambiguous request framing")
    if not transfer:
        length = int(lengths[0]) if lengths else 0
        if not 0 <= length <= MAX_BODY:
            raise ValueError("Request exceeds 1 MiB")
        body = handler.rfile.read(length)
        if len(body) != length:
            raise ValueError("Incomplete request")
        return body
    if transfer != "chunked":
        raise ValueError("Unsupported transfer encoding")
    body = bytearray()
    while True:
        line = handler.rfile.readline(4096)
        if not line.endswith(b"\r\n"):
            raise ValueError("Invalid chunk framing")
        length = int(line.split(b";", 1)[0], 16)
        if length < 0 or len(body) + length > MAX_BODY:
            raise ValueError("Request exceeds 1 MiB")
        if not length:
            # No request trailers are accepted on this endpoint.
            if handler.rfile.readline(4096) != b"\r\n":
                raise ValueError("Request trailers are not supported")
            return bytes(body)
        chunk = handler.rfile.read(length)
        if len(chunk) != length or handler.rfile.read(2) != b"\r\n":
            raise ValueError("Invalid chunk framing")
        body.extend(chunk)


def handle_http(handler, config) -> bool:
    parsed = urlsplit(handler.path)
    if parsed.path not in PUBLIC_PATHS:
        return False
    headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
    try:
        if config.get("mcp.enabled") is not True and not config.paths.config.exists():
            runner = None
        else:
            runner = _runner(config, handler.server.server_address[1])
        if runner is None:
            status, body = 404, b'{"error":"mcp_disabled"}'
        else:
            body_in = _read_body(handler)
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": handler.command, "scheme": "http", "root_path": "", "path": parsed.path,
                     "raw_path": parsed.path.encode(), "query_string": parsed.query.encode(),
                     "headers": [(k.lower().encode(), v.encode("latin-1")) for k, v in handler.headers.items()
                                 if k.lower() not in {"transfer-encoding", "connection"}],
                     "client": handler.client_address, "server": handler.server.server_address}
            status, headers, body = runner.dispatch(scope, body_in)
    except ValueError:
        status, body = 400, b'{"error":"invalid_mcp_request_or_configuration"}'
    except Exception:
        status, body = 503, b'{"error":"mcp_unavailable","detail":"Check MCP dependency and administrator settings"}'
    handler.send_response(status)
    for key, value in headers:
        if key.lower() not in {b"content-length", b"transfer-encoding", b"connection"}:
            handler.send_header(key.decode("latin-1"), value.decode("latin-1"))
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)
    handler.close_connection = True
    return True

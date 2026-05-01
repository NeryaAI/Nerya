"""Local stdlib HTTP server for Nerya.

This is intentionally small: it's a convenience surface for the dashboard
and CI. Production deployments should front it with a real framework.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ..core.config import Config
from ..sdk import InternalClient
from . import auth as auth_mod
from . import routes_agent, routes_approvals, routes_capability, routes_dev, routes_discovery, routes_evolution
from . import routes_exchanges, routes_health, routes_llm, routes_market
from . import routes_memory, routes_messages, routes_portfolio, routes_provider_auth, routes_scripts, routes_security
from . import routes_skills, routes_strategies_runtime, routes_strategy
from . import routes_strategy_history, routes_trading
from . import routes_teams
from . import routes_triggers, routes_wallet, routes_workspace
from . import routes_gateway
from . import routes_operator, routes_inbox, routes_agent_tasks, routes_accounts
from . import routes_account_intake
from . import routes_control_plane


Route = tuple[str, str, Callable[[InternalClient, dict[str, Any]], dict[str, Any]]]

_ROUTES: list[Route] = []


def _register(method: str, path: str, handler):
    _ROUTES.append((method.upper(), path, handler))


def _collect_routes() -> None:
    if _ROUTES:
        return
    for mod in (routes_health, routes_workspace, routes_agent,
                routes_skills, routes_triggers, routes_trading,
                routes_llm, routes_memory, routes_strategy_history, routes_scripts,
                routes_messages, routes_evolution, routes_security,
                routes_market, routes_portfolio, routes_wallet,
                routes_exchanges, routes_discovery, routes_dev,
                routes_capability, routes_approvals,
                routes_provider_auth, routes_gateway, routes_teams,
                routes_strategies_runtime, routes_strategy,
                routes_operator, routes_inbox, routes_agent_tasks,
                routes_control_plane, routes_accounts,
                routes_account_intake):
        for method, path, handler in mod.routes():
            _register(method, path, handler)


def _match(method: str, path: str):
    for m, p, h in _ROUTES:
        if m == method and p == path:
            return h
    return None


def build_server(config: Config, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    _collect_routes()
    client = InternalClient.boot(config.paths.root)
    routes_gateway.launch_configured_gateways_on_start(client)

    class Handler(BaseHTTPRequestHandler):
        def _cors(self) -> None:
            # The dashboard normally goes through its own /api/proxy so this
            # is only useful for local dev tools / curl.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, X-Nerya-Token",
            )

        def _write(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length).decode("utf-8") or "{}"
            try:
                return json.loads(raw)
            except Exception:
                return {"_raw": raw}

        def _collect_headers(self) -> dict[str, str]:
            return {k.lower(): v for k, v in self.headers.items()}

        def _check_auth(self, method: str, path: str):
            client_addr = (
                self.client_address[0]
                if isinstance(self.client_address, tuple)
                else str(self.client_address)
            )
            result = auth_mod.check_request(
                config,
                method=method,
                path=path,
                client_addr=client_addr,
                headers=self._collect_headers(),
            )
            if result.ok:
                result = auth_mod.authorize_route(
                    config,
                    result,
                    method=method,
                    path=path,
                    client_addr=client_addr,
                )
            return result

        def do_GET(self):  # noqa: N802
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(self.path)
            auth = self._check_auth("GET", parsed.path)
            if not auth.ok:
                self._write(auth.status, {
                    "error": "unauthorized",
                    "reason": auth.reason,
                })
                return
            handler = _match("GET", parsed.path)
            if not handler:
                self._write(404, {"error": "not_found", "path": self.path})
                return
            query = {k: v[0] if len(v) == 1 else v
                     for k, v in parse_qs(parsed.query).items()}
            try:
                result = handler(client, query)
                self._write(200, result if result is not None else {})
            except Exception as exc:  # pragma: no cover
                self._write(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_POST(self):  # noqa: N802
            path_only = self.path.split("?")[0]
            auth = self._check_auth("POST", path_only)
            if not auth.ok:
                self._write(auth.status, {
                    "error": "unauthorized",
                    "reason": auth.reason,
                })
                return
            handler = _match("POST", path_only)
            if not handler:
                self._write(404, {"error": "not_found", "path": self.path})
                return
            try:
                self._write(200, handler(client, self._read_body()))
            except Exception as exc:  # pragma: no cover
                import traceback
                tb = traceback.format_exc()
                self._write(500, {
                    "error": f"{type(exc).__name__}: {exc}",
                    "trace": tb.splitlines()[-12:],
                })

        def log_message(self, fmt, *args):  # silence default logging
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve(config: Config, host: str = "127.0.0.1", port: int = 8787) -> None:
    srv = build_server(config, host=host, port=port)
    print(f"[nerya] local api on http://{host}:{port}")
    srv.serve_forever()

"""Local stdlib HTTP server for Nerya.

This is intentionally small: it's a convenience surface for the dashboard
and CI. Production deployments should front it with a real framework.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ..core.config import Config
from ..sdk import InternalClient
from ..skills.kernel import SkillKernel
from . import auth as auth_mod
from . import routes_agent, routes_approvals, routes_auth, routes_capability, routes_dev, routes_discovery, routes_evolution
from . import routes_exchanges, routes_health, routes_llm, routes_market
from . import routes_browsers, routes_browsers_session, routes_data_sources, routes_memory, routes_messages, routes_network, routes_oauth, routes_portfolio, routes_provider_auth, routes_scripts, routes_search, routes_security
from . import routes_skills, routes_strategies_runtime, routes_strategy
from . import routes_strategy_history, routes_trading
from . import routes_teams
from . import routes_triggers, routes_wallet, routes_workspace
from . import routes_gateway
from . import routes_operator, routes_inbox, routes_agent_tasks, routes_accounts
from . import routes_account_intake
from . import routes_control_plane
from . import routes_charts
# Runtime capability catalog, data-source sync, trading evidence vault,
# and E2E verification artifacts.
from . import routes_capabilities
from . import routes_data_source_sync
from . import routes_evidence
from . import routes_e2e_artifacts
# Runtime feature flags.
from . import routes_runtime_flags
# Durable raw tool-result store.
from . import routes_tool_raw


Route = tuple[str, str, Callable[[InternalClient, dict[str, Any]], dict[str, Any]]]

_ROUTES: list[Route] = []
_CRON_THREADS: dict[str, threading.Thread] = {}
_ACCOUNT_REFRESH_THREADS: dict[str, threading.Thread] = {}
_LIVE_ORDER_POLL_THREADS: dict[str, threading.Thread] = {}
_SHARED_SKILLS: dict[str, SkillKernel] = {}
_SHARED_SKILLS_LOCK = threading.RLock()
_THREAD_CLIENTS = threading.local()
log = logging.getLogger(__name__)


class StreamingResponse:
    """Marker that lets a route handler stream chunks instead of returning JSON.

    The dispatcher (:meth:`do_GET`/:meth:`do_POST`) detects this object,
    writes the headers, and then iterates ``generator``, flushing each
    chunk to the client. Used for Server-Sent Events (``/gateway/events/stream``)
    so the dashboard can subscribe via ``EventSource`` and receive every
    inbound/outbound/phase tick the moment it lands in the gateway ring
    buffer — no polling.

    The generator should yield either ``bytes`` or ``str``. Empty values
    are skipped. The dispatcher catches connection errors and silently
    aborts so a disconnected client does not log noise.
    """

    def __init__(
        self,
        *,
        generator,
        content_type: str = "text/event-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.generator = generator
        self.content_type = content_type
        self.extra_headers = dict(headers or {})

    def run(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", self.content_type)
            handler.send_header("Cache-Control", "no-cache, no-transform")
            handler.send_header("Connection", "keep-alive")
            # Disable nginx/Vercel proxy buffering for true streaming.
            handler.send_header("X-Accel-Buffering", "no")
            for key, value in self.extra_headers.items():
                handler.send_header(key, value)
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, X-Nerya-Token",
            )
            handler.end_headers()
            try:
                handler.wfile.flush()
            except Exception:  # pragma: no cover - best-effort
                pass
            for chunk in self.generator:
                if chunk is None or chunk == "":
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                handler.wfile.write(chunk)
                try:
                    handler.wfile.flush()
                except Exception:  # pragma: no cover - best-effort
                    pass
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Client closed the EventSource — that's the normal exit path.
            return
        except Exception:  # pragma: no cover - background guard
            log.exception("streaming response generator failed")
            return


def _register(method: str, path: str, handler):
    _ROUTES.append((method.upper(), path, handler))


def _collect_routes(config: Config | None = None) -> None:
    if _ROUTES:
        return
    base_modules = (routes_health, routes_auth, routes_workspace, routes_agent,
                routes_skills, routes_triggers, routes_trading,
                routes_llm, routes_memory, routes_strategy_history, routes_scripts,
                routes_search, routes_browsers, routes_browsers_session, routes_data_sources,
                routes_messages, routes_evolution, routes_security, routes_network,
                routes_market, routes_portfolio, routes_wallet,
                routes_exchanges, routes_discovery, routes_dev,
                routes_capability, routes_approvals,
                routes_provider_auth, routes_oauth, routes_gateway, routes_teams,
                routes_strategies_runtime, routes_strategy,
                routes_operator, routes_inbox, routes_agent_tasks,
                routes_control_plane, routes_accounts,
                routes_account_intake, routes_charts,
                            # Runtime capability and evidence surfaces
                            routes_capabilities, routes_data_source_sync,
                            routes_evidence, routes_e2e_artifacts,
                            # Runtime feature flags
                            routes_runtime_flags,
                            # Raw tool-result store
                            routes_tool_raw)
    for mod in base_modules:
        for method, path, handler in mod.routes():
            _register(method, path, handler)


def _path_params(pattern: str, path: str) -> dict[str, str] | None:
    if pattern == path:
        return {}
    pattern_parts = [part for part in pattern.strip("/").split("/") if part]
    path_parts = [part for part in path.strip("/").split("/") if part]
    if len(pattern_parts) != len(path_parts):
        return None
    params: dict[str, str] = {}
    for expected, actual in zip(pattern_parts, path_parts):
        if expected.startswith("{") and expected.endswith("}") and len(expected) > 2:
            params[expected[1:-1]] = actual
            continue
        if expected != actual:
            return None
    return params


def _match(method: str, path: str):
    for m, p, h in _ROUTES:
        if m != method:
            continue
        params = _path_params(p, path)
        if params is not None:
            return h, params
    return None, {}


def _status_body_from_result(result: Any) -> tuple[int, dict[str, Any]]:
    status = 200
    body = result if result is not None else {}
    if isinstance(body, dict) and isinstance(body.get("_status"), int):
        status = int(body["_status"])
        body = {k: v for k, v in body.items() if k != "_status"}
    return status, body


def _start_cron_scheduler(client: InternalClient) -> None:
    if os.environ.get("NERYA_DISABLE_CRON", "").strip().lower() in {"1", "true", "yes"}:
        return
    key = str(client.config.paths.root.resolve())
    thread = _CRON_THREADS.get(key)
    if thread is not None and thread.is_alive():
        return

    from ..triggers.cron import CronScheduler

    scheduler = CronScheduler(client.config, client.triggers_runtime)
    thread = threading.Thread(
        target=scheduler.run_forever,
        kwargs={"poll_s": 1.0},
        name=f"nerya-cron-{client.config.paths.root.name}",
        daemon=True,
    )
    thread.start()
    _CRON_THREADS[key] = thread


def _start_account_refresh_loop(client: InternalClient) -> None:
    if os.environ.get("NERYA_DISABLE_ACCOUNT_REFRESH", "").strip().lower() in {"1", "true", "yes"}:
        return
    key = str(client.config.paths.root.resolve())
    thread = _ACCOUNT_REFRESH_THREADS.get(key)
    if thread is not None and thread.is_alive():
        return

    def _run() -> None:
        from ..trading.account_refresh import (
            live_refresh_interval_seconds,
            refresh_account_marks,
        )

        # The fast loop tick is governed by the live cadence (default
        # 60s). ``refresh_account_marks(only_due=True)`` skips paper
        # accounts whose snapshots are still inside their longer
        # window, so we don't pay the projection cost on every tick.
        while True:
            tick = live_refresh_interval_seconds(client.config)
            try:
                refresh_account_marks(
                    client.config,
                    run_executors=True,
                    only_due=True,
                )
            except Exception:  # pragma: no cover - background loop guard
                log.exception("account refresh loop failed")
            time.sleep(max(5.0, tick))

    thread = threading.Thread(
        target=_run,
        name=f"nerya-account-refresh-{client.config.paths.root.name}",
        daemon=True,
    )
    thread.start()
    _ACCOUNT_REFRESH_THREADS[key] = thread


def _start_live_order_poller(client: InternalClient) -> None:
    """Background loop that polls non-terminal live orders.

    Drives the same ``OrderTracker.active_orders`` slice the executor
    pipeline updates, calls ``connector.get_order`` for each, applies
    any new fills to the :class:`PositionBook`, and promotes the
    tracker through terminal states. The loop is restart-safe: when
    the process boots, ``active_orders`` is the union of orders that
    were submitted by any prior process and never terminated, so the
    poller naturally picks them up.

    The cadence is governed by ``trading.live_order_poll_interval_s``
    (default 5s) — short enough to feel real-time on the dashboard but
    long enough to leave plenty of headroom under venue rate limits.
    """

    if os.environ.get("NERYA_DISABLE_ORDER_POLLER", "").strip().lower() in {"1", "true", "yes"}:
        return
    key = str(client.config.paths.root.resolve())
    thread = _LIVE_ORDER_POLL_THREADS.get(key)
    if thread is not None and thread.is_alive():
        return

    def _run() -> None:
        from ..trading.order_polling import poll_active_live_orders

        while True:
            tick = float(
                client.config.get("trading.live_order_poll_interval_s", 5.0)
            )
            try:
                poll_active_live_orders(client.config)
            except Exception:  # pragma: no cover - background loop guard
                log.exception("live order poller failed")
            time.sleep(max(1.0, tick))

    thread = threading.Thread(
        target=_run,
        name=f"nerya-live-order-poller-{client.config.paths.root.name}",
        daemon=True,
    )
    thread.start()
    _LIVE_ORDER_POLL_THREADS[key] = thread


def _client_for_current_thread(config: Config) -> InternalClient:
    """Return a client whose lazy DB handles belong to this request thread."""
    key = str(config.paths.root.resolve())
    cache = getattr(_THREAD_CLIENTS, "clients", None)
    if not isinstance(cache, dict):
        cache = {}
        _THREAD_CLIENTS.clients = cache
    client = cache.get(key)
    if client is None:
        client = InternalClient.from_config(
            config,
            skills=_shared_skills_for_config(config),
        )
        cache[key] = client
    return client


def _shared_skills_for_config(config: Config) -> SkillKernel:
    """Reuse the expensive skill registry across short-lived request threads.

    ``ThreadingHTTPServer`` creates fresh request threads under browser
    fan-out. Keeping the whole InternalClient thread-local protects lazy
    SQLite handles in TriggerRouter, but booting SkillKernel on every thread
    made dashboard page loads pay the builtin/workspace skill scan many times.
    """

    key = str(config.paths.root.resolve())
    with _SHARED_SKILLS_LOCK:
        skills = _SHARED_SKILLS.get(key)
        if skills is None:
            skills = SkillKernel.boot(config)
            _SHARED_SKILLS[key] = skills
        return skills


def build_server(
    config: Config,
    host: str = "127.0.0.1",
    port: int = 18317,
    *,
    start_cron: bool = True,
) -> ThreadingHTTPServer:
    _collect_routes(config)
    startup_client = InternalClient.from_config(config)
    routes_gateway.launch_configured_gateways_on_start(startup_client)
    routes_network.launch_configured_tunnels_on_start(startup_client)
    # Install built-in data-source sync contributors so ``/data-sources/sync-now``
    # actually refreshes the ledger (notebook, model catalog, gateway registry,
    # paper account, public market clients). See
    # ``nerya.data_sources.sync_contributors`` for the canonical list.
    try:
        from ..data_sources import sync_contributors as _sync_contributors
        _sync_contributors.install_default_contributors()
        _sync_contributors.seed_additional_rows(startup_client)
    except Exception:  # pragma: no cover - defensive
        log.exception("failed to install data-source sync contributors")
    if start_cron:
        _start_cron_scheduler(startup_client)
        _start_account_refresh_loop(startup_client)
        _start_live_order_poller(startup_client)

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
            handler, path_params = _match("GET", parsed.path)
            if not handler:
                self._write(404, {"error": "not_found", "path": self.path})
                return
            query = {k: v[0] if len(v) == 1 else v
                     for k, v in parse_qs(parsed.query).items()}
            query.update(path_params)
            try:
                result = handler(_client_for_current_thread(config), query)
                if isinstance(result, StreamingResponse):
                    result.run(self)
                else:
                    status, body = _status_body_from_result(result)
                    self._write(status, body)
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
            handler, path_params = _match("POST", path_only)
            if not handler:
                self._write(404, {"error": "not_found", "path": self.path})
                return
            try:
                payload = self._read_body()
                if path_params:
                    payload = {**path_params, **payload}
                result = handler(
                    _client_for_current_thread(config),
                    payload,
                )
                status, body = _status_body_from_result(result)
                self._write(status, body)
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


def serve(config: Config, host: str = "127.0.0.1", port: int = 18317) -> None:
    srv = build_server(config, host=host, port=port)
    print(f"[nerya] local api on http://{host}:{port}")
    srv.serve_forever()

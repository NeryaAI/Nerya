"""Browser session routes.

Lightweight, in-process session manager that lets the dashboard drive
the configured headless browser engine (Lightpanda / CloakBrowser /
Obscura) one URL at a time. Each session keeps:

* the engine that handled the request (snapshot of ``selected`` at
  start),
* the *current* URL (last successful navigation),
* a short navigation history,
* the rendered markdown / HTML payload from the most recent fetch.

State is kept in-memory only — restarting the API server discards open
sessions. This is intentional: the underlying ``browser_engines.fetch``
is stateless (a one-shot CLI invocation) so there is nothing to clean
up on shutdown.

Routes
------
``POST /browsers/session/start``
    body: ``{url, engine?, session_id?, timeout_s?}``
    → opens a new session (auto-id when omitted), fetches the URL,
      returns the session record.
``POST /browsers/session/navigate``
    body: ``{session_id, url, timeout_s?}``
    → fetches another URL within an existing session, appends to history.
``POST /browsers/session/snapshot``
    body: ``{session_id}``
    → re-fetches the current URL, useful for "reload".
``POST /browsers/session/close``
    body: ``{session_id}``
    → drops the session.
``GET  /browsers/session/list``
    → list all open sessions (light view).
``GET  /browsers/session/get``
    → ``?session_id=`` returns the full record (with the rendered body).

The sole dependency is :mod:`nerya.integrations.browser_engines`. When
no engine is selected (or the requested engine is not installed) the
endpoint returns a structured error so the dashboard can surface a
"please install lightpanda first" affordance.
"""

from __future__ import annotations

import base64
import io
import json
import queue
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..integrations import browser_engines as _be
from ..core.proxy import browser_proxy_config_for_workspace


_LOCK = threading.RLock()
_SESSIONS: dict[str, dict[str, Any]] = {}
_HISTORY_LIMIT = 50

# Stateful CDP runtimes keyed by session_id. Each entry holds the live
# browser + page handles for as long as the operator keeps the session
# open. CloakBrowser uses Playwright/CDP handles; Camofox uses the
# browser server's REST tab API. Lightpanda / Obscura still use the
# one-shot fetch path until a CDP bridge is enabled. We carry an RLock
# per runtime so concurrent action requests serialize cleanly.
_RUNTIME: dict[str, dict[str, Any]] = {}
_RUNTIME_LOCK = threading.RLock()
_CDP_ENGINES = {"camofox", "cloakbrowser"}
_EVENT_LIMIT = 200
_SECRET_QUERY_KEYS = frozenset({
    "access_token", "api_key", "apikey", "auth", "authorization",
    "code", "key", "password", "refresh_token", "secret", "session",
    "sig", "signature", "token",
})
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
        r"([:=]\s*)[^\s'\"&]{4,}"
    ),
)


def _safe_value(obj: Any, name: str, default: Any = "") -> Any:
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    try:
        return value() if callable(value) else value
    except Exception:
        return default


def _redact_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    for rx in _SECRET_PATTERNS:
        if rx.groups >= 2:
            text = rx.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        else:
            text = rx.sub(lambda m: f"{m.group(1)}[redacted]", text)
    if len(text) > limit:
        return text[:limit] + "\n[truncated]"
    return text


def _redact_url(raw: Any) -> str:
    url = str(raw or "")
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        query = urlencode(
            [
                (
                    key,
                    "[redacted]" if key.lower() in _SECRET_QUERY_KEYS else value,
                )
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
            ],
            doseq=True,
        )
        fragment = "[redacted]" if parts.fragment else ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, fragment))
    except Exception:
        return _redact_text(url, 2000)


def _append_event(runtime: dict[str, Any], bucket: str, event: dict[str, Any]) -> None:
    lock = runtime.get("events_lock")

    def _write() -> None:
        rows = runtime.setdefault(bucket, [])
        rows.append(event)
        if len(rows) > _EVENT_LIMIT:
            del rows[:-_EVENT_LIMIT]

    if lock is not None:
        with lock:
            _write()
    else:
        _write()


def _is_api_event(event: dict[str, Any]) -> bool:
    resource_type = str(event.get("resource_type") or "").lower()
    if resource_type in {"eventsource", "fetch", "websocket", "xhr"}:
        return True
    try:
        path = urlsplit(str(event.get("url") or "")).path.lower()
    except Exception:
        path = ""
    return "/api/" in path or path.endswith("/api") or "/graphql" in path


def _runtime_events(
    runtime: dict[str, Any],
    bucket: str,
    *,
    limit: int = 50,
    kind: str = "",
    api_only: bool = False,
    clear: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    limit = max(1, min(int(limit or 50), _EVENT_LIMIT))
    lock = runtime.get("events_lock")

    def _read() -> tuple[list[dict[str, Any]], int]:
        rows = list(runtime.get(bucket) or [])
        total = len(rows)
        if kind:
            rows = [r for r in rows if str(r.get("kind") or "") == kind]
        if api_only:
            rows = [r for r in rows if _is_api_event(r)]
        selected = rows[-limit:]
        if clear:
            runtime[bucket] = []
        return selected, total

    if lock is not None:
        with lock:
            return _read()
    return _read()


def _install_event_listeners(runtime: dict[str, Any]) -> None:
    if runtime.get("events_attached"):
        return
    page = runtime.get("page")
    on = getattr(page, "on", None)
    if not callable(on):
        runtime["events_attached"] = False
        return

    def _on_console(msg: Any) -> None:
        loc = _safe_value(msg, "location", {}) or {}
        if not isinstance(loc, dict):
            loc = {}
        event = {
            "ts": _now_iso(),
            "kind": "console",
            "type": str(_safe_value(msg, "type", "log") or "log"),
            "text": _redact_text(_safe_value(msg, "text", "")),
        }
        if loc:
            event["location"] = {
                "url": _redact_url(loc.get("url")),
                "line": loc.get("lineNumber") or loc.get("line"),
                "column": loc.get("columnNumber") or loc.get("column"),
            }
        _append_event(runtime, "console_events", event)

    def _on_page_error(exc: Any) -> None:
        _append_event(runtime, "console_events", {
            "ts": _now_iso(),
            "kind": "pageerror",
            "type": type(exc).__name__,
            "text": _redact_text(exc),
        })

    def _on_request(req: Any) -> None:
        post_data = _safe_value(req, "post_data", "")
        event = {
            "ts": _now_iso(),
            "kind": "request",
            "method": str(_safe_value(req, "method", "GET") or "GET"),
            "url": _redact_url(_safe_value(req, "url", "")),
            "resource_type": str(_safe_value(req, "resource_type", "") or ""),
        }
        if post_data:
            event["post_data_preview"] = _redact_text(post_data, 1000)
        _append_event(runtime, "network_events", event)

    def _on_response(resp: Any) -> None:
        req = _safe_value(resp, "request", None)
        event = {
            "ts": _now_iso(),
            "kind": "response",
            "status": _safe_value(resp, "status", None),
            "url": _redact_url(_safe_value(resp, "url", "")),
            "resource_type": (
                str(_safe_value(req, "resource_type", "") or "") if req else ""
            ),
            "method": str(_safe_value(req, "method", "") or "") if req else "",
        }
        _append_event(runtime, "network_events", event)

    for event_name, handler in (
        ("console", _on_console),
        ("pageerror", _on_page_error),
        ("request", _on_request),
        ("response", _on_response),
    ):
        try:
            on(event_name, handler)
        except Exception:
            pass
    runtime["events_attached"] = True


def _now_iso() -> str:
    # ISO-8601 UTC, second precision is plenty for a UI history.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_session_id() -> str:
    return "bs_" + secrets.token_hex(6)


def _summarise(record: dict[str, Any]) -> dict[str, Any]:
    """Return a small, list-friendly view of a session record."""
    last = record.get("last") or {}
    return {
        "session_id": record["session_id"],
        "engine": record.get("engine"),
        "current_url": record.get("current_url") or "",
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "history_count": len(record.get("history") or []),
        "last_ok": bool(last.get("ok")),
        "last_fetch_method": last.get("fetch_method") or "",
        "last_bytes": last.get("bytes") or 0,
        "last_elapsed_ms": last.get("elapsed_ms") or 0,
        "cdp": bool(record.get("cdp")),
    }


def _runtime_get(sid: str) -> dict[str, Any] | None:
    with _RUNTIME_LOCK:
        return _RUNTIME.get(sid)


def _runtime_drop(sid: str) -> None:
    with _RUNTIME_LOCK:
        runtime = _RUNTIME.pop(sid, None)
    if not runtime:
        return
    if runtime.get("kind") == "camofox_service":
        try:
            _be.camofox_close_runtime(runtime)
        except Exception:
            pass
        return
    if runtime.get("kind") == "cloakbrowser_worker":
        worker = runtime.get("worker")
        if worker is not None:
            try:
                worker.close()
            except Exception:
                pass
        return
    page = runtime.get("page")
    context = runtime.get("context")
    browser = runtime.get("browser")
    try:
        if page is not None:
            page.close()
    except Exception:
        pass
    try:
        if context is not None:
            context.close()
    except Exception:
        pass
    try:
        if browser is not None:
            browser.close()
    except Exception:
        pass


def _import_cloakbrowser():
    try:
        import cloakbrowser  # type: ignore[import-not-found]
        return cloakbrowser
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"cloakbrowser not installed ({type(exc).__name__}: {exc}); "
            "install via Settings → Browsers"
        ) from exc


def _launch_with_timeout(cb, timeout_s: float) -> tuple[Any, Exception | None]:
    """Run ``cloakbrowser.launch()`` in a watchdog thread.

    First-launch scenarios download a ~200MB stealth Chromium binary from
    upstream CDN. When the download stalls (corporate firewall, broken
    TLS, slow link) the synchronous ``launch()`` would hang the HTTP
    handler indefinitely; this wrapper drops the wait after
    ``timeout_s`` so the API can return a structured error.
    """
    out: dict[str, Any] = {"browser": None, "exc": None}

    def _runner():
        try:
            out["browser"] = cb.launch()
        except Exception as exc:  # noqa: BLE001
            out["exc"] = exc

    thr = threading.Thread(target=_runner, name="cloak-launch", daemon=True)
    thr.start()
    thr.join(timeout=max(5.0, float(timeout_s)))
    if thr.is_alive():
        return None, TimeoutError(
            "cloakbrowser launch did not finish within "
            f"{int(timeout_s)}s — first-time launch downloads a stealth "
            "Chromium binary (~200MB). Retry once the download completes, "
            "or pre-download via `python -c \"import cloakbrowser; "
            "cloakbrowser.launch().close()\"`."
        )
    return out["browser"], out["exc"]


class _CloakBrowserWorker:
    """Own a CloakBrowser/Playwright sync runtime on one stable thread.

    Playwright's sync objects are backed by greenlets and cannot be
    created in one thread then used from another request thread. The
    local API server is a ``ThreadingHTTPServer``, so every session keeps
    a small worker that serializes page operations on the thread that
    launched the browser.
    """

    def __init__(self, cb: Any, *, ignore_https_errors: bool, proxy_cfg: dict[str, Any] | None) -> None:
        self._cb = cb
        self._ignore_https_errors = bool(ignore_https_errors)
        self._proxy_cfg = proxy_cfg
        self._jobs: queue.Queue[Any] = queue.Queue()
        self._ready: queue.Queue[Any] = queue.Queue(maxsize=1)
        self.runtime: dict[str, Any] = {
            "engine": "cloakbrowser",
            "kind": "cloakbrowser_worker",
            "worker": self,
            "lock": threading.RLock(),
            "events_lock": threading.RLock(),
            "console_events": [],
            "network_events": [],
        }
        self._thread = threading.Thread(
            target=self._run,
            name="cloakbrowser-session",
            daemon=True,
        )
        self._thread.start()

    def _new_context_page(self, browser: Any) -> tuple[Any | None, Any]:
        context = None
        page = None
        try:
            context_kwargs: dict[str, Any] = {
                "ignore_https_errors": self._ignore_https_errors,
            }
            if self._proxy_cfg:
                context_kwargs["proxy"] = self._proxy_cfg
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
        except TypeError:
            try:
                context = browser.new_context()
                page = context.new_page()
            except Exception:  # noqa: BLE001
                context = None
        except Exception:  # noqa: BLE001
            context = None
        if page is None:
            try:
                page = browser.new_page(
                    ignore_https_errors=self._ignore_https_errors,
                )
            except TypeError:
                page = browser.new_page()
        return context, page

    def _run(self) -> None:
        try:
            browser = self._cb.launch()
            context, page = self._new_context_page(browser)
            self.runtime.update({
                "browser": browser,
                "context": context,
                "page": page,
            })
            _install_event_listeners(self.runtime)
            self._ready.put({"ok": True})
        except Exception as exc:  # noqa: BLE001
            self._ready.put({"ok": False, "exc": exc})
            return

        try:
            while True:
                item = self._jobs.get()
                if item is None:
                    return
                fn, outq = item
                try:
                    outq.put((True, fn(self.runtime)))
                except BaseException as exc:  # noqa: BLE001
                    outq.put((False, exc))
        finally:
            page = self.runtime.get("page")
            context = self.runtime.get("context")
            browser = self.runtime.get("browser")
            for handle in (page, context, browser):
                try:
                    if handle is not None:
                        handle.close()
                except Exception:
                    pass

    def wait_ready(self, timeout_s: float) -> None:
        try:
            result = self._ready.get(timeout=max(5.0, float(timeout_s)))
        except queue.Empty as exc:
            raise TimeoutError(
                "cloakbrowser launch did not finish within "
                f"{int(timeout_s)}s. First launch may download a stealth "
                "Chromium binary; retry after it completes."
            ) from exc
        if not result.get("ok"):
            exc = result.get("exc") or RuntimeError("cloakbrowser launch failed")
            raise exc

    def call(self, fn, *, timeout_s: float) -> Any:
        outq: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._jobs.put((fn, outq))
        try:
            ok, value = outq.get(timeout=max(1.0, float(timeout_s)))
        except queue.Empty as exc:
            raise TimeoutError(f"cloakbrowser action timed out after {timeout_s}s") from exc
        if ok:
            return value
        raise value

    def close(self) -> None:
        self._jobs.put(None)


def _do_fetch(workspace_root, engine: str | None, url: str, timeout_s: float) -> dict[str, Any]:
    """Hit the browser engine via the integration layer."""
    return _be.fetch(
        workspace_root=workspace_root,
        name=engine or None,
        url=url,
        timeout_s=timeout_s,
    )


def _resolve_engine(client, override: str | None) -> tuple[str, str | None]:
    """Resolve which engine to use, returning ``(name, error)``."""
    if override:
        name = override.strip().lower()
        st = _be.status(client.config.paths.root)
        engines = {row["name"]: row for row in (st.get("engines") or [])}
        row = engines.get(name)
        if not row:
            return name, f"unknown engine: {override!r}"
        if not row.get("installed"):
            return name, f"engine {name!r} is not installed"
        return name, None

    st = _be.status(client.config.paths.root)
    selected = st.get("selected") or ""
    if not selected:
        return "", "no engine selected — pick one in Settings → Browsers first"
    return str(selected), None


def routes():
    def start(client, payload):
        body = payload or {}
        url = (body.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url is required"}
        timeout_s = float(body.get("timeout_s") or 60)
        engine_override = (body.get("engine") or "").strip() or None

        engine, err = _resolve_engine(client, engine_override)
        if err:
            return {"ok": False, "error": err, "engine": engine}

        sid = (body.get("session_id") or "").strip() or _new_session_id()
        with _LOCK:
            existing = _SESSIONS.get(sid)
            if existing and existing.get("engine") and engine_override is None:
                # Resume: keep the original engine to keep navigation
                # consistent within a session.
                engine = existing["engine"]

        result = _do_fetch(client.config.paths.root, engine, url, timeout_s)
        now = _now_iso()
        with _LOCK:
            record = _SESSIONS.get(sid) or {
                "session_id": sid,
                "engine": engine,
                "created_at": now,
                "history": [],
            }
            record["engine"] = engine
            record["updated_at"] = now
            if result.get("ok"):
                record["current_url"] = url
                record["history"].append({
                    "ts": now,
                    "url": url,
                    "ok": True,
                    "fetch_method": result.get("fetch_method"),
                    "bytes": result.get("bytes") or 0,
                    "elapsed_ms": result.get("elapsed_ms") or 0,
                })
                record["history"] = record["history"][-_HISTORY_LIMIT:]
            record["last"] = result
            _SESSIONS[sid] = record

        return {"ok": True, **_summarise(record), "result": result}

    def navigate(client, payload):
        body = payload or {}
        sid = (body.get("session_id") or "").strip()
        url = (body.get("url") or "").strip()
        if not sid:
            return {"ok": False, "error": "session_id is required"}
        if not url:
            return {"ok": False, "error": "url is required"}
        timeout_s = float(body.get("timeout_s") or 60)
        with _LOCK:
            record = _SESSIONS.get(sid)
            if not record:
                return {"ok": False, "error": "session_not_found"}
            engine = record.get("engine")

        result = _do_fetch(client.config.paths.root, engine, url, timeout_s)
        now = _now_iso()
        with _LOCK:
            record = _SESSIONS.get(sid)
            if not record:
                return {"ok": False, "error": "session_not_found"}
            record["updated_at"] = now
            if result.get("ok"):
                record["current_url"] = url
                record["history"].append({
                    "ts": now,
                    "url": url,
                    "ok": True,
                    "fetch_method": result.get("fetch_method"),
                    "bytes": result.get("bytes") or 0,
                    "elapsed_ms": result.get("elapsed_ms") or 0,
                })
                record["history"] = record["history"][-_HISTORY_LIMIT:]
            record["last"] = result

        return {"ok": True, **_summarise(record), "result": result}

    def snapshot(client, payload):
        body = payload or {}
        sid = (body.get("session_id") or "").strip()
        if not sid:
            return {"ok": False, "error": "session_id is required"}
        timeout_s = float(body.get("timeout_s") or 60)
        with _LOCK:
            record = _SESSIONS.get(sid)
            if not record:
                return {"ok": False, "error": "session_not_found"}
            engine = record.get("engine")
            url = record.get("current_url") or ""
        if not url:
            return {"ok": False, "error": "session_has_no_current_url"}

        result = _do_fetch(client.config.paths.root, engine, url, timeout_s)
        now = _now_iso()
        with _LOCK:
            record = _SESSIONS.get(sid)
            if not record:
                return {"ok": False, "error": "session_not_found"}
            record["updated_at"] = now
            record["last"] = result

        return {"ok": True, **_summarise(record), "result": result}

    def screenshot(client, payload):
        body = payload or {}
        sid = (body.get("session_id") or "").strip()
        url = (body.get("url") or "").strip()
        full_page = bool(body.get("full_page", True))
        timeout_s = float(body.get("timeout_s") or 60)
        if not sid:
            return {"ok": False, "error": "session_id is required"}
        with _LOCK:
            record = _SESSIONS.get(sid)
            if not record:
                return {"ok": False, "error": "session_not_found"}
            engine = record.get("engine")
            if not url:
                url = record.get("current_url") or ""
        if not url:
            return {"ok": False, "error": "session_has_no_current_url"}

        # Ignore full_page flag for binary engines (CLI doesn't expose it).
        _ = full_page
        result = _be.screenshot(
            workspace_root=client.config.paths.root,
            name=engine or None,
            url=url,
            timeout_s=timeout_s,
        )
        now = _now_iso()
        data_uri: str | None = None
        if result.get("ok"):
            try:
                raw = Path(result["path"]).read_bytes()
                data_uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
            except Exception as exc:  # noqa: BLE001
                result["data_uri_error"] = f"{type(exc).__name__}: {exc}"

        with _LOCK:
            record = _SESSIONS.get(sid)
            if not record:
                return {"ok": False, "error": "session_not_found"}
            record["updated_at"] = now
            shot = {
                "ts": now,
                "url": url,
                "ok": bool(result.get("ok")),
                "path": result.get("path"),
                "bytes": result.get("bytes") or 0,
                "elapsed_ms": result.get("elapsed_ms") or 0,
                "fetch_method": result.get("fetch_method") or "",
                "error": result.get("error"),
                "stderr_tail": result.get("stderr_tail"),
            }
            if data_uri:
                shot["data_uri"] = data_uri
            shots = record.get("screenshots") or []
            shots.append(shot)
            record["screenshots"] = shots[-12:]
            record["last_screenshot"] = shot

        return {"ok": True, "session_id": sid, "engine": engine,
                **{k: v for k, v in result.items() if k != "path"},
                "path": result.get("path"),
                "data_uri": data_uri}

    def cdp_open(client, payload):
        body = payload or {}
        url = (body.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url is required"}
        engine = (body.get("engine") or "").strip().lower()
        if not engine:
            st = _be.status(client.config.paths.root)
            selected_engine = str(st.get("selected") or "").strip().lower()
            if selected_engine in _CDP_ENGINES:
                engine = selected_engine
            else:
                for row in st.get("engines") or []:
                    name = str(row.get("name") or "").strip().lower()
                    ready = row.get("ready") if "ready" in row else row.get("installed")
                    if name in _CDP_ENGINES and row.get("installed") and ready:
                        engine = name
                        break
        engine = engine or "camofox"
        if engine not in _CDP_ENGINES:
            return {
                "ok": False,
                "error": f"engine {engine!r} does not support CDP yet",
                "supported": sorted(_CDP_ENGINES),
            }
        sid = (body.get("session_id") or "").strip() or _new_session_id()
        timeout_ms = int(float(body.get("timeout_s") or 30) * 1000)
        ignore_https_errors = body.get("ignore_https_errors")
        if ignore_https_errors is None:
            ignore_https_errors = True  # be permissive for stealth fetches
        wait_until = (body.get("wait_until") or "domcontentloaded").strip() or "domcontentloaded"

        if engine == "camofox":
            runtime = _runtime_get(sid)
            try:
                if runtime is None:
                    opened = _be.camofox_open_tab(
                        client.config.paths.root,
                        session_id=sid,
                        url=url,
                        timeout_s=float(body.get("timeout_s") or 30),
                        trace=bool(body.get("trace", True)),
                    )
                    if not opened.get("ok"):
                        return opened
                    camo_runtime = opened.get("runtime")
                    if not isinstance(camo_runtime, dict):
                        return {"ok": False, "error": "camofox_runtime_missing"}
                    runtime = {
                        "engine": engine,
                        "kind": "camofox_service",
                        **camo_runtime,
                        "lock": threading.RLock(),
                        "events_lock": threading.RLock(),
                        "console_events": [],
                        "network_events": [],
                    }
                    with _RUNTIME_LOCK:
                        _RUNTIME[sid] = runtime
                    current_url = str(opened.get("current_url") or url)
                    last_result = opened
                else:
                    with runtime["lock"]:
                        last_result = _be.camofox_action_runtime(
                            runtime,
                            "goto",
                            {"url": url, "timeout_s": float(body.get("timeout_s") or 60)},
                            timeout_s=float(body.get("timeout_s") or 60),
                        )
                    if not last_result.get("ok"):
                        return last_result
                    current_url = str(last_result.get("current_url") or last_result.get("url") or url)
                runtime["current_url"] = current_url
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": "camofox_open_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }

            now = _now_iso()
            with _LOCK:
                record = _SESSIONS.get(sid) or {
                    "session_id": sid, "engine": engine,
                    "created_at": now, "history": [],
                }
                record["engine"] = engine
                record["updated_at"] = now
                record["current_url"] = current_url
                record["cdp"] = True
                record["interactive"] = True
                record["history"].append({
                    "ts": now, "url": url, "ok": True,
                    "fetch_method": "camofox_open", "bytes": 0, "elapsed_ms": 0,
                })
                record["history"] = record["history"][-_HISTORY_LIMIT:]
                record["last"] = last_result
                _SESSIONS[sid] = record

            return {
                "ok": True,
                **_summarise(record),
                "current_url": current_url,
                "result": last_result,
            }

        try:
            cb = _import_cloakbrowser()
        except RuntimeError as exc:
            return {"ok": False, "error": "module_unavailable", "detail": str(exc)}

        runtime = _runtime_get(sid)
        if runtime is None:
            # Default 10 minutes — first-launch downloads ~200MB stealth
            # Chromium plus retries from upstream CDN/GitHub Releases. Operator
            # can shorten via the UI (cdpLaunchTimeoutSec) or
            # ``launch_timeout_s`` in the request body.
            launch_timeout_s = float(body.get("launch_timeout_s") or 600)
            try:
                proxy_cfg = browser_proxy_config_for_workspace(client.config.paths)
                worker = _CloakBrowserWorker(
                    cb,
                    ignore_https_errors=bool(ignore_https_errors),
                    proxy_cfg=proxy_cfg,
                )
                worker.wait_ready(launch_timeout_s)
                runtime = worker.runtime
            except Exception as err:  # noqa: BLE001
                detail = f"{type(err).__name__}: {err}"
                hint = ""
                low = detail.lower()
                if "ssl" in low or "handshake" in low or "tls" in low:
                    hint = (
                        "Upstream stealth-binary download failed TLS "
                        "handshake. Run `python -c \"import cloakbrowser; "
                        "cloakbrowser.launch().close()\"` from a network "
                        "that can reach the cloakbrowser CDN, or set "
                        "HTTPS_PROXY / set CLOAKBROWSER_BINARY_URL "
                        "to a mirror, then retry."
                    )
                elif isinstance(err, TimeoutError):
                    hint = (
                        "First-time launch downloads ~200MB. Increase "
                        "launch_timeout_s in the request payload, or "
                        "pre-download outside the dashboard."
                    )
                return {"ok": False, "error": "launch_failed",
                        "detail": detail, "hint": hint}
            with _RUNTIME_LOCK:
                _RUNTIME[sid] = runtime

        try:
            if runtime.get("kind") == "cloakbrowser_worker":
                worker = runtime.get("worker")

                def _goto(rt: dict[str, Any]) -> str:
                    with rt["lock"]:
                        _install_event_listeners(rt)
                        rt["page"].goto(url, timeout=timeout_ms, wait_until=wait_until)
                        return rt["page"].url

                current_url = worker.call(
                    _goto,
                    timeout_s=max(float(body.get("timeout_s") or 30) + 5.0, 10.0),
                )
            else:
                with runtime["lock"]:
                    _install_event_listeners(runtime)
                    runtime["page"].goto(url, timeout=timeout_ms, wait_until=wait_until)
                    current_url = runtime["page"].url
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            hint = ""
            low = msg.lower()
            if "ssl" in low or "ssl:" in low or "certificate" in low or "tls" in low:
                hint = (
                    "TLS handshake aborted by the upstream site. Try "
                    "'ignore_https_errors=true' (default), check that the "
                    "stealth chromium binary finished downloading on first "
                    "launch (cloakbrowser caches ~200MB locally), or test the "
                    "URL via the non-CDP simple mode first to confirm the "
                    "site itself is reachable."
                )
            elif "connect" in low and ("eof" in low or "reset" in low or "refused" in low):
                hint = (
                    "Connection was reset before the page could load. The "
                    "stealth Chromium build may be still downloading on first "
                    "launch — wait a minute and retry, or verify network "
                    "egress to the target host."
                )
            elif "timeout" in low:
                hint = (
                    f"Page did not finish loading within {timeout_ms}ms. "
                    "Increase timeout_s in the request, switch wait_until to "
                    "'load' or 'commit', or verify the site is reachable."
                )
            return {"ok": False, "error": "page_error",
                    "detail": f"{type(exc).__name__}: {msg}",
                    "hint": hint}

        now = _now_iso()
        with _LOCK:
            record = _SESSIONS.get(sid) or {
                "session_id": sid, "engine": engine,
                "created_at": now, "history": [],
            }
            record["engine"] = engine
            record["updated_at"] = now
            record["current_url"] = current_url
            record["cdp"] = True
            record["history"].append({
                "ts": now, "url": url, "ok": True,
                "fetch_method": "cdp_goto", "bytes": 0, "elapsed_ms": 0,
            })
            record["history"] = record["history"][-_HISTORY_LIMIT:]
            _SESSIONS[sid] = record

        return {"ok": True, **_summarise(record), "current_url": current_url}

    def cdp_action(client, payload):
        body = payload or {}
        sid = (body.get("session_id") or "").strip()
        if not sid:
            return {"ok": False, "error": "session_id is required"}
        action = (body.get("action") or "").strip()
        params = body.get("payload") or {}
        if not isinstance(params, dict):
            params = {}
        runtime = _runtime_get(sid)
        if runtime is None:
            return {"ok": False, "error": "session_not_found_or_not_cdp"}
        if runtime.get("kind") == "cloakbrowser_worker" and not runtime.get("_inside_worker"):
            worker = runtime.get("worker")
            if worker is None:
                return {"ok": False, "error": "worker_unavailable"}

            def _run_in_worker(rt: dict[str, Any]) -> dict[str, Any]:
                rt["_inside_worker"] = True
                try:
                    return cdp_action(client, payload)
                finally:
                    rt.pop("_inside_worker", None)

            try:
                return worker.call(
                    _run_in_worker,
                    timeout_s=float(params.get("timeout_s") or body.get("timeout_s") or 30) + 5.0,
                )
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": "action_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
        if runtime.get("kind") == "camofox_service":
            started = time.monotonic()
            try:
                with runtime["lock"]:
                    result = _be.camofox_action_runtime(
                        runtime,
                        action,
                        params,
                        timeout_s=float(params.get("timeout_s") or 30),
                    )
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": "action_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            if not result.get("ok"):
                return result
            elapsed_ms = int((time.monotonic() - started) * 1000)
            current_url = str(
                result.get("current_url")
                or result.get("url")
                or runtime.get("current_url")
                or ""
            )
            if current_url:
                runtime["current_url"] = current_url
            result["elapsed_ms"] = elapsed_ms
            result["current_url"] = current_url
            now = _now_iso()
            with _LOCK:
                record = _SESSIONS.get(sid)
                if record is not None:
                    record["updated_at"] = now
                    if current_url:
                        record["current_url"] = current_url
                    record["history"].append({
                        "ts": now, "url": current_url, "ok": True,
                        "fetch_method": f"camofox_{action}",
                        "bytes": 0, "elapsed_ms": elapsed_ms,
                    })
                    record["history"] = record["history"][-_HISTORY_LIMIT:]
            return result

        page = runtime.get("page")
        if page is None:
            return {"ok": False, "error": "page_unavailable"}

        started = time.monotonic()
        result: dict[str, Any] = {"ok": True, "action": action}
        try:
            with runtime["lock"]:
                if action == "click_xy":
                    x = float(params.get("x", 0))
                    y = float(params.get("y", 0))
                    page.mouse.click(x, y)
                    result["click"] = {"x": x, "y": y}
                elif action == "click_selector":
                    sel = str(params.get("selector") or "").strip()
                    if not sel:
                        return {"ok": False, "error": "selector_required"}
                    page.click(sel, timeout=int(float(params.get("timeout_s", 5)) * 1000))
                    result["selector"] = sel
                elif action == "type":
                    sel = str(params.get("selector") or "").strip()
                    text = str(params.get("text") or "")
                    delay_ms = int(params.get("delay_ms") or 0)
                    if sel:
                        page.fill(sel, text)
                    else:
                        page.keyboard.type(text, delay=delay_ms)
                    result["typed"] = len(text)
                elif action == "press":
                    key = str(params.get("key") or "").strip()
                    if not key:
                        return {"ok": False, "error": "key_required"}
                    page.keyboard.press(key)
                    result["key"] = key
                elif action == "scroll":
                    dx = float(params.get("dx") or 0)
                    dy = float(params.get("dy") or 0)
                    page.mouse.wheel(dx, dy)
                    result["delta"] = {"dx": dx, "dy": dy}
                elif action == "drag":
                    source_sel = str(params.get("source_selector") or "").strip()
                    target_sel = str(params.get("target_selector") or "").strip()
                    if source_sel and target_sel and hasattr(page, "drag_and_drop"):
                        page.drag_and_drop(
                            source_sel,
                            target_sel,
                            timeout=int(float(params.get("timeout_s", 5)) * 1000),
                        )
                        result["drag"] = {
                            "source_selector": source_sel,
                            "target_selector": target_sel,
                        }
                    else:
                        missing = [
                            name for name in ("x", "y", "to_x", "to_y")
                            if params.get(name) is None
                        ]
                        if missing:
                            return {
                                "ok": False,
                                "error": "drag_requires_coordinates_or_selectors",
                                "missing": missing,
                            }
                        x = float(params.get("x"))
                        y = float(params.get("y"))
                        to_x = float(params.get("to_x"))
                        to_y = float(params.get("to_y"))
                        steps = max(1, min(int(params.get("steps") or 12), 80))
                        page.mouse.move(x, y)
                        page.mouse.down()
                        page.mouse.move(to_x, to_y, steps=steps)
                        page.mouse.up()
                        result["drag"] = {
                            "from": {"x": x, "y": y},
                            "to": {"x": to_x, "y": to_y},
                            "steps": steps,
                        }
                elif action == "scroll_to":
                    page.evaluate(
                        "({x, y}) => window.scrollTo(x, y)",
                        {"x": float(params.get("x") or 0),
                         "y": float(params.get("y") or 0)},
                    )
                elif action == "goto":
                    target = str(params.get("url") or "").strip()
                    if not target:
                        return {"ok": False, "error": "url_required"}
                    page.goto(target, timeout=int(float(params.get("timeout_s", 30)) * 1000))
                    result["url"] = page.url
                elif action == "go_back":
                    page.go_back()
                    result["url"] = page.url
                elif action == "go_forward":
                    page.go_forward()
                    result["url"] = page.url
                elif action == "reload":
                    page.reload()
                    result["url"] = page.url
                elif action == "eval":
                    expr = str(params.get("expression") or "")
                    if not expr:
                        return {"ok": False, "error": "expression_required"}
                    try:
                        value = page.evaluate(expr)
                    except Exception as exc:  # noqa: BLE001
                        return {"ok": False, "error": "eval_failed",
                                "detail": f"{type(exc).__name__}: {exc}"}
                    # Truncate to keep payloads reasonable.
                    text = ""
                    try:
                        import json as _json
                        text = _json.dumps(value, ensure_ascii=False, default=str)[:8000]
                    except Exception:
                        text = str(value)[:8000]
                    result["value"] = text
                elif action == "title":
                    result["title"] = page.title()
                elif action == "snapshot":
                    max_chars = max(200, min(int(params.get("max_chars") or 8000), 50000))
                    snap: dict[str, Any] = {
                        "url": page.url,
                        "title": page.title(),
                    }
                    try:
                        text = page.evaluate(
                            "document.body ? document.body.innerText : ''"
                        )
                        snap["text"] = _redact_text(text, max_chars)
                    except Exception as exc:  # noqa: BLE001
                        snap["text_error"] = f"{type(exc).__name__}: {exc}"
                    if bool(params.get("include_html", False)):
                        try:
                            snap["html"] = _redact_text(page.content(), max_chars)
                        except Exception as exc:  # noqa: BLE001
                            snap["html_error"] = f"{type(exc).__name__}: {exc}"
                    result["snapshot"] = snap
                elif action == "wait_for_selector":
                    sel = str(params.get("selector") or "").strip()
                    if not sel:
                        return {"ok": False, "error": "selector_required"}
                    page.wait_for_selector(
                        sel,
                        timeout=int(float(params.get("timeout_s", 10)) * 1000),
                    )
                    result["selector"] = sel
                elif action == "wait":
                    ms = max(0, min(int(params.get("ms") or 1000), 60000))
                    if hasattr(page, "wait_for_timeout"):
                        page.wait_for_timeout(ms)
                    else:
                        time.sleep(ms / 1000.0)
                    result["waited_ms"] = ms
                elif action == "get_console":
                    events, total = _runtime_events(
                        runtime,
                        "console_events",
                        limit=int(params.get("limit") or 50),
                        kind=str(params.get("kind") or ""),
                        clear=bool(params.get("clear", False)),
                    )
                    result["console"] = events
                    result["count"] = len(events)
                    result["total"] = total
                elif action in {"get_network", "get_api_requests"}:
                    events, total = _runtime_events(
                        runtime,
                        "network_events",
                        limit=int(params.get("limit") or 50),
                        kind=str(params.get("kind") or ""),
                        api_only=(
                            True if action == "get_api_requests"
                            else bool(params.get("api_only", False))
                        ),
                        clear=bool(params.get("clear", False)),
                    )
                    result["events"] = events
                    result["count"] = len(events)
                    result["total"] = total
                elif action == "clear_events":
                    with runtime.get("events_lock") or threading.RLock():
                        console_count = len(runtime.get("console_events") or [])
                        network_count = len(runtime.get("network_events") or [])
                        runtime["console_events"] = []
                        runtime["network_events"] = []
                    result["cleared"] = {
                        "console": console_count,
                        "network": network_count,
                    }
                elif action == "api_fetch":
                    target = str(params.get("url") or "").strip()
                    if not target:
                        return {"ok": False, "error": "url_required"}
                    method = str(params.get("method") or "GET").upper()
                    headers = params.get("headers") if isinstance(params.get("headers"), dict) else {}
                    body_value = params.get("body")
                    if "json" in params:
                        body_value = json.dumps(params.get("json"), ensure_ascii=False)
                        headers = dict(headers or {})
                        headers.setdefault("content-type", "application/json")
                    if body_value is not None and not isinstance(body_value, str):
                        body_value = json.dumps(body_value, ensure_ascii=False, default=str)
                    max_chars = max(0, min(int(params.get("max_chars") or 8000), 50000))
                    value = page.evaluate(
                        """
                        async ({url, method, headers, body, credentials, maxChars}) => {
                          const init = {method, headers: headers || {}, credentials: credentials || "same-origin"};
                          if (body !== null && body !== undefined) init.body = body;
                          const response = await fetch(url, init);
                          const text = await response.text();
                          return {
                            ok: response.ok,
                            status: response.status,
                            status_text: response.statusText,
                            url: response.url,
                            content_type: response.headers.get("content-type") || "",
                            text: text.slice(0, maxChars),
                            truncated: text.length > maxChars
                          };
                        }
                        """,
                        {
                            "url": target,
                            "method": method,
                            "headers": headers,
                            "body": body_value,
                            "credentials": params.get("credentials") or "same-origin",
                            "maxChars": max_chars,
                        },
                    )
                    if isinstance(value, dict):
                        value["url"] = _redact_url(value.get("url"))
                        if "text" in value:
                            value["text"] = _redact_text(value.get("text"), max_chars)
                    result["response"] = value
                elif action == "url":
                    pass
                else:
                    return {"ok": False, "error": f"unknown_action: {action}"}

                # Refresh the session current URL for any nav-changing op.
                current_url = page.url
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "action_failed",
                    "detail": f"{type(exc).__name__}: {exc}"}

        elapsed_ms = int((time.monotonic() - started) * 1000)
        result["elapsed_ms"] = elapsed_ms
        now = _now_iso()
        with _LOCK:
            record = _SESSIONS.get(sid)
            if record is not None:
                record["updated_at"] = now
                record["current_url"] = current_url
                record["history"].append({
                    "ts": now, "url": current_url, "ok": True,
                    "fetch_method": f"cdp_{action}",
                    "bytes": 0, "elapsed_ms": elapsed_ms,
                })
                record["history"] = record["history"][-_HISTORY_LIMIT:]
        result["current_url"] = current_url
        return result

    def cdp_screenshot(client, payload):
        body = payload or {}
        sid = (body.get("session_id") or "").strip()
        if not sid:
            return {"ok": False, "error": "session_id is required"}
        full_page = bool(body.get("full_page", True))
        runtime = _runtime_get(sid)
        if runtime is None:
            return {"ok": False, "error": "session_not_found_or_not_cdp"}
        if runtime.get("kind") == "cloakbrowser_worker" and not runtime.get("_inside_worker"):
            worker = runtime.get("worker")
            if worker is None:
                return {"ok": False, "error": "worker_unavailable"}

            def _run_in_worker(rt: dict[str, Any]) -> dict[str, Any]:
                rt["_inside_worker"] = True
                try:
                    return cdp_screenshot(client, payload)
                finally:
                    rt.pop("_inside_worker", None)

            try:
                return worker.call(
                    _run_in_worker,
                    timeout_s=float((payload or {}).get("timeout_s") or 30) + 10.0,
                )
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": "screenshot_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
        if runtime.get("kind") == "camofox_service":
            started = time.monotonic()
            ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            out_path = (
                Path(client.config.paths.root) / "state" / "browsers"
                / "screenshots" / f"camofox-{sid}-{ts}.png"
            )
            try:
                with runtime["lock"]:
                    result = _be.camofox_screenshot_runtime(
                        runtime,
                        out_path=out_path,
                        timeout_s=float((payload or {}).get("timeout_s") or 30),
                    )
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": "screenshot_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            elapsed_ms = int((time.monotonic() - started) * 1000)
            raw = Path(result["path"]).read_bytes()
            data_uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
            current_url = str(runtime.get("current_url") or "")
            now = _now_iso()
            with _LOCK:
                record = _SESSIONS.get(sid)
                if record is not None:
                    shot_meta = {
                        "ts": now, "url": current_url,
                        "ok": True,
                        "path": result.get("path"),
                        "bytes": len(raw),
                        "elapsed_ms": elapsed_ms,
                        "fetch_method": "camofox_screenshot",
                        "data_uri": data_uri,
                    }
                    shots = record.get("screenshots") or []
                    shots.append(shot_meta)
                    record["screenshots"] = shots[-12:]
                    record["last_screenshot"] = shot_meta
            return {
                "ok": True,
                "session_id": sid,
                "engine": runtime.get("engine"),
                "url": current_url,
                "bytes": len(raw),
                "elapsed_ms": elapsed_ms,
                "fetch_method": "camofox_screenshot",
                "path": result.get("path"),
                "data_uri": data_uri,
            }
        page = runtime.get("page")
        if page is None:
            return {"ok": False, "error": "page_unavailable"}

        started = time.monotonic()
        try:
            with runtime["lock"]:
                buf = io.BytesIO()
                # Playwright/cloakbrowser supports full_page kwarg.
                shot = page.screenshot(full_page=full_page)
                buf.write(shot)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "screenshot_failed",
                    "detail": f"{type(exc).__name__}: {exc}"}
        elapsed_ms = int((time.monotonic() - started) * 1000)
        raw = buf.getvalue()
        data_uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")

        # Persist a tiny copy on disk so it shows in normal screenshot history.
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        out_path = (
            Path(client.config.paths.root) / "state" / "browsers"
            / "screenshots" / f"cdp-{sid}-{ts}.png"
        )
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(raw)
        except Exception:
            out_path = None  # type: ignore[assignment]

        now = _now_iso()
        with _LOCK:
            record = _SESSIONS.get(sid)
            if record is not None:
                shot_meta = {
                    "ts": now, "url": record.get("current_url") or "",
                    "ok": True,
                    "path": str(out_path) if out_path else None,
                    "bytes": len(raw),
                    "elapsed_ms": elapsed_ms,
                    "fetch_method": "cdp_screenshot",
                    "data_uri": data_uri,
                }
                shots = record.get("screenshots") or []
                shots.append(shot_meta)
                record["screenshots"] = shots[-12:]
                record["last_screenshot"] = shot_meta

        return {
            "ok": True, "session_id": sid,
            "engine": runtime.get("engine"),
            "url": runtime["page"].url,
            "bytes": len(raw),
            "elapsed_ms": elapsed_ms,
            "fetch_method": "cdp_screenshot",
            "path": str(out_path) if out_path else None,
            "data_uri": data_uri,
        }

    def cdp_close(client, payload):
        sid = ((payload or {}).get("session_id") or "").strip()
        if not sid:
            return {"ok": False, "error": "session_id is required"}
        _runtime_drop(sid)
        with _LOCK:
            record = _SESSIONS.get(sid)
            if record is not None:
                record["cdp"] = False
        return {"ok": True, "closed": True, "session_id": sid}

    def close(client, payload):
        sid = ((payload or {}).get("session_id") or "").strip()
        # Drop CDP runtime first so we don't leak a Playwright browser.
        _runtime_drop(sid)
        with _LOCK:
            removed = _SESSIONS.pop(sid, None) if sid else None
        return {"ok": True, "removed": bool(removed), "session_id": sid}

    def list_sessions(_client, _payload):
        with _LOCK:
            return {
                "ok": True,
                "count": len(_SESSIONS),
                "sessions": [_summarise(r) for r in _SESSIONS.values()],
            }

    def get(client, query):
        sid = ""
        if isinstance(query, dict):
            sid = (query.get("session_id") or "").strip()
        with _LOCK:
            record = _SESSIONS.get(sid)
        if not record:
            return {"ok": False, "error": "session_not_found"}
        return {"ok": True, **record}

    return [
        ("POST", "/browsers/session/start", start),
        ("POST", "/browsers/session/navigate", navigate),
        ("POST", "/browsers/session/snapshot", snapshot),
        ("POST", "/browsers/session/screenshot", screenshot),
        ("POST", "/browsers/session/close", close),
        ("GET",  "/browsers/session/list", list_sessions),
        ("GET",  "/browsers/session/get", get),
        # Interactive routes. The historical path names stay ``cdp_*`` for
        # dashboard compatibility; Camofox is REST-backed, CloakBrowser is
        # Playwright/CDP-backed.
        ("POST", "/browsers/session/cdp_open", cdp_open),
        ("POST", "/browsers/session/cdp_action", cdp_action),
        ("POST", "/browsers/session/cdp_screenshot", cdp_screenshot),
        ("POST", "/browsers/session/cdp_close", cdp_close),
    ]

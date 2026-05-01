"""Dev-mode recorder.

When dev mode is on, Nerya captures a detailed trace of every external
interaction so the operator can audit agent behaviour end-to-end:

- every HTTP request and response (redacted)
- every tool / skill invocation (args, result, latency, caller)
- every unhandled error (message + traceback + classified category)

The recorder is intentionally cheap: it appends to JSONL files under
``<workspace>/dev_logs/`` and mirrors the most recent events to a bounded
in-memory ring so the API layer can surface "what happened in the last
turn?" without scanning files.

Activation:

* ``runtime.dev_mode: true`` in ``nerya.yml``, or
* ``NERYA_DEV_MODE=1`` in the process env.

Both set a process-wide switch that :func:`is_active` reads; any wiring
code can simply do::

    from nerya.core.devmode import record_http, is_active
    if is_active():
        record_http(...)

There is no global import-time state beyond the switch and the recorder
singleton; workspace paths are resolved lazily so the module is safe to
import before a workspace is bootstrapped.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

from . import jsonl
from .paths import WorkspacePaths, resolve_workspace
from .redaction import redact_text
from .time import now_iso


_ACTIVE = False
_RECORDER: Optional["DevRecorder"] = None
_LOCK = threading.RLock()


def enable(active: bool = True) -> None:
    """Force dev-mode on/off for the rest of the process."""
    global _ACTIVE
    _ACTIVE = bool(active)


def is_active() -> bool:
    if _ACTIVE:
        return True
    return os.environ.get("NERYA_DEV_MODE", "").lower() in {"1", "true", "yes", "on"}


def get_recorder(paths: WorkspacePaths | None = None) -> "DevRecorder":
    """Return the process-wide :class:`DevRecorder`, creating it on first use."""
    global _RECORDER
    with _LOCK:
        if _RECORDER is None:
            if paths is None:
                paths = resolve_workspace()
            _RECORDER = DevRecorder(paths=paths)
        return _RECORDER


def reset() -> None:
    """Reset the process-wide recorder. Intended for tests."""
    global _RECORDER, _ACTIVE
    with _LOCK:
        _RECORDER = None
        _ACTIVE = False


# ---------------------------------------------------------------------------
@dataclass
class DevEvent:
    kind: str                       # "http" | "tool" | "error" | "note"
    at: str                         # ISO timestamp
    data: dict[str, Any] = field(default_factory=dict)


class DevRecorder:
    """Append-only JSONL recorder with bounded in-memory ring."""

    #: keep at most this many events per kind in memory
    RING_SIZE = 512

    #: truncate any recorded string payload longer than this
    MAX_TEXT = 16 * 1024

    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths
        self._rings: dict[str, Deque[DevEvent]] = {
            "http": deque(maxlen=self.RING_SIZE),
            "tool": deque(maxlen=self.RING_SIZE),
            "error": deque(maxlen=self.RING_SIZE),
            "note": deque(maxlen=self.RING_SIZE),
        }
        self._lock = threading.Lock()

    # ---- public paths ------------------------------------------------------
    @property
    def dir(self) -> Path:
        return self.paths.root / "dev_logs"

    def file(self, kind: str) -> Path:
        return self.dir / f"{kind}.jsonl"

    # ---- recording -------------------------------------------------------
    def record_http(
        self,
        *,
        method: str,
        url: str,
        req_headers: dict[str, str] | None = None,
        req_body: Any = None,
        status: int | None = None,
        resp_headers: dict[str, str] | None = None,
        resp_body: Any = None,
        elapsed_ms: float | None = None,
        error: str | None = None,
        caller: str | None = None,
    ) -> None:
        doc = {
            "method": method.upper(),
            "url": _redact_url(url),
            "req_headers": _redact_headers(req_headers or {}),
            "req_body": _redact_body(req_body),
            "status": status,
            "resp_headers": _redact_headers(resp_headers or {}),
            "resp_body": _redact_body(resp_body),
            "elapsed_ms": elapsed_ms,
            "error": error,
            "caller": caller or _infer_caller(),
        }
        self._record("http", doc)

    def record_tool_call(
        self,
        *,
        tool: str,
        args: Any = None,
        result: Any = None,
        error: str | None = None,
        elapsed_ms: float | None = None,
        caller: str | None = None,
    ) -> None:
        doc = {
            "tool": tool,
            "args": _redact_body(args),
            "result": _redact_body(result),
            "error": error,
            "elapsed_ms": elapsed_ms,
            "caller": caller,
        }
        self._record("tool", doc)

    def record_error(
        self,
        exc: BaseException,
        *,
        where: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        doc = {
            "where": where,
            "type": type(exc).__name__,
            "message": redact_text(str(exc)),
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-self.MAX_TEXT :],
            "context": _redact_body(context or {}),
        }
        self._record("error", doc)

    def note(self, message: str, **extra: Any) -> None:
        self._record("note", {"message": redact_text(message), **extra})

    # ---- retrieval --------------------------------------------------------
    def recent(self, kind: str = "http", limit: int = 50) -> list[DevEvent]:
        ring = self._rings.get(kind)
        if ring is None:
            return []
        return list(ring)[-max(0, int(limit)) :]

    # ---- impl --------------------------------------------------------------
    def _record(self, kind: str, data: dict[str, Any]) -> None:
        ev = DevEvent(kind=kind, at=now_iso(), data=data)
        with self._lock:
            self._rings.setdefault(kind, deque(maxlen=self.RING_SIZE)).append(ev)
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                jsonl.append(self.file(kind), {"kind": kind, "at": ev.at, **data})
            except Exception:  # dev-log should never break the agent
                pass


# =============================================================== free fns
def record_http(**kwargs: Any) -> None:
    """Module-level convenience: record only when dev mode is active."""
    if not is_active():
        return
    try:
        get_recorder().record_http(**kwargs)
    except Exception:
        pass


def record_tool_call(**kwargs: Any) -> None:
    if not is_active():
        return
    try:
        get_recorder().record_tool_call(**kwargs)
    except Exception:
        pass


def record_error(exc: BaseException, **kwargs: Any) -> None:
    if not is_active():
        return
    try:
        get_recorder().record_error(exc, **kwargs)
    except Exception:
        pass


# =============================================================== redaction helpers
_SENSITIVE_HEADERS = {
    "authorization",
    "x-api-key",
    "x-mbx-apikey",
    "okx-access-key",
    "okx-access-sign",
    "okx-access-passphrase",
    "bybit-sign",
    "x-bapi-api-key",
    "x-bapi-sign",
    "api-secret",
    "cookie",
    "set-cookie",
}


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADERS:
            out[k] = "[redacted]"
        else:
            out[k] = str(v)[:512]
    return out


def _redact_url(url: str) -> str:
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        sp = urlsplit(url)
        q = parse_qsl(sp.query, keep_blank_values=True)
        redacted = [(k, "[redacted]" if k.lower() in {"api_key", "apikey", "signature", "token"} else v) for k, v in q]
        return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(redacted), sp.fragment))
    except Exception:
        return url


def _redact_body(body: Any) -> Any:
    try:
        if body is None or isinstance(body, (int, float, bool)):
            return body
        if isinstance(body, (bytes, bytearray)):
            txt = bytes(body).decode("utf-8", errors="replace")
            return redact_text(txt)[: DevRecorder.MAX_TEXT]
        if isinstance(body, str):
            return redact_text(body)[: DevRecorder.MAX_TEXT]
        if isinstance(body, dict):
            return {k: _redact_body(v) for k, v in body.items()}
        if isinstance(body, list):
            return [_redact_body(v) for v in body[:64]]
        return redact_text(json.dumps(body, default=str))[: DevRecorder.MAX_TEXT]
    except Exception:
        return "[unrecordable]"


def _infer_caller() -> str:
    frame = None
    try:
        import inspect
        frame = inspect.currentframe()
        if frame is None:
            return ""
        # skip record_http, record, and the direct caller wrapper
        outer = inspect.getouterframes(frame, context=0)
        for entry in outer[3:]:
            mod = entry.frame.f_globals.get("__name__", "")
            if mod and not mod.startswith("nerya.core.devmode"):
                return f"{mod}:{entry.function}:{entry.lineno}"
    except Exception:
        pass
    finally:
        del frame
    return ""


__all__ = [
    "DevRecorder",
    "DevEvent",
    "enable",
    "is_active",
    "get_recorder",
    "reset",
    "record_http",
    "record_tool_call",
    "record_error",
]

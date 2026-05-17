"""E2E artifact auto-capture helpers.

Instead of relying on the operator (or a CI helper) to manually call
``POST /ops/e2e/run/start`` + ``record`` + ``finalize`` for every check,
these helpers wrap the whole round-trip in one call.

Two integration points:

- :func:`capture_request_response` — used by the agent ``/agent/run_turn``
  route to record one e2e run per turn (open + record + finalize). Gated
  by ``NERYA_E2E_AUTO_CAPTURE_RUN_TURN`` so default behavior is unchanged.
- :func:`capture_dashboard_smoke` — exposed via ``POST /ops/e2e/auto-capture``
  so the dashboard's smoke runner can take a screenshot/DOM bundle and
  drop it as a single artifact, without orchestrating the three sub-routes.

The helpers always honor ``runtime.e2e_artifact_capture``; failures are
swallowed and logged so production calls never break the caller.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from . import e2e_artifacts as _e2e


_LOG = logging.getLogger(__name__)


def _flag_enabled(client: Any) -> bool:
    try:
        from ..runtime import feature_flags as ff
        return bool(ff.is_enabled(client, "runtime.e2e_artifact_capture"))
    except Exception:  # pragma: no cover - defensive
        return True


def _auto_capture_run_turn_enabled() -> bool:
    raw = os.environ.get("NERYA_E2E_AUTO_CAPTURE_RUN_TURN", "")
    return str(raw).strip().lower() in {"1", "true", "on", "yes", "y"}


def capture_request_response(
    client: Any,
    *,
    label: str,
    method: str,
    url: str,
    request_body: Any = None,
    response_body: Any = None,
    status_code: int = 0,
    elapsed_ms: int = 0,
    request_headers: Optional[dict[str, Any]] = None,
    response_headers: Optional[dict[str, Any]] = None,
    base_url: str = "",
    env: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Open + record + finalize a single-step e2e run in one call.

    Returns the finalized meta dict, or ``None`` when the flag is off
    or capture fails. Caller never has to manage run ids.
    """

    if not _flag_enabled(client):
        return None
    try:
        run = _e2e.open_run(client, label=label, base_url=base_url, env=env or {})
        run.log(f"AUTO START {label}")
        run.write_http(
            method=method,
            url=url,
            request_body=request_body,
            response_body=response_body,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            request_headers=request_headers,
            response_headers=response_headers,
        )
        return run.finalize(status="ok" if 200 <= int(status_code or 0) < 400 else "error")
    except Exception:
        _LOG.exception("auto_capture.capture_request_response failed")
        return None


def maybe_capture_run_turn(
    client: Any,
    *,
    request_payload: Any,
    response: Any,
    started_at_ms: float,
) -> Optional[dict[str, Any]]:
    """Capture an ``/agent/run_turn`` round-trip if the env opt-in is set.

    Default behavior is to NOT capture (turns would otherwise spam the
    artifacts directory). Operators flip ``NERYA_E2E_AUTO_CAPTURE_RUN_TURN=1``
    to start recording. Failures are swallowed.
    """

    if not _flag_enabled(client):
        return None
    if not _auto_capture_run_turn_enabled():
        return None
    elapsed_ms = int(max(0.0, (time.time() - float(started_at_ms or 0.0)) * 1000.0))
    return capture_request_response(
        client,
        label="agent.run_turn",
        method="POST",
        url="/agent/run_turn",
        request_body=request_payload,
        response_body=response,
        status_code=200,
        elapsed_ms=elapsed_ms,
        env={"NERYA_E2E_AUTO_CAPTURE_RUN_TURN": "1"},
    )


def capture_dashboard_smoke(
    client: Any,
    *,
    label: str = "dashboard.smoke",
    checks: list[dict[str, Any]] | None = None,
    screenshot_b64: Optional[str] = None,
    dom_html: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Capture a dashboard smoke run as a single artifact bundle.

    ``checks`` is a list of ``{"method","url","status_code","elapsed_ms",
    "request_body","response_body"}`` dicts. Optional screenshot/DOM
    payloads are written as additional steps.
    """

    if not _flag_enabled(client):
        return None
    try:
        run = _e2e.open_run(client, label=label)
        run.log(f"DASHBOARD SMOKE START {label}")
        for check in checks or []:
            run.write_http(
                method=str(check.get("method") or "GET"),
                url=str(check.get("url") or ""),
                request_body=check.get("request_body"),
                response_body=check.get("response_body"),
                status_code=int(check.get("status_code") or 0),
                elapsed_ms=int(check.get("elapsed_ms") or 0),
            )
        if screenshot_b64:
            import base64

            try:
                data = base64.b64decode(screenshot_b64)
                run.write_screenshot(name="dashboard", data=data)
            except Exception:
                _LOG.warning("dashboard smoke screenshot decode failed")
        if dom_html:
            run.write_dom(name="dashboard", html=str(dom_html))
        all_ok = all(
            200 <= int(c.get("status_code") or 0) < 400
            for c in (checks or [])
        )
        return run.finalize(status="ok" if all_ok else "error")
    except Exception:
        _LOG.exception("auto_capture.capture_dashboard_smoke failed")
        return None


__all__ = [
    "capture_request_response",
    "maybe_capture_run_turn",
    "capture_dashboard_smoke",
]

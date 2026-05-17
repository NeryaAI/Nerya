"""HTTP routes for E2E verification artifacts.

Routes:

- ``GET  /ops/e2e/runs``                 - list past runs (with meta)
- ``GET  /ops/e2e/run?id=<run_id>``      - fetch single run meta
- ``POST /ops/e2e/run/record``           - record an HTTP step into a run
- ``POST /ops/e2e/run/finalize``         - finalize an open run

These are intentionally thin so the dashboard and CI helpers can drive
them directly. The full artifact-capture API lives in
:mod:`nerya.ops.e2e_artifacts`.
"""

from __future__ import annotations

from typing import Any

from ..ops import e2e_artifacts as e2e
from ..runtime import feature_flags as ff
from ._envelope import action, blocked, debug_ref, ok, source_ref


_FLAG = "runtime.e2e_artifact_capture"
_OPEN_RUNS: dict[str, e2e.ArtifactRun] = {}


def _gated(client):
    if ff.is_enabled(client, _FLAG):
        return None
    return blocked(
        "E2E artifact capture is disabled via feature flag",
        data={"flag": _FLAG},
        debug_refs=[debug_ref("flag", _FLAG)],
    )


def _list_handler(client, _payload):
    g = _gated(client)
    if g is not None:
        return g
    runs = e2e.list_runs(client)
    return ok(
        f"{len(runs)} run(s) on disk",
        data={"runs": runs, "count": len(runs)},
    )


def _get_handler(client, query):
    g = _gated(client)
    if g is not None:
        return g
    q = query if isinstance(query, dict) else {}
    run_id = str(q.get("id") or q.get("run_id") or "").strip()
    if not run_id:
        return {"ok": False, "error": "id_required", "_status": 400}
    meta = e2e.get_run(client, run_id)
    if meta is None:
        return {"ok": False, "error": "not_found", "id": run_id, "_status": 404}
    return ok(
        f"run {run_id}",
        data={"run": meta},
        source_refs=[source_ref("e2e_run", run_id)],
    )


def _start_handler(client, payload):
    g = _gated(client)
    if g is not None:
        return g
    body = payload or {}
    run = e2e.open_run(
        client,
        label=str(body.get("label") or ""),
        base_url=str(body.get("base_url") or ""),
        env=body.get("env") if isinstance(body.get("env"), dict) else {},
    )
    _OPEN_RUNS[run.run_id] = run
    run.log(f"START {run.label}")
    return ok(
        "run started",
        data={"run_id": run.run_id, "started_at": run.started_at},
    )


def _record_handler(client, payload):
    g = _gated(client)
    if g is not None:
        return g
    body = payload or {}
    run_id = str(body.get("run_id") or "").strip()
    run = _OPEN_RUNS.get(run_id)
    if run is None:
        return {"ok": False, "error": "run_not_found", "run_id": run_id, "_status": 404}
    artifact = run.write_http(
        method=str(body.get("method") or "GET"),
        url=str(body.get("url") or ""),
        request_body=body.get("request_body"),
        response_body=body.get("response_body"),
        status_code=int(body.get("status_code") or 0),
        elapsed_ms=int(body.get("elapsed_ms") or 0),
        request_headers=body.get("request_headers") if isinstance(body.get("request_headers"), dict) else None,
        response_headers=body.get("response_headers") if isinstance(body.get("response_headers"), dict) else None,
    )
    return ok("step recorded", data={"artifact": artifact})


def _finalize_handler(client, payload):
    g = _gated(client)
    if g is not None:
        return g
    body = payload or {}
    run_id = str(body.get("run_id") or "").strip()
    run = _OPEN_RUNS.pop(run_id, None)
    if run is None:
        return {"ok": False, "error": "run_not_found", "run_id": run_id, "_status": 404}
    status = str(body.get("status") or "ok")
    meta = run.finalize(status=status)
    return ok(
        "run finalized",
        data={"run": meta},
        primary_action=action(
            id="open_run",
            label="Open run",
            href=f"/ops/e2e/run?id={run_id}",
        ),
    )


def _auto_capture_handler(client, payload):
    """One-shot auto-capture endpoint used by the dashboard smoke runner.

    Accepts a payload of the form::

        {
          "label": "dashboard.smoke",
          "checks": [
            {"method": "GET", "url": "/healthz", "status_code": 200, ...},
            ...
          ],
          "screenshot_b64": "...",  # optional
          "dom_html": "..."         # optional
        }

    Returns the finalized run meta. Equivalent to start + record + ... +
    finalize but in a single round-trip.
    """

    g = _gated(client)
    if g is not None:
        return g
    body = payload or {}
    from ..ops import auto_capture as ac

    meta = ac.capture_dashboard_smoke(
        client,
        label=str(body.get("label") or "dashboard.smoke"),
        checks=body.get("checks") if isinstance(body.get("checks"), list) else [],
        screenshot_b64=body.get("screenshot_b64"),
        dom_html=body.get("dom_html"),
    )
    if meta is None:
        return {"ok": False, "error": "capture_failed", "_status": 500}
    return ok(
        f"auto-captured run {meta.get('run_id')}",
        data={"run": meta},
        primary_action=action(
            id="open_run",
            label="Open run",
            href=f"/ops/e2e/run?id={meta.get('run_id')}",
        ),
    )


def routes():
    return [
        ("GET", "/ops/e2e/runs", _list_handler),
        ("GET", "/ops/e2e/run", _get_handler),
        ("POST", "/ops/e2e/run/start", _start_handler),
        ("POST", "/ops/e2e/run/record", _record_handler),
        ("POST", "/ops/e2e/run/finalize", _finalize_handler),
        ("POST", "/ops/e2e/auto-capture", _auto_capture_handler),
    ]

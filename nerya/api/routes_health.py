from __future__ import annotations

from ..ops import certification_evidence as _ev
from ..ops.certification import run_gate
from ..ops.preflight import run_preflight


def _preflight(client, params):
    mode = (params or {}).get("mode") or "prod_paper"
    if mode not in ("local_dev", "prod_paper", "canary_live", "full_live"):
        return {"error": f"unknown mode {mode!r}"}
    report = run_preflight(client.config, mode=mode)
    return report.asdict()


def _certify(client, params):
    gate = ((params or {}).get("gate") or "A").upper()
    if gate not in ("A", "B", "C"):
        return {"error": f"unknown gate {gate!r}; want A, B, or C"}
    return run_gate(client.config, gate).asdict()  # type: ignore[arg-type]


def _evidence_list(client, _params):
    return _ev.summary(client.config.paths)


def _evidence_record(client, params):
    params = params or {}
    kind = params.get("kind")
    strategy_id = params.get("strategy_id")
    payload = params.get("payload") or {}
    if not isinstance(kind, str) or kind not in _ev.EVIDENCE_KINDS:
        return {"error": f"invalid kind {kind!r}; "
                         f"want one of {list(_ev.EVIDENCE_KINDS)}"}
    if not isinstance(strategy_id, str) or not strategy_id:
        return {"error": "strategy_id required"}
    if not isinstance(payload, dict):
        return {"error": "payload must be an object"}
    rec = _ev.record(client.config.paths, kind=kind,
                     strategy_id=strategy_id, payload=payload)
    return rec.asdict()


def routes():
    return [
        ("GET", "/health", lambda client, _p: {"status": "ok"}),
        ("GET", "/", lambda client, _p: {"service": "nerya", "status": "ok"}),
        ("GET", "/ops/preflight", _preflight),
        ("GET", "/ops/certify", _certify),
        ("GET", "/ops/evidence", _evidence_list),
        ("POST", "/ops/evidence/record", _evidence_record),
    ]

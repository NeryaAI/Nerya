"""Version-bound workflow checks. No strategy execution, model or exchange calls.

A configuration check, a historical replay and live operation are different
claims. This module never promotes one into another or grants activation.
"""
from __future__ import annotations

import hashlib
import json
import re
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.redaction import redact_display_dict
from .workflow_graph import WorkflowError, package_revision
from .workflow_service import source_files, _safe_root, _read_files
from .validator import validate_proposal_files


def source_revision(files: dict[str, str]) -> str:
    # Presentation-only movements/annotations do not invalidate a code replay.
    return package_revision({k: v for k, v in files.items() if k != "workflow.json"})


def replay_provenance(package: Any, cfg: Any, series: dict[str, dict[str, list]],
                      *, proposal_id: str | None, allow_mock: bool) -> dict[str, Any]:
    files, omitted = _read_files(package.root)
    data = []
    is_sample = False
    for market, by_tf in sorted(series.items()):
        is_sample |= market.split(":", 1)[0].lower() in {"mock", "paper"}
        for timeframe, rows in sorted(by_tf.items()):
            digest = hashlib.sha256()
            stamps = []
            for row in rows:
                digest.update(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode())
                digest.update(b"\n")
                stamps.append(int(row.get("ts", 0)))
                envelope = row.get("_envelope") or {}
                is_sample |= isinstance(envelope, dict) and envelope.get("truth") == "mock"
                is_sample |= bool(row.get("fixture"))
            data.append({"market": market, "timeframe": timeframe, "rows": len(rows),
                "first_ts": stamps[0] if stamps else None, "last_ts": stamps[-1] if stamps else None,
                "duplicate_timestamps": len(stamps) - len(set(stamps)),
                "out_of_order": any(b < a for a, b in zip(stamps, stamps[1:])),
                "sha256": digest.hexdigest()})
    return {"version": 1, "strategy_id": package.strategy_id, "proposal_id": proposal_id,
        "source_revision": source_revision(files) if not omitted else None,
        "omitted_files": omitted, "package_hash": package.content_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_kind": "sample" if is_sample else "unverified" if allow_mock else "historical",
        "allow_mock": allow_mock, "datasets": data,
        "assumptions": {"initial_capital_usd": cfg.initial_capital_usd,
            "fee_bps_by_venue": cfg.fee_bps_by_venue, "slip_bps_by_venue": cfg.slip_bps_by_venue,
            "fill_rule": "next_bar_open; at data end falls back to signal close",
            "warmup_bars": cfg.warmup_bars, "requested_window_days": cfg.window_days},
        "scope": "Historical code replay; no real Agent answers, live fills or out-of-sample claim."}


def _read_report(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    folder = root / "backtests"
    if folder.is_symlink() or not folder.resolve().is_relative_to(root.resolve()):
        return None, ["unsafe_report_directory"]
    if not folder.is_dir():
        return None, []
    for run in sorted(folder.iterdir(), reverse=True)[:50]:
        if run.is_symlink() or not run.is_dir():
            continue
        path = run / "metrics.json"
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            warnings.append("unsafe_report_path"); continue
        try:
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                raw = stream.read(2_000_001)
            if len(raw) > 2_000_000:
                warnings.append("oversized_report"); continue
            metrics = json.loads(raw)
            if not isinstance(metrics, dict):
                warnings.append("invalid_report"); continue
            return {"id": run.name, "metrics": metrics}, warnings
        except (ValueError, OSError):
            warnings.append("unreadable_report")
    return None, warnings


def _datasets_complete(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for row in value:
        if not isinstance(row, dict):
            return False
        if type(row.get("rows")) is not int or row["rows"] < 1:
            return False
        if not isinstance(row.get("market"), str) or not row["market"] or not isinstance(row.get("timeframe"), str) or not row["timeframe"]:
            return False
        if not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"]):
            return False
        if type(row.get("first_ts")) is not int or type(row.get("last_ts")) is not int or row["last_ts"] < row["first_ts"]:
            return False
        if row.get("duplicate_timestamps") != 0 or row.get("out_of_order") is not False:
            return False
    return True


def _window_complete(metrics: dict[str, Any]) -> bool:
    try:
        requested = float(metrics["requested_window_days"])
        actual = float(metrics["backtest_days"])
        frame = re.fullmatch(r"(\d+)([mhd])", str(metrics.get("tf", "")))
        bar_days = int(frame[1]) * {"m":60,"h":3600,"d":86400}[frame[2]] / 86400 if frame else 0
        # Candle opening timestamps omit the final candle duration. Tolerate
        # one candle, never an arbitrary percentage of a missing window.
        return math.isfinite(requested) and math.isfinite(actual) and requested > 0 and actual + bar_days + 1e-9 >= requested
    except (ValueError, TypeError, KeyError):
        return False


def replay_status(report: dict[str, Any] | None, revision: str,
                  strategy_id: str, proposal_id: str | None) -> dict[str, Any]:
    if report is None:
        return {"status": "missing"}
    metrics = report["metrics"]
    provenance = metrics.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    verdict = str(metrics.get("verdict") or "unknown").upper()
    if provenance.get("version") != 1 or not provenance.get("source_revision"):
        status = "unbound"
    elif provenance.get("strategy_id") != strategy_id or provenance.get("proposal_id") != proposal_id:
        status = "unbound"
    elif provenance["source_revision"] != revision or provenance.get("source_changed_during_run"):
        status = "stale"
    elif verdict == "FAIL":
        status = "failed"
    elif provenance.get("data_kind") == "sample":
        status = "sample"
    elif provenance.get("data_kind") != "historical":
        status = "unbound"
    elif (verdict != "PASS" or metrics.get("coverage_ok") is not True
          or not _datasets_complete(provenance.get("datasets"))
          or provenance.get("source_changed_during_run") is not False
          or not _window_complete(metrics)
          or metrics.get("timeframe_fallback") is not False or metrics.get("missing_timeframes")):
        status = "limited"
    else:
        status = "verified"
    keys = ("evaluation_mode", "verdict", "replay", "total_trades", "total_return_pct",
        "max_drawdown_pct", "sharpe_ratio", "total_fees_usd", "total_slippage_usd",
        "start_utc", "end_utc", "backtest_days", "requested_window_days", "coverage_ok", "coverage_message",
        "requested_primary_timeframe", "tf", "requested_timeframes", "timeframes",
        "timeframe_fallback", "missing_timeframes", "flags")
    return {"status": status, "id": report["id"], "provenance": provenance,
            "metrics": {k: metrics[k] for k in keys if k in metrics}}


def check_workflow(paths: Any, strategy_id: str, proposal_id: str | None = None,
                   *, base_revision: str = "", schedules: list | None = None) -> dict[str, Any]:
    files, source = source_files(paths, strategy_id, proposal_id)
    revision = package_revision(files)
    if base_revision and base_revision != revision:
        raise WorkflowError("revision_conflict: reload before checking this version")
    from ..core import yaml_io
    try:
        manifest = yaml_io.loads(files.get("strategy.yml", "")) or {}
    except Exception as exc:
        raise WorkflowError("strategy.yml could not be parsed") from exc
    if not isinstance(manifest, dict) or str(manifest.get("strategy_id") or manifest.get("id")) != strategy_id:
        raise WorkflowError("Manifest strategy ID does not match the requested package")
    # No importing or invoking user code. Existing full validator keeps its default.
    validation = validate_proposal_files(strategy_id=strategy_id, files=files, smoke_test=False).asdict()
    if source["omitted_files"]:
        validation["ok"] = False
        validation["blockers"].append({"code": "omitted_files", "where": "", "message": "Some source files could not be checked"})
    code_revision = source_revision(files)
    target_root = (_safe_root(_safe_root(paths.proposals, proposal_id) / "after" / "strategies", strategy_id)
                   if proposal_id else _safe_root(paths.strategies, strategy_id))
    report, warnings = _read_report(target_root)
    replay = replay_status(report, code_revision, strategy_id, proposal_id)
    if warnings and replay["status"] == "verified":
        replay["status"] = "limited"
    raw_sources = manifest.get("data_sources") or []
    if isinstance(raw_sources, dict):
        raw_sources = [dict(v, id=k) for k, v in raw_sources.items() if isinstance(v, dict)]
    if not isinstance(raw_sources, list):
        raw_sources = []
    sources = [{k: row[k] for k in ("id", "title", "provider", "capability", "markets", "market", "timeframe", "timeframes", "limit") if k in row}
               for row in raw_sources if isinstance(row, dict)]
    jobs = []
    for job in schedules or []:
        if not isinstance(job, dict):
            continue
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if str(job.get("strategy_id") or payload.get("strategy_id") or "") == strategy_id:
            jobs.append({k: job[k] for k in ("id", "enabled", "paused", "kind", "target", "cron", "every_seconds", "timezone") if k in job})
    operation = "candidate" if proposal_id else "unknown" if schedules is None else "scheduled" if any(j.get("enabled") is True and j.get("paused") is not True for j in jobs) else "paused" if jobs else "not_installed"
    latest, _ = source_files(paths, strategy_id, proposal_id)
    if package_revision(latest) != revision:
        raise WorkflowError("revision_conflict: source changed during check")
    result = redact_display_dict({"ok": True, "schema": "workflow-check/v1",
        "target": {"strategy_id": strategy_id, "proposal_id": proposal_id, "state": source["state"],
                   "revision": revision, "source_revision": code_revision, "checked_at": datetime.now(timezone.utc).isoformat()},
        "validation": {**validation, "scope": "schema_and_ast_only"},
        "replay": replay, "report_warnings": warnings, "sources": sources,
        "operation": {"state": operation, "installed_schedules": [] if proposal_id else jobs},
        "mode": manifest.get("mode"), "evaluation_mode": (manifest["evaluation"].get("mode", "trading") if isinstance(manifest.get("evaluation"), dict) else "trading"),
        "next_step": "repair" if not validation["ok"] else "replay" if replay["status"] != "verified" else "paper_observation",
        "unverified": ["branch_coverage", "real_agent_analysis", "out_of_sample", "forward_paper_run", "live_execution"]})
    # Public content digests are generated evidence identifiers, not credentials.
    # Restore only these typed fields; arbitrary metadata stays redacted.
    result["target"].update(revision=revision, source_revision=code_revision)
    original_provenance = replay.get("provenance", {})
    public_provenance = result["replay"].get("provenance", {})
    for key in ("source_revision", "package_hash"):
        digest = original_provenance.get(key)
        if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
            public_provenance[key] = digest
    raw_datasets = original_provenance.get("datasets")
    if isinstance(raw_datasets, list):
        for before, after in zip(raw_datasets, public_provenance.get("datasets", [])):
            if isinstance(before, dict) and isinstance(after, dict) and isinstance(before.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", before["sha256"]):
                after["sha256"] = before["sha256"]
    return result

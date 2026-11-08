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
from types import SimpleNamespace
from typing import Any

from ..core.redaction import redact_display_dict
from .workflow_graph import WorkflowError, package_revision
from .workflow_service import source_files, _safe_root, _read_files
from .validator import validate_proposal_files
from ..core.runtime_identity import BUILD_ID, SDK_BUILD_ID


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
                is_sample |= isinstance(envelope, dict) and (envelope.get("truth") == "mock" or envelope.get("mode") in {"mock", "paper", "sample"})
                is_sample |= bool(row.get("fixture"))
            data.append({"market": market, "timeframe": timeframe, "rows": len(rows),
                "first_ts": stamps[0] if stamps else None, "last_ts": stamps[-1] if stamps else None,
                "duplicate_timestamps": len(stamps) - len(set(stamps)),
                "out_of_order": any(b < a for a, b in zip(stamps, stamps[1:])),
                "sha256": digest.hexdigest()})
    return {"version": 1, "strategy_id": package.strategy_id, "proposal_id": proposal_id,
        "runtime_build_id": BUILD_ID, "sdk_build_id": SDK_BUILD_ID,
        "source_revision": source_revision(files) if not omitted else None,
        "omitted_files": omitted, "package_hash": package.content_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_kind": "sample" if is_sample else "unverified" if allow_mock else "historical",
        "allow_mock": allow_mock, "datasets": data,
        "assumptions": {"initial_capital_usd": cfg.initial_capital_usd,
            "fee_bps_by_venue": cfg.fee_bps_by_venue, "slip_bps_by_venue": cfg.slip_bps_by_venue,
            "fill_rule": "next_available_bar_open; final strategy intents rejected without a next bar; remaining positions liquidated separately at last close",
            "warmup_bars": cfg.warmup_bars, "requested_window_days": cfg.window_days},
        "scope": "Historical code replay; no real Agent answers, live fills or out-of-sample claim."}


def replay_run_sort_key(path: Path) -> tuple[float, int]:
    """Prefixes such as repair_ and run-1_ are not chronological order."""
    match = re.search(r"(\d{8}_\d{6})$", path.name)
    try:
        modified = path.stat().st_mtime_ns if not path.is_symlink() else 0
    except OSError:
        modified = 0
    timestamp = modified / 1_000_000_000
    if match:
        try:
            timestamp = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return (timestamp, modified)


def _read_report(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    folder = root / "backtests"
    if folder.is_symlink() or not folder.resolve().is_relative_to(root.resolve()):
        return None, ["unsafe_report_directory"]
    if not folder.is_dir():
        return None, []
    for run in sorted(folder.iterdir(), key=replay_run_sort_key, reverse=True)[:50]:
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


def replay_scope_receipt(config, binding, *, replay=None, command_id=None):
    """Check pool-price bytes without making producer defaults user constraints."""
    from .request_scope import (ScopeError, check_receipt, current_contract,
                                target_manifest, _price_simulation_receipt, _timestamp, compare)

    if not replay or not (replay.get("provenance") or {}).get("price_simulation"):
        return check_receipt(config, binding, replay=replay, command_id=command_id)
    try:
        contract = current_contract(config, command_id=command_id)
        if contract is None:
            return None
        _, sid, _, pid = binding["artifact_id"].split(":")
        args = {"strategy_id": sid, "proposal_id": None if pid == "installed" else pid}
        manifest = target_manifest(config, args)
        constraints = dict(contract["constraints"])
        window = replay.get("data_manifest") or {}
        for key in ("start_utc", "end_utc"):
            if key not in constraints:
                constraints[key] = _timestamp(window.get("requested_" + key)
                                              or replay.get("requested_" + key))
        if constraints["end_utc"] <= constraints["start_utc"]:
            raise ScopeError()
        # Only the local byte-check context gains the producer's default window.
        # The admitted contract and its evidence hash remain unchanged.
        context = {**contract, "constraints": constraints}
        hashes = _price_simulation_receipt(context, manifest, replay, config, args=args)
        compare(context, manifest, replay=replay, config=config)
        return {"status": "passed", "request_scope_hash": contract["request_scope_hash"],
                "source_revision": binding["revision"], "reason": "",
                "differences": [], "prepared_inputs": hashes}
    except Exception as exc:
        return {"status": "failed" if isinstance(exc, ScopeError) and exc.reason == "request_scope_mismatch" else "missing",
                "request_scope_hash": "", "source_revision": binding["revision"],
                "reason": exc.reason if isinstance(exc, ScopeError) else "request_scope_unverified",
                "differences": exc.differences if isinstance(exc, ScopeError) else []}


def _completed_rate_receipt(root: Path, report: dict[str, Any], revision: str,
                            strategy_id: str, proposal_id: str | None) -> dict[str, Any] | None:
    # Lazy import: tool_verification imports source_revision from this module.
    # Its output attestor only needs a read boundary, not runtime configuration.
    from .tool_verification import _output_path, _rate_snapshots_attested

    metrics = report["metrics"]
    provenance = metrics.get("provenance")
    replay = metrics.get("replay")
    scope = "historical_rate_policy_simulation"
    if (not isinstance(provenance, dict) or not isinstance(replay, dict)
            or provenance.get("version") != 1
            or provenance.get("source_revision") != revision
            or provenance.get("sdk_build_id") != SDK_BUILD_ID
            or provenance.get("strategy_id") != strategy_id
            or provenance.get("proposal_id") != proposal_id
            or provenance.get("source_changed_during_run") is not False
            or provenance.get("data_kind") != "historical_observations"
            or provenance.get("scope") != scope
            or metrics.get("backtest_kind") != "rate_policy_replay"
            or metrics.get("evaluation_mode") != "simulation"
            or metrics.get("performance_evidence") is not False
            or replay.get("scope") != scope
            or replay.get("strategy_execution_verified") is not True
            or replay.get("financial_execution_allowed") is not False
            or replay.get("errors") != 0 or replay.get("sdk_errors") != 0
            or replay.get("order_accounting_ok") is not True):
        return None
    isolation = provenance.get("isolation")
    if (not isinstance(isolation, dict) or isolation.get("source_revision") != revision
            or isolation.get("network") != "denied" or isolation.get("process_spawn") != "denied"
            or isolation.get("process_exited") is not True
            or isolation.get("process_group_stopped") is not True):
        return None
    if not isinstance(report.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", report["id"]):
        return None
    config = SimpleNamespace(paths=SimpleNamespace(root=root.resolve()))
    try:
        run = _output_path(config, f"backtests/{report['id']}/run.json")
        if not run.is_file() or not 0 < run.stat().st_size <= 100_000:
            return None
        state = json.loads(run.read_text())
        if (not isinstance(state, dict) or state.get("status") != "completed"
                or state.get("preflight_only")
                or state.get("strategy_id") != strategy_id
                or state.get("proposal_id") != proposal_id
                or state.get("backtest_ts") != report["id"]
                or state.get("source_revision") != revision
                or state.get("performance_evidence") is not False):
            return None
        binding = {"artifact_id": f"strategy:{strategy_id}:proposal:{proposal_id or 'installed'}",
                   "revision": revision}
        data = {"run_receipt": str(run), "report_path": str(run.with_name("report.md")),
                "provenance": provenance}
        if not _rate_snapshots_attested(config, data, binding):
            return None
    except (OSError, ValueError, TypeError, UnicodeError):
        return None
    return {"status": "completed", "operation_completed": True,
            "strategy_id": strategy_id, "proposal_id": proposal_id,
            "backtest_ts": report["id"], "source_revision": revision,
            "scope": scope, "performance_evidence": False,
            "requested_window_complete": metrics.get("requested_window_complete") is True,
            "requested_window_days": metrics.get("requested_window_days"),
            "verdict": metrics.get("verdict"), "start_utc": metrics.get("start_utc"),
            "end_utc": metrics.get("end_utc"), "tf": metrics.get("tf"),
            "metrics_path": str(run.with_name("metrics.json")), "run_receipt": str(run)}


def completed_replay_receipt(root: Path, strategy_id: str, proposal_id: str | None) -> dict[str, Any] | None:
    """A finished calculation is evidence even when its economic verdict is FAIL.

    Used by authoring tools to avoid requesting the same replay indefinitely.
    This does not approve trading, waive a new requested window or certify profit.
    """
    files, omitted = _read_files(root)
    if omitted:
        return None
    report, warnings = _read_report(root)
    if report is None or warnings:
        return None
    metrics = report["metrics"]
    provenance = metrics.get("provenance") or {}
    if not isinstance(provenance, dict):
        return None
    revision = source_revision(files)
    if metrics.get("backtest_kind") == "rate_policy_replay":
        return _completed_rate_receipt(root, report, revision, strategy_id, proposal_id)
    if (provenance.get("source_revision") != revision
            or provenance.get("sdk_build_id") != SDK_BUILD_ID
            or provenance.get("strategy_id") != strategy_id
            or provenance.get("proposal_id") != proposal_id
            or provenance.get("source_changed_during_run") is not False
            or provenance.get("data_kind") != "historical"
            or metrics.get("requested_window_complete") is not True
            or not _datasets_complete(provenance.get("datasets"))):
        return None
    run_path = root / "backtests" / report["id"]
    state_path = run_path / "run.json"
    if state_path.is_symlink() or not state_path.is_file() or state_path.stat().st_size > 100_000:
        return None
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("status") != "completed" or state.get("preflight_only"):
        return None
    replay = metrics.get("replay") or {}
    if not isinstance(replay, dict):
        return None
    if replay.get("errors") or replay.get("sdk_errors") or replay.get("order_accounting_ok") is False:
        return None
    result = {"status": "completed", "strategy_id": strategy_id, "proposal_id": proposal_id,
            "backtest_ts": report["id"], "source_revision": revision,
            "verdict": metrics.get("verdict"), "requested_window_days": metrics.get("requested_window_days"),
            "requested_window_complete": True, "start_utc": metrics.get("start_utc"),
            "end_utc": metrics.get("end_utc"), "tf": metrics.get("tf"),
            "metrics_path": str(run_path / "metrics.json"), "run_receipt": str(state_path)}
    if replay.get("scope") == "historical_pool_price_simulation":
        result.update(scope="historical_pool_price_simulation", performance_evidence=False)
    return result


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
                  strategy_id: str, proposal_id: str | None, *, root: Path | None = None) -> dict[str, Any]:
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
    elif provenance.get("data_kind") == "historical_observations":
        # Rate observations never certify trading performance, even with full coverage.
        status = "limited"
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
        "timeframe_fallback", "missing_timeframes", "flags", "backtest_kind",
        "requested_window_complete", "performance_evidence", "limitations")
    result = {"status": status, "id": report["id"], "provenance": provenance,
            "metrics": {k: metrics[k] for k in keys if k in metrics}}
    if metrics.get("backtest_kind") == "rate_policy_replay":
        receipt = None
        if root is not None and status == "limited":
            files, omitted = _read_files(root)
            if not omitted and source_revision(files) == revision:
                receipt = _completed_rate_receipt(root, report, revision, strategy_id, proposal_id)
        result.update(operation_completed=receipt is not None,
                      requested_window_complete=metrics.get("requested_window_complete") is True,
                      scope="historical_rate_policy_simulation", performance_evidence=False)
    return result


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
    replay = replay_status(report, code_revision, strategy_id, proposal_id, root=target_root)
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

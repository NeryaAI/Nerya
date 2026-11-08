"""Adapt existing strategy source/replay evidence to the common check receipt.

There is no second validator or approval lane here. Snapshot identities come
from the same safe source reader and source_revision used by workflow checks.
"""
from __future__ import annotations

from typing import Any
import re
import json
import math
from contextlib import closing
from pathlib import Path

from ..tools.result_contracts import tool_json_data
from ..tools.types import ToolCall, ToolResult, ToolResultPart
from ..tools.verification import attach_verification, verification_receipt
from .verification import source_revision, _datasets_complete, replay_scope_receipt
from .workflow_service import source_files
from ..db.sqlite import connect_readonly
from ..tools.verification import read_verification_receipt


def current_command_delivery_receipts(config, command_id: str, session_id: str):
    """Read host metadata from this command and its verified decision chain."""
    from ..task_delivery import DeliveryError, delivery_command_chain
    allowed = {"strategy_backtest", "strategy_validate",
               "strategy_draft_proposal", "strategy_submit_proposal", "research_verify"}
    receipts = []
    with closing(connect_readonly(config.paths.db)) as con:
        con.execute("BEGIN")
        chain = delivery_command_chain(config.paths, con, command_id)
        if chain[0]["session_id"] != session_id:
            raise DeliveryError("command_not_found", 404)
        for command in reversed(chain):
            rows = con.execute("SELECT payload_json FROM agent_command_events WHERE command_id=? ORDER BY id", (command["command_id"],))
            for row in rows:
                try:
                    event = json.loads(row[0])
                except (ValueError, TypeError):
                    continue
                if (not isinstance(event, dict) or event.get("kind") != "tool.complete"
                        or event.get("session_id") != session_id or event.get("turn_id") != command["turn_id"]
                        or event.get("action") not in allowed or event.get("skill_id", "native") != "native"):
                    continue
                receipt = read_verification_receipt(event.get("verification"))
                if receipt and (
                        event.get("action") == "research_verify"
                        and receipt["artifact_id"] == "research:" + command["command_id"]
                        or event.get("action") != "research_verify"
                        and re.fullmatch(r"strategy:[^:]+:proposal:[^:]+", receipt["artifact_id"])):
                    checks = {check["check_id"]: check for check in receipt["checks"]}
                    if (checks.get("historical_data", {}).get("status") == "missing"
                            and all(checks.get(key, {}).get("status") == "passed"
                                    for key in ("historical_run", "report_output"))):
                        # Reconcile the saved producer output, never repeat its operation.
                        try:
                            run = _output_path(config, checks["historical_run"]["evidence_ref"])
                            data = {**json.loads(run.with_name("metrics.json").read_text()),
                                    "run_receipt": str(run),
                                    "report_path": checks["report_output"]["evidence_ref"]}
                            if _rate_snapshots_attested(config, data, receipt):
                                checks["historical_data"]["status"] = "passed"
                        except (OSError, ValueError, TypeError):
                            pass
                    receipts.append(receipt)
    checks = [{"artifact_id": receipt["artifact_id"], "producer_owned": True} for receipt in receipts]
    versions = delivery_source_versions(config, {"command_id": command_id, "session_id": session_id, "checks": checks})
    return receipts, versions


def source_binding(config: Any, call: ToolCall) -> dict[str, str]:
    args = call.arguments or {}
    sid = str(args.get("strategy_id") or "")
    pid = str(args.get("proposal_id") or "")
    revision = "unbound"
    try:
        if pid and not sid:
            from .proposal_files import read_proposal_strategy_files
            sid, _ = read_proposal_strategy_files(config.paths, pid)
        files, source = source_files(config.paths, sid, pid or None)
        if files and not source.get("omitted_files"):
            revision = source_revision(files)
    except Exception:
        # The operation still owns its error contract. A missing/unreadable
        # snapshot must reduce verification, not manufacture source identity.
        pass
    return {"artifact_id": f"strategy:{sid or 'unknown'}:proposal:{pid or 'installed'}", "revision": revision}


def _output_path(config, value: Any) -> Path:
    """Host output only: bound to this workspace, with no symlink aliases."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing_output")
    root = config.paths.root.resolve()
    path = Path(value)
    path = path if path.is_absolute() else root / path
    relative = path.relative_to(root)
    if ".." in relative.parts or any((root / Path(*relative.parts[:i])).is_symlink()
                                     for i in range(1, len(relative.parts) + 1)):
        raise ValueError("unsafe_output")
    return path


def _candidate_ref(config, binding: dict) -> str:
    match = re.fullmatch(r"strategy:([A-Za-z0-9_-]+):proposal:([A-Za-z0-9_-]+)", binding["artifact_id"])
    if config is None or not match or binding["revision"] == "unbound":
        return ""
    sid, pid = match.groups()
    ref = f"strategies/{sid}" if pid == "installed" else f"evolution/proposals/{pid}/after/strategies/{sid}"
    try:
        path = _output_path(config, ref)
        current = source_binding(config, ToolCall(name="strategy_validate",
            arguments={"strategy_id": sid, "proposal_id": "" if pid == "installed" else pid}))
        return ref if path.is_dir() and current == binding else ""
    except (OSError, ValueError):
        return ""


def _replay_output_refs(config, data: dict, binding: dict) -> tuple[str, str]:
    """Attest this producer run's persisted outputs, never delivery tool paths."""
    if config is None:
        return "", ""
    try:
        run = _output_path(config, data.get("run_receipt"))
        report = _output_path(config, data.get("report_path"))
        metrics = _output_path(config, str(run.with_name("metrics.json")))
        if run.name != "run.json" or report.parent != run.parent:
            return "", ""
        if any(not path.is_file() or not 0 < path.stat().st_size <= 16_000_000
               for path in (run, report, metrics)):
            return "", ""
        receipt = json.loads(run.read_text(encoding="utf-8"))
        saved = json.loads(metrics.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or not isinstance(saved, dict):
            return "", ""
        identity = f"strategy:{receipt.get('strategy_id')}:proposal:{receipt.get('proposal_id') or 'installed'}"
        provenance = saved.get("provenance")
        if (receipt.get("status") != "completed" or receipt.get("preflight_only")
                or identity != binding["artifact_id"] or not isinstance(provenance, dict)
                or provenance.get("source_revision") != binding["revision"]
                or provenance.get("source_changed_during_run") is not False):
            return "", ""
        root = config.paths.root.resolve()
        return run.relative_to(root).as_posix(), report.relative_to(root).as_posix()
    except (OSError, ValueError, UnicodeError):
        return "", ""


def recorded_replay_observations(paths, command) -> list[dict]:
    """Bound historical facts to producer hashes, without issuing new checks."""
    import hashlib
    from types import SimpleNamespace
    from ..tools.verification import read_verification_receipt

    config, observations = SimpleNamespace(paths=paths), []
    try:
        blocks = json.loads(command["result_json"] or "{}").get("blocks", [])
        for envelope in blocks:
            block = envelope.get("block", envelope)
            if (block.get("kind") != "tool_result" or block.get("action") != "strategy_backtest"
                    or block.get("ok") is not True
                    or envelope.get("turn_id", command["turn_id"]) != command["turn_id"]):
                continue
            receipt = read_verification_receipt(block.get("verification"))
            if not receipt:
                continue
            checks = {check["check_id"]: check for check in receipt["checks"]}
            scope = checks.get("request_scope_match", {})
            proof = scope.get("evidence_ref", "").split(":", 4)
            if (scope.get("status") != "passed" or len(proof) != 5
                    or proof[:2] != ["request_scope", command["command_id"]]
                    or not all(re.fullmatch(r"[a-f0-9]{64}", part) for part in proof[2:4])
                    or any(checks.get(key, {}).get("status") != "passed"
                           for key in ("historical_run", "report_output"))):
                continue
            binding = {key: receipt[key] for key in ("artifact_id", "revision")}
            data = {"run_receipt": checks["historical_run"]["evidence_ref"],
                    "report_path": checks["report_output"]["evidence_ref"]}
            run_ref, _ = _replay_output_refs(config, data, binding)
            metrics_path = _output_path(config, proof[4])
            if not run_ref or metrics_path != _output_path(config, run_ref).with_name("metrics.json"):
                continue
            raw = metrics_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != proof[3]:
                continue
            metrics = json.loads(raw)

            def fields(value, keys):
                if not isinstance(value, dict):
                    return {}
                return {key: value[key] for key in keys if key in value and (
                    type(value[key]) is bool or value[key] is None
                    or isinstance(value[key], (int, float)) and math.isfinite(value[key])
                    or isinstance(value[key], str) and len(value[key]) <= 128)}

            gaps = metrics.get("gaps") or []
            if not isinstance(gaps, list):
                continue
            observations.append({
                **binding, "command_id": command["command_id"],
                "metrics_ref": metrics_path.relative_to(paths.root.resolve()).as_posix(),
                "metrics_sha256": proof[3],
                "scope": "Historical recorded observation, not current verification or replay permission. Raw acquisition offsets are not additional modeled exclusions.",
                **fields(metrics, ("requested_start_utc", "requested_end_utc", "coverage_ok",
                                   "requested_window_complete", "performance_evidence")),
                "modeled_coverage": fields(metrics.get("modeled_coverage"),
                    ("scope", "covered_seconds", "requested_seconds", "complete")),
                "excluded_gaps": [fields(gap, ("start", "end", "seconds")) for gap in gaps[:8]],
                "gaps_truncated": len(gaps) > 8,
                **fields((metrics.get("provenance") or {}).get("assumptions"), ("max_gap_seconds",)),
                "raw_acquisition": fields(metrics.get("observation_coverage"),
                    ("completeness", "observations", "leading_gap_seconds", "trailing_gap_seconds")),
            })
            if len(observations) == 2:
                break
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass
    return observations


def _pool_price_snapshots_attested(config, data: dict, binding: dict) -> bool:
    """Bind native pool-price replay to its persisted public dataset."""
    import hashlib
    try:
        run_ref, _ = _replay_output_refs(config, data, binding)
        if not run_ref:
            return False
        metrics = json.loads(_output_path(config, run_ref).with_name('metrics.json').read_text())
        provenance, replay = metrics.get('provenance', {}), metrics.get('replay', {})
        proof = provenance.get('price_simulation', {})
        receipts = provenance.get('prepared_datasets', [])
        if (replay.get('scope') != 'historical_pool_price_simulation'
                or replay.get('strategy_execution_verified') is not True
                or metrics.get('performance_evidence') is not False
                or provenance != data.get('provenance') or replay != data.get('replay')
                or len(receipts) != 1 or proof.get('prepared_input_sha256') != receipts[0].get('sha256')):
            return False
        receipt = receipts[0]
        if receipt.get('snapshot_file') != 'prepared_inputs/' + receipt['id'] + '.json':
            return False
        path = _output_path(config, str(_output_path(config, run_ref).parent / receipt['snapshot_file']))
        if not 0 < path.stat().st_size <= 4_000_000:
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get('sha256'):
            return False
        scope = replay_scope_receipt(config, binding, replay={**metrics, 'run_receipt': run_ref})
        return bool(scope and scope.get('status') == 'passed')
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _rate_snapshots_attested(config, data: dict, binding: dict) -> bool:
    """Verify saved public observations, independently of window coverage."""
    import hashlib

    try:
        run_ref, _ = _replay_output_refs(config, data, binding)
        if not run_ref:
            return False
        run = _output_path(config, run_ref)
        saved = json.loads(run.with_name("metrics.json").read_text())
        provenance = saved.get("provenance", {})
        replay = saved.get("replay", {})
        datasets = provenance.get("datasets")
        sampled_lp = replay.get("scope") == "sampled_fixed_range_counterfactual"
        if (not (sampled_lp or (saved.get("backtest_kind") == "rate_policy_replay"
                              and replay.get("scope") == "historical_rate_policy_simulation"))
                or provenance.get("data_kind") != "historical_observations"
                or provenance != data.get("provenance")
                or replay.get("strategy_execution_verified") is not True
                or not isinstance(datasets, list) or len(datasets) != 1):
            return False
        row = datasets[0]
        if (not isinstance(row, dict) or row.get("data_kind") != "historical_observations"
                or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", row.get("id", ""))
                or row.get("snapshot_file") != "prepared_inputs/" + row["id"] + ".json"):
            return False
        snapshot = _output_path(config, str(run.parent / row["snapshot_file"]))
        if not 0 < snapshot.stat().st_size <= 4_000_000:
            return False
        blob = snapshot.read_bytes()
        payload = json.loads(blob)
        envelope = payload.get("_envelope", {})
        sources = envelope.get("sources")
        observation_kind = "historical_lp_pool_state" if sampled_lp else "historical_rate_observations"
        if not sampled_lp and replay.get("accrual_basis") == "aave_normalized_income":
            if any(value.get("provider") != "aave" or value.get("action") != "reserve_index_history"
                   for value in (row, envelope)):
                return False
            observation_kind = "historical_reserve_index_observations"
        if sampled_lp:
            from ..core import yaml_io
            sid, pid = binding["artifact_id"].split(":")[1::2]
            files, source = source_files(config.paths, sid, None if pid == "installed" else pid)
            manifest = yaml_io.loads(files.get("strategy.yml", "")) or {}
            rows_hash = hashlib.sha256(json.dumps(payload.get("rows"), sort_keys=True,
                separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
            if (source.get("omitted_files") or source_revision(files) != binding["revision"]
                    or replay != data.get("replay") or replay.get("accounting_verified") is not True
                    or replay.get("entrypoint") != manifest.get("entrypoint")
                    or not replay.get("entrypoint")
                    or replay.get("dataset_id") != row["id"]
                    or replay.get("prepared_input_sha256") != row.get("sha256")
                    or replay.get("rows_sha256") != envelope.get("rows_sha256")
                    or replay.get("rows_sha256") != rows_hash
                    or type(replay.get("input_observations")) is not int
                    or replay["input_observations"] != row.get("rows")
                    or any(type(replay.get(k)) is not int or replay[k] < 0
                           for k in ("admitted_observations", "excluded_observations"))
                    or replay["admitted_observations"] + replay["excluded_observations"] != row.get("rows")
                    or row.get("provider") != "uniswap" or row.get("action") != "pool_history"):
                return False
        return (hashlib.sha256(blob).hexdigest() == row.get("sha256")
                and envelope.get("mock") is False
                and envelope.get("observation_kind") == observation_kind
                and isinstance(payload.get("rows"), list)
                and type(row.get("rows")) is int and 2 <= row["rows"] <= (64 if sampled_lp else 500)
                and len(payload["rows"]) == row["rows"]
                and isinstance(sources, list) and bool(sources)
                and sources == row.get("sources")
                and all(isinstance(source, dict) and isinstance(source.get("sha256"), str)
                        and re.fullmatch(r"[a-f0-9]{64}", source["sha256"]) for source in sources))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _lp_cadence_snapshots_attested(config, data: dict, binding: dict) -> bool:
    """Rebuild saved cadence evidence locally before admitting full coverage."""
    import hashlib
    from ..core import yaml_io

    try:
        from ..skills.builtin.backtest.scripts.lp_replay import cadence_receipt
        from ..skills.builtin.backtest.scripts.lp_state_requirements import prepare_lp_cadence
        run_ref, _ = _replay_output_refs(config, data, binding)
        if not run_ref:
            return False
        run = _output_path(config, run_ref)
        saved = json.loads(run.with_name("metrics.json").read_text())
        provenance, replay = saved.get("provenance", {}), saved.get("replay", {})
        datasets = provenance.get("datasets")
        if (replay.get("scope") != "cadence_fixed_range_counterfactual"
                or provenance.get("data_kind") != "historical_observations"
                or provenance != data.get("provenance") or replay != data.get("replay")
                or any(replay.get(k) is not True for k in ("accounting_verified",
                    "strategy_execution_verified", "requested_window_complete"))
                or saved.get("requested_window_complete") is not True
                or data.get("requested_window_complete") is not True
                or not isinstance(datasets, list) or len(datasets) != 1):
            return False
        row = datasets[0]
        if (not isinstance(row, dict) or row.get("provider") != "uniswap"
                or row.get("action") != "pool_history" or row.get("data_kind") != "historical_observations"
                or row.get("requested_window_complete") is not True
                or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", row.get("id", ""))
                or row.get("snapshot_file") != "prepared_inputs/" + row["id"] + ".json"):
            return False
        snapshot = _output_path(config, str(run.parent / row["snapshot_file"]))
        if not 0 < snapshot.stat().st_size <= 4_000_000:
            return False
        blob = snapshot.read_bytes()
        sid, pid = binding["artifact_id"].split(":")[1::2]
        files, source = source_files(config.paths, sid, None if pid == "installed" else pid)
        manifest = yaml_io.loads(files.get("strategy.yml", "")) or {}
        if (source.get("omitted_files") or source_revision(files) != binding["revision"]
                or not replay.get("entrypoint") or replay["entrypoint"] != manifest.get("entrypoint")
                or replay.get("dataset_id") != row["id"]
                or row["id"] != manifest["params"]["lp_replay"]["dataset_id"]):
            return False
        target = {"strategy_id": sid} if pid == "installed" else {"proposal_id": pid}
        prepared = prepare_lp_cadence(config, target)
        receipt = cadence_receipt(prepared)
        counts = {"input_observations": len(prepared["observations"]),
            "admitted_observations": len(prepared["observations"]), "excluded_observations": 0,
            "decision_count": len(prepared["decision_timestamps"]), "interval_count": len(prepared["intervals"])}
        # Request-scope validation owns catalog identity; the byte hash below
        # still binds its envelope along with the reconstructed cadence receipt.
        compact = {k: v for k, v in json.loads(blob).items() if k != "_envelope"}
        return (compact == receipt and receipt["protocol"] == "nerya.lp_cadence.v1"
                and receipt["source_revision"] == prepared["source_revision"] == binding["revision"]
                and replay.get("cadence_sha256") == receipt["prepared_sha256"]
                and replay.get("prepared_input_sha256") == row.get("sha256") == hashlib.sha256(blob).hexdigest()
                and type(row.get("rows")) is int and row["rows"] == counts["input_observations"]
                and all(type(replay.get(k)) is int and replay[k] == v for k, v in counts.items()))
    except (ImportError, OSError, ValueError, TypeError, AttributeError, KeyError):
        return False


def _visualization_output_ref(config, data: dict, binding: dict) -> str:
    """Check the persisted producer format, never hydrated/browser chart blocks."""
    try:
        run_ref, _ = _replay_output_refs(config, data, binding)
        if not run_ref:
            return ""
        run = _output_path(config, run_ref)
        receipt = json.loads(run.read_text(encoding="utf-8"))
        chart_path = _output_path(config, str(run.with_name("chart.json")))
        if not chart_path.is_file() or not 0 < chart_path.stat().st_size <= 16_000_000:
            return ""
        chart = json.loads(chart_path.read_text(encoding="utf-8"))
        meta = chart.get("meta")
        if (receipt.get("backtest_ts") != run.parent.name
                or _output_path(config, receipt.get("run_path")) != run.parent
                or chart.get("schema_version") != "1.0" or not isinstance(meta, dict)
                or meta.get("backtest_ts") != receipt["backtest_ts"]
                or meta.get("strategy_id") != receipt["strategy_id"]):
            return ""

        def number(value):
            return type(value) in (int, float) and math.isfinite(value)

        panels, tables = chart.get("panels"), chart.get("tables")
        if not isinstance(panels, list) or not panels or not isinstance(tables, list):
            return ""
        seen, has_curve = set(), False
        for panel in panels:
            if (not isinstance(panel, dict) or not isinstance(panel.get("id"), str)
                    or not panel["id"] or panel["id"] in seen
                    or panel.get("type") not in {"line", "area", "candlestick", "overlay_spans"}
                    or not isinstance(panel.get("series"), list)):
                return ""
            seen.add(panel["id"])
            for series in panel["series"]:
                if (not isinstance(series, dict) or series.get("kind") not in {"line", "area", "candles", "markers"}
                        or not isinstance(series.get("data"), list)):
                    return ""
                previous = None
                for point in series["data"]:
                    if not isinstance(point, dict) or not number(point.get("time")):
                        return ""
                    when = point["time"]
                    if when <= 0 or (previous is not None and (when < previous or
                            (when == previous and series["kind"] != "markers"))):
                        return ""
                    previous = when
                    fields = ("open", "high", "low", "close") if series["kind"] == "candles" else ("value",)
                    if series["kind"] != "markers" and not all(number(point.get(key)) for key in fields):
                        return ""
                    if series["kind"] == "candles" and (point["low"] > min(point["open"], point["close"])
                            or point["high"] < max(point["open"], point["close"]) or point["low"] > point["high"]):
                        return ""
                    if series["kind"] == "markers" and (point.get("position") not in {"aboveBar", "belowBar", "inBar"}
                            or point.get("shape") not in {"arrowUp", "arrowDown", "circle", "square"}):
                        return ""
                has_curve |= panel["id"] == "equity" and bool(series["data"]) and series["kind"] in {"line", "area"}
        seen.clear()
        for table in tables:
            if (not isinstance(table, dict) or not isinstance(table.get("id"), str)
                    or not table["id"] or table["id"] in seen
                    or not isinstance(table.get("columns"), list) or not isinstance(table.get("rows"), list)):
                return ""
            columns = table["columns"]
            if (any(not isinstance(column, str) or not column for column in columns)
                    or len(set(columns)) != len(columns)
                    or any(not isinstance(row, list) or len(row) != len(columns) or not columns
                           or any(cell is not None and type(cell) not in (str, bool) and not number(cell) for cell in row)
                           for row in table["rows"])):
                return ""
            seen.add(table["id"])
        return chart_path.relative_to(config.paths.root.resolve()).as_posix() if has_curve and "trades" in seen else ""
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return ""


def attach_strategy_checks(result: ToolResult, *, before: dict[str, str], after: dict[str, str],
                           kind: str, preflight_only: bool = False, config: Any = None,
                           merge_verification: dict | None = None) -> ToolResult:
    data = tool_json_data(result) or (result.error.recovery_hint if result.error else {})
    if kind == "validation" and isinstance(data.get("validation"), dict):
        data = data["validation"]
    stable = before == after and before["revision"] != "unbound"
    completed = not result.is_error and data.get("ok") is True
    unresolved = (data.get("execution_state") == "unknown" or data.get("terminal") is False
                  or data.get("status") in {"pending", "submitted", "confirming", "awaiting_approval"})
    operation_status = "pending" if unresolved else "passed" if completed else "failed"
    checks = [{"check_id": "source_binding", "label": "Source version unchanged",
               "status": "passed" if stable else "missing" if before["revision"] == "unbound" else "stale",
               "evidence_ref": _candidate_ref(config, before) if stable else ""}]
    request_scope = None
    if config is not None and config.get("runtime.command_id"):
        request_scope = replay_scope_receipt(config, before, replay=data if kind != "validation" else None)
    if request_scope is not None:
        scope_ref = "request_scope:" + str(config.get("runtime.command_id")) + ":" + request_scope["request_scope_hash"]
        if kind != "validation" and request_scope["status"] == "passed":
            try:
                import hashlib
                run_ref, report_ref = _replay_output_refs(config, data, before)
                if not run_ref:
                    raise ValueError("missing_scope_replay_output")
                metrics = _output_path(config, run_ref).with_name("metrics.json")
                saved = {**json.loads(metrics.read_text()), "run_receipt": run_ref}
                saved_scope = replay_scope_receipt(config, before, replay=saved)
                if not saved_scope or saved_scope.get("request_scope_hash") != request_scope["request_scope_hash"]:
                    raise ValueError("saved_replay_scope_changed")
                request_scope = saved_scope
                scope_ref += ":" + hashlib.sha256(metrics.read_bytes()).hexdigest() + ":" + metrics.relative_to(config.paths.root).as_posix()
            except (OSError, ValueError):
                request_scope["status"], request_scope["reason"] = "missing", "request_scope_replay_unverified"
        # Candidate checks never overwrite or satisfy the final replay scope check.
        checks.append({"check_id": "candidate_scope_match" if kind == "validation" else "request_scope_match",
                       "required": kind != "validation", "label": "Candidate and replay match admitted request",
                       "status": request_scope["status"] if stable else "stale",
                       "message": request_scope["reason"],
                       "evidence_ref": scope_ref})
    unverified = ["out_of_sample", "forward_paper_run", "live_execution"]
    if kind == "validation":
        structural = data.get("structural") or {}
        structural_status = structural.get("status")
        checks.append({"check_id": "strategy_contract", "label": "Strategy package structure",
                       "status": structural_status if structural_status in {"passed", "failed", "missing"} else operation_status,
                       "message": "; ".join(str(item.get("message", "")) for item in data.get("blockers", [])
                                            if isinstance(item, dict))[:2048]})
        unverified.extend(["historical_replay", "full_branch_coverage"])
        scope = "Strategy package checks; not historical performance or activation."
        behavioral = data.get("behavioral") or {}
        behavioral_status = behavioral.get("status")
        checks.append({"check_id": "behavioral_tests", "label": "Executed candidate tests",
                       "status": ("passed" if behavioral_status == "passed" and behavioral.get("exit_code") == 0
                                  else "missing" if behavioral_status in {None, "not_run", "not_present"}
                                  else "pending" if behavioral_status in {"pending", "running"} else "failed"),
                       "message": str(behavioral.get("summary") or behavioral.get("reason") or "Candidate tests have not been verified")[:2048]})
        # Keep executed behavioral checks separate from structural validation.
        # These rows come directly from the validator, never model tool args.
        for check in data.get("checks", []):
            if isinstance(check, dict) and check.get("check_id") not in {row["check_id"] for row in checks}:
                checks.append(check)
    else:
        provenance = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
        replay = data.get("replay") if isinstance(data.get("replay"), dict) else {}
        run_ref, report_ref = (_replay_output_refs(config, data, before)
            if stable and completed and not unresolved and not preflight_only else ("", ""))
        cadence_lp = replay.get("scope") == "cadence_fixed_range_counterfactual"
        cadence_attested = bool(run_ref) and cadence_lp and _lp_cadence_snapshots_attested(config, data, before)
        checks.extend([
            {"check_id": "historical_run", "label": "Historical replay completed",
             "status": "missing" if preflight_only else operation_status,
             "evidence_ref": run_ref},
            {"check_id": "report_output", "label": "Persisted replay report",
             "status": "passed" if report_ref else "missing", "evidence_ref": report_ref},
            {"check_id": "data_coverage", "label": "Requested history coverage",
             "status": "failed" if data.get("coverage_ok") is False
                 or data.get("requested_window_complete") is False else
                 "passed" if data.get("requested_window_complete") is True
                 and (not cadence_lp or cadence_attested) else "missing",
             "message": str(data.get("coverage_message") or "")[:2048]},
            {"check_id": "historical_data", "label": "Historical source snapshots",
             "status": ("passed" if cadence_attested else "missing") if cadence_lp else
                 ("passed" if _pool_price_snapshots_attested(config, data, before) else "missing")
                 if replay.get('scope') == 'historical_pool_price_simulation' else
                 "passed" if (provenance.get("data_kind") == "historical" and
                 _datasets_complete(provenance.get("datasets"))) or
                 _rate_snapshots_attested(config, data, before) else "missing"},
            {"check_id": "replay_source", "label": "Replay matches this source version",
             "status": "passed" if stable and provenance.get("source_revision") == before["revision"]
                 and provenance.get("source_changed_during_run") is False else "stale"},
            {"check_id": "execution_integrity", "label": "Replay execution and order accounting",
             "status": "missing" if replay.get("strategy_execution_verified") is False else "failed" if replay.get("errors") or replay.get("sdk_errors")
                 or replay.get("order_accounting_ok") is False else "passed" if completed and replay else "missing"},
            {"check_id": "historical_performance", "label": "Historical trading performance evidence",
             "required": False, "status": "passed" if data.get("performance_evidence") is True else "missing",
             "message": ("Producer confirms historical performance evidence; this does not verify live performance."
                         if data.get("performance_evidence") is True else
                         "Historical trading performance is unverified: simulated, stub, or input-only replay results are not real trading performance evidence.")},
        ])
        if config is not None and config.get("runtime.command_id"):
            from .request_scope import _snapshot, ScopeError
            from ..task_delivery import replay_visualization_requested
            try:
                snapshot = _snapshot(config)
            except ScopeError:
                snapshot = None  # The existing required scope check fails closed.
            if snapshot and any(replay_visualization_requested(doc["text"])
                                for doc in snapshot["binding"]["documents"]):
                chart_ref = _visualization_output_ref(config, data, before) if run_ref else ""
                checks.append({"check_id": "visualization_output", "label": "Persisted replay chart and tables",
                    "status": "passed" if chart_ref else "missing", "evidence_ref": chart_ref})
        if data.get("performance_evidence") is not True:
            unverified.append("historical_agent_trading_performance")
        scope = "Historical calculation and provenance; economic losses do not mean execution failed."
    prior = read_verification_receipt(merge_verification)
    if prior and all(prior[key] == before[key] for key in ("artifact_id", "revision")):
        merged = {row["check_id"]: row for row in prior["checks"]}
        merged.update({row["check_id"]: row for row in checks})
        checks = list(merged.values())
        scope = prior["scope"] + " " + scope
        unverified = list(dict.fromkeys(prior["unverified"] + unverified))
    for check in checks:
        if (kind == "validation" and check["check_id"] != "source_binding") or check["check_id"] in {"candidate_scope_match", "strategy_contract", "behavioral_tests",
                                "data_coverage", "historical_data", "execution_integrity"}:
            check["required"] = False
    receipt = verification_receipt(**before, scope=scope, checks=checks, unverified=unverified)
    # A successful tool can report incomplete verification. These are distinct.
    if result.semantic_success is None:
        result.semantic_success = completed and not unresolved
    result = attach_verification(result, receipt)
    payload = tool_json_data(result)
    if payload is None:
        payload = {"ok": False, "verification": result.metadata["verification"]}
        result.content.append(ToolResultPart.json_part(payload))
    if request_scope is not None:
        payload["request_scope"] = request_scope
    command_id = str(config.get("runtime.command_id", "") or "") if config is not None else ""
    if command_id:
        from ..task_delivery import DeliveryError, record_delivery_verification
        try:
            delivery = record_delivery_verification(config.paths, command_id=command_id, receipt=receipt)
            if delivery is not None:
                # Generic compaction retains the first bounded top-level fields.
                # Keep the revision before a large replay's metrics/path fields.
                projection = {"delivery_revision": delivery["delivery_revision"],
                              "delivery_completion": delivery["completion"], **payload}
                payload.clear()
                payload.update(projection)
        except DeliveryError as exc:
            # Do not lose a completed producer operation or encourage rerunning it.
            payload["delivery_error"] = exc.code
            payload["delivery_next_action"] = "Delivery projection was not updated; reconcile it before claiming completion. Do not repeat the completed operation."
    return result


def delivery_source_versions(config: Any, delivery: dict) -> dict[str | tuple[str, str], str]:
    """Read source identities and check-specific invalidations; never execute source."""
    from ..tools.native.research_verification import research_source_versions
    versions = research_source_versions(config, delivery)
    for row in delivery.get("checks", []):
        artifact_id = row.get("artifact_id", "")
        match = re.fullmatch(r"strategy:([A-Za-z0-9_-]+):proposal:([A-Za-z0-9_-]+)", artifact_id)
        if not row.get("producer_owned") or not match or artifact_id in versions:
            continue
        sid, pid = match.groups()
        binding = source_binding(config, ToolCall(name="strategy_validate", arguments={
            "strategy_id": sid, "proposal_id": "" if pid == "installed" else pid,
        }))
        versions[artifact_id] = binding["revision"]
    # Raw source identity alone cannot keep a request-scope receipt current.
    # Scope/output invalidation must not impersonate a source revision change.
    from .request_scope import current_contract
    for row in delivery.get("checks", []):
        if row.get("check_id") not in {"request_scope_match", "candidate_scope_match"} or not row.get("producer_owned"):
            continue
        try:
            contract = current_contract(config, command_id=delivery["command_id"])
            expected = "request_scope:" + delivery["command_id"] + ":" + contract["request_scope_hash"]
            ref = row.get("evidence_ref", "")
            identity = ref.split(":", 3)
            if len(identity) >= 3 and identity[0] == "request_scope" and identity[1] != delivery["command_id"]:
                from .request_scope import equivalent_continuation_scope
                with closing(connect_readonly(config.paths.db)) as con:
                    con.execute("BEGIN")
                    if equivalent_continuation_scope(config, con, command_id=delivery["command_id"],
                            source_command_id=identity[1], source_scope_hash=identity[2]):
                        expected = ":".join(identity[:3])
            if row["check_id"] == "candidate_scope_match" and ref == expected:
                continue
            if ref.startswith(expected + ":"):
                import hashlib
                digest, path = ref[len(expected) + 1:].split(":", 1)
                metrics = _output_path(config, path)
                if hashlib.sha256(metrics.read_bytes()).hexdigest() == digest:
                    saved = {**json.loads(metrics.read_text()), "run_receipt": str(metrics.with_name('run.json'))}
                    if replay_scope_receipt(config, {"artifact_id": row["artifact_id"], "revision": versions[row["artifact_id"]]}, replay=saved, command_id=delivery["command_id"])['status'] == 'passed':
                        continue
        except Exception:
            pass
        versions[(row["artifact_id"], row["check_id"])] = "request_scope_stale"
    for row in delivery.get("checks", []):
        if row.get("check_id") != "visualization_output" or not row.get("producer_owned") or row.get("status") != "passed":
            continue
        related = {check["check_id"]: check for check in delivery["checks"]
                   if check.get("artifact_id") == row["artifact_id"] and check.get("revision") == row["revision"]
                   and check.get("producer_owned")}
        data = {"run_receipt": related.get("historical_run", {}).get("evidence_ref"),
                "report_path": related.get("report_output", {}).get("evidence_ref")}
        ref = _visualization_output_ref(config, data, {"artifact_id": row["artifact_id"], "revision": row["revision"]})
        if not ref or ref != row.get("evidence_ref"):
            versions[(row["artifact_id"], row["check_id"])] = "visualization_output_stale"
    return versions


def attach_saved_replay(result: ToolResult, *, config: Any, binding: dict) -> ToolResult:
    """Re-attest saved simulation outputs after validation; never run a replay."""
    from ..core import yaml_io
    from .verification import _read_report

    payload = tool_json_data(result) or {}
    if result.is_error or payload.get("ok") is not True:
        return result
    ref = _candidate_ref(config, binding)
    if not ref:
        return result
    sid, pid = binding["artifact_id"].split(":")[1::2]
    files, source = source_files(config.paths, sid, None if pid == "installed" else pid)
    manifest = yaml_io.loads(files.get("strategy.yml", "")) or {}
    params = manifest.get("params") or {}
    scope = ("historical_rate_policy_simulation" if isinstance(params.get("rate_replay"), dict)
             else "historical_pool_price_simulation" if isinstance(params.get("price_simulation"), dict) else None)
    if (source.get("omitted_files") or source_revision(files) != binding["revision"]
            or scope is None):
        return result
    # Missing/unreadable reports still produce missing checks, not a silent skip.
    data = {"ok": False, "replay": {"scope": scope},
            "performance_evidence": False}
    try:
        report, warnings = _read_report(_output_path(config, ref))
        if (report is not None and not warnings
                and (report["metrics"].get("replay") or {}).get("scope") == scope
                and report["metrics"].get("performance_evidence") is False):
            run = _output_path(config, f"{ref}/backtests/{report['id']}/run.json")
            data = {**report["metrics"], "ok": True, "run_receipt": str(run),
                    "report_path": str(run.with_name("report.md"))}
            data["ok"] = bool(_replay_output_refs(config, data, binding)[0])
    except (OSError, ValueError, TypeError, UnicodeError):
        pass
    refreshed = attach_strategy_checks(ToolResult.from_json(tool_use_id=result.tool_use_id,
        name=result.name, data=data), before=binding,
        after=source_binding(config, ToolCall(name="strategy_validate", arguments={
            "strategy_id": sid, "proposal_id": None if pid == "installed" else pid})),
        kind="backtest", config=config, merge_verification=result.metadata.get("verification"))
    receipt = refreshed.metadata["verification"]
    attach_verification(result, receipt)
    refreshed_payload = tool_json_data(refreshed) or {}
    for key in ("delivery_revision", "delivery_completion", "delivery_error",
                "delivery_next_action", "request_scope"):
        if key in refreshed_payload:
            payload[key] = refreshed_payload[key]
    payload["backtest_evidence"] = {"rechecked_saved_outputs": True, "replay_executed": False,
        "run_receipt": data.get("run_receipt"), "report_path": data.get("report_path"), "scope": scope,
        "performance_evidence": False}
    result.semantic_success = result.semantic_success is not False and all(
        row["status"] == "passed" for row in receipt["checks"] if row["required"])
    return result


def attach_inline_validation(result: ToolResult, *, config: Any, before: dict | None = None) -> ToolResult:
    data = tool_json_data(result) or (result.error.recovery_hint if result.error else {})
    if not data.get("proposal_id") or result.is_error:
        return result
    if not isinstance(data.get("validation"), dict):
        data["validation"] = {"ok": None, "status": "not_run",
                              "structural": {"status": "missing"},
                              "behavioral": {"status": "not_run"}}
    call = ToolCall(name=result.name, arguments={"strategy_id": data.get("strategy_id"), "proposal_id": data["proposal_id"]})
    after = source_binding(config, call)
    return attach_strategy_checks(result, before=before or after, after=after, kind="validation", config=config)

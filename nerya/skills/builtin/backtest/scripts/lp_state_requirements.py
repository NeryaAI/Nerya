"""Offline cadence/checkpoint inventory; never replay or coverage admission."""
from bisect import bisect_left
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3

from nerya.core import yaml_io
from nerya.data_api.uniswap_history import RPC_ORIGINS, _address, _hash
from nerya.data_api.types import DataApiContext
from nerya.data_api.uniswap_events import _fold_range
from nerya.strategies.scheduler_bridge import compile_trading_schedule
from nerya.strategies.verification import source_revision
from nerya.strategies.workflow_service import _read_files
from nerya.triggers.schedule import _cron_matches
from .backtest_run import _load_target_package
from .lp_accounting import ConcentratedLiquidityLedger
from .lp_replay import _uint, _window_utc
from .replay_inputs import read_manifest
from .uniswap_event_data import MAX_BYTES, _require, _verify, event_history_operation


def _utc(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _times(package, start, end, cancel):
    _require(package.manifest.schedule.type == "cron", "unsupported_schedule_requires_saved_cron_interval_anchor_unavailable")
    entry = compile_trading_schedule(package)
    lo, hi = start, end
    if entry.starts_at:
        lo = max(lo, datetime.fromisoformat(entry.starts_at.replace("Z", "+00:00")).timestamp())
    if entry.ends_at:
        hi = min(hi, datetime.fromisoformat(entry.ends_at.replace("Z", "+00:00")).timestamp())
    cursor = math.ceil(lo / 60) * 60
    decisions = []
    while cursor is not None and cursor < hi:
        cancel()
        now = datetime.fromtimestamp(cursor, timezone.utc)
        if _cron_matches(entry.cron, entry._cron_time(now)):
            decisions.append(cursor)
            _require(len(decisions) <= 10_000, "decision_limit_exceeded")
        cursor += 60
    return decisions


def _checkpoint(cp, identity, lower, upper):
    _require(all(cp["pool"].get(k) == v for k, v in identity.items()), "checkpoint_pool_identity_mismatch")
    bounds = cp["range"]
    _require(bounds["tick_lower"] == lower and bounds["tick_upper"] == upper, "checkpoint_range_mismatch")
    for side in ("lower", "upper"):
        bound = bounds[side]
        gross = _uint(bound["liquidity_gross"], 128)
        net = bound["liquidity_net"]
        _require(isinstance(net, str) and re.fullmatch(r"-?[0-9]{1,39}", net)
                 and -2**127 <= int(net) < 2**127 and abs(int(net)) <= gross, "invalid_boundary_net")
        _require(type(bound["initialized"]) is bool and bound["initialized"] == (gross > 0),
                 "invalid_boundary_initialization")
        for i in (0, 1):
            growth = _uint(bound[f"fee_growth_outside{i}_x128"], 256)
            _require(gross > 0 or growth == 0, "invalid_uninitialized_growth")
    _uint(cp["liquidity"], 128)
    ledger = ConcentratedLiquidityLedger(token0_units="0", token1_units="0",
        decimals=identity["decimals"], lower=lower, upper=upper, action_cost_token1="0")
    ledger.observe(block_number=cp["block_number"], sqrt_price_x96=_uint(cp["slot0"]["sqrt_price_x96"], 160),
        tick=cp["slot0"]["tick"], global_growth=[_uint(cp[f"fee_growth_global{i}_x128"], 256) for i in (0, 1)],
        lower_outside=[bounds["lower"][f"fee_growth_outside{i}_x128"] for i in (0, 1)],
        upper_outside=[bounds["upper"][f"fee_growth_outside{i}_x128"] for i in (0, 1)],
        initialized=[bounds[side]["initialized"] for side in ("lower", "upper")])


def _matches(root, identity, lower, upper, times, cancel, edges=None, checkpoints=None, evidence=None):
    matches, conflicts, archives, chunks = {}, set(), 0, 0
    for path in root.glob("*/archive.sqlite3"):
        cancel()
        _require(not path.is_symlink() and not path.parent.is_symlink()
                 and path.resolve().is_relative_to(root.resolve()), "unsafe_event_archive")
        # Read-only connection and one immutable chunk at a time; no whole-archive materialization.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            metadata = db.execute("SELECT metadata FROM request WHERE id=1").fetchone()
            _require(metadata is not None and len(metadata[0]) <= MAX_BYTES, "invalid_archive_metadata")
            meta = json.loads(metadata[0])
            if (meta.get("pool_address") != identity["pool_address"] or meta.get("chain_id") != 1
                    or meta.get("tick_lower") != lower or meta.get("tick_upper") != upper):
                continue
            _require(set(meta) == {"pool_address", "chain_id", "tick_lower", "tick_upper",
                     "rpc_provider", "start_block", "end_block", "chunk_blocks"}
                     and _hash(meta) == path.parent.name and meta["rpc_provider"] in RPC_ORIGINS,
                     "archive_metadata_identity_mismatch")
            lo, hi, size = (meta[k] for k in ("start_block", "end_block", "chunk_blocks"))
            _require(all(type(v) is int for v in (lo, hi, size)) and 1 <= lo <= hi < 2**64
                     and 1 <= size <= 100, "invalid_archive_bounds")
            archives += 1
            previous, ordinal = None, 0
            for number, first, last, count, digest, blob in db.execute(
                    "SELECT ordinal,start_block,end_block,row_count,evidence_sha256,evidence FROM chunks ORDER BY ordinal"):
                cancel()
                _require(isinstance(blob, bytes) and len(blob) <= MAX_BYTES and number == ordinal
                         and int(first) == lo and int(last) == min(hi, lo + size - 1) and lo <= hi,
                         "invalid_archive_chunk_sequence")
                output = json.loads(blob)
                env = output["_envelope"]
                request = {k: meta[k] for k in ("pool_address", "rpc_provider", "tick_lower", "tick_upper")}
                request.update(start_block=int(first), end_block=int(last))
                _verify(output, request, previous)
                _require(digest == env["evidence_sha256"] and count == len(output["rows"]), "archive_chunk_hash_mismatch")
                for cp in env["checkpoints"].values():
                    _checkpoint(cp, identity, lower, upper)
                headers = env["headers"]
                for header in headers:
                    _require(type(header["timestamp"]) is int and 0 <= header["timestamp"] <= 253402300799
                             and all(isinstance(header[k], str) and re.fullmatch(r"0x[0-9a-f]{64}", header[k])
                                     for k in ("hash", "parent_hash")), "invalid_archive_header")
                relevant = False
                if edges is not None:
                    order = [(r["block_number"], r["transaction_index"], r["log_index"]) for r in output["rows"]]
                    _require(all(a < b for a, b in zip(order, order[1:]))
                             and len({(r[0], r[2]) for r in order}) == len(order), "invalid_archive_event_order")
                    initial, terminal = (env["checkpoints"][name] for name in ("initial", "terminal"))
                    if _fold_range(output["rows"], initial["range"], terminal["range"])["verified"]:
                        a, b = [(cp["block_number"], cp["block_hash"], _hash(cp)) for cp in (initial, terminal)]
                        edges.setdefault(a, set()).add(b)
                        relevant = headers[0]["timestamp"] < times[-1] and headers[-1]["timestamp"] >= times[0]
                block_checkpoints = {cp["block_number"]: cp for cp in env["checkpoints"].values()}
                for a, b in zip(headers, headers[1:]):
                    cp = block_checkpoints.get(a["number"])
                    if cp is None:
                        continue
                    for stamp in times[bisect_left(times, a["timestamp"]):bisect_left(times, b["timestamp"])]:
                        proof = {"block_number": a["number"], "block_hash": a["hash"],
                            "block_timestamp": a["timestamp"], "next_block_timestamp": b["timestamp"],
                            "checkpoint_sha256": _hash(cp)}
                        if stamp in matches and matches[stamp] != proof:
                            conflicts.add(stamp)
                        matches[stamp] = proof
                        if checkpoints is not None:
                            checkpoints[(cp["block_number"], cp["block_hash"], proof["checkpoint_sha256"])] = cp
                        relevant = True
                if evidence is not None and relevant:
                    evidence[(str(path), number, digest)] = {"archive": str(path), "chunk_id": number,
                                                            "evidence_sha256": digest}
                previous, lo, ordinal = env, int(last) + 1, ordinal + 1
                chunks += 1
    for stamp in conflicts:
        matches.pop(stamp, None)
    return matches, len(conflicts), archives, chunks


def _intervals(config, times, matches, edges, identity, lower, upper, provider, cancel):
    verified = missing = pending = 0
    preview, next_args, first_missing = [], None, None
    for left, right in zip(times, times[1:]):
        cancel()
        if left not in matches or right not in matches:
            pending += 1
            status = "pending_states"
        else:
            a, b = [tuple(matches[t][k] for k in ("block_number", "block_hash", "checkpoint_sha256"))
                    for t in (left, right)]
            frontier, seen = [a], set()
            while frontier:
                cancel()
                node = frontier.pop()
                if node in seen:
                    continue
                seen.add(node)
                if node == b:
                    break
                frontier.extend(n for n in edges.get(node, ()) if node[0] < n[0] <= b[0] and n not in seen)
            if b in seen:
                verified += 1
                status = "verified"
            else:
                missing += 1
                status = "missing_event_bridge"
                if first_missing is None:
                    first_missing = (a, b)
        if len(preview) < 10:
            preview.append({"start": _utc(left), "end": _utc(right), "status": status})
    if first_missing is not None:
        a, b = first_missing
        if a[0] < b[0]:
            archive = event_history_operation(config, {"action": "inspect", "dataset_kind": "uniswap_pool_events",
                "pool_address": identity["pool_address"], "rpc_provider": provider, "tick_lower": lower, "tick_upper": upper,
                "start_block": a[0] + 1, "end_block": b[0], "chunk_blocks": 100, "max_chunks": 1}, check_cancel=cancel)
            next_args = archive["next_download_args"]
    complete = len(matches) == len(times) and verified == len(times) - 1
    return {"interval_count": len(times) - 1, "verified_interval_count": verified,
        "missing_interval_count": missing, "pending_state_interval_count": pending, "interval_preview": preview,
        "next_interval_download_args": next_args, "range_continuity_verified": complete,
        "range_continuity_status": "verified" if complete else "unverified"}


def inspect_lp_state_requirements(config, args, check_cancel=None):
    return _inspect_lp_state_requirements(config, args, check_cancel)


def _inspect_lp_state_requirements(config, args, check_cancel=None, prepared=None):
    cancel = check_cancel or (lambda: None)
    cancel()
    _require(isinstance(args, dict) and not args.keys() - {"action", "dataset_kind", "proposal_id", "strategy_id"}
             and args.get("action", "inspect") == "inspect"
             and args.get("dataset_kind", "lp_state_requirements") == "lp_state_requirements", "invalid_requirements_args")
    ids = [key for key in ("proposal_id", "strategy_id") if key in args]
    _require(len(ids) == 1 and isinstance(args[ids[0]], str)
             and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", args[ids[0]]), "exactly_one_saved_target_required")
    package = _load_target_package(config.paths, args.get("strategy_id"), args.get("proposal_id"))
    _require(package.root.resolve().is_relative_to(config.paths.root.resolve()), "package_outside_workspace")
    files, omitted = _read_files(package.root)
    _require(not omitted, "incomplete_source_snapshot")
    revision = source_revision(files)
    raw = yaml_io.loads(files["strategy.yml"])
    params = package.manifest.extras.get("params") or {}
    source, replay = params.get("lp_source"), params.get("lp_replay")
    _require(isinstance(source, dict) and isinstance(replay, dict), "saved_lp_configuration_required")
    _require(replay.get("mode") != "sampled",
        "lp_state_requirements_mode_mismatch: saved lp_replay.mode=sampled cannot certify full-window coverage. "
        "Preserve the existing sampled report and its limits. Read the admitted request and backtest "
        "Skill history-data cadence branch: for requested full-window replay, revise the SAME candidate "
        "through normal proposal tools to cadence mode at its saved schedule, validate, then inspect its "
        "new source revision. If the user requested only sampled research, keep that mode and report its limits. "
        "A mode change does not waive missing states, interval evidence or replay checks.")
    declaration = read_manifest(package.root)
    datasets = [row for row in (declaration or {}).get("datasets", []) if row["id"] == replay.get("dataset_id")]
    _require(len(datasets) == 1 and datasets[0]["provider"] == "uniswap"
             and datasets[0]["action"] == "pool_history", "exact_lp_dataset_required")
    declared = datasets[0]["args"]
    rpc_provider = declared.get("rpc_provider", "publicnode")
    _require(rpc_provider in RPC_ORIGINS, "fixed_rpc_provider_required")
    identity = {k: source[k] for k in ("token0", "token1", "decimals", "fee", "tick_spacing")}
    identity.update(chain_id=1, pool_address=_address(source["pool_id"]))
    lower, upper, spacing = source["tick_lower"], source["tick_upper"], identity["tick_spacing"]
    _require(source.get("protocol") == "uniswap_v3" and source.get("chain_id", 1) == 1
             and source.get("chain") in ("Ethereum", "ethereum", "1")
             and all(type(v) is int for v in (lower, upper, spacing)) and 0 < spacing <= 32767
             and -887272 <= lower < upper <= 887272 and lower % spacing == upper % spacing == 0
             and _address(declared.get("pool_address")) == identity["pool_address"]
             and declared.get("tick_lower") == lower and declared.get("tick_upper") == upper
             and not {"start_block", "end_block"} & declared.keys(), "lp_declaration_scope_mismatch")
    start, end = _window_utc(declared.get("start")), _window_utc(declared.get("end"))
    _require(0 < end - start <= 366 * 86400, "window_outside_366_day_bound")
    decisions = _times(package, start, end, cancel)
    times = sorted(set([start, *decisions, end]))
    root = Path(config.paths.artifacts) / "backtest_cache" / "pool-events"
    _require(root.resolve().is_relative_to(config.paths.root.resolve()), "archive_outside_workspace")
    edges, checkpoints, evidence = {}, {}, {}
    matches, conflicts, archives, chunks = _matches(root, identity, lower, upper, times, cancel, edges,
        checkpoints if prepared is not None else None, evidence if prepared is not None else None)
    intervals = _intervals(config, times, matches, edges, identity, lower, upper, rpc_provider, cancel)
    matched_decisions = sum(t in matches for t in decisions)
    next_missing = next((t for t in times if t not in matches), None)
    prior = max((t for t in matches if next_missing is not None and t < next_missing), default=None)
    anchor = {key: matches[prior][key] for key in ("block_number", "block_hash",
              "block_timestamp", "next_block_timestamp")} if prior is not None else None
    current, skipped = _read_files(package.root)
    _require(not skipped and source_revision(current) == revision and read_manifest(package.root) == declaration,
             "source_changed_during_inspection")
    cancel()
    if prepared is not None:
        prepared.update(times=times, decisions=decisions, matches=matches, checkpoints=checkpoints, evidence=evidence)
    return {"ok": True, "result_type": "historical_data", "read_only": True, "dataset_kind": "lp_state_requirements",
        "strategy_id": package.strategy_id, "proposal_id": args.get("proposal_id"), "source_revision": revision,
        "dataset_id": replay["dataset_id"], "pool": identity, "rpc_provider": rpc_provider,
        "tick_lower": lower, "tick_upper": upper,
        "resolver_anchor": anchor,
        "next_download_args": None if next_missing is None else {
            "action": "download", "dataset_kind": "lp_state_requirements", ids[0]: args[ids[0]],
            "expected_source_revision": revision, "expected_next_timestamp": _utc(next_missing), "max_states": 1},
        "schedule": raw["schedule"], "schedule_basis": "saved_schedule_hypothetical_regardless_of_enabled",
        "timezone": package.manifest.schedule.timezone or "UTC",
        "window": {"start": declared["start"], "end": declared["end"],
                   "decision_policy": "start_inclusive_end_exclusive_plus_opening_and_terminal"},
        "decision_count": len(decisions), "required_state_count": len(times),
        "matched_decision_count": matched_decisions, "missing_decision_count": len(decisions) - matched_decisions,
        "boundaries": {"opening": {"timestamp": _utc(start), "matched": start in matches,
                                   "overlaps_decision": start in decisions},
                       "terminal": {"timestamp": _utc(end), "matched": end in matches, "overlaps_decision": False}},
        "matched_state_count": len(matches), "missing_state_count": len(times) - len(matches),
        "conflicting_state_count": conflicts, "archives_inspected": archives, "chunks_inspected": chunks,
        "preview": [{"timestamp": _utc(t), "kind": "terminal" if t == end else
                     "opening" if t == start and t not in decisions else "decision",
                     "matched": t in matches, **matches.get(t, {})} for t in times[:10]],
        **intervals,
        "coverage_complete": False, "requested_window_complete": False, "replay_input": False,
        "tick_fee_reconstruction_verified": False, "performance_evidence": False,
        "financial_execution_allowed": False}


class LPDataIncomplete(ValueError):
    def __init__(self, summary):
        super().__init__("lp_cadence_data_incomplete")
        fields = ("source_revision", "required_state_count", "matched_state_count", "missing_state_count",
                  "conflicting_state_count", "interval_count", "verified_interval_count",
                  "missing_interval_count", "pending_state_interval_count")
        self.receipt = {k: summary[k] for k in fields}
        self.receipt.update(protocol="nerya.lp_cadence.v1", ready=False, replay_input=False, coverage_complete=False,
            reasons=[k for k in ("missing_state_count", "conflicting_state_count", "missing_interval_count",
                                "pending_state_interval_count") if summary[k]])


def prepare_lp_cadence(config, args, check_cancel=None):
    """Internal all-or-nothing host assembly; not a tool completion or replay receipt."""
    context = {}
    summary = _inspect_lp_state_requirements(config, args, check_cancel, context)
    if not summary["range_continuity_verified"]:
        raise LPDataIncomplete(summary)
    matches, decisions = context["matches"], set(context["decisions"])
    observations = []
    for stamp in context["times"]:
        if check_cancel:
            check_cancel()
        proof = matches[stamp]
        key = tuple(proof[k] for k in ("block_number", "block_hash", "checkpoint_sha256"))
        observations.append({"timestamp": stamp, "decision": stamp in decisions,
                             "checkpoint": context["checkpoints"][key], "floor_proof": proof})
    return {"protocol": "nerya.lp_cadence.v1", "source_revision": summary["source_revision"],
        "requested_window": {k: summary["window"][k] for k in ("start", "end")},
        "schedule": summary["schedule"], "pool": summary["pool"],
        "tick_lower": summary["tick_lower"], "tick_upper": summary["tick_upper"],
        "decision_timestamps": context["decisions"], "observations": observations,
        "intervals": [{"start": a, "end": b, "start_checkpoint_sha256": matches[a]["checkpoint_sha256"],
                       "end_checkpoint_sha256": matches[b]["checkpoint_sha256"], "verified": True}
                      for a, b in zip(context["times"], context["times"][1:])],
        "evidence": [{**ref, "archive": Path(ref["archive"]).relative_to(config.paths.root).as_posix()}
                     for _, ref in sorted(context["evidence"].items())]}


def download_lp_states(config, args, *, check_cancel=None, cancel_token=None, provider=None):
    """Acquire a bounded number of missing states; no policy execution or fee admission."""
    from nerya.harness.cancellation import raise_if_cancelled
    from nerya.data_api.uniswap_events import UniswapEventProvider

    _require(isinstance(args, dict) and not args.keys() - {
        "action", "dataset_kind", "proposal_id", "strategy_id",
        "expected_source_revision", "expected_next_timestamp", "max_states"}
        and args.get("action") == "download"
        and args.get("dataset_kind", "lp_state_requirements") == "lp_state_requirements", "invalid_state_download_args")
    limit = args.get("max_states", 1)
    _require(type(limit) is int and 1 <= limit <= 4, "invalid_state_download_budget")
    revision = args.get("expected_source_revision")
    _require(isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{64}", revision), "expected_source_revision_required")
    expected = _window_utc(args.get("expected_next_timestamp"))
    inspect_args = {k: args[k] for k in ("proposal_id", "strategy_id") if k in args}
    inspect_args["action"] = "inspect"

    def cancelled():
        raise_if_cancelled(cancel_token)
        if check_cancel:
            check_cancel()

    before = inspect_lp_state_requirements(config, inspect_args, cancelled)
    _require(before["source_revision"] == revision, "state_download_source_changed")
    cursor = before["next_download_args"]
    if cursor is not None:
        _require(expected <= _window_utc(cursor["expected_next_timestamp"]), "state_cursor_ahead")
    downloaded = []
    if cursor is not None and expected == _window_utc(cursor["expected_next_timestamp"]):
        provider = provider or UniswapEventProvider()
        for _ in range(limit):
            cancelled()
            at_utc = cursor["expected_next_timestamp"]
            requested = {"pool_address": before["pool"]["pool_address"], "rpc_provider": before["rpc_provider"],
                         "tick_lower": before["tick_lower"], "tick_upper": before["tick_upper"], "at_utc": at_utc}
            output = provider.pool_event_slice(DataApiContext(config_like=config, cancel_token=cancel_token),
                                               requested, anchor=before["resolver_anchor"])
            env = output["_envelope"]
            part = env["request_args"]
            _require(all(part.get(k) == v for k, v in requested.items() if k != "at_utc")
                     and part["start_block"] == part["end_block"], "state_download_identity_mismatch")
            _verify(output, part, None)
            a, b = env["headers"]
            _require(a["timestamp"] <= _window_utc(at_utc) < b["timestamp"], "state_download_floor_unproven")
            _checkpoint(env["checkpoints"]["initial"], before["pool"], before["tick_lower"], before["tick_upper"])
            files, omitted = _read_files(_load_target_package(config.paths,
                args.get("strategy_id"), args.get("proposal_id")).root)
            _require(not omitted and source_revision(files) == revision, "state_download_source_changed")

            class FetchedSlice:
                def pool_event_slice(self, context, request):
                    _require(request == part, "state_archive_request_mismatch")
                    return output

            stored = event_history_operation(config, {**part, "action": "download",
                "chunk_blocks": 1, "max_chunks": 1, "expected_next_block": part["start_block"]},
                check_cancel=cancelled, provider=FetchedSlice(), cancel_token=cancel_token)
            downloaded.append({"timestamp": at_utc, "block_number": a["number"],
                               "store_path": stored["store_path"], "evidence_sha256": env["evidence_sha256"]})
            current = inspect_lp_state_requirements(config, inspect_args, cancelled)
            cursor = current["next_download_args"]
            before = current
            if cursor is None:
                break
            _require(_window_utc(cursor["expected_next_timestamp"]) > _window_utc(at_utc), "state_download_no_progress")
    return {**before, "read_only": False, "downloaded_state_count": len(downloaded),
            "downloaded_states": downloaded, "request_budget_states": limit}

"""Append-only bounded pool-event archives; never full-window LP replay input."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from nerya.data_api.types import DataApiContext
from nerya.data_api.uniswap_history import RPC_ORIGINS, _address, _canonical, _hash
from nerya.harness.cancellation import raise_if_cancelled
from nerya.tools.native.paths import resolve_workspace_path, to_workspace_relative

MAX_BYTES = 4_000_000


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _verify(output, request, previous):
    blob = _canonical(output)
    _require(len(blob) <= MAX_BYTES, "event_chunk_too_large")
    rows, env = output["rows"], output["_envelope"]
    lo, hi = request["start_block"], request["end_block"]
    pool = _address(request["pool_address"])
    _require(isinstance(rows, list) and env["provider"] == "uniswap"
             and env["action"] == "pool_event_slice" and env["mock"] is False
             and env["rpc_provider"] == request["rpc_provider"]
             and env["pool"]["chain_id"] == 1 and env["pool"]["pool_address"] == pool
             and env["request_args"] == request
             and env["requested_range"] == {"start_block": lo, "end_block": hi, "inclusive": True},
             "event_chunk_identity_mismatch")
    receipt = env["range_receipt"]
    _require(receipt["complete"] is True and receipt["blocks"] == hi - lo + 1
             and receipt["logs"] == len(rows) and receipt["topics_filter"] is None,
             "event_range_receipt_incomplete")
    _require(env["rows_sha256"] == _hash(rows) and env["evidence_sha256"] == _hash({
        "rows": rows, "_envelope": {k: v for k, v in env.items() if k != "evidence_sha256"}}),
        "event_chunk_hash_mismatch")
    headers = env["headers"]
    _require([h["number"] for h in headers] == list(range(lo - 1, hi + 1))
             and all(b["parent_hash"] == a["hash"] and b["timestamp"] > a["timestamp"]
                     for a, b in zip(headers, headers[1:])), "event_header_chain_mismatch")
    for name, header in (("initial", headers[0]), ("terminal", headers[-1])):
        cp = env["checkpoints"][name]
        _require(cp["block_number"] == header["number"] and cp["block_hash"] == header["hash"]
                 and cp["pool"] == env["pool"], "event_checkpoint_identity_mismatch")
        if "tick_lower" in request:
            _require(all(cp.get("range", {}).get(key) == request[key]
                         for key in ("tick_lower", "tick_upper")), "event_checkpoint_range_mismatch")
    _require(all(row["address"] == pool and row["removed"] is False
                 and type(row["block_number"]) is int and lo <= row["block_number"] <= hi
                 and row["block_hash"] == headers[row["block_number"] - lo + 1]["hash"]
                 for row in rows), "event_row_identity_mismatch")
    if previous is not None:
        _require(env["checkpoints"]["initial"] == previous["checkpoints"]["terminal"]
                 and headers[0] == previous["headers"][-1], "event_checkpoint_link_mismatch")
    return blob


def _state(db):
    count, rows = db.execute("SELECT count(*), coalesce(sum(row_count), 0) FROM chunks").fetchone()
    last = db.execute("SELECT start_block, end_block, evidence_sha256, evidence FROM chunks "
                      "ORDER BY ordinal DESC LIMIT 1").fetchone()
    return count, rows, last


def event_history_operation(config, args, progress=None, check_cancel=None, provider=None, cancel_token=None):
    """Inspect locally or append at most max_chunks verified producer slices.

    provider is an injectable UniswapEventProvider-compatible object. Cancellation
    and failure propagate; inspect returns the durable cursor. Progress follows commit.
    """
    required = {"action", "pool_address", "rpc_provider", "start_block", "end_block"}
    _require(isinstance(args, dict) and required <= args.keys()
             and not args.keys() - required - {"chunk_blocks", "max_chunks", "dataset_kind", "expected_next_block",
                                                "tick_lower", "tick_upper"},
             "exact_event_request_required")
    _require(args["action"] in ("inspect", "download")
             and args.get("dataset_kind", "uniswap_pool_events") == "uniswap_pool_events",
             "invalid_event_action_or_dataset")
    request = {key: args[key] for key in ("pool_address", "rpc_provider", "start_block", "end_block")}
    if "tick_lower" in args or "tick_upper" in args:
        lower, upper = args.get("tick_lower"), args.get("tick_upper")
        _require(type(lower) is int and type(upper) is int and -887272 <= lower < upper <= 887272,
                 "paired_event_tick_range_required")
        request.update(tick_lower=lower, tick_upper=upper)
    _address(request["pool_address"])
    _require(isinstance(request["rpc_provider"], str) and request["rpc_provider"] in RPC_ORIGINS,
             "fixed_rpc_provider_required")
    lo, hi = request["start_block"], request["end_block"]
    size, budget = args.get("chunk_blocks", 100), args.get("max_chunks", 1)
    _require(type(lo) is int and type(hi) is int and 1 <= lo <= hi < 2**64
             and type(size) is int and 1 <= size <= 100
             and type(budget) is int and 1 <= budget <= 10, "invalid_event_bounds_or_budget")
    cursor = args.get("expected_next_block")
    _require("expected_next_block" not in args or type(cursor) is int and lo <= cursor <= hi,
             "invalid_expected_next_block")
    metadata = {**request, "chunk_blocks": size, "chain_id": 1}
    request_id = _hash(metadata)
    path = Path(config.paths.artifacts) / "backtest_cache" / "pool-events" / request_id / "archive.sqlite3"
    path = resolve_workspace_path(str(path), root=config.paths.artifacts)
    _require(path.is_relative_to(Path(config.paths.root).resolve()), "archive_outside_workspace")
    store_path = to_workspace_relative(path, Path(config.paths.root))
    expected, downloaded = (hi - lo) // size + 1, 0

    def summary(state=(0, 0, None)):
        count, rows, last = state
        complete = count == expected and last is not None and int(last[1]) == hi
        next_block = None if complete else int(last[1]) + 1 if last else lo
        last_range = json.loads(last[3])["_envelope"].get("range_continuity") if last else None
        return {"ok": True, "result_type": "historical_data",
            "dataset_kind": "uniswap_pool_events", "request_id": request_id, "request": metadata,
            "store_path": store_path, "chunks_completed": count, "chunks_expected": expected,
            "chunks_downloaded": downloaded, "rows": rows,
            "stored_bounds": {"start_block": lo, "end_block": int(last[1])} if last else None,
            "next_block": next_block,
            "next_download_args": None if complete else {**request, "action": "download",
                "dataset_kind": "uniswap_pool_events", "chunk_blocks": size,
                "max_chunks": budget, "expected_next_block": next_block},
            "last_evidence_sha256": last[2] if last else None,
            "last_chunk_range_continuity": {"start_block": int(last[0]), "end_block": int(last[1]),
                                            "receipt": last_range} if last_range else None,
            "status": "ready" if complete else "incomplete", "resumable": not complete,
            "completed": int(last[1]) - lo + 1 if last else 0, "total": hi - lo + 1,
            "unit": "blocks", "phase": "ready" if complete else "incomplete" if inspecting else "downloading",
            "event_range_complete": complete, "coverage_complete": False, "incomplete": True,
            "core_state_verified": False, "core_state_status": "unverified",
            "requested_window_complete": False, "tick_fee_reconstruction_verified": False,
            "replay_input": False}

    def cancelled():
        raise_if_cancelled(cancel_token)
        if check_cancel:
            check_cancel()

    inspecting = args["action"] == "inspect"
    if inspecting and not path.exists():
        return summary()
    if not inspecting:
        cancelled()
        path.parent.mkdir(parents=True, exist_ok=True)
    uri = path.resolve().as_uri() + ("?mode=ro" if inspecting else "?mode=rwc")
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        if not inspecting:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS request (id INTEGER PRIMARY KEY CHECK(id=1), metadata BLOB NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS chunks (ordinal INTEGER PRIMARY KEY, "
                           "start_block TEXT UNIQUE NOT NULL, end_block TEXT NOT NULL, row_count INTEGER NOT NULL, "
                           "evidence_sha256 TEXT NOT NULL, evidence BLOB NOT NULL "
                           "CHECK(typeof(evidence)='blob' AND length(evidence)<=4000000))")
                for table in ("request", "chunks"):
                    for op in ("UPDATE", "DELETE"):
                        db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} "
                                   "BEGIN SELECT RAISE(ABORT, 'immutable event archive'); END")
                db.execute("INSERT OR IGNORE INTO request VALUES (1, ?)", (_canonical(metadata),))
        _require(db.execute("SELECT metadata FROM request WHERE id=1").fetchone() == (_canonical(metadata),),
                 "archive_request_mismatch")
        if inspecting:
            return summary(_state(db))
        for _ in range(budget):
            cancelled()
            # ponytail: one writer holds the lock for one bounded producer call.
            # Parallel fetching would need reservations; no retry loop here.
            with db:
                db.execute("BEGIN IMMEDIATE")
                state = _state(db)
                count, _, last = state
                start = int(last[1]) + 1 if last else lo
                if cursor is not None:
                    _require(cursor <= start, "expected_next_block_ahead_of_archive")
                    if cursor < start:
                        return summary(state)
                if start > hi:
                    break
                previous = json.loads(last[3]) if last else None
                if previous:
                    _verify(previous, previous["_envelope"]["request_args"], None)
                if provider is None:
                    from nerya.data_api.uniswap_events import UniswapEventProvider
                    provider = UniswapEventProvider()
                part = {**request, "start_block": start, "end_block": min(hi, start + size - 1)}
                cancelled()
                output = provider.pool_event_slice(DataApiContext(config_like=config, cancel_token=cancel_token), part)
                blob = _verify(output, part, previous["_envelope"] if previous else None)
                db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)",
                           (count, str(start), str(part["end_block"]), len(output["rows"]),
                            output["_envelope"]["evidence_sha256"], blob))
                cancelled()
            downloaded += 1
            if cursor is not None:
                cursor = part["end_block"] + 1
            if progress:
                progress(summary(_state(db)))
        return summary(_state(db))

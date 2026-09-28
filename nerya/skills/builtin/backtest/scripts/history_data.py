"""Download once, repair gaps, and replay locally. Shared by tools and CLI.

This script owns network orchestration; HistoryStore only owns durable data.
No strategy, account order, scheduler or Agent model is executed here.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .....core.config import load_config as load_workspace_config
from .....data.history_store import (
    HistoryDataError, HistoryStore, coverage, is_sample, market_key,
    normalize_row, rows_hash, timeframe_seconds, window_grid,
)


class HistoryIncompleteError(RuntimeError):
    def __init__(self, receipt: dict[str, Any]):
        self.receipt = receipt
        super().__init__(f"{receipt['market']} {receipt['timeframe']}: "
                         f"{receipt['missing_bars']} historical bars missing; "
                         "download is resumable, not a complete requested window")


def _derive_from_local_finer(
    store: HistoryStore,
    market: str,
    target_tf: str,
    start: int,
    end: int,
    *,
    closed_before: int,
) -> tuple[int, str | None]:
    """Materialize missing target candles from complete verified finer bars.

    This is a lossless OHLCV resample only: the target interval must be an
    integer multiple of the source interval and every expected source candle
    in a target bucket must exist. Partial buckets are never synthesized.
    Native target candles already in the store win and are not overwritten.
    """
    target_step = timeframe_seconds(target_tf)
    existing = {int(row["ts"]) for row in store.read(market, target_tf, start, end)}
    candidates = []
    for source_tf in store.available_timeframes(market):
        source_step = timeframe_seconds(source_tf)
        if source_step < target_step and target_step % source_step == 0:
            candidates.append((source_step, source_tf))
    # Prefer the closest available source (1h -> 4h over 5m -> 4h): fewer
    # rows to read while preserving exactly the same OHLCV aggregation.
    candidates.sort(reverse=True)
    for source_step, source_tf in candidates:
        source_rows = store.read(market, source_tf, start, end)
        source_by_ts = {int(row["ts"]): row for row in source_rows}
        if not source_by_ts:
            continue
        first, stop, _ = window_grid(start, end, target_tf)
        factor = target_step // source_step
        derived: list[dict[str, Any]] = []
        for bucket in range(first, stop, target_step):
            if bucket in existing or bucket + target_step > closed_before:
                continue
            stamps = [bucket + i * source_step for i in range(factor)]
            parts = [source_by_ts.get(ts) for ts in stamps]
            if any(row is None for row in parts):
                continue
            complete = [row for row in parts if row is not None]
            derived.append({
                "ts": bucket,
                "open": float(complete[0]["open"]),
                "high": max(float(row["high"]) for row in complete),
                "low": min(float(row["low"]) for row in complete),
                "close": float(complete[-1]["close"]),
                "volume": sum(float(row["volume"]) for row in complete),
                "_envelope": {
                    "source": "local_resample",
                    "mode": "cached",
                    "derived_from_timeframe": source_tf,
                },
            })
        if derived:
            store.put(
                market,
                target_tf,
                derived,
                source=f"local_resample:{source_tf}",
                verified=True,
                closed_before=closed_before,
            )
            return len(derived), source_tf
    return 0, None


def utc_seconds(value: Any) -> int:
    if isinstance(value, bool):
        raise HistoryDataError("boolean date is invalid")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise HistoryDataError("date must be finite")
        return int(value)
    text = str(value).strip()
    if text.isdigit() and len(text) != 8:
        return int(text)
    try:
        date = datetime.strptime(text, "%Y%m%d") if len(text) == 8 and text.isdigit() else datetime.fromisoformat(text.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return int(date.timestamp())
    except (ValueError, OverflowError) as exc:
        raise HistoryDataError("date must be UTC ISO-8601, YYYYMMDD or Unix seconds") from exc


def resolve_window(*, start: Any = None, end: Any = None, days: int = 180,
                   now: int | None = None) -> tuple[int, int]:
    now = int(time.time()) if now is None else int(now)
    hi = utc_seconds(end) if end not in (None, "") else now
    if isinstance(days, bool) or not math.isfinite(float(days)) or float(days) <= 0:
        raise HistoryDataError("days must be finite and positive")
    lo = utc_seconds(start) if start not in (None, "") else hi - int(float(days) * 86400)
    if lo < 0 or hi <= lo or hi > now:
        raise HistoryDataError("expected 0 <= start < end <= current UTC time")
    return lo, hi


def store_root(config: Any, custom: str | Path | None = None) -> Path:
    workspace = config.paths.root.resolve()
    root = Path(custom).expanduser() if custom else config.paths.artifacts / "backtest_cache"
    if not root.is_absolute():
        root = workspace / root
    root = root.resolve()
    if not root.is_relative_to(workspace):
        raise HistoryDataError("historical data directory must remain inside the workspace")
    return root


def _segments(start: int, end: int, tf: str, *, monthly: bool):
    """Archive-sized chunks for supported venues, bounded API pages otherwise."""
    cursor = int(start)
    step = timeframe_seconds(tf)
    while cursor < end:
        if monthly:
            date = datetime.fromtimestamp(cursor, timezone.utc)
            following = date.replace(year=date.year + (date.month == 12),
                                     month=date.month % 12 + 1, day=1,
                                     hour=0, minute=0, second=0, microsecond=0)
            stop = min(end, int(following.timestamp()))
        else:
            stop = min(end, cursor + step * 1000)
        yield cursor, stop
        cursor = stop


def prepare_series(
    store: HistoryStore, market: str, tf: str, start: int, end: int, *,
    config_like: Any = None, offline: bool = False, max_requests: int = 2048,
    derive_local: bool = True,
    retries: int = 2, timeout_seconds: float = 300,
    progress: Callable[[dict[str, Any]], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
    fetch: Callable[..., list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from .data_cache import _binance_vision_base, _source_fetch

    key = market_key(market)
    step = timeframe_seconds(tf)
    if max_requests < 1 or not 0 <= retries <= 5 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise HistoryDataError("invalid download budget")
    # The high boundary is the last fully CLOSED candle boundary. Future or
    # forming candles are never marked missing and never enter the store.
    now = int(time.time())
    end = min(int(end), now - now % step)
    if int(start) >= end:
        raise HistoryDataError("requested interval contains no closed candles")
    rows = store.read(key, tf, int(start), end)
    by_ts = {int(row["ts"]): row for row in rows}
    initial = len(rows)
    derived_rows, derived_from = (0, None)
    if derive_local and not coverage(rows, int(start), end, tf)["complete"]:
        derived_rows, derived_from = _derive_from_local_finer(
            store, key, tf, int(start), end, closed_before=now
        )
    if derived_rows:
        rows = store.read(key, tf, int(start), end)
        by_ts = {int(row["ts"]): row for row in rows}
    receipt: dict[str, Any] = {
        "job_id": "history_" + uuid.uuid4().hex,
        "market": key, "timeframe": tf, "status": "preparing",
        "store_path": str(store.path), "cached_rows": initial,
        "derived_rows": derived_rows, "derived_from_timeframe": derived_from,
        "downloaded_rows": 0, "requests": 0, "errors": [],
        "offline": offline, "resumable": True,
    }

    def publish(status: str) -> None:
        receipt.update(coverage(by_ts.values(), int(start), end, tf))
        receipt["status"] = status
        receipt["downloaded_rows"] = max(0, len(by_ts) - initial - derived_rows)
        receipt["updated_at"] = datetime.now(timezone.utc).isoformat()
        if not offline:
            store.record_job(receipt)
        if progress:
            progress(dict(receipt))

    missing = coverage(rows, int(start), end, tf)["missing_ranges"]
    if offline or not missing:
        publish("ready" if not missing else "incomplete")
        receipt["sha256"] = rows_hash(rows)
        return rows, receipt

    loader = fetch or _source_fetch
    monthly = _binance_vision_base(key) is not None
    segments = [
        segment
        for gap in missing
        for segment in _segments(gap["start"], gap["end"], tf, monthly=monthly)
    ]
    # Binance archive windows can predate a newly listed market by months.
    # Probe the newest monthly chunks first so a bounded download can retain
    # real post-listing candles instead of spending its whole timeout proving
    # that old, pre-listing months are empty. Full-window completeness is still
    # evaluated against the original requested range below.
    if monthly:
        segments.reverse()
    queue = deque(segments)
    deadline = time.monotonic() + float(timeout_seconds)
    publish("downloading")
    try:
        while queue:
            if check_cancel:
                check_cancel()
            if time.monotonic() >= deadline or receipt["requests"] >= max_requests:
                receipt["errors"].append({"reason": "download_budget_exhausted", "retryable": True})
                break
            lo, hi = queue.popleft()
            # Another worker may already have filled this interval. Refresh it
            # before network IO; row uniqueness also protects concurrent commits.
            known = store.read(key, tf, lo, hi)
            by_ts.update((row["ts"], row) for row in known)
            if coverage(known, lo, hi, tf)["complete"]:
                continue
            previous_count = len(by_ts)
            error: Exception | None = None
            for attempt in range(retries + 1):
                if check_cancel:
                    check_cancel()
                if time.monotonic() >= deadline or receipt["requests"] >= max_requests:
                    break
                receipt["requests"] += 1
                try:
                    batch = loader(key, tf=tf, start=lo, end=hi - 1,
                                   allow_mock=False, config_like=config_like)
                    if not batch:
                        raise RuntimeError("source returned no rows for requested interval")
                    accepted = []
                    grid_first, _, _ = window_grid(lo, hi, tf)
                    for row in batch:
                        if is_sample(key, row):
                            raise HistoryDataError("source returned sample data for historical request")
                        item = normalize_row(row)
                        # Dropping off-grid rows is not resampling: an hourly
                        # open aligned to 4h still contains only ONE hour's OHLC.
                        # Reject a mixed-period response, never certify it.
                        aligned = item["ts"] >= grid_first and (item["ts"] - grid_first) % step == 0
                        if lo <= item["ts"] < hi and not aligned:
                            raise HistoryDataError(f"source returned off-grid {tf} candle at {item['ts']}")
                        if lo <= item["ts"] < hi and item["ts"] + step <= now and aligned:
                            accepted.append(item)
                    if not accepted:
                        raise RuntimeError("source did not honor the requested historical interval")
                    origins = sorted({str((row.get("_envelope") or {}).get("source") or row.get("source") or "public_market_data") for row in accepted})
                    store.put(key, tf, accepted, source=",".join(origins), closed_before=now)
                    by_ts.update((row["ts"], row) for row in accepted)
                    error = None
                    break
                except HistoryDataError:
                    # Invalid/sample data is deterministic, not a retryable
                    # network failure. No part of this segment was committed.
                    raise
                except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                    error = exc
                    if attempt < retries and time.monotonic() < deadline:
                        time.sleep(min(0.25 * 2 ** attempt, max(0, deadline - time.monotonic())))
            if error:
                receipt["errors"].append({"start": lo, "end": hi, "reason": str(error)[:400], "retryable": True})
            # A partial segment is never marked covered. Retry its actual holes
            # only when useful progress was made; a stalled provider cannot loop.
            segment_rows = [row for ts, row in by_ts.items() if lo <= ts < hi]
            holes = coverage(segment_rows, lo, hi, tf)["missing_ranges"]
            if holes and len(by_ts) > previous_count:
                queue.extendleft(reversed([(gap["start"], gap["end"]) for gap in holes]))
            elif holes and not error:
                receipt["errors"].append({"start": lo, "end": hi, "reason": "source_made_no_progress", "retryable": True})
            publish("downloading")
    except BaseException as exc:
        receipt["errors"].append({"reason": type(exc).__name__, "message": str(exc)[:400]})
        publish("cancelled" if type(exc).__name__ in {"CancelledError", "KeyboardInterrupt"} else "failed")
        raise

    rows = store.read(key, tf, int(start), end)
    by_ts = {row["ts"]: row for row in rows}
    receipt["sha256"] = rows_hash(rows)
    publish("ready" if coverage(rows, int(start), end, tf)["complete"] else "incomplete")
    return rows, receipt


def history_operation(config: Any, arguments: dict[str, Any], *, progress=None, check_cancel=None) -> dict[str, Any]:
    """Stable JSON interface used by historical_data and nerya data."""
    action = str(arguments.get("action") or "list")
    store = HistoryStore(store_root(config, arguments.get("data_dir")))
    if action == "list":
        return {"ok": True, "result_type": "historical_data", "datasets": store.inventory(), "store_path": str(store.path)}
    if action == "status":
        job = store.job(str(arguments.get("job_id") or ""))
        return {"ok": job is not None, "result_type": "historical_data", "job": job}
    markets = arguments.get("markets") or []
    timeframes = arguments.get("timeframes") or []
    if not isinstance(markets, list) or not markets or not isinstance(timeframes, list) or not timeframes:
        raise HistoryDataError("markets and timeframes must be non-empty arrays")
    if len(markets) * len(timeframes) > 100:
        raise HistoryDataError("prepare at most 100 market/timeframe datasets per call")
    lo, hi = resolve_window(start=arguments.get("start"), end=arguments.get("end"), days=arguments.get("days", 180))
    results = []
    if action not in {"download", "inspect"}:
        raise HistoryDataError("action must be list, inspect, download or status")
    for market in dict.fromkeys(markets):
        for tf in dict.fromkeys(timeframes):
            _, receipt = prepare_series(store, market, tf, lo, hi, config_like=config,
                offline=action == "inspect", derive_local=action != "inspect", max_requests=int(arguments.get("max_requests", 2048)),
                timeout_seconds=float(arguments.get("timeout_seconds", 300)),
                progress=progress, check_cancel=check_cancel)
            results.append(receipt)
    return {"ok": all(r["complete"] for r in results), "result_type": "historical_data",
            "status": "ready" if all(r["complete"] for r in results) else "incomplete",
            "datasets": results, "store_path": str(store.path),
            "message": "Existing historical rows are retained. Incomplete datasets can resume by repeating the same download; only gaps are fetched."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare reusable local historical data")
    parser.add_argument("action", choices=["list", "inspect", "download", "status"])
    parser.add_argument("--workspace")
    parser.add_argument("--markets", nargs="+")
    parser.add_argument("--timeframes", nargs="+")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--data-dir")
    parser.add_argument("--job-id")
    args = parser.parse_args(argv)
    try:
        out = history_operation(load_workspace_config(args.workspace), vars(args))
    except (HistoryDataError, OSError, RuntimeError) as exc:
        out = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    print(json.dumps(out, ensure_ascii=False, allow_nan=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

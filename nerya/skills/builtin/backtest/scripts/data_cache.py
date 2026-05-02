"""Read-through candle cache for backtests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .....data.candles import fetch_candles


class NoHistoricalDataError(RuntimeError):
    """Raised when the candle source returns no usable rows."""


@dataclass(frozen=True)
class CandleRange:
    market: str
    tf: str
    start: int
    end: int


def _tf_seconds(tf: str) -> int:
    unit = tf[-1].lower()
    qty = int(tf[:-1] or 1)
    if unit == "m":
        return qty * 60
    if unit == "h":
        return qty * 3600
    if unit == "d":
        return qty * 86400
    raise ValueError(f"unsupported timeframe: {tf}")


def cache_path_for(rng: CandleRange, cache_root: str | Path) -> Path:
    venue, _, symbol = rng.market.partition(":")
    safe_symbol = (symbol or rng.market).replace("/", "_").replace("\\", "_").replace(":", "_")
    return (
        Path(cache_root)
        / "candles"
        / (venue or "unknown").upper()
        / safe_symbol.upper()
        / rng.tf
        / f"{rng.start}_{rng.end}.parquet"
    )


def get_candles(
    market: str,
    tf: str,
    start: int,
    end: int,
    cache_root: str | Path,
    *,
    allow_mock: bool | None = None,
) -> list[dict[str, Any]]:
    rng = CandleRange(market=market, tf=tf, start=int(start), end=int(end))
    path = cache_path_for(rng, cache_root)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    rows = _source_fetch(market, tf=tf, start=start, end=end, allow_mock=allow_mock)
    if not rows:
        raise NoHistoricalDataError(f"no historical candles for {market} {tf}")
    filtered = [
        _normalise_row(row)
        for row in rows
        if start <= int(row.get("ts", row.get("ts_ms", 0))) <= end
    ]
    if not filtered:
        raise NoHistoricalDataError(f"no historical candles in requested range for {market} {tf}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # V1 keeps a stable .parquet filename but stores JSON so the skill has no
    # pyarrow dependency. The suffix is reserved for a v2 transparent swap.
    path.write_text(json.dumps(filtered, ensure_ascii=False), encoding="utf-8")
    return filtered


def _source_fetch(
    market: str,
    *,
    tf: str,
    start: int,
    end: int,
    allow_mock: bool | None = None,
) -> list[dict[str, Any]]:
    count = max(1, int((int(end) - int(start)) / _tf_seconds(tf)) + 5)
    return list(fetch_candles(market, count=count, interval=tf, allow_mock=allow_mock) or [])


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    ts = int(row.get("ts", row.get("ts_ms", 0)))
    if ts > 10_000_000_000:
        ts = ts // 1000
    return {
        "ts": ts,
        "open": float(row.get("open", 0.0)),
        "high": float(row.get("high", 0.0)),
        "low": float(row.get("low", 0.0)),
        "close": float(row.get("close", 0.0)),
        "volume": float(row.get("volume", 0.0)),
    }


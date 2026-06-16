"""Read-through candle cache for backtests."""

from __future__ import annotations

import json
import csv
import io
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .....data.candles import fetch_candles


class NoHistoricalDataError(RuntimeError):
    """Raised when the candle source returns no usable rows."""


_BINANCE_VISION_REQUEST_TIMEOUT_SECONDS = 3.0
_BINANCE_VISION_TOTAL_TIMEOUT_SECONDS = 20.0


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
    config_like: Any | None = None,
) -> list[dict[str, Any]]:
    rng = CandleRange(market=market, tf=tf, start=int(start), end=int(end))
    path = cache_path_for(rng, cache_root)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    rows = _source_fetch(
        market,
        tf=tf,
        start=start,
        end=end,
        allow_mock=allow_mock,
        config_like=config_like,
    )
    if not rows:
        raise NoHistoricalDataError(f"no historical candles for {market} {tf}")
    filtered = [
        _normalise_row(row)
        for row in rows
        if start <= int(row.get("ts", row.get("ts_ms", 0))) <= end
    ]
    if not filtered:
        raise NoHistoricalDataError(f"no historical candles in requested range for {market} {tf}")
    if not _is_mock_result(market, filtered):
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
    config_like: Any | None = None,
) -> list[dict[str, Any]]:
    count = max(1, int((int(end) - int(start)) / _tf_seconds(tf)) + 5)
    if _binance_vision_base(market) is not None:
        rows = _fetch_binance_vision(market, tf=tf, start=start, end=end)
        if rows:
            return rows[-count:]
    return list(
        fetch_candles(
            market,
            count=count,
            interval=tf,
            allow_mock=allow_mock,
            config_like=config_like,
            start=start,
            end=end,
        )
        or []
    )


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    ts = _to_seconds(row.get("ts", row.get("ts_ms", 0)))
    return {
        "ts": ts,
        "open": float(row.get("open", 0.0)),
        "high": float(row.get("high", 0.0)),
        "low": float(row.get("low", 0.0)),
        "close": float(row.get("close", 0.0)),
        "volume": float(row.get("volume", 0.0)),
    }


def _venue_of(market: str) -> str:
    return market.split(":", 1)[0].upper() if ":" in market else ""


def _symbol_of(market: str) -> str:
    return market.split(":", 1)[-1].replace("/", "").replace("-", "").upper()


def _binance_vision_base(market: str) -> str | None:
    venue = _venue_of(market)
    if venue in {"BINANCE", "BINANCE_SPOT"}:
        return "data/spot/daily/klines"
    if venue in {
        "BINANCE_PERPETUAL",
        "BINANCE_PERP",
        "BINANCEUSDM",
        "BINANCE_USDM",
        "BINANCE_FUTURES",
        "BINANCE_UM",
    }:
        return "data/futures/um/daily/klines"
    if venue in {
        "BINANCE_COINM_PERPETUAL",
        "BINANCE_COINM",
        "BINANCECOINM",
        "BINANCE_CM",
    }:
        return "data/futures/cm/daily/klines"
    return None


def _fetch_binance_vision(
    market: str,
    *,
    tf: str,
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    symbol = _symbol_of(market)
    base = _binance_vision_base(market)
    if not symbol or base is None:
        return []
    start_day = datetime.fromtimestamp(int(start), tz=timezone.utc).date()
    end_day = datetime.fromtimestamp(int(end), tz=timezone.utc).date()
    rows: list[dict[str, Any]] = []
    day = start_day
    deadline = time.monotonic() + _binance_vision_total_timeout_seconds()
    while day <= end_day:
        if time.monotonic() >= deadline:
            break
        url = (
            f"https://data.binance.vision/{base}/"
            f"{symbol}/{tf}/{symbol}-{tf}-{day.isoformat()}.zip"
        )
        rows.extend(_read_binance_vision_zip(url, start=start, end=end))
        day += timedelta(days=1)
    rows.sort(key=lambda row: int(row["ts"]))
    return rows


def _read_binance_vision_zip(url: str, *, start: int, end: int) -> list[dict[str, Any]]:
    payload = _download_binance_vision_payload(
        url,
        timeout=_binance_vision_request_timeout_seconds(),
    )
    if not payload:
        return []

    out: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".csv"):
                    continue
                with zf.open(name) as fh:
                    text = io.TextIOWrapper(fh, encoding="utf-8", newline="")
                    for row in csv.reader(text):
                        if len(row) < 6 or row[0] == "open_time":
                            continue
                        ts = _to_seconds(row[0])
                        if ts < int(start) or ts > int(end):
                            continue
                        out.append({
                            "ts": ts,
                            "open": float(row[1]),
                            "high": float(row[2]),
                            "low": float(row[3]),
                            "close": float(row[4]),
                            "volume": float(row[5]),
                        })
    except Exception:
        return []
    return out


def _download_binance_vision_payload(url: str, *, timeout: float) -> bytes | None:
    code = (
        "import sys, urllib.error, urllib.request\n"
        "url = sys.argv[1]\n"
        "timeout = float(sys.argv[2])\n"
        "try:\n"
        "    with urllib.request.urlopen(url, timeout=timeout) as resp:\n"
        "        sys.stdout.buffer.write(resp.read())\n"
        "except urllib.error.HTTPError as exc:\n"
        "    sys.exit(0 if exc.code == 404 else 1)\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code, url, str(float(timeout))],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(float(timeout) + 1.0, 1.0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _binance_vision_request_timeout_seconds() -> float:
    return _float_env(
        "NERYA_BACKTEST_BINANCE_VISION_REQUEST_TIMEOUT_SECONDS",
        _BINANCE_VISION_REQUEST_TIMEOUT_SECONDS,
        minimum=0.5,
    )


def _binance_vision_total_timeout_seconds() -> float:
    return _float_env(
        "NERYA_BACKTEST_BINANCE_VISION_TOTAL_TIMEOUT_SECONDS",
        _BINANCE_VISION_TOTAL_TIMEOUT_SECONDS,
        minimum=0.0,
    )


def _float_env(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return max(float(minimum), value)


def _to_seconds(value: Any) -> int:
    ts = int(float(value or 0))
    while ts > 10_000_000_000:
        ts //= 1000
    return ts


def _is_mock_result(market: str, rows: list[dict[str, Any]]) -> bool:
    if _venue_of(market) in {"MOCK", "PAPER"}:
        return False
    return bool(rows and isinstance(rows[0], dict) and rows[0].get("_envelope", {}).get("truth") == "mock")

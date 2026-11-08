"""Read-through candle cache for backtests."""

from __future__ import annotations

import csv
import io
import os
import time
import zipfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .....data.candles import fetch_candles
from .....data.history_store import HistoryStore, HistoryDataError, normalize_row, timeframe_seconds, is_sample


class NoHistoricalDataError(RuntimeError):
    """Raised when the candle source returns no usable rows."""

    def __init__(self, message: str, *, reason: str = 'no_historical_data',
                 retryable: bool | None = None, receipt: dict[str, Any] | None = None):
        super().__init__(message)
        self.reason = reason
        self.retryable = retryable
        self.receipt = receipt


_BINANCE_VISION_REQUEST_TIMEOUT_SECONDS = 3.0
_BINANCE_VISION_TOTAL_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class CandleRange:
    market: str
    tf: str
    start: int
    end: int


def _tf_seconds(tf: str) -> int:
    return timeframe_seconds(tf)


class HistoricalRows(list):
    """List-compatible result carrying explicit download/coverage evidence."""
    def __init__(self, rows, receipt):
        super().__init__(rows)
        self.history_receipt = receipt


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
    offline: bool = False,
    progress=None,
    check_cancel=None,
    timeout_seconds: float = 300,
) -> list[dict[str, Any]]:
    if _venue_of(market) in {"MOCK", "PAPER"} or allow_mock is True:
        if offline:
            raise NoHistoricalDataError("sample data is not stored in the historical cache")
        rows = _source_fetch(market, tf=tf, start=start, end=end,
                             allow_mock=allow_mock, config_like=config_like)
        filtered = [_normalise_row(row) for row in rows]
        filtered = [row for row in filtered if int(start) <= row["ts"] <= int(end)]
        if not filtered:
            raise NoHistoricalDataError(f"no candles in requested range for {market} {tf}")
        return HistoricalRows(filtered, {"status": "sample", "complete": False, "persistent": False})
    from .history_data import prepare_series
    rows, receipt = prepare_series(HistoryStore(cache_root), market, tf, int(start), int(end) + 1,
        config_like=config_like, offline=offline, progress=progress,
        check_cancel=check_cancel, timeout_seconds=timeout_seconds)
    if not rows:
        raise NoHistoricalDataError(
            f"no historical candles for {market} {tf}; download status={receipt['status']}",
            reason='backtest_data_incomplete', retryable=True, receipt=receipt)
    return HistoricalRows(rows, receipt)


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
    return normalize_row(row)


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
        # Long ranges use one monthly archive instead of 28-31 daily requests.
        # The caller commits each month and checks actual gaps; hitting this
        # bounded fetch deadline can never certify an incomplete year as full.
        month_end = (day.replace(year=day.year + (day.month == 12), month=day.month % 12 + 1, day=1) - timedelta(days=1))
        if (min(month_end, end_day) - day).days >= 2:
            month = day.strftime("%Y-%m")
            monthly_base = base.replace("/daily/", "/monthly/")
            url = f"https://data.binance.vision/{monthly_base}/{symbol}/{tf}/{symbol}-{tf}-{month}.zip"
            monthly = _read_binance_vision_zip(url, start=start, end=end)
            if monthly:
                rows.extend(monthly)
                day = month_end + timedelta(days=1)
                continue
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
            if sum(info.file_size for info in zf.infolist()) > 256 * 1024 * 1024:
                raise HistoryDataError("expanded historical archive exceeds 256 MiB")
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
                            "_envelope": {"source": "binance_vision", "mode": "live"},
                        })
    except (zipfile.BadZipFile, UnicodeError, ValueError, OSError) as exc:
        raise HistoryDataError(f"invalid historical archive: {exc}") from exc
    return out


def _download_binance_vision_payload(url: str, *, timeout: float) -> bytes | None:
    # No interpreter process per archive; use the normal, bounded HTTP path.
    # Only absent archives return None. Network and authorization errors remain
    # errors, so a transient failure is not mistaken for unavailable history.
    request = urllib.request.Request(url, headers={"User-Agent": "Nerya/history-v2"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(128 * 1024 * 1024 + 1)
            if len(payload) > 128 * 1024 * 1024:
                raise HistoryDataError("historical archive exceeds download size limit")
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code in {404, 410}:
                return None
            if (exc.code == 429 or 500 <= exc.code < 600) and attempt < 2:
                try:
                    delay = float(exc.headers.get("Retry-After", 0.5 * 2 ** attempt))
                except (TypeError, ValueError):
                    delay = 0.5 * 2 ** attempt
                time.sleep(max(0, min(delay, 8)))
                continue
            raise


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
    return any(is_sample(market, row) for row in rows)

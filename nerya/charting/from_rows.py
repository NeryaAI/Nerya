"""Convenience builders that turn flat dict-rows into chart_blocks.

The composer in ``nerya.charting.composer`` is the canonical entry
point but it expects already-shaped ``ChartSeries`` data. Most skills
hold their data in flat-row form (``[{"date": "...", "close": 123},
...]``) and want a one-line "draw a line / candle of column X" call.

This module owns those one-liners:

* :func:`line_chart_from_rows` — pick a time column and a value column;
  get back a ``ChartBlock.as_dict()`` ready to drop into
  ``out["chart_blocks"]``.
* :func:`candle_chart_from_rows` — same, but for OHLCV rows.
* :func:`equity_curve_from_rows` — common case: time + value with
  drawdown overlay auto-computed.

Each helper accepts a ``BulkContext`` (or ``None`` to default to
``inline``) and forwards path / chart_id / title metadata to the
composer. They never raise on empty input — they return ``None`` so
callers can drop the chart entirely if there's nothing to draw.

The helpers are *intentionally lenient* on column naming. Real data
sources rename ``"date"`` to ``"timestamp"`` to ``"ts"`` to ``"t"`` on
a whim; rather than force every skill to pre-normalise, we accept a
list of candidate names and pick the first present.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .composer import BulkContext, build_chart_block


# Column-name lookup tables. Add to these as new data sources show up
# rather than special-casing in every skill.
_TIME_KEYS = ("time", "ts", "timestamp", "date", "datetime", "t", "as_of")
_CLOSE_KEYS = ("close", "c", "value", "price", "last")
_OPEN_KEYS = ("open", "o")
_HIGH_KEYS = ("high", "h")
_LOW_KEYS = ("low", "l")
_VOLUME_KEYS = ("volume", "v", "vol")


def _coerce_unix_seconds(value: Any) -> Optional[int]:
    """Best-effort convert a row's time field to unix seconds (UTC).

    Accepts ints / floats (heuristic on magnitude for ms vs s),
    ISO 8601 strings (``2024-01-15``, ``2024-01-15T10:30:00Z``), and
    plain ``YYYY-MM-DD``. Returns ``None`` when nothing makes sense.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        # ``True == 1`` would silently coerce to epoch+1s; reject it.
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:  # millis since epoch
            return int(v / 1000.0)
        if v > 1e9:  # seconds since epoch
            return int(v)
        if v > 0:  # day index? assume seconds anyway
            return int(v)
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Try ISO with optional Z.
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s.replace("Z", "+0000"), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except ValueError:
                continue
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_key(row: dict[str, Any], candidates: Iterable[str]) -> Optional[str]:
    """Return the first key in ``candidates`` that exists on ``row``."""

    for key in candidates:
        if key in row:
            return key
    return None


def line_chart_from_rows(
    rows: list[dict[str, Any]],
    *,
    title: str,
    skill: str,
    action: str,
    value_keys: Iterable[str] = _CLOSE_KEYS,
    time_keys: Iterable[str] = _TIME_KEYS,
    series_name: str = "value",
    chart_id: Optional[str] = None,
    path: str = "inline",
    ctx: Optional[BulkContext] = None,
    insights: Iterable[str] = (),
    color: Optional[str] = None,
    subtitle: Optional[str] = None,
    as_of: str = "",
) -> Optional[dict[str, Any]]:
    """Build a single-line ChartBlock dict from row-shaped data.

    Returns ``None`` when ``rows`` is empty or no row has both a
    parseable time and a numeric value. The caller decides whether to
    omit the ``chart_blocks`` field entirely or to surface a warning.
    """

    if not rows:
        return None
    first = rows[0]
    if not isinstance(first, dict):
        return None
    time_key = _pick_key(first, time_keys)
    value_key = _pick_key(first, value_keys)
    if not time_key or not value_key:
        return None

    points: list[dict[str, Any]] = []
    for row in rows:
        t = _coerce_unix_seconds(row.get(time_key))
        v = _coerce_float(row.get(value_key))
        if t is None or v is None:
            continue
        points.append({"time": t, "value": v})
    if not points:
        return None
    points.sort(key=lambda p: p["time"])

    series_payload: dict[str, Any] = {"type": "line", "name": series_name, "data": points}
    if color:
        series_payload["color"] = color

    block = build_chart_block(
        chart_kind="line",
        title=title,
        subtitle=subtitle,
        series=[series_payload],
        source={"skill": skill, "action": action, "as_of": as_of},
        path=path,  # type: ignore[arg-type]
        ctx=ctx,
        chart_id=chart_id,
        insights=list(insights),
    )
    return block.as_dict()


def candle_chart_from_rows(
    rows: list[dict[str, Any]],
    *,
    title: str,
    skill: str,
    action: str,
    chart_id: Optional[str] = None,
    path: str = "inline",
    ctx: Optional[BulkContext] = None,
    subtitle: Optional[str] = None,
    insights: Iterable[str] = (),
    as_of: str = "",
) -> Optional[dict[str, Any]]:
    """Build an OHLCV candlestick ChartBlock dict from row-shaped data.

    Looks for ``time``/``ts``/``date`` + ``open``/``high``/``low``/
    ``close`` (with the usual one-letter aliases). Skips rows missing
    any of the four prices. Volume is optional.
    """

    if not rows:
        return None
    first = rows[0]
    if not isinstance(first, dict):
        return None
    tk = _pick_key(first, _TIME_KEYS)
    ok = _pick_key(first, _OPEN_KEYS)
    hk = _pick_key(first, _HIGH_KEYS)
    lk = _pick_key(first, _LOW_KEYS)
    ck = _pick_key(first, _CLOSE_KEYS)
    if not (tk and ok and hk and lk and ck):
        return None
    vk = _pick_key(first, _VOLUME_KEYS)

    candles: list[dict[str, Any]] = []
    for row in rows:
        t = _coerce_unix_seconds(row.get(tk))
        o = _coerce_float(row.get(ok))
        h = _coerce_float(row.get(hk))
        l_ = _coerce_float(row.get(lk))
        c = _coerce_float(row.get(ck))
        if t is None or None in (o, h, l_, c):
            continue
        candle = {"time": t, "open": o, "high": h, "low": l_, "close": c}
        if vk is not None:
            v = _coerce_float(row.get(vk))
            if v is not None:
                candle["volume"] = v
        candles.append(candle)
    if not candles:
        return None
    candles.sort(key=lambda p: p["time"])

    block = build_chart_block(
        chart_kind="candlestick",
        title=title,
        subtitle=subtitle,
        series=[{"type": "candlestick", "name": "ohlc", "data": candles}],
        source={"skill": skill, "action": action, "as_of": as_of},
        path=path,  # type: ignore[arg-type]
        ctx=ctx,
        chart_id=chart_id,
        insights=list(insights),
    )
    return block.as_dict()


def equity_curve_from_rows(
    rows: list[dict[str, Any]],
    *,
    title: str = "Equity curve",
    skill: str = "backtest",
    action: str = "render_chart",
    value_keys: Iterable[str] = ("equity", "value", "balance", "nav"),
    time_keys: Iterable[str] = _TIME_KEYS,
    chart_id: Optional[str] = None,
    path: str = "inline",
    ctx: Optional[BulkContext] = None,
    insights: Iterable[str] = (),
    initial_capital: Optional[float] = None,
    as_of: str = "",
) -> Optional[dict[str, Any]]:
    """Equity curve + auto-derived drawdown overlay.

    Same input shape as :func:`line_chart_from_rows`; returns a
    multi-series block (``equity`` line + ``drawdown_pct`` overlay
    line) so the dashboard can show both with a single envelope.

    We build the equity + drawdown series *before* calling the
    composer so both end up persisted together when ``path="bulk"``.
    Doing it the other way round (composer first, mutate after) would
    desync the overlay from the artifact, since the composer rewrites
    inline ``series.data`` to ``data_uri`` references.
    """

    if not rows:
        return None
    first = rows[0] if isinstance(rows[0], dict) else None
    if first is None:
        return None
    time_key = _pick_key(first, time_keys)
    value_key = _pick_key(first, value_keys)
    if not time_key or not value_key:
        return None

    equity_points: list[dict[str, Any]] = []
    for row in rows:
        t = _coerce_unix_seconds(row.get(time_key))
        v = _coerce_float(row.get(value_key))
        if t is None or v is None:
            continue
        equity_points.append({"time": t, "value": v})
    if not equity_points:
        return None
    equity_points.sort(key=lambda p: p["time"])

    drawdown: list[dict[str, Any]] = []
    peak: float | None = None
    for p in equity_points:
        v = float(p["value"])
        peak = v if peak is None else max(peak, v)
        dd = -((peak - v) / peak * 100.0) if peak else 0.0
        drawdown.append({"time": p["time"], "value": round(dd, 4)})

    insights_list = list(insights)
    if initial_capital:
        last_value = float(equity_points[-1]["value"])
        total_return = (last_value / initial_capital - 1.0) * 100.0
        insights_list.insert(0, f"Total return: {total_return:+.2f}%")

    block = build_chart_block(
        chart_kind="line",
        title=title,
        series=[
            {"type": "line", "name": "equity", "data": equity_points, "color": "#22c55e"},
            {
                "type": "line",
                "name": "drawdown_pct",
                "data": drawdown,
                "color": "#ef4444",
                "line_width": 1,
            },
        ],
        source={"skill": skill, "action": action, "as_of": as_of},
        path=path,  # type: ignore[arg-type]
        ctx=ctx,
        chart_id=chart_id,
        insights=insights_list,
    )
    return block.as_dict()


__all__ = [
    "line_chart_from_rows",
    "candle_chart_from_rows",
    "equity_curve_from_rows",
]

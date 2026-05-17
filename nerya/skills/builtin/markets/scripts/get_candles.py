"""Historical OHLCV (candlestick) for a single market — emits an
interactive ``chart_block`` alongside the raw data.

Standalone CLI usage::

    python -m nerya.skills.builtin.markets.scripts.get_candles \\
        --json '{"market": "binance:BTC/USDT", "interval": "1d", "limit": 60}'

Output schema::

    {
      "market": str,
      "venue": str,
      "interval": str,
      "limit": int,
      "ohlcv": [{ "ts_ms": int, "open": float, "high": float,
                  "low": float, "close": float, "volume": float }, ...],
      "envelope": {"truth": "live"|"mock"|"degraded", ...},
      "error": str | None,
      "chart_blocks": [<ChartBlock dict>],   # only on success
      "chart_path": "inline" | "bulk"        # echoed for the agent
    }

The ``chart_blocks`` field is the contract the kernel watches for: when
present, ``AgentKernel._splice_chart_blocks`` injects a ``kind="chart"``
envelope into ``turn.blocks`` right after the matching ``tool_result``,
and the dashboard renders it with the lightweight-charts canvas.

Path selection
--------------

The script defaults to ``path="bulk"`` whenever a workspace is provided
(production case via the agent kernel), so the heavy OHLCV payload
lives in ``artifacts/charts/<id>.json`` instead of bloating the LLM
context. If ``path="inline"`` is requested or no workspace is given
(standalone CLI / tests), the data is embedded in the envelope.

When the venue is unavailable the ``envelope.truth`` is ``degraded``
and ``chart_blocks`` is omitted — there's nothing to draw. No mock
fallback unless ``NERYA_ALLOW_MOCK_DATA=1``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Literal

from nerya.agent.chart_block import stable_chart_id
from nerya.charting import BulkContext, build_chart_block
from nerya.core.paths import WorkspacePaths
from nerya.core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
)
from nerya.workspace.artifact_store import ArtifactStore

from ._connector import public_connector, venue_of, workspace_root


# Reasonable defaults: "1d for the last 60 days" is the K-line a human
# trader would expect when they ask "show me BTCUSD". Bulk path keeps
# context lean even at 60 candles.
DEFAULT_INTERVAL = "1d"
DEFAULT_LIMIT = 60
MAX_LIMIT = 1000


def _normalise_kline_row(row: list[Any]) -> dict[str, float] | None:
    # Connector klines come back as ``[ts_ms, open, high, low, close, volume]``.
    if not row or len(row) < 5:
        return None
    try:
        ts_ms = int(row[0])
        return {
            "ts_ms": ts_ms,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]) if len(row) > 5 and row[5] is not None else 0.0,
        }
    except (TypeError, ValueError):
        return None


def _build_chart(
    *,
    market: str,
    venue: str,
    interval: str,
    ohlcv: list[dict[str, float]],
    workspace: str | None,
    path: Literal["inline", "bulk"],
) -> dict[str, Any]:
    """Compose the ChartBlock and return its as_dict() form."""

    candles = [
        {
            "time": int(row["ts_ms"] // 1000),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        }
        for row in ohlcv
    ]
    if not candles:
        return {}

    ctx = None
    if path == "bulk" and workspace:
        ctx = BulkContext(
            artifact_store=ArtifactStore(WorkspacePaths(root=workspace_root(workspace))),
        )
    elif path == "bulk" and not workspace:
        # Bulk requested but no workspace — degrade to inline rather
        # than fail the whole quote, but tag the warning so callers see
        # *why* their preferred path didn't take effect.
        path = "inline"

    chart_id = stable_chart_id(
        "markets",
        "get_candles",
        {"market": market, "venue": venue, "interval": interval, "n": len(candles)},
    )

    block = build_chart_block(
        chart_kind="candlestick",
        title=f"{market} · {interval} · {len(candles)} bars",
        subtitle=f"venue: {venue}" if venue else None,
        series=[{"type": "candlestick", "name": "ohlc", "data": candles}],
        source={
            "skill": "markets",
            "action": "get_candles",
            "as_of": "",  # filled by caller via ts_ms; left blank for stable id
        },
        insights=_summarise(candles),
        path=path,
        ctx=ctx,
        chart_id=chart_id,
    )
    return block.as_dict()


def _summarise(candles: list[dict[str, Any]]) -> list[str]:
    if not candles:
        return []
    first = candles[0]["close"]
    last = candles[-1]["close"]
    if first == 0:
        change_pct = 0.0
    else:
        change_pct = (last - first) / abs(first) * 100.0
    high = max(c["high"] for c in candles)
    low = min(c["low"] for c in candles)
    sign = "+" if change_pct >= 0 else ""
    return [
        f"first close: {first:g}",
        f"last close: {last:g}",
        f"period change: {sign}{change_pct:.2f}%",
        f"period range: {low:g} – {high:g}",
    ]


def run(
    *,
    market: str,
    interval: str = DEFAULT_INTERVAL,
    limit: int = DEFAULT_LIMIT,
    path: Literal["inline", "bulk", "auto"] = "auto",
    workspace: str | None = None,
) -> dict[str, Any]:
    venue = venue_of(market)
    if not market:
        return {"error": "market is required"}

    requested_path: Literal["inline", "bulk"]
    if path == "auto":
        # Default policy: bulk when we have a workspace (production
        # agent path), inline when we don't (standalone CLI / tests).
        # The agent's prompt-side heuristic still applies — it
        # can override by passing ``path`` explicitly.
        requested_path = "bulk" if workspace else "inline"
    elif path in ("inline", "bulk"):
        requested_path = path
    else:
        return {"error": f"path must be 'inline' or 'bulk', got {path!r}"}

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    try:
        conn = public_connector(market, workspace=workspace)
    except Exception as exc:
        env = degraded_envelope(
            "klines",
            error=f"connector_unavailable:{type(exc).__name__}",
            venue=venue,
        ).as_dict()
        return {
            "market": market,
            "venue": venue,
            "interval": interval,
            "limit": limit,
            "ohlcv": [],
            "envelope": env,
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        rows = conn.get_klines(market, interval=interval, limit=limit)
    except Exception as exc:
        env = degraded_envelope(
            "klines",
            error=f"connector_error:{type(exc).__name__}",
            venue=venue,
        ).as_dict()
        return {
            "market": market,
            "venue": venue,
            "interval": interval,
            "limit": limit,
            "ohlcv": [],
            "envelope": env,
            "error": f"{type(exc).__name__}: {exc}",
        }

    ohlcv = [r for r in (_normalise_kline_row(row) for row in rows or []) if r]
    if not ohlcv:
        # Empty result — surface as a degraded envelope, *not* a chart.
        env = degraded_envelope(
            "klines",
            error="no_klines",
            venue=venue,
        ).as_dict()
        return {
            "market": market,
            "venue": venue,
            "interval": interval,
            "limit": limit,
            "ohlcv": [],
            "envelope": env,
            "error": "no klines returned",
        }

    if venue in ("mock", "paper", ""):
        env = mock_envelope(source="mock", venue=venue or "mock").as_dict()
    else:
        env = live_envelope(source=venue, venue=venue).as_dict()

    chart_dict = _build_chart(
        market=market,
        venue=venue,
        interval=interval,
        ohlcv=ohlcv,
        workspace=workspace,
        path=requested_path,
    )
    out: dict[str, Any] = {
        "market": market,
        "venue": venue,
        "interval": interval,
        "limit": limit,
        "ohlcv": ohlcv,
        "envelope": env,
        "error": None,
        "chart_path": chart_dict.get("path", requested_path) if chart_dict else None,
    }
    if chart_dict:
        out["chart_blocks"] = [chart_dict]
    return out


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    parser.add_argument("--workspace", dest="workspace", default=None)
    parser.add_argument("--market", dest="market", default=None)
    parser.add_argument("--interval", dest="interval", default=None)
    parser.add_argument("--limit", dest="limit", type=int, default=None)
    parser.add_argument(
        "--path",
        dest="path",
        choices=("auto", "inline", "bulk"),
        default=None,
        help="Chart data path; defaults to 'auto' (bulk if workspace given)",
    )
    args = parser.parse_args()

    payload = _load_payload(args)
    market = args.market or payload.get("market") or ""
    interval = args.interval or payload.get("interval") or DEFAULT_INTERVAL
    limit = args.limit if args.limit is not None else int(payload.get("limit") or DEFAULT_LIMIT)
    path = args.path or payload.get("path") or "auto"
    workspace = args.workspace or payload.get("workspace")

    try:
        result = run(
            market=market,
            interval=interval,
            limit=limit,
            path=path,
            workspace=workspace,
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

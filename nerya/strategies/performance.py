"""Strategy performance snapshots.

The self-evolution loop needs a deterministic, schema-stable view of
*how the strategy is actually doing* — derived from the same ledgers
the dashboard already reads — so the tuning subagent can ground its
proposals in numbers instead of vibes.

Inputs (read-only)
------------------
* ``runs/<run_id>.json`` written by :class:`StrategyRunStore` (statuses,
  durations, error kinds, per-tick mode).
* ``strategy_history/<strategy_id>/{orders,fills,risk,pnl,decisions,
  subagents}.jsonl`` written by :mod:`nerya.strategy_history.store`.

Outputs
-------
:class:`StrategyPerformanceSnapshot` — a single typed dict-like object
with three groups of metrics:

* **Run metrics** — total ticks, success rate, hold rate, error rate,
  median duration, last run timestamp.
* **Trade metrics** — submitted intents, filled orders, cumulative PnL,
  drawdown floor, win rate, average slippage.
* **Cost metrics** — risk rejects, subagent counts, last review timestamp.
* **Evolution context** — recent post-apply observations produced by
  validation, paper/shadow/live ticks, and operator feedback.

The snapshot is intentionally *flat* and JSON-serialisable so a tuning
subagent can be prompted with ``json.dumps(snapshot.asdict(), …)``
directly.

Boundaries
----------
This module never writes anything and never calls into the runtime.
It is safe to import from the validator, dashboard, evolution loop,
or CLI.
"""

from __future__ import annotations

import html
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import quote as url_quote

from ..connectors.http import UrllibHttp
from ..core import jsonl
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from ..core.truth import live_envelope, mock_envelope, resolve_allow_mock
from ..data.candles import fetch_candles, mock_candles
from ..data.equities import EquitiesClient
from ..data.features import compute_features
from ..data.news import fetch_news, mock_news
from ..evolution.observation_summary import (
    POST_APPLY_HEALTHY_STATUSES,
    POST_APPLY_NEGATIVE_STATUSES,
    summarize_observation_weights,
)
from ..strategy_history import store as history_store
from .package import StrategyPackage, load_package
from .state import StrategyRunRecord, StrategyRunStore


_LOG = logging.getLogger(__name__)
_RSS_ITEM_RE = re.compile(r"<item\b.*?</item>", re.DOTALL | re.IGNORECASE)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass
class StrategyPerformanceSnapshot:
    """Read-only metrics bundle the tuning loop / dashboard consumes."""

    strategy_id: str
    package_hash: str
    generated_at: str
    lookback_runs: int
    runs_considered: int
    run_metrics: dict[str, Any] = field(default_factory=dict)
    trade_metrics: dict[str, Any] = field(default_factory=dict)
    cost_metrics: dict[str, Any] = field(default_factory=dict)
    risk_metrics: dict[str, Any] = field(default_factory=dict)
    market_context: dict[str, Any] = field(default_factory=dict)
    news_context: dict[str, Any] = field(default_factory=dict)
    evolution_context: dict[str, Any] = field(default_factory=dict)
    last_run_at: Optional[str] = None
    last_review_at: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_snapshot(
    paths: WorkspacePaths,
    strategy_id: str,
    *,
    lookback_runs: int = 200,
    package: Optional[StrategyPackage] = None,
    config_like: Any | None = None,
) -> StrategyPerformanceSnapshot:
    """Compose a :class:`StrategyPerformanceSnapshot` from on-disk data."""

    pkg = package
    if pkg is None:
        try:
            pkg = load_package(paths, strategy_id)
        except Exception:
            pkg = None

    runs = StrategyRunStore(paths, strategy_id).list(limit=lookback_runs)
    notes: list[str] = []

    run_metrics = _summarise_runs(runs)
    last_run_at = runs[0].finished_at if runs else None

    intents = _read(paths, strategy_id, "intents")
    orders = _read(paths, strategy_id, "orders")
    fills = _read(paths, strategy_id, "fills")
    pnls = _read(paths, strategy_id, "pnl")
    risk_rows = _read(paths, strategy_id, "risk")
    decisions = _read(paths, strategy_id, "decisions")
    reviews = _read(paths, strategy_id, "reviews")
    subagent_rows = _read(paths, strategy_id, "subagents")

    trade_metrics = _summarise_trades(intents, orders, fills, pnls)
    risk_metrics = _summarise_risk(risk_rows, decisions)
    cost_metrics = _summarise_costs(subagent_rows)
    market_context = _build_market_context(pkg, config_like=config_like)
    news_context = _build_news_context(pkg, config_like=config_like)
    evolution_context = _build_evolution_context(paths, strategy_id)

    last_review_at = None
    if reviews:
        last_review_at = (
            reviews[-1].get("ts") if isinstance(reviews[-1], dict) else None
        )

    if not runs:
        notes.append("no runs recorded yet")
    if not orders:
        notes.append("no orders recorded yet")

    return StrategyPerformanceSnapshot(
        strategy_id=strategy_id,
        package_hash=(pkg.content_hash if pkg is not None else ""),
        generated_at=now_iso(),
        lookback_runs=lookback_runs,
        runs_considered=len(runs),
        run_metrics=run_metrics,
        trade_metrics=trade_metrics,
        cost_metrics=cost_metrics,
        risk_metrics=risk_metrics,
        market_context=market_context,
        news_context=news_context,
        evolution_context=evolution_context,
        last_run_at=last_run_at,
        last_review_at=last_review_at,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read(
    paths: WorkspacePaths, strategy_id: str, name: str
) -> list[dict[str, Any]]:
    try:
        return list(history_store.read_ledger(paths, strategy_id, name))
    except Exception:
        _LOG.exception("read_ledger %s failed for %s", name, strategy_id)
        return []


def _build_evolution_context(
    paths: WorkspacePaths,
    strategy_id: str,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    rows = [
        row for row in jsonl.read_all(paths.journal("evolution"))
        if row.get("kind") == "proposal.post_apply_observation"
        and str(row.get("strategy_id") or "") == strategy_id
    ]
    rows.sort(key=lambda row: str(row.get("observed_at") or row.get("ts") or ""))
    recent = rows[-max(1, int(limit)) :]
    summary = summarize_observation_weights(recent)
    by_status = summary["by_status"]
    by_source = summary["by_source"]
    evidence_refs: list[str] = []
    for row in recent:
        evidence_refs.extend(_str_list(row.get("evidence_refs")))
    return {
        "post_apply_observation_count": len(rows),
        "recent_count": len(recent),
        "by_status": by_status,
        "by_source": by_source,
        "negative_count": sum(
            by_status.get(status, 0) for status in POST_APPLY_NEGATIVE_STATUSES
        ),
        "healthy_count": sum(
            by_status.get(status, 0) for status in POST_APPLY_HEALTHY_STATUSES
        ),
        "observing_count": by_status.get("observing", 0) + by_status.get("pending", 0),
        "decay": summary["decay"],
        "weighted_by_status": summary["weighted_by_status"],
        "uncapped_weighted_by_status": summary["uncapped_weighted_by_status"],
        "weighted_by_source": summary["weighted_by_source"],
        "weighted_negative_count": summary["weighted_negative_count"],
        "weighted_healthy_count": summary["weighted_healthy_count"],
        "weighted_observing_count": summary["weighted_observing_count"],
        "dominant_sources": summary["dominant_sources"],
        "last_observed_at": (
            recent[-1].get("observed_at") or recent[-1].get("ts")
            if recent else None
        ),
        "recent_observations": [_compact_observation(row) for row in recent[-8:]],
        "evidence_refs": _unique_strings(evidence_refs)[-12:],
        "notes": [
            "recent_observations are post-apply evidence available to strategy_tuner",
            "observing paper/live/shadow ticks are evidence, not proof of improvement",
            "weighted counts use recency decay and source caps so high-frequency ticks do not dominate tuning",
        ],
    }


def _compact_observation(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    compact_metrics = {
        key: metrics.get(key)
        for key in (
            "mode", "run_status", "duration_ms", "llm_calls",
            "subagent_calls", "result_status", "has_intent", "has_order",
            "total_return_pct", "max_drawdown_pct", "verdict",
        )
        if key in metrics
    }
    return {
        "id": row.get("id"),
        "proposal_id": row.get("proposal_id"),
        "status": row.get("status"),
        "source": row.get("source"),
        "observed_at": row.get("observed_at") or row.get("ts"),
        "run_id": row.get("run_id"),
        "summary": str(row.get("summary") or "")[:500],
        "metrics": compact_metrics,
        "evidence_refs": _str_list(row.get("evidence_refs"))[:6],
    }


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _build_market_context(
    package: StrategyPackage | None,
    *,
    config_like: Any | None,
) -> dict[str, Any]:
    if package is None:
        return {"markets": [], "timeframe": "", "items": [], "notes": ["package unavailable"]}
    timeframe = _review_timeframe(package)
    items: list[dict[str, Any]] = []
    for market in list(package.manifest.markets)[:12]:
        rows = _fetch_review_candles(
            str(market),
            timeframe=timeframe,
            config_like=config_like,
        )
        envelope = _first_envelope(rows)
        features = compute_features(rows)
        items.append(
            {
                "market": market,
                "timeframe": timeframe,
                "candles_count": len(rows),
                "recent_candles": _compact_candles(rows[-24:]),
                "features": features,
                "_envelope": envelope,
            }
        )
    return {
        "timeframe": timeframe,
        "markets": list(package.manifest.markets),
        "items": items,
        "notes": [
            "recent_candles are the K-line tail used for strategy tuning review",
            "features are computed from the same recent candle window",
        ],
    }


def _build_news_context(
    package: StrategyPackage | None,
    *,
    config_like: Any | None,
) -> dict[str, Any]:
    if package is None:
        return {"items": [], "count": 0, "notes": ["package unavailable"]}
    equity_symbols = sorted(_equity_symbols(package.manifest.markets))
    if equity_symbols:
        return _build_equity_news_context(equity_symbols, config_like=config_like)
    try:
        if resolve_allow_mock(config_like=config_like):
            rows = mock_news()
        else:
            rows = fetch_news(limit=12, allow_mock=False, config_like=config_like)
    except Exception as exc:
        return {
            "items": [],
            "count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    tickers = _market_symbols(package.manifest.markets)
    filtered = []
    for row in rows[:24]:
        if not isinstance(row, dict):
            continue
        symbols = {
            str(t).upper()
            for t in (row.get("tickers") or [])
            if str(t).strip()
        }
        text = " ".join([
            str(row.get("title") or ""),
            str(row.get("body") or ""),
            str(row.get("summary") or ""),
        ]).upper()
        matched = sorted((symbols | {t for t in tickers if t and t in text}) & tickers)
        if tickers and not matched:
            continue
        filtered.append(
            {
                "source": row.get("source"),
                "title": row.get("title"),
                "summary": row.get("summary") or row.get("body"),
                "published_at": row.get("published_at") or row.get("ts"),
                "link": row.get("link"),
                "tickers": list(row.get("tickers") or []),
                "matched_tickers": matched,
                "_envelope": row.get("_envelope") if isinstance(row.get("_envelope"), dict) else None,
            }
        )
        if len(filtered) >= 12:
            break
    if not filtered:
        filtered = [
            {
                "source": row.get("source"),
                "title": row.get("title"),
                "summary": row.get("summary") or row.get("body"),
                "published_at": row.get("published_at") or row.get("ts"),
                "link": row.get("link"),
                "tickers": list(row.get("tickers") or []),
                "_envelope": row.get("_envelope") if isinstance(row.get("_envelope"), dict) else None,
            }
            for row in rows[:8]
            if isinstance(row, dict)
        ]
    return {
        "count": len(filtered),
        "symbols": sorted(tickers),
        "items": filtered,
        "notes": [
            "news is best-effort and envelope-marked; absence of matching news is evidence, not an error",
        ],
    }


def _build_equity_news_context(
    tickers: list[str],
    *,
    config_like: Any | None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    if resolve_allow_mock(config_like=config_like):
        env = mock_envelope(
            "financial_datasets",
            provider="financialdatasets.ai",
        ).as_dict()
        for ticker in tickers[:12]:
            items.extend(_mock_equity_news(ticker, env=env))
        return {
            "count": len(items),
            "symbols": tickers,
            "items": items,
            "notes": [
                "equity news is ticker-matched for strategy tuning review",
                "mock equity news was used because mock mode is explicitly enabled",
            ],
        }

    client = EquitiesClient()
    for ticker in tickers[:12]:
        try:
            payload = client.news(ticker, limit=12)
        except Exception as exc:
            errors.append(f"{ticker}:{type(exc).__name__}: {exc}")
            continue
        env = payload.get("_envelope") if isinstance(payload, dict) else None
        rows = _extract_equity_news_rows(payload)
        if not rows:
            err = ""
            if isinstance(env, dict):
                err = str(env.get("error") or "")
            errors.append(f"{ticker}:{err or 'no_items'}")
            continue
        for row in rows[:12]:
            if not isinstance(row, dict):
                continue
            items.append(
                {
                    "source": row.get("source") or "financial_datasets",
                    "title": row.get("title") or row.get("headline"),
                    "summary": (
                        row.get("summary")
                        or row.get("description")
                        or row.get("body")
                        or row.get("content")
                    ),
                    "published_at": (
                        row.get("published_at")
                        or row.get("published_date")
                        or row.get("date")
                        or row.get("created_at")
                    ),
                    "link": row.get("link") or row.get("url"),
                    "tickers": list(row.get("tickers") or [ticker]),
                    "matched_tickers": [ticker],
                    "_envelope": env if isinstance(env, dict) else None,
                }
            )
            if len(items) >= 24:
                break
        if len(items) >= 24:
            break
    if not items:
        fallback = _fetch_yahoo_equity_news(
            tickers,
            limit=24,
        )
        items.extend(fallback["items"])
        errors.extend(fallback["errors"])
    notes = [
        "equity news is sourced per ticker via Financial Datasets when keys are configured, then Yahoo Finance RSS as a no-key fallback",
        "absence of matching equity news is evidence, not an error",
    ]
    if errors:
        notes.append("degraded equity news: " + "; ".join(errors[:8]))
    return {
        "count": len(items),
        "symbols": tickers,
        "items": items,
        "errors": errors,
        "notes": notes,
    }


def _fetch_yahoo_equity_news(tickers: list[str], *, limit: int) -> dict[str, Any]:
    if limit <= 0:
        return {"items": [], "errors": []}
    http = UrllibHttp(rate_limit_per_sec=4.0)
    env = live_envelope(
        "yahoo_finance_rss",
        provider="finance.yahoo.com",
    ).as_dict()
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for ticker in tickers[:12]:
        url = (
            "https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={url_quote(ticker)}&region=US&lang=en-US"
        )
        try:
            status, body = http.request("GET", url, timeout=15.0)
        except Exception as exc:
            errors.append(f"{ticker}:yahoo_rss:{type(exc).__name__}")
            continue
        if status >= 400:
            errors.append(f"{ticker}:yahoo_rss:http_{status}")
            continue
        xml = body.get("raw") if isinstance(body, dict) else ""
        rows = _parse_equity_rss(xml, ticker=ticker, env=env)
        if not rows:
            errors.append(f"{ticker}:yahoo_rss:no_items")
            continue
        items.extend(rows)
        if len(items) >= limit:
            break
    if not items and not errors:
        errors.append("yahoo_rss:no_items")
    return {"items": items[:limit], "errors": errors}


def _parse_equity_rss(
    xml: str,
    *,
    ticker: str,
    env: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw_item in _RSS_ITEM_RE.findall(xml or ""):
        title = _rss_tag(raw_item, "title")
        if not title:
            continue
        out.append(
            {
                "source": "yahoo_finance_rss",
                "title": title,
                "summary": _rss_tag(raw_item, "description"),
                "published_at": _rss_tag(raw_item, "pubDate"),
                "link": _rss_tag(raw_item, "link"),
                "tickers": [ticker],
                "matched_tickers": [ticker],
                "_envelope": env,
            }
        )
    return out


def _rss_tag(raw_item: str, tag: str) -> str:
    match = re.search(
        rf"<{tag}[^>]*>(.*?)</{tag}>",
        raw_item or "",
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return ""
    raw = match.group(1).strip()
    if raw.startswith("<![CDATA["):
        raw = raw[9:]
        if raw.endswith("]]>"):
            raw = raw[:-3]
    return html.unescape(re.sub(r"<[^>]+>", "", raw).strip())


def _extract_equity_news_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("news", "items", "articles", "results", "data"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _mock_equity_news(ticker: str, *, env: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "source": "financial_datasets",
            "title": f"{ticker} earnings and guidance remain the key review catalyst",
            "summary": (
                f"Mock equity headline for {ticker}; use live Financial "
                "Datasets keys for production news evidence."
            ),
            "published_at": "",
            "link": "",
            "tickers": [ticker],
            "matched_tickers": [ticker],
            "_envelope": env,
        }
    ]


def _review_timeframe(package: StrategyPackage) -> str:
    markets = [m.lower() for m in package.manifest.markets]
    if any(m.startswith(("yahoo:", "tushare:", "polygon_io:")) for m in markets):
        return "1d"
    sched = package.manifest.schedule
    if sched.every_seconds and sched.every_seconds <= 300:
        return "1m"
    if sched.every_seconds and sched.every_seconds <= 3600:
        return "15m"
    cron = str(sched.cron or "")
    if cron.startswith("*/1") or cron.startswith("* "):
        return "1m"
    if cron.startswith("*/5") or cron.startswith("*/15"):
        return "15m"
    return "1d"


def _fetch_review_candles(
    market: str,
    *,
    timeframe: str,
    config_like: Any | None,
) -> list[dict[str, Any]]:
    venue = market.split(":", 1)[0].upper() if ":" in market else ""
    if venue in {"MOCK", "PAPER"}:
        return mock_candles(market, count=96, interval_s=_timeframe_seconds(timeframe))
    try:
        return fetch_candles(
            market,
            count=96,
            interval=timeframe,
            allow_mock=None,
            config_like=config_like,
        )
    except Exception:
        return []


def _compact_candles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "ts": row.get("ts"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "_envelope": row.get("_envelope") if isinstance(row.get("_envelope"), dict) else None,
            }
        )
    return out


def _first_envelope(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        env = row.get("_envelope") if isinstance(row, dict) else None
        if isinstance(env, dict):
            return env
    return None


def _market_symbols(markets: Iterable[str]) -> set[str]:
    symbols: set[str] = set()
    for market in markets:
        text = str(market or "").split(":", 1)[-1].upper()
        for sep in ("/", "-", "_"):
            text = text.split(sep, 1)[0]
        for quote in ("USDT", "USDC", "USD", "BUSD"):
            if text.endswith(quote) and len(text) > len(quote):
                text = text[: -len(quote)]
                break
        if text:
            symbols.add(text)
    return symbols


def _equity_symbols(markets: Iterable[str]) -> set[str]:
    symbols: set[str] = set()
    for market in markets:
        text = str(market or "").strip()
        low = text.lower()
        if ":" in text:
            venue, tail = text.split(":", 1)
            if venue.lower() not in {"yahoo", "nasdaq", "nyse", "amex", "arca", "bats", "otc"}:
                continue
            text = tail
        elif not low.startswith("^") and any(sep in text for sep in ("/", "-")):
            continue
        symbol = text.strip().replace(".", "-").upper()
        if symbol and not symbol.endswith(("=X", "-USD")):
            symbols.add(symbol)
    return symbols


def _timeframe_seconds(timeframe: str) -> int:
    raw = str(timeframe or "1m").strip().lower()
    try:
        if raw.endswith("m"):
            return max(1, int(raw[:-1] or "1")) * 60
        if raw.endswith("h"):
            return max(1, int(raw[:-1] or "1")) * 3600
        if raw.endswith("d"):
            return max(1, int(raw[:-1] or "1")) * 86400
    except ValueError:
        pass
    return 60


def _summarise_runs(runs: Iterable[StrategyRunRecord]) -> dict[str, Any]:
    runs = list(runs)
    total = len(runs)
    if total == 0:
        return {
            "total": 0,
            "ok": 0,
            "hold": 0,
            "error": 0,
            "submitted": 0,
            "ok_rate": 0.0,
            "hold_rate": 0.0,
            "error_rate": 0.0,
            "median_duration_ms": 0,
            "p95_duration_ms": 0,
            "modes": {},
        }
    ok = sum(1 for r in runs if r.status == "ok")
    hold = sum(1 for r in runs if r.status == "hold")
    submitted = sum(1 for r in runs if r.status == "submitted")
    err = sum(1 for r in runs if r.status == "error")
    durations = sorted(int(r.duration_ms or 0) for r in runs)
    modes: dict[str, int] = {}
    for r in runs:
        modes[r.mode] = modes.get(r.mode, 0) + 1
    return {
        "total": total,
        "ok": ok,
        "hold": hold,
        "submitted": submitted,
        "error": err,
        "ok_rate": _safe_rate(ok, total),
        "hold_rate": _safe_rate(hold, total),
        "error_rate": _safe_rate(err, total),
        "median_duration_ms": _percentile(durations, 0.5),
        "p95_duration_ms": _percentile(durations, 0.95),
        "modes": modes,
    }


def _summarise_trades(
    intents: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    pnls: list[dict[str, Any]],
) -> dict[str, Any]:
    submitted_orders = len(orders)
    filled = len(fills)
    fill_rate = _safe_rate(filled, max(1, submitted_orders))

    pnl_total = 0.0
    pnl_series: list[float] = []
    wins = losses = 0
    current_win_streak = current_loss_streak = 0
    max_win_streak = max_loss_streak = 0
    for row in pnls:
        amt = _coerce_float(_get_nested(row, "pnl", "realized_usd"))
        if amt is None:
            amt = _coerce_float(_get_nested(row, "pnl", "pnl_usd"))
        if amt is None:
            amt = _coerce_float(_get_nested(row, "pnl", "value"))
        if amt is None:
            continue
        pnl_total += amt
        pnl_series.append(pnl_total)
        if amt > 0:
            wins += 1
            current_win_streak += 1
            current_loss_streak = 0
        elif amt < 0:
            losses += 1
            current_loss_streak += 1
            current_win_streak = 0
        else:
            current_win_streak = 0
            current_loss_streak = 0
        max_win_streak = max(max_win_streak, current_win_streak)
        max_loss_streak = max(max_loss_streak, current_loss_streak)
    drawdown = _max_drawdown(pnl_series) if pnl_series else 0.0

    slippages: list[float] = []
    for row in fills:
        s = _coerce_float(_get_nested(row, "fill", "slippage_bps"))
        if s is None:
            s = _coerce_float(_get_nested(row, "fill", "slippage"))
        if s is not None:
            slippages.append(float(s))
    avg_slip = sum(slippages) / len(slippages) if slippages else 0.0

    closed = wins + losses
    win_rate = _safe_rate(wins, max(1, closed))

    return {
        "intents": len(intents),
        "orders": submitted_orders,
        "fills": filled,
        "fill_rate": fill_rate,
        "pnl_total_usd": pnl_total,
        "max_drawdown_usd": drawdown,
        "wins": wins,
        "losses": losses,
        "closed": closed,
        "win_rate": win_rate,
        "current_win_streak": current_win_streak,
        "current_loss_streak": current_loss_streak,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "avg_slippage": avg_slip,
        "slippage_samples": len(slippages),
        "paper_live_divergence_bps": 0.0,
        "paper_live_divergence_samples": 0,
    }


def _summarise_risk(
    risk_rows: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    rejects = 0
    blocks = 0
    for row in risk_rows:
        verdict = _get_nested(row, "risk_decision", "verdict")
        if not isinstance(verdict, str):
            continue
        v = verdict.lower()
        if v in ("reject", "rejected", "block", "blocked"):
            rejects += 1
        if v == "blocked":
            blocks += 1
    holds = 0
    for row in decisions:
        action = _get_nested(row, "decision", "action")
        if isinstance(action, str) and action.lower() == "hold":
            holds += 1
    return {
        "risk_rows": len(risk_rows),
        "risk_rejects": rejects,
        "risk_blocks": blocks,
        "decision_rows": len(decisions),
        "decision_holds": holds,
    }


def _summarise_costs(subagent_rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_name: dict[str, int] = {}
    for row in subagent_rows:
        name = str(row.get("name") or "")
        if name:
            by_name[name] = by_name.get(name, 0) + 1
    return {
        "subagent_invocations": len(subagent_rows),
        "subagent_by_name": by_name,
    }


# ---------------------------------------------------------------------------
# Tiny stats helpers
# ---------------------------------------------------------------------------


def _safe_rate(num: float, den: float) -> float:
    if not den:
        return 0.0
    try:
        return round(float(num) / float(den), 4)
    except Exception:
        return 0.0


def _percentile(sorted_values: list[int], p: float) -> int:
    if not sorted_values:
        return 0
    if p <= 0:
        return sorted_values[0]
    if p >= 1:
        return sorted_values[-1]
    idx = int(math.floor(p * (len(sorted_values) - 1)))
    return int(sorted_values[idx])


def _max_drawdown(series: list[float]) -> float:
    if not series:
        return 0.0
    peak = series[0]
    worst = 0.0
    for v in series:
        if v > peak:
            peak = v
        dd = v - peak
        if dd < worst:
            worst = dd
    return float(worst)


def _coerce_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_nested(row: Any, *path: str) -> Any:
    cur = row
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


__all__ = [
    "StrategyPerformanceSnapshot",
    "build_snapshot",
]

"""Read-only market observation Source for the internal Trigger plane.

This is a pollable source, not a background network daemon. It reads public
market snapshots, emits only live-data alerts, and delegates delivery to the
existing task Trigger ledger.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..core import yaml_io
from ..data.candles import fetch_public_ticker
from ..data.features import compute_features
from ..data.funding import fetch_funding
from ..agent.task_control import dispatch_trigger, record_trigger_source_health


@dataclass(frozen=True)
class MarketWatch:
    id: str
    market: str
    price_change_pct: float | None = None
    funding_rate_abs: float | None = None
    volatility_pct: float | None = None
    candle_interval: str = "1m"
    candle_count: int = 30
    cooldown_seconds: int = 900
    enabled: bool = True


def load_market_watches(paths) -> list[MarketWatch]:
    document = yaml_io.load(paths.triggers_market_sources_file, default={}) or {}
    watches = []
    for row in document.get("watches") or []:
        if not isinstance(row, Mapping) or not row.get("id") or not row.get("market"):
            continue
        watches.append(MarketWatch(
            id=str(row["id"]), market=str(row["market"]),
            price_change_pct=float(row["price_change_pct"]) if row.get("price_change_pct") is not None else None,
            funding_rate_abs=float(row["funding_rate_abs"]) if row.get("funding_rate_abs") is not None else None,
            volatility_pct=float(row["volatility_pct"]) if row.get("volatility_pct") is not None else None,
            candle_interval=str(row.get("candle_interval") or "1m"),
            candle_count=max(15, min(int(row.get("candle_count") or 30), 200)),
            cooldown_seconds=max(0, int(row.get("cooldown_seconds") or 900)),
            enabled=bool(row.get("enabled", True)),
        ))
    return watches


def _envelope_live(value: Any) -> bool:
    envelope = value.get("_envelope") if isinstance(value, Mapping) else None
    return isinstance(envelope, Mapping) and envelope.get("mode") == "live" and not envelope.get("degraded")


def _watch_state(paths, watch_id: str) -> dict[str, Any] | None:
    from ..db.sqlite import connect
    with connect(paths.db) as con:
        row = con.execute("SELECT * FROM market_watch_state WHERE watch_id=?", (watch_id,)).fetchone()
    return dict(row) if row else None


def _save_watch_state(paths, watch: MarketWatch, *, sample_ts: float, price: float | None,
                      funding: float | None, volatility: float | None, alerts: dict[str, Any], cursor: str) -> None:
    from ..db.sqlite import connect
    with connect(paths.db) as con:
        con.execute("""INSERT INTO market_watch_state
            (watch_id,market,last_sample_ts,last_price,last_funding_rate,last_volatility_pct,last_alerts_json,last_cursor,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(watch_id) DO UPDATE SET market=excluded.market,
            last_sample_ts=excluded.last_sample_ts,last_price=excluded.last_price,last_funding_rate=excluded.last_funding_rate,
            last_volatility_pct=excluded.last_volatility_pct,last_alerts_json=excluded.last_alerts_json,
            last_cursor=excluded.last_cursor,updated_at=excluded.updated_at""",
                     (watch.id, watch.market, sample_ts, price, funding, volatility, json.dumps(alerts, separators=(",", ":")), cursor, sample_ts))
        con.commit()


def _volatility_pct(candles: list[dict[str, Any]]) -> float | None:
    features = compute_features(candles)
    atr = features.get("atr_14")
    close = features.get("close")
    if atr is None or not close:
        return None
    return abs(float(atr)) / abs(float(close)) * 100.0


def poll(config, *, watches: list[MarketWatch] | None = None, now: float | None = None,
         ticker_fetch: Callable[..., dict[str, Any]] = fetch_public_ticker,
         funding_fetch: Callable[..., dict[str, Any]] = fetch_funding,
         candle_fetch: Callable[..., list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Poll configured watches once and dispatch newly crossed alerts."""
    paths = config.paths
    now = float(now if now is not None else time.time())
    watches = list(watches if watches is not None else load_market_watches(paths))
    emitted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for watch in watches:
        if not watch.enabled:
            continue
        try:
            ticker = ticker_fetch(watch.market, allow_mock=False, config_like=config)
            funding = funding_fetch(watch.market, allow_mock=False, config_like=config)
            candles = candle_fetch(watch.market, interval=watch.candle_interval, count=watch.candle_count, config_like=config) if candle_fetch else []
            if not _envelope_live(ticker) or not _envelope_live(funding) or (candle_fetch and candles and not _envelope_live(candles[0])):
                raise ValueError("market_source_not_live")
            price = float(ticker.get("last") or ticker.get("mid") or ticker.get("price") or 0.0)
            funding_rate = float(funding.get("funding_rate") or 0.0)
            volatility = _volatility_pct(candles) if candles else None
            state = _watch_state(paths, watch.id)
            previous_price = float(state["last_price"]) if state and state.get("last_price") else None
            previous_alerts = json.loads(state["last_alerts_json"] or "{}") if state else {}
            cursor = f"{watch.id}:{int(now)}"
            candidates: list[tuple[str, dict[str, Any]]] = []
            if watch.price_change_pct is not None and previous_price:
                change = (price - previous_price) / previous_price * 100.0
                if abs(change) >= watch.price_change_pct:
                    candidates.append(("price.move", {"change_pct": change, "price": price, "previous_price": previous_price}))
            if watch.funding_rate_abs is not None and abs(funding_rate) >= watch.funding_rate_abs:
                candidates.append(("funding.extreme", {"funding_rate": funding_rate, "mark_price": funding.get("mark_price")}))
            if watch.volatility_pct is not None and volatility is not None and volatility >= watch.volatility_pct:
                candidates.append(("volatility.extreme", {"volatility_pct": volatility}))
            for kind, detail in candidates:
                last_emitted = float(previous_alerts.get(kind) or 0.0)
                if now - last_emitted < watch.cooldown_seconds:
                    continue
                event_id = "evt_market_" + hashlib.sha256(f"{watch.id}:{kind}:{cursor}".encode()).hexdigest()[:32]
                result = dispatch_trigger(paths, {"event_id": event_id, "source": "market", "kind": kind,
                    "source_cursor": cursor, "idempotency_key": event_id, "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    "payload": {"watch_id": watch.id, "market": watch.market, "mode": "live", **detail}}, config=config)
                previous_alerts[kind] = now
                emitted.append({"event_id": event_id, "kind": kind, "market": watch.market, "delivery": result})
            _save_watch_state(paths, watch, sample_ts=now, price=price, funding=funding_rate, volatility=volatility,
                              alerts=previous_alerts, cursor=cursor)
            record_trigger_source_health(paths, source="market", cursor=cursor, state="ready")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors.append({"watch_id": watch.id, "market": watch.market, "error": error})
            record_trigger_source_health(paths, source="market", cursor=watch.id, state="degraded", error=error, increment_failure=True)
    return {"ok": not errors, "source": "market", "emitted": emitted, "errors": errors, "polled": len(watches)}


__all__ = ["MarketWatch", "load_market_watches", "poll"]

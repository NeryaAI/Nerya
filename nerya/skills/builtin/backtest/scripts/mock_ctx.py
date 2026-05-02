"""Backtest StrategyContext mirror."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .....strategies.result import ResultBuilder
from .config import BacktestConfig, MockSurfaceCfg


class BacktestUnsupportedSurfaceError(RuntimeError):
    """Raised when a live-only surface is used during backtest."""

    def __init__(self, surface: str) -> None:
        super().__init__(
            f"ctx.{surface} is unsupported in OHLCV backtests; gate this call on "
            "ctx.runmode == 'backtest' or set mock_surfaces."
            f"{surface}.mode = 'stub'/'replay'"
        )


@dataclass
class MockMarket:
    market: str
    bars_by_market: dict[str, list[dict[str, Any]]]

    def candles(
        self,
        market: str,
        *,
        timeframe: str = "1m",
        limit: int = 100,
        account: str | None = None,
    ) -> list[dict[str, Any]]:
        del timeframe, account
        return list(self.bars_by_market.get(market, []))[-int(limit):]

    def ticker(self, market: str, *, account: str | None = None) -> dict[str, Any]:
        del account
        close = self.mark_price(market)
        return {"bid": close * 0.9999, "ask": close * 1.0001, "mid": close}

    def mark_price(self, market: str, *, account: str | None = None) -> float:
        del account
        rows = self.bars_by_market.get(market, [])
        if not rows:
            return 0.0
        return float(rows[-1].get("close", 0.0))

    def orderbook(self, market: str, *, depth: int = 20, account: str | None = None) -> dict[str, Any]:
        del market, depth, account
        raise BacktestUnsupportedSurfaceError("market.orderbook")

    def features(
        self,
        market: str,
        *,
        timeframe: str = "1m",
        lookback: int = 100,
        account: str | None = None,
    ) -> dict[str, Any]:
        rows = self.candles(market, timeframe=timeframe, limit=lookback, account=account)
        if not rows:
            return {"market": market, "timeframe": timeframe, "rows": 0}
        closes = [float(r.get("close", 0.0)) for r in rows]
        highs = [float(r.get("high", 0.0)) for r in rows]
        lows = [float(r.get("low", 0.0)) for r in rows]
        volumes = [float(r.get("volume", 0.0)) for r in rows]
        return {
            "market": market,
            "timeframe": timeframe,
            "rows": len(rows),
            "first": rows[0],
            "last": rows[-1],
            "close_min": min(closes),
            "close_max": max(closes),
            "high_max": max(highs),
            "low_min": min(lows),
            "volume_sum": sum(volumes),
        }


@dataclass
class MockTrading:
    pending_orders: list[dict[str, Any]]
    strategy_id: str

    def submit_intent(self, **payload: Any) -> dict[str, Any]:
        if len(payload) == 1 and isinstance(next(iter(payload.values())), dict):
            payload = dict(next(iter(payload.values())))
        intent_id = f"bt_{uuid.uuid4().hex[:12]}"
        record = {
            "intent_id": intent_id,
            "strategy_id": self.strategy_id,
            "market": payload.get("market"),
            "side": payload.get("side") or payload.get("action") or "buy",
            "size": payload.get("size", payload.get("notional_usd", payload.get("amount", 0))),
            "size_unit": payload.get("size_unit", "usd"),
            "order_type": payload.get("order_type", "market"),
            "reason": payload.get("reason") or payload.get("reasoning_ref") or "",
            "confidence": payload.get("confidence"),
            "raw": dict(payload),
        }
        self.pending_orders.append(record)
        return {
            "ok": True,
            "status": "submitted",
            "intent_id": intent_id,
            "intent": dict(record),
            "risk_decision": {"ok": True, "mode": "backtest"},
        }


@dataclass
class MockState:
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def update(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    def compare_and_set(self, key: str, *, expect: Any, new_value: Any) -> bool:
        if self.data.get(key) != expect:
            return False
        self.data[key] = new_value
        return True

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


@dataclass
class MockDedupe:
    seen_ids: set[str] = field(default_factory=set)

    def seen(self, item_id: str) -> bool:
        return str(item_id) in self.seen_ids

    def mark(self, item_id: str) -> None:
        self.seen_ids.add(str(item_id))

    def news(self, items: list[dict[str, Any]], *, bucket: str = "news", max_keys: int = 5000) -> list[dict[str, Any]]:
        del bucket, max_keys
        fresh: list[dict[str, Any]] = []
        for item in items:
            key = str(item.get("id") or item.get("guid") or item.get("link") or uuid.uuid4().hex)
            if key in self.seen_ids:
                continue
            self.seen_ids.add(key)
            fresh.append(item)
        return fresh


@dataclass
class MockClock:
    ts: int

    def now_iso(self) -> str:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def now_ms(self) -> int:
        return int(self.ts) * 1000

    def now_ts_ms(self) -> int:
        return self.now_ms()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc)


@dataclass
class MockAudit:
    sink: Callable[[dict[str, Any]], None] | None = None

    def log(self, kind: str, payload: dict[str, Any] | None = None, *, level: str = "info") -> None:
        record = {"kind": f"strategy.{kind}", "level": level, "payload": dict(payload or {})}
        if self.sink:
            self.sink(record)

    def record(self, kind: str, payload: dict[str, Any] | None = None, *, level: str = "info") -> None:
        self.log(kind, payload, level=level)


class _GatedSurface:
    def __init__(self, name: str, cfg: MockSurfaceCfg) -> None:
        self.name = name
        self.cfg = cfg

    def _value(self) -> Any:
        if self.cfg.mode == "stub":
            return self.cfg.payload
        if self.cfg.mode == "replay":
            raise NotImplementedError(f"mock_surfaces.{self.name}.mode=replay is reserved for v2")
        raise BacktestUnsupportedSurfaceError(self.name)


class MockNews(_GatedSurface):
    def fetch(self, **_: Any) -> list[dict[str, Any]]:
        value = self._value()
        return list(value or [])


class MockLLM(_GatedSurface):
    def classify(self, **_: Any) -> dict[str, Any]:
        value = self._value()
        return dict(value or {})

    def extract_json(self, **_: Any) -> dict[str, Any]:
        value = self._value()
        return dict(value or {})

    def analyze_signal(self, **_: Any) -> dict[str, Any]:
        value = self._value()
        return dict(value or {})


class MockSubAgents(_GatedSurface):
    def run(self, *_: Any, **__: Any) -> dict[str, Any]:
        value = self._value()
        return dict(value or {})

    def run_many(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        value = self._value()
        return list(value or [])


class MockMessages(_GatedSurface):
    def send(self, **_: Any) -> dict[str, Any]:
        value = self._value()
        return dict(value or {"ok": True, "queued": False})

    def enqueue(self, **kwargs: Any) -> dict[str, Any]:
        return self.send(**kwargs)


@dataclass
class SimpleConfigView:
    strategy_id: str
    title: str = ""
    mode: str = "backtest"
    markets: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()
    news_sources: tuple[str, ...] = ()
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class MockCtx:
    strategy_id: str
    market_name: str
    bars_by_market: dict[str, list[dict[str, Any]]]
    current_bar: dict[str, Any]
    pending_orders: list[dict[str, Any]]
    config_obj: BacktestConfig
    state: MockState
    audit_sink: Callable[[dict[str, Any]], None] | None = None
    config: SimpleConfigView | None = None
    result: ResultBuilder = field(default_factory=ResultBuilder)

    def __post_init__(self) -> None:
        self.market = MockMarket(self.market_name, self.bars_by_market)
        self.trading = MockTrading(self.pending_orders, self.strategy_id)
        self.audit = MockAudit(self.audit_sink)
        self.clock = MockClock(int(self.current_bar.get("ts", 0)))
        self.dedupe = MockDedupe()
        self.news = MockNews("news", self.config_obj.mock_surfaces["news"])
        self.llm = MockLLM("llm", self.config_obj.mock_surfaces["llm"])
        self.subagents = MockSubAgents("subagents", self.config_obj.mock_surfaces["subagents"])
        self.messages = MockMessages("messages", self.config_obj.mock_surfaces["messages"])
        self.policy = {"mode": "backtest"}
        self.trigger = {"source": "backtest"}
        self.prompt = None
        if self.config is None:
            self.config = SimpleConfigView(
                strategy_id=self.strategy_id,
                markets=tuple(self.config_obj.markets),
            )

    @property
    def runmode(self) -> str:
        return "backtest"

    @property
    def mode(self) -> str:
        return "backtest"


def append_jsonl(path: Any) -> Callable[[dict[str, Any]], None]:
    def _write(record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return _write


"""Backtest StrategyContext mirror."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

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
    timeframe_bars_by_market: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)

    def candles(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: str | None = None,
        symbol: str | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        del account
        market, timeframe, limit = self._normalise_candle_args(
            market,
            args,
            timeframe=interval or timeframe,
            limit=count or limit,
            symbol=symbol,
        )
        rows = self.timeframe_bars_by_market.get(market, {}).get(timeframe)
        if rows is None:
            rows = self.bars_by_market.get(market, [])
        return list(rows)[-int(limit):]

    def _normalise_candle_args(
        self,
        market: str | None,
        args: tuple[Any, ...],
        *,
        timeframe: str,
        limit: int,
        symbol: str | None = None,
    ) -> tuple[str, str, int]:
        """Accept common generated-code candle call shapes.

        The documented API is ``candles(market, timeframe=..., limit=...)``.
        LLM-authored strategy drafts often use positional variants such as
        ``candles(market, "1d", 120)``; the backtest mock should accept those
        when the meaning is unambiguous so validation and replay share one SDK
        contract.
        """

        chosen_market = str(symbol or market or self.market)
        chosen_timeframe = str(timeframe or "1m")
        chosen_limit = int(limit or 100)
        if args:
            chosen_timeframe = str(args[0])
        if len(args) >= 2:
            try:
                chosen_limit = int(args[1])
            except Exception:
                chosen_limit = int(limit or 100)
        if market and not symbol and not args and self._looks_like_timeframe(str(market)):
            chosen_market = self.market
            chosen_timeframe = str(market)
        return chosen_market, chosen_timeframe, chosen_limit

    @staticmethod
    def _looks_like_timeframe(value: str) -> bool:
        text = value.strip().lower()
        return len(text) >= 2 and text[:-1].isdigit() and text[-1] in {"m", "h", "d", "w"}

    def ticker(self, market: str, *, account: str | None = None) -> dict[str, Any]:
        del account
        close = self.mark_price(market)
        return {"bid": close * 0.9999, "ask": close * 1.0001, "mid": close}

    def get_ticker(self, market: str, *, account: str | None = None) -> dict[str, Any]:
        """Compatibility alias for generated strategy code."""

        return self.ticker(market, account=account)

    def get_candles(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: str | None = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Compatibility alias matching common market-data wording.

        Mirrors :meth:`candles` positional tolerance (for example
        ``get_candles("SOL/USDT", "1h", limit=200)``) so backtest and live
        replay share one SDK contract for generated strategies.
        """

        return self.candles(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def ohlcv(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: str | None = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Compatibility alias for generated strategies that ask for OHLCV."""

        return self.candles(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def get_ohlcv(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: str | None = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Compatibility alias for SDKs/generated code that use get_ohlcv."""

        return self.ohlcv(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def klines(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: str | None = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Compatibility alias for generated strategies that ask for klines."""

        return self.candles(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def mark_price(self, market: str, *, account: str | None = None) -> float:
        del account
        rows = self.bars_by_market.get(market, [])
        if not rows:
            by_tf = self.timeframe_bars_by_market.get(market, {})
            rows = next((candidate for candidate in by_tf.values() if candidate), [])
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
            "reason": payload.get("reason") or payload.get("reasoning") or payload.get("reasoning_ref") or "",
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

    def open_position(
        self,
        *,
        market: str,
        side: str,
        sizing: Any = None,
        entry: Any = None,
        protection: Any = None,
        confidence: float = 0.0,
        reasoning_ref: str = "",
        source: str = "script",
        **extra: Any,
    ) -> dict[str, Any]:
        """Backtest-mode equivalent of ``TradingAPI.open_position``.

        Translates the v6 control-plane signature (``side='long' | 'short'``
        + structured ``sizing`` + ``protection``) into the legacy
        intent record the backtest engine's :func:`settle` consumes via
        :attr:`pending_orders`. Returns a dict with ``ok``,
        ``status``, ``intent_id``, ``protection`` and ``bracket_id`` so
        the strategy entrypoint can return us as a ``StrategyResult``-
        shaped object.
        """

        intent_id = f"bt_{uuid.uuid4().hex[:12]}"
        # Map control-plane "long" / "short" onto the executor's
        # buy/sell (long entries are buys, short entries are sells).
        cp_side = str(side or "long").lower()
        legacy_side = "buy" if cp_side in ("long", "buy") else "sell"
        sizing_d = dict(sizing) if isinstance(sizing, dict) else {}
        method = str(sizing_d.get("method") or "fixed_usd")
        if method == "fixed_usd":
            size = float(sizing_d.get("fixed_usd") or 0.0)
            size_unit = "usd"
        elif method == "fixed_base":
            size = float(sizing_d.get("fixed_base") or 0.0)
            size_unit = "base"
        else:
            # close_all / reduce_pct / pct_nav / risk_to_stop:
            # Backtest harness sees these only in close_position which
            # has its own placeholder; for unfamiliar open methods,
            # fall back to the policy's default order USD.
            size = float(sizing_d.get("fixed_usd") or 0.0)
            size_unit = "usd"
        record = {
            "intent_id": intent_id,
            "strategy_id": self.strategy_id,
            "market": market,
            "side": legacy_side,
            "size": size,
            "size_unit": size_unit,
            "order_type": "market",
            "reason": reasoning_ref or "open_position",
            "confidence": float(confidence or 0.0),
            "plan_action": "open_position",
            "protection": dict(protection) if isinstance(protection, dict) else None,
            "raw": {
                "method": "open_position",
                "side": cp_side,
                "sizing": sizing_d or None,
                "entry": dict(entry) if isinstance(entry, dict) else entry,
                "protection": dict(protection) if isinstance(protection, dict) else protection,
                **extra,
            },
        }
        self.pending_orders.append(record)
        bracket_id = f"bkt_{uuid.uuid4().hex[:10]}" if record["protection"] else None
        return {
            "ok": True,
            "status": "submitted",
            "intent_id": intent_id,
            "intent": dict(record),
            "bracket_id": bracket_id,
            "protection": record["protection"],
            "risk_decision": {"ok": True, "mode": "backtest"},
        }

    def close_position(
        self,
        *,
        market: str,
        side: str,
        entry: Any = None,
        confidence: float = 0.0,
        reasoning_ref: str = "",
        source: str = "script",
        **extra: Any,
    ) -> dict[str, Any]:
        """Backtest-mode equivalent of ``TradingAPI.close_position``.

        ``side`` is the *existing* position direction (``long`` / ``short``);
        we emit the inverse leg into :attr:`pending_orders`. Sizing is
        forced to ``close_all`` upstream — for the in-process backtest
        we simply tag the record and let :func:`settle` figure out the
        remaining position size via the portfolio book.
        """

        intent_id = f"bt_{uuid.uuid4().hex[:12]}"
        position_side = str(side or "long").lower()
        legacy_side = "sell" if position_side == "long" else "buy"
        record = {
            "intent_id": intent_id,
            "strategy_id": self.strategy_id,
            "market": market,
            "side": legacy_side,
            # close_all sentinel — the backtest portfolio settle
            # interprets size==0 as "flatten the position".
            "size": 0.0,
            "size_unit": "base",
            "order_type": "market",
            "reason": reasoning_ref or "close_position",
            "confidence": float(confidence or 0.0),
            "plan_action": "close_position",
            "close_all": True,
            "raw": {
                "method": "close_position",
                "side": position_side,
                "entry": dict(entry) if isinstance(entry, dict) else entry,
                **extra,
            },
        }
        self.pending_orders.append(record)
        return {
            "ok": True,
            "status": "submitted",
            "intent_id": intent_id,
            "intent": dict(record),
            "risk_decision": {"ok": True, "mode": "backtest"},
        }

    def reduce_position(
        self,
        *,
        market: str,
        side: str,
        reduce_pct: float = 1.0,
        confidence: float = 0.0,
        reasoning_ref: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        """Backtest-mode equivalent of ``TradingAPI.reduce_position``."""

        intent_id = f"bt_{uuid.uuid4().hex[:12]}"
        position_side = str(side or "long").lower()
        legacy_side = "sell" if position_side == "long" else "buy"
        pct = max(0.0, min(1.0, float(reduce_pct or 1.0)))
        record = {
            "intent_id": intent_id,
            "strategy_id": self.strategy_id,
            "market": market,
            "side": legacy_side,
            "size": 0.0,  # resolved against current position by settle
            "size_unit": "base",
            "order_type": "market",
            "reason": reasoning_ref or "reduce_position",
            "confidence": float(confidence or 0.0),
            "plan_action": "reduce_position",
            "reduce_pct": pct,
            "raw": {"method": "reduce_position", "side": position_side, "reduce_pct": pct, **extra},
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
class MockPolicy:
    max_single_order_usd: float = 0.0
    max_daily_notional_usd: float = 0.0
    max_open_positions: int = 1
    min_confidence: float = 0.0
    allow_direct_order: bool = True
    require_subagent_before_order: bool = False
    default_order_usd: float = 100.0
    max_run_seconds: float = 60.0
    default_tier: str = "light"
    allowed_tiers: tuple[str, ...] = ("light",)
    max_calls_per_run: int = 0
    raw_policy: dict[str, Any] = field(default_factory=dict)
    raw_llm_policy: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None = None, llm_raw: dict[str, Any] | None = None) -> "MockPolicy":
        raw = dict(raw or {})
        llm_raw = dict(llm_raw or {})
        return cls(
            max_single_order_usd=float(raw.get("max_single_order_usd", 0.0) or 0.0),
            max_daily_notional_usd=float(raw.get("max_daily_notional_usd", 0.0) or 0.0),
            max_open_positions=int(raw.get("max_open_positions", 1) or 1),
            min_confidence=float(raw.get("min_confidence", 0.0) or 0.0),
            allow_direct_order=bool(raw.get("allow_direct_order", True)),
            require_subagent_before_order=bool(raw.get("require_subagent_before_order", False)),
            default_order_usd=float(raw.get("default_order_usd", 100.0) or 100.0),
            max_run_seconds=float(raw.get("max_run_seconds", 60.0) or 60.0),
            default_tier=str(llm_raw.get("default_tier", "light") or "light"),
            allowed_tiers=tuple(str(v) for v in (llm_raw.get("allowed_tiers") or ["light"])),
            max_calls_per_run=int(llm_raw.get("max_calls_per_run", 0) or 0),
            raw_policy=raw,
            raw_llm_policy=llm_raw,
        )

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


@dataclass
class MockPortfolio:
    state: MockState

    def positions(self, market: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key, value in self.state.data.items():
            if not key.startswith("position:") or not isinstance(value, dict):
                continue
            pos_market = key.split(":", 1)[1]
            if market and pos_market != market:
                continue
            qty = float(value.get("qty", 0.0) or 0.0)
            if abs(qty) <= 1e-12:
                continue
            out.append({
                "market": pos_market,
                "size": abs(qty),
                "quantity": abs(qty),
                "entry_price": float(value.get("avg_price", 0.0) or 0.0),
                "side": "long" if qty > 0 else "short",
            })
        return out

    def open_positions(self, market: str | None = None) -> list[dict[str, Any]]:
        return self.positions(market=market)


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
    timeframe_bars_by_market: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)
    policy_obj: MockPolicy | None = None
    config: SimpleConfigView | None = None
    result: Any = field(default_factory=lambda: _result_builder())

    def __post_init__(self) -> None:
        self.market = MockMarket(self.market_name, self.bars_by_market, self.timeframe_bars_by_market)
        self.trading = MockTrading(self.pending_orders, self.strategy_id)
        self.audit = MockAudit(self.audit_sink)
        self.clock = MockClock(int(self.current_bar.get("ts", 0)))
        self.dedupe = MockDedupe()
        self.portfolio = MockPortfolio(self.state)
        self.news = MockNews("news", self.config_obj.mock_surfaces["news"])
        self.llm = MockLLM("llm", self.config_obj.mock_surfaces["llm"])
        self.subagents = MockSubAgents("subagents", self.config_obj.mock_surfaces["subagents"])
        self.messages = MockMessages("messages", self.config_obj.mock_surfaces["messages"])
        self.policy = self.policy_obj or MockPolicy()
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

    @property
    def symbol(self) -> str:
        return self.market_name

    @property
    def timeframe(self) -> str:
        return str(getattr(self.config_obj, "tf", "") or "1m")

    @property
    def market_data(self) -> MockMarket:
        return self.market

    def ohlcv(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: str | None = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Top-level OHLCV helper matching ``ctx.market.candles``."""

        return self.market.candles(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def get_ohlcv(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: str | None = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Top-level compatibility alias matching ``ctx.ohlcv``."""

        return self.ohlcv(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def get_candles(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: str | None = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Top-level compatibility alias for common generated-code wording."""

        return self.ohlcv(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def klines(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: str | None = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Top-level compatibility alias matching ``ctx.market.klines``."""

        return self.ohlcv(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def history(
        self,
        market: str | None = None,
        timeframe: str = "1m",
        field: str = "close",
        *,
        length: int = 100,
        count: int | None = None,
        limit: int | None = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[float]:
        """Return one numeric field from OHLCV rows for common generated code."""

        rows = self.ohlcv(
            market,
            timeframe=timeframe,
            limit=count or limit or length,
            symbol=symbol,
            **kwargs,
        )
        values: list[float] = []
        for row in rows:
            try:
                values.append(float(row.get(field, 0.0)))
            except Exception:
                values.append(0.0)
        return values

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(f"nerya.strategy.{self.strategy_id}.backtest")

    @property
    def log(self) -> logging.Logger:
        return self.logger

    def now(self) -> datetime:
        return self.clock.now()


def append_jsonl(path: Any) -> Callable[[dict[str, Any]], None]:
    def _write(record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return _write


def _result_builder() -> Any:
    from .....strategies.result import ResultBuilder
    return ResultBuilder()

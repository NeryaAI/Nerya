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

    def __init__(self, surface: str, *, detail: str | None = None) -> None:
        if detail:
            super().__init__(f"ctx.{surface} is unsupported in OHLCV backtests: {detail}")
        else:
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
    primary_timeframe: str = "1m"

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
        # Timeframe keys and lookups are normalised to lowercase so a
        # strategy asking for ``"15M"`` against ``"15m"``-keyed bars (or
        # vice versa) finds its data instead of silently falling back to
        # the primary bars.
        wanted = str(timeframe or "").strip().lower()
        by_tf = {
            str(key or "").strip().lower(): rows
            for key, rows in (self.timeframe_bars_by_market.get(market, {}) or {}).items()
        }
        rows = by_tf.get(wanted)
        if rows is None and self._is_foreign_timeframe(wanted):
            # Silent substitution would make a "15m" indicator compute on
            # the primary bars — validated/promoted behaviour would then
            # diverge from live, which computes on real 15m bars. Fail loudly.
            raise BacktestUnsupportedSurfaceError(
                f"market.candles timeframe={timeframe!r}",
                detail=(
                    f"no {timeframe} bars were provided for this replay "
                    f"(primary timeframe {self.primary_timeframe!r}); pass "
                    f"timeframe_candles_by_market or request the primary timeframe"
                ),
            )
        if rows is None:
            rows = self.bars_by_market.get(market, [])
        return list(rows)[-int(limit):]

    def _is_foreign_timeframe(self, timeframe: str) -> bool:
        """True when ``timeframe`` is neither the primary nor provided."""

        wanted = str(timeframe or "").strip().lower()
        primary = str(self.primary_timeframe or "").strip().lower()
        if not wanted or wanted == primary:
            return False
        provided = self.timeframe_bars_by_market.get(self.market, {})
        return not any(str(tf or "").strip().lower() == wanted for tf in provided)

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
    # Backing state (the engine's position/NAV mirror) and the current
    # bar close, used to build a paper-equivalent fill estimate for the
    # terminal envelope.
    state: Any = None
    mark_price: float = 0.0

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
            "plan_action": payload.get("plan_action")
            or (payload.get("meta") or {}).get("plan_action", ""),
            "raw": dict(payload),
        }
        self.pending_orders.append(record)
        return self._paper_envelope(record)

    # ------------------------------------------------------------------
    # Paper-equivalent terminal envelope
    # ------------------------------------------------------------------

    def _paper_envelope(self, record: dict[str, Any], **extra: Any) -> dict[str, Any]:
        """Return the terminal envelope for a backtest order.

        Since the C5/E5 fix the compat adapters treat **only** a
        ``filled`` status as executed (a ``submitted`` ack never
        registers a position), and the live paper pipeline answers
        ``submit_intent`` with a terminal ``filled`` envelope carrying
        the executor's ``order`` summary. The mock must answer with the
        same shape or imported Freqtrade/VNpy strategies would never
        track a position in replay (every trade would degenerate into
        an engine forced_close). The ``order`` summary mirrors
        ``submit.py``'s paper path: ``order_id`` / ``filled_size`` /
        ``avg_price`` estimated from the current bar close and the
        engine's position mirror.

        The envelope is an optimistic paper-shaped ack: the engine's
        :func:`settle` stays authoritative for accounting (cash,
        max-open-trades and shorting gates can still reject the order
        when it is booked at the next bar's open).
        """

        price = float(self.mark_price or 0.0)
        qty = self._estimate_filled_size(record, price)
        order_id = f"bto_{uuid.uuid4().hex[:12]}"
        envelope: dict[str, Any] = {
            "ok": True,
            "status": "filled",
            "intent_id": record.get("intent_id"),
            "intent": dict(record),
            "order_id": order_id,
            "order": {
                "order_id": order_id,
                "intent_id": record.get("intent_id"),
                "status": "filled",
                "filled_size": qty,
                "avg_price": price,
                "notional_usd": qty * price,
            },
            "risk_decision": {"ok": True, "mode": "backtest"},
        }
        envelope.update(extra)
        return envelope

    def _estimate_filled_size(self, record: dict[str, Any], price: float) -> float:
        """Estimate the base size the engine's settle is about to book."""

        action = str(record.get("plan_action") or "").lower()
        market = str(record.get("market") or "")
        book_signed = self._book_qty(market)
        book_qty = abs(book_signed)
        side = str(record.get("side") or "buy").lower()
        if action in {"close_position", "close", "exit"} or record.get("close_all"):
            return book_qty
        if action in {"reduce_position", "reduce"}:
            pct = max(0.0, min(1.0, float(record.get("reduce_pct") or 1.0)))
            return book_qty * pct
        if not action and book_qty > 0.0 and (side == "sell") == (book_signed > 0.0):
            # Legacy bare submit_intent exit: the side opposes the held
            # book, so settle flattens the whole position.
            return book_qty
        size = float(record.get("size") or 0.0)
        unit = str(record.get("size_unit") or "usd").lower()
        if unit in {"base", "qty", "quantity"}:
            return max(size, 0.0)
        if unit in {"pct_nav", "pct", "percent", "nav_pct", "percent_nav", "equity_pct"}:
            pct = size / 100.0 if size > 1.0 else max(0.0, size)
            nav = float((self._nav() or {}).get("equity") or (self._nav() or {}).get("cash") or 0.0)
            return (nav * pct) / price if price > 0.0 else 0.0
        return size / price if price > 0.0 else 0.0

    def _nav(self) -> dict[str, float]:
        raw = self.state.get("__portfolio_nav__") if self.state is not None else None
        return raw if isinstance(raw, dict) else {}

    def _book_qty(self, market: str) -> float:
        if self.state is None:
            return 0.0
        pos = self.state.get(f"position:{market}")
        if not isinstance(pos, dict):
            return 0.0
        try:
            return float(pos.get("qty") or 0.0)
        except (TypeError, ValueError):
            return 0.0

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
        method = str(sizing_d.get("method") or "fixed_usd").lower()
        if method == "fixed_usd":
            size = float(sizing_d.get("fixed_usd") or 0.0)
            size_unit = "usd"
        elif method == "fixed_base":
            size = float(sizing_d.get("fixed_base") or 0.0)
            size_unit = "base"
        elif method in {"pct_nav", "pct", "percent", "nav_pct", "percent_nav", "equity_pct"}:
            # Percentage-of-NAV sizing: carry the fraction through so the
            # engine can resolve it against live portfolio equity at fill
            # time. Accept the value under a few common keys.
            pct_val = sizing_d.get("pct_nav")
            for alt in ("pct", "percent", "value", "fraction"):
                if pct_val is None:
                    pct_val = sizing_d.get(alt)
            size = float(pct_val or 0.0)
            size_unit = "pct_nav"
        else:
            # Unsupported open sizing (risk_to_stop, close_all, ...): prefer an
            # explicit fixed_usd if present, otherwise leave the engine's
            # cash-fraction fallback to size it (never silently zero).
            fixed = sizing_d.get("fixed_usd")
            size = float(fixed) if fixed else 0.0
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
        return self._paper_envelope(
            record,
            bracket_id=bracket_id,
            protection=record["protection"],
        )

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
        return self._paper_envelope(record)

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
        return self._paper_envelope(record)


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
            entry = float(value.get("avg_price", 0.0) or 0.0)
            # Signed size mirrors the live merged-position contract
            # (negative = short) so generated strategies that read the
            # signed size compute side-aware PnL/exits correctly.
            out.append({
                "market": pos_market,
                "size": qty,
                "quantity": qty,
                "qty": qty,
                "avg_price": entry,
                "entry_price": entry,
                "side": "long" if qty > 0 else "short",
            })
        return out

    def open_positions(self, market: str | None = None) -> list[dict[str, Any]]:
        return self.positions(market=market)

    def _nav(self) -> dict[str, float]:
        raw = self.state.get("__portfolio_nav__") if hasattr(self.state, "get") else None
        return raw if isinstance(raw, dict) else {}

    @property
    def equity_usd(self) -> float:
        nav = self._nav()
        return float(nav.get("equity") or nav.get("nav") or 0.0)

    @property
    def cash_usd(self) -> float:
        return float(self._nav().get("cash") or 0.0)

    def summary(self) -> dict[str, Any]:
        """Mirror the live ``StrategyPortfolio.summary`` shape.

        Backed by the NAV the engine mirrors into ``MockState`` each bar,
        so backtest NAV reads match the live contract.
        """
        nav = self._nav()
        equity = float(nav.get("equity") or nav.get("nav") or 0.0)
        cash = float(nav.get("cash") or 0.0)
        return {"totals": {"equity_usd": equity, "cash_usd": cash, "nav_usd": equity}}

    def ledger(self, account_id: str | None = None) -> dict[str, Any]:
        """Compatibility accessor for strategies that read NAV via a ledger.

        Returns the same ``{equity, nav, cash}`` view in backtest and live
        (see ``StrategyPortfolio.ledger``) so a strategy that backtests
        behaves identically when promoted.
        """
        nav = self._nav()
        equity = float(nav.get("equity") or nav.get("nav") or 0.0)
        cash = float(nav.get("cash") or 0.0)
        return {"equity": equity, "nav": equity, "cash": cash, "account_id": account_id or ""}


@dataclass
class MockPnL:
    """Mirror the live ``StrategyPnL.summary`` shape during backtest.

    Backed by the NAV the engine mirrors into ``MockState`` each bar, so
    legacy strategy templates that read ``ctx.pnl.summary()`` don't crash
    in backtest (the live facade exists; the mock previously did not).
    """

    state: MockState

    def _nav(self) -> dict[str, float]:
        raw = self.state.get("__portfolio_nav__") if hasattr(self.state, "get") else None
        return raw if isinstance(raw, dict) else {}

    def summary(self) -> dict[str, Any]:
        nav = self._nav()
        equity = float(nav.get("equity") or nav.get("nav") or 0.0)
        realized = float(nav.get("realized_pnl") or 0.0)
        return {
            "equity_usd": equity,
            "pnl_total_usd": realized,
            "realized_pnl": realized,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "max_drawdown_usd": 0.0,
            "drawdown_pct": 0.0,
        }


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
        self.market = MockMarket(
            self.market_name,
            self.bars_by_market,
            self.timeframe_bars_by_market,
            primary_timeframe=str(getattr(self.config_obj, "tf", "1m") or "1m"),
        )
        self.trading = MockTrading(
            self.pending_orders,
            self.strategy_id,
            state=self.state,
            mark_price=float(self.current_bar.get("close", 0.0) or 0.0),
        )
        self.audit = MockAudit(self.audit_sink)
        self.clock = MockClock(int(self.current_bar.get("ts", 0)))
        self.dedupe = MockDedupe()
        self.portfolio = MockPortfolio(self.state)
        self.pnl = MockPnL(self.state)
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

    # Top-level trading forwards. The live ``StrategyContext`` exposes
    # ``ctx.open_position`` / ``ctx.close_position`` / ``ctx.reduce_position``
    # directly (mirroring TradingAPI); strategy code written against that
    # surface must run unmodified under backtest.
    def open_position(self, **kwargs: Any) -> dict[str, Any]:
        return self.trading.open_position(**kwargs)

    def close_position(self, **kwargs: Any) -> dict[str, Any]:
        return self.trading.close_position(**kwargs)

    def reduce_position(self, **kwargs: Any) -> dict[str, Any]:
        return self.trading.reduce_position(**kwargs)

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

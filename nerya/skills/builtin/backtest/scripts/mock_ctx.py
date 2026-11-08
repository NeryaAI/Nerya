"""Backtest StrategyContext mirror."""

from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import BacktestConfig, MockSurfaceCfg
from .order_evidence import track_submission
from .....strategies.prompt_io import StrategyPromptIO
from .....strategies.config_view import StrategyConfig
from .....strategies.candle_view import candle_snapshots, StateMapping


class BacktestUnsupportedSurfaceError(RuntimeError):
    """Raised when a live-only surface is used during backtest."""

    def __init__(self, surface: str, *, detail: str | None = None) -> None:
        self.surface = surface
        if detail:
            super().__init__(f"ctx.{surface} is unsupported in OHLCV backtests: {detail}")
        else:
            super().__init__(
                f"ctx.{surface} is unsupported in OHLCV backtests. "
                "Existing gated surfaces may use an explicit mode='stub' with a "
                "contract-valid payload for simulated branch testing only, not "
                "real AI/input performance evidence. mode='replay' is not implemented. "
                "Do not bypass strategy gates to obtain a passing backtest."
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
        # Uppercase M denotes months, not minutes. Fixed-period replay must
        # not turn a monthly request into minute candles.
        if str(timeframe).strip().endswith("M"):
            raise BacktestUnsupportedSurfaceError("market.candles", detail="calendar-month candles are not minute candles; use a supported explicit timeframe")
        wanted = str(timeframe or "").strip().lower()
        by_tf = {
            str(key or "").strip().lower(): rows
            for key, rows in (self.timeframe_bars_by_market.get(market, {}) or {}).items()
        }
        rows = by_tf.get(wanted)
        if rows is None and wanted != str(self.primary_timeframe).strip().lower():
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
        # A strategy may edit its local rows, never the engine's future input
        # or another market callback's shared historical prefix.
        return candle_snapshots(rows[-int(limit):], timeframe)

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
        rows = self.bars_by_market.get(market, [])
        from .data_cache import _tf_seconds
        stamp = rows[-1].get("ts") if rows else None
        timestamp = (int(stamp) + _tf_seconds(self.primary_timeframe)) * 1000 if stamp is not None else None
        return {"market": market, "bid": None, "ask": None, "mid": close,
                "last": close, "price": close, "ts_ms": timestamp,
                "source": "historical_bar_close", "spread_bps": None,
                "_envelope": {"mode": "historical", "source": "historical_bar_close",
                              "book_available": False}}

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
        # This pure formatter only calls self.candles(). Reuse the actual SDK
        # so RSI/ATR/ADX/MACD and empty-history semantics cannot drift again.
        from .....strategies.context import StrategyMarket
        return StrategyMarket.features(self, market, timeframe=timeframe,
                                       lookback=lookback, account=account)


@dataclass
class MockTrading:
    pending_orders: list[dict[str, Any]]
    strategy_id: str
    # Backing state (the engine's position/NAV mirror) and the current
    # bar close, used to build a paper-equivalent fill estimate for the
    # terminal envelope.
    state: Any = None
    mark_price: float = 0.0
    audit: Any = None
    clock: Any = None
    policy: Any = None
    market_data: Any = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    execution_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    accounts: tuple[str, ...] = ()

    def execution_status(self, receipt: Any) -> dict[str, Any]:
        from .....strategies.execution_status import scoped_ids, status_result
        ids = scoped_ids(receipt, self.strategy_id, self.accounts)
        if ids is None or len(ids["intent_id"]) != 1:
            return status_result()
        intent_id = next(iter(ids["intent_id"]))
        record = self.execution_records.get(intent_id)
        if not record or record.get("strategy_id") != self.strategy_id:
            return status_result()
        if any(ids[key] for key in ("executor_id", "session_id", "plan_id", "reservation_id")):
            return status_result()
        if ids["order_id"] - set(record.get("order_ids", [])):
            return status_result()
        return status_result(record["status"])

    @track_submission
    def submit_intent(self, **payload: Any) -> dict[str, Any]:
        if len(payload) == 1 and isinstance(next(iter(payload.values())), dict):
            payload = dict(next(iter(payload.values())))
        self._check_entry(payload)
        size = payload.get("size", payload.get("notional_usd", payload.get("amount")))
        if isinstance(size, bool) or size is None or not math.isfinite(float(size)) or float(size) <= 0:
            raise ValueError("submit_intent requires a finite positive size; use close_position for a full exit")
        side = str(payload.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("submit_intent side must be buy or sell")
        unit = str(payload.get("size_unit") or "usd").lower()
        if unit not in {"usd", "base"}:
            raise BacktestUnsupportedSurfaceError("trading.submit_intent", detail=f"historical size unit {unit!r} requires explicit base or USD conversion")
        if not str(payload.get("market") or "").strip():
            raise ValueError("submit_intent requires market")
        intent_id = f"bt_{uuid.uuid4().hex[:12]}"
        record = {
            "sdk_method": "submit_intent",
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
            or (payload.get("metadata") or {}).get("plan_action", "")
            or (payload.get("meta") or {}).get("plan_action", ""),
            "raw": dict(payload),
        }
        self.pending_orders.append(record)
        return self._paper_envelope(record)

    # ------------------------------------------------------------------
    # Queued replay receipt; settlement owns terminal fills
    # ------------------------------------------------------------------

    def _paper_envelope(self, record: dict[str, Any], **extra: Any) -> dict[str, Any]:
        """A queued intent is not a fill; settlement owns the execution receipt.

        Strategies must read ctx.portfolio on the next tick for actual holdings.
        Estimates stay explicitly separate and must not register a position.
        """
        self.execution_records[str(record["intent_id"])] = {
            "strategy_id": self.strategy_id, "status": "submitted", "order_ids": [],
        }
        price = float(self.market_data.mark_price(str(record.get("market") or ""))
                      if self.market_data is not None else self.mark_price or 0.0)
        qty = self._estimate_filled_size(record, price)
        envelope: dict[str, Any] = {
            "ok": True,
            "status": "submitted",
            "execution_phase": "queued",
            "intent_id": record.get("intent_id"),
            "intent": dict(record),
            "order_id": None,
            "order": {
                "order_id": None,
                "intent_id": record.get("intent_id"),
                "status": "submitted",
                "filled_size": 0.0,
                "avg_price": None,
            },
            "execution_estimate": {"size": qty, "price": price, "notional_usd": qty * price},
            "risk_decision": {"ok": True, "mode": "backtest", "settlement_pending": True},
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
        if record.get("sdk_method") == "submit_intent":
            amount = float(record["size"])
            return amount if record.get("size_unit") == "base" else amount / price if price > 0 else 0.0
        if action in {"close_position", "close", "exit"} or record.get("close_all"):
            return book_qty
        if action in {"reduce_position", "reduce"}:
            if record.get("fixed_base") is not None:
                return min(book_qty, float(record["fixed_base"]))
            pct = max(0.0, min(1.0, float(record.get("reduce_pct", 0.0))))
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

    @track_submission
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
        if cp_side not in {"long", "short", "buy", "sell"}:
            raise ValueError("open_position side must be long or short")
        legacy_side = "buy" if cp_side in ("long", "buy") else "sell"
        sizing_d = sizing.asdict() if hasattr(sizing, "asdict") else dict(sizing or {})
        from .....trading.order_intents import SizingPolicy
        # Use the public SDK validator before float conversion. Otherwise a
        # dictionary containing True/NaN could pass only in historical replay.
        sizing_d = SizingPolicy(**sizing_d).asdict()
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
            raise BacktestUnsupportedSurfaceError("trading.open_position", detail=f"sizing method {method!r} requires a matching historical execution model; it is not converted to an all-cash order")
        if not math.isfinite(size) or size <= 0 or (size_unit == "pct_nav" and size > 1):
            raise ValueError("open_position requires positive sizing; pct_nav must be in (0, 1]")
        cap = sizing_d.get("max_notional_usd")
        if cap is not None and (not math.isfinite(float(cap)) or float(cap) <= 0):
            raise ValueError("max_notional_usd must be finite and positive")
        self._check_entry(entry)
        from .historical_protection import normalize_protection
        try:
            protection_d = normalize_protection(protection, side="long" if legacy_side == "buy" else "short")
        except ValueError as exc:
            raise BacktestUnsupportedSurfaceError("trading.open_position.protection", detail=str(exc)) from exc
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
            "protection": protection_d or None,
            "max_notional_usd": sizing_d.get("max_notional_usd"),
            "raw": {
                "method": "open_position",
                "side": cp_side,
                "sizing": sizing_d or None,
                "entry": entry.asdict() if hasattr(entry, "asdict") else entry,
                "protection": protection_d or None,
                **extra,
            },
        }
        self.pending_orders.append(record)
        return self._paper_envelope(
            record,
            bracket_id=None,
            protection=record["protection"],
        )

    @track_submission
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

        self._check_entry(entry)
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

    @track_submission
    def reduce_position(
        self,
        *,
        market: str,
        side: str,
        reduce_pct: float | None = None,
        fixed_base: float | None = None,
        entry: Any = None,
        confidence: float = 0.0,
        reasoning_ref: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        """Backtest-mode equivalent of ``TradingAPI.reduce_position``."""

        from .....trading.order_intents import SizingPolicy

        if reduce_pct is None and fixed_base is None:
            raise ValueError("reduce_position requires reduce_pct or fixed_base")
        if reduce_pct is not None:
            sizing = SizingPolicy(method="reduce_pct", reduce_pct=float(reduce_pct))
        else:
            sizing = SizingPolicy(method="fixed_base", fixed_base=float(fixed_base))
        value = sizing.reduce_pct if reduce_pct is not None else sizing.fixed_base
        if not math.isfinite(value):
            raise ValueError("reduce_position sizing must be finite")
        self._check_entry(entry)
        intent_id = f"bt_{uuid.uuid4().hex[:12]}"
        position_side = str(side or "long").lower()
        legacy_side = "sell" if position_side == "long" else "buy"
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
            "reduce_pct": sizing.reduce_pct,
            "fixed_base": sizing.fixed_base,
            "raw": {"method": "reduce_position", "side": position_side, "sizing": sizing.asdict(), **extra},
        }
        self.pending_orders.append(record)
        return self._paper_envelope(record)

    @staticmethod
    def _check_entry(entry: Any) -> None:
        raw = entry.asdict() if hasattr(entry, "asdict") else dict(entry or {})
        if raw.get("order_type", "market") != "market":
            raise BacktestUnsupportedSurfaceError("trading.entry", detail="only market entries are simulated; limit/stop orders require a historical order execution model")

    def attach_protection(self, **kwargs: Any) -> dict[str, Any]:
        raise BacktestUnsupportedSurfaceError("trading.attach_protection", detail="live executors are not invoked during historical replay")

    def cancel_executor(self, *, executor_id: str) -> dict[str, Any]:
        raise BacktestUnsupportedSurfaceError("trading.cancel_executor", detail="there is no live executor in isolated historical replay")

    def risk_preview(self, **kwargs: Any) -> dict[str, Any]:
        raise BacktestUnsupportedSurfaceError("trading.risk_preview", detail="current live account risk is not historical risk evidence; replay settlement applies the configured historical limits")

    def portfolio_snapshot(self, *, account: str | None = None) -> dict[str, Any]:
        raise BacktestUnsupportedSurfaceError("trading.portfolio_snapshot", detail="broker accounts/reservations require historical account snapshots; ctx.portfolio.summary(), positions() and position() expose the isolated replay book")

    def signal(self, *, market: str, signal_kind: str, confidence: float, reasoning_ref: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        record = {"kind": "agent.signal", "strategy_id": self.strategy_id, "market": market,
            "signal_kind": str(signal_kind), "confidence": float(confidence),
            "reasoning_ref": str(reasoning_ref or ""), "payload": dict(payload or {}),
            "ts": self.clock.now_iso() if self.clock else ""}
        if self.audit:
            self.audit.log("signal", record)
        return {"status": "recorded", **record}


@dataclass
class MockState(StateMapping):
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

    def freeze(self, *, iso: str, ms: int | None = None) -> None:
        raise BacktestUnsupportedSurfaceError("clock.freeze", detail="the historical engine owns the replay clock; strategy code must not move it")


@dataclass
class MockPolicy:
    max_single_order_usd: float = 0.0
    max_daily_notional_usd: float = 0.0
    max_open_positions: int = 0
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
            max_open_positions=int(raw.get("max_open_positions", 0) or 0),
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
    market_data: MockMarket | None = None

    def positions(self, market: str | None = None) -> list[Any]:
        # Reuse the actual SDK read model, with a lazy import to avoid the
        # context -> backtest bridge -> engine import cycle.
        from .....strategies.context import StrategyPosition, _market_key

        out: list[StrategyPosition] = []
        for key, value in self.state.data.items():
            if not key.startswith("position:") or not isinstance(value, dict):
                continue
            pos_market = key.split(":", 1)[1]
            if market and _market_key(pos_market) != _market_key(market):
                continue
            qty = float(value.get("qty", 0.0) or 0.0)
            if abs(qty) <= 1e-12:
                continue
            entry = float(value.get("avg_price", 0.0) or 0.0)
            # Signed size mirrors the live merged-position contract
            # (negative = short) so generated strategies that read the
            # signed size compute side-aware PnL/exits correctly.
            mark = self.market_data.mark_price(pos_market) if self.market_data else entry
            out.append(StrategyPosition(market=pos_market, size=qty, avg_price=entry,
                market_value_usd=abs(qty) * mark,
                unrealized_pnl_usd=(mark - entry) * qty))
        return out

    def position(self, market: str) -> Any:
        rows = self.positions(market)
        return rows[0] if rows else None

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
    _events: list[dict[str, Any]] = field(default_factory=list)

    def log(self, kind: str, payload: dict[str, Any] | None = None, *, level: str = "info") -> None:
        record = {"kind": f"strategy.{kind}", "level": level, "payload": dict(payload or {})}
        self._events.append(record)
        if self.sink:
            self.sink(record)

    def record(self, kind: str, payload: dict[str, Any] | None = None, *, level: str = "info") -> None:
        self.log(kind, payload, level=level)

    def events(self) -> list[dict[str, Any]]:
        return list(self._events)


class _GatedSurface:
    def __init__(self, name: str, cfg: MockSurfaceCfg, audit: MockAudit | None = None) -> None:
        self.name = name
        self.cfg = cfg
        self.audit = audit
        self.call_counts: dict[str, int] = {}

    def _value(self) -> Any:
        # Record only actual attempts, never prompts, arguments or payloads.
        mode = self.cfg.mode
        self.call_counts[mode] = self.call_counts.get(mode, 0) + 1
        if self.audit:
            self.audit.log("mock_surface_call", {"surface": self.name, "mode": mode, "count": 1})
        if self.cfg.mode == "stub":
            return self.cfg.payload
        if self.cfg.mode == "replay":
            raise BacktestUnsupportedSurfaceError(self.name, detail=
                f"mock_surfaces.{self.name}.mode='replay' is not implemented; "
                "mode='stub' only simulates supplied payloads and is not historical "
                "AI/input performance evidence. Do not bypass strategy gates.")
        raise BacktestUnsupportedSurfaceError(self.name)


class MockNews(_GatedSurface):
    def register(self, source_id: str, fetcher: Any) -> None:
        raise BacktestUnsupportedSurfaceError("news.register", detail="live fetchers are not registered during replay; configure durable historical observations")

    def fetch(self, **_: Any) -> list[dict[str, Any]]:
        value = self._value()
        return list(value or [])


class MockLLM(_GatedSurface):
    _calls_made: int = 0

    def _value(self) -> Any:
        value = super()._value()
        self._calls_made += 1
        return value

    @property
    def calls_made(self) -> int:
        return self._calls_made

    def compress(self, **_: Any) -> dict[str, Any]:
        return dict(self._value() or {})

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
class SimpleConfigView(StrategyConfig):
    """Compatibility name; all manifest access uses the runtime SDK model."""
    mode: str = "backtest"


class BacktestPromptIO:
    """Reuse real, deterministic formatters without granting artifact writes.

    Formatting an Agent task is not running a model. LLM/subagent surfaces
    remain independently gated by MockSurfaceCfg.
    """
    csv = StrategyPromptIO.csv
    markdown_table = StrategyPromptIO.markdown_table
    json_block = StrategyPromptIO.json_block
    truncate_csv = StrategyPromptIO.truncate_csv

    def artifact(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise BacktestUnsupportedSurfaceError(
            "prompt.artifact", detail="strategy artifact writes are unavailable during OHLCV replay",
        )


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
    run_id: str = ""
    session_id: str | None = None
    strategy_root: Path | None = None
    backtest_replay: Any = None
    stream: Any = None
    execution_records: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from .....strategies.context import StrategyDedupe, StrategyRunDeadline, StrategyTriggerContext

        self.run_id = self.run_id or f"backtest:{self.strategy_id}:{self.current_bar.get('ts', '')}:{self.market_name}"
        self.run_deadline = StrategyRunDeadline()
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
            policy=self.policy_obj or MockPolicy(),
            market_data=self.market,
            execution_records=self.execution_records,
            accounts=tuple(getattr(self.config, "accounts", ())),
        )
        self.audit = MockAudit(self.audit_sink)
        from .data_cache import _tf_seconds
        # OHLCV timestamps are opening times; the full bar is knowable at close.
        self.clock = MockClock(int(self.current_bar.get("ts", 0)) + _tf_seconds(self.config_obj.tf))
        self.trading.audit = self.audit
        self.trading.clock = self.clock
        self.dedupe = StrategyDedupe(self.state)
        self.portfolio = MockPortfolio(self.state, self.market)
        self.pnl = MockPnL(self.state)
        self.news = MockNews("news", self.config_obj.mock_surfaces["news"], self.audit)
        self.llm = MockLLM("llm", self.config_obj.mock_surfaces["llm"], self.audit)
        self.subagents = MockSubAgents("subagents", self.config_obj.mock_surfaces["subagents"], self.audit)
        self.messages = MockMessages("messages", self.config_obj.mock_surfaces["messages"], self.audit)
        self.policy = self.policy_obj or MockPolicy()
        self.trigger = StrategyTriggerContext(source="backtest", kind="market.candle_closed",
            strategy_id=self.strategy_id, event_id=self.run_id,
            occurred_at=self.clock.now_iso(), payload={"market": self.market_name,
                "timeframe": self.config_obj.tf, "candle": dict(self.current_bar),
                "closed_at": self.clock.now_iso(), "historical": True})
        self.prompt = BacktestPromptIO()
        if self.config is None:
            self.config = SimpleConfigView(
                strategy_id=self.strategy_id,
                markets=tuple(self.config_obj.markets),
            )

        from nerya.strategies.input_context import StrategyInputContext
        self.inputs = StrategyInputContext(sources=getattr(self.config, "extras", {}).get("data_sources", []),
            market=self.market, news=self.news, markets=tuple(self.config.markets),
            run_id=self.run_id, clock=self.clock)

    @property
    def mock_surface_calls(self) -> list[dict[str, Any]]:
        return [{"surface": surface.name, "mode": mode, "count": count}
                for surface in (self.news, self.llm, self.subagents, self.messages)
                for mode, count in sorted(surface.call_counts.items())]

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

"""Lightweight VNpy stand-ins for running VNpy strategy code on Nerya.

Strategy code written against VNpy imports things like::

    from vnpy_ctastrategy import (
        CtaTemplate, StopOrder, TickData, BarData, TradeData, OrderData,
    )
    from vnpy.trader.utility import BarGenerator, ArrayManager
    from vnpy.trader.constant import Direction, Offset, Interval

Installing the full VNpy stack (event engine + gateway + database) just
to execute a strategy's *logic* on Nerya is neither necessary nor
desirable — the strategy only ever sees the objects it is handed. So
this module registers dependency-free stand-ins under the real module
names:

* :mod:`vnpy.trader.constant` — the ``Direction`` / ``Offset`` /
  ``Status`` / ``Interval`` / ``Exchange`` enums.
* :mod:`vnpy.trader.object` — the ``BarData`` / ``TickData`` /
  ``TradeData`` / ``OrderData`` dataclasses.
* :mod:`vnpy.trader.utility` — ``BarGenerator`` (minute→window bar
  synthesis) and ``ArrayManager`` (rolling numpy arrays + indicators).
* :mod:`vnpy_ctastrategy` / ``vnpy_ctastrategy.template`` —
  :class:`CtaTemplate` and :class:`StopOrder`.

``install()`` is idempotent and only registers a module when the real
framework is not importable (``importlib.util.find_spec``), so a
strategy written against a genuine VNpy install keeps using the real
classes. ``BarGenerator`` boundary handling matches VNpy's documented
behaviour for the common MINUTE/HOUR windows; exotic edge cases (e.g.
half-finished hour bars at session gaps) are simplified.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

import numpy as np


# ---------------------------------------------------------------------------
# vnpy.trader.constant — enums
# ---------------------------------------------------------------------------


class Direction(Enum):
    LONG = "多"
    SHORT = "空"
    NET = "净"


class Offset(Enum):
    NONE = ""
    OPEN = "开"
    CLOSE = "平"
    CLOSETODAY = "平今"


class Status(Enum):
    SUBMITTING = "提交中"
    NOTTRADED = "未成交"
    PARTTRADED = "部分成交"
    ALLTRADED = "全部成交"
    CANCELLED = "已撤销"
    REJECTED = "拒绝"


class Interval(Enum):
    TICK = "tick"
    MINUTE = "1m"
    HOUR = "1h"
    DAILY = "d"
    WEEKLY = "w"


class Exchange(Enum):
    LOCAL = "LOCAL"
    BINANCE = "BINANCE"
    OKX = "OKX"
    BYBIT = "BYBIT"
    GATEIO = "GATE"
    HUOBI = "HUOBI"
    BITGET = "BITGET"
    XT = "XT"
    FTX = "FTX"
    COINBASE = "COINBASE"
    NYMEX = "NYMEX"
    COMEX = "COMEX"
    CME = "CME"
    CBOT = "CBOT"
    SGX = "SGX"
    HKEX = "HKEX"
    SSE = "SSE"
    SZSE = "SZSE"
    CFFEX = "CFFEX"
    SHFE = "SHFE"
    DCE = "DCE"
    CZCE = "CZCE"
    INE = "INE"
    GFEX = "GFEX"
    SMART = "SMART"
    IDEALPRO = "IDEALPRO"


# ---------------------------------------------------------------------------
# vnpy.trader.object — dataclasses
# ---------------------------------------------------------------------------


def _extract_vt_symbol(vt_symbol: str) -> "tuple[str, Exchange]":
    symbol, _, ex = str(vt_symbol).partition(".")
    try:
        return symbol, Exchange(ex)
    except ValueError:
        return symbol, Exchange.LOCAL


@dataclass
class BarData:
    symbol: str
    exchange: Exchange
    datetime: datetime

    interval: Optional[Interval] = None
    volume: float = 0
    turnover: float = 0
    open_interest: float = 0
    open_price: float = 0
    high_price: float = 0
    low_price: float = 0
    close_price: float = 0

    @property
    def vt_symbol(self) -> str:
        return f"{self.symbol}.{self.exchange.value}"


@dataclass
class TickData:
    symbol: str
    exchange: Exchange
    datetime: datetime

    name: str = ""
    volume: float = 0
    turnover: float = 0
    open_interest: float = 0
    last_price: float = 0
    limit_up: float = 0
    limit_down: float = 0
    open_price: float = 0
    high_price: float = 0
    low_price: float = 0
    pre_close: float = 0
    bid_price_1: float = 0
    bid_price_2: float = 0
    bid_price_3: float = 0
    bid_price_4: float = 0
    bid_price_5: float = 0
    ask_price_1: float = 0
    ask_price_2: float = 0
    ask_price_3: float = 0
    ask_price_4: float = 0
    ask_price_5: float = 0
    bid_volume_1: float = 0
    bid_volume_2: float = 0
    bid_volume_3: float = 0
    bid_volume_4: float = 0
    bid_volume_5: float = 0
    ask_volume_1: float = 0
    ask_volume_2: float = 0
    ask_volume_3: float = 0
    ask_volume_4: float = 0
    ask_volume_5: float = 0
    local_time: datetime | None = None

    @property
    def vt_symbol(self) -> str:
        return f"{self.symbol}.{self.exchange.value}"


@dataclass
class TradeData:
    symbol: str
    exchange: Exchange
    datetime: datetime

    orderid: str = ""
    tradeid: str = ""
    direction: Direction = Direction.LONG
    offset: Offset = Offset.NONE
    price: float = 0
    volume: float = 0

    @property
    def vt_symbol(self) -> str:
        return f"{self.symbol}.{self.exchange.value}"

    @property
    def vt_tradeid(self) -> str:
        return f"{self.vt_symbol}.{self.tradeid}"

    def is_buy(self) -> bool:
        return self.direction == Direction.LONG


@dataclass
class OrderData:
    symbol: str
    exchange: Exchange
    orderid: str

    type: Any = None
    direction: Direction = Direction.LONG
    offset: Offset = Offset.NONE
    price: float = 0
    volume: float = 0
    traded: float = 0
    status: Status = Status.SUBMITTING
    datetime: datetime | None = None
    reference: str = ""

    @property
    def vt_symbol(self) -> str:
        return f"{self.symbol}.{self.exchange.value}"

    @property
    def vt_orderid(self) -> str:
        return f"{self.vt_symbol}.{self.orderid}"

    def is_active(self) -> bool:
        return self.status in {Status.SUBMITTING, Status.NOTTRADED, Status.PARTTRADED}


@dataclass
class StopOrder:
    vt_orderid: str
    symbol: str
    exchange: Exchange
    direction: Direction
    offset: Offset
    price: float
    volume: float
    stop_price: float
    strategy_name: str = ""
    status: Status = Status.SUBMITTING
    datetime: datetime | None = None

    @property
    def vt_symbol(self) -> str:
        return f"{self.symbol}.{self.exchange.value}"


STOP_ORDER_PREFIX: str = "STOP."
_stop_order_count: int = 0


def new_stop_order_id() -> str:
    global _stop_order_count
    _stop_order_count += 1
    return f"{STOP_ORDER_PREFIX}{_stop_order_count}"


# ---------------------------------------------------------------------------
# vnpy.trader.utility — BarGenerator / ArrayManager
# ---------------------------------------------------------------------------


class BarGenerator:
    """Minute-bar synthesiser compatible with ``vnpy.BarGenerator``.

    * ``update_tick(tick)`` synthesises 1-minute bars from ticks.
    * ``update_bar(bar)`` either forwards bars (``window=1``) or merges
      them into window bars emitted through ``on_window_bar``.
      MINUTE windows align on ``minute % window == 0``; HOUR windows
      synthesise hour bars from minute bars first; DAILY windows build
      on hour bars.
    * ``trans_tick``/``last_tick``/``window_bar`` mirror the public
      attributes strategies commonly poke at.
    """

    def __init__(
        self,
        on_bar: Callable[[BarData], None],
        window: int = 1,
        on_window_bar: Callable[[BarData], None] | None = None,
        interval: Interval = Interval.MINUTE,
    ) -> None:
        self.on_bar: Callable[[BarData], None] = on_bar
        self.window: int = max(int(window or 1), 1)
        self.on_window_bar: Callable[[BarData], None] = on_window_bar or (lambda bar: None)
        self.interval: Interval = interval

        self.last_tick: TickData | None = None
        self.last_bar: BarData | None = None
        self.window_bar: BarData | None = None

        self.hour_bar: BarData | None = None
        self.day_bar: BarData | None = None
        self.window_hours: int = 0
        self.window_days: int = 0

    # -- tick -> 1m bar ------------------------------------------------

    def update_tick(self, tick: TickData) -> None:
        minute_ms = 60 * 1000
        ts = int(tick.datetime.timestamp() * 1000)
        bar_minute = (ts // minute_ms) * minute_ms
        dt = datetime.fromtimestamp(bar_minute / 1000.0)

        if self.last_bar is None or self.last_bar.datetime != dt:
            if self.last_bar is not None:
                self.on_bar(self.last_bar)
            self.last_bar = BarData(
                symbol=tick.symbol,
                exchange=tick.exchange,
                datetime=dt,
                interval=Interval.MINUTE,
                open_price=tick.last_price,
                high_price=tick.last_price,
                low_price=tick.last_price,
                close_price=tick.last_price,
                volume=tick.volume,
            )
            return

        self.last_bar.high_price = max(self.last_bar.high_price, tick.last_price)
        self.last_bar.low_price = min(self.last_bar.low_price, tick.last_price)
        self.last_bar.close_price = tick.last_price
        self.last_bar.volume += max(float(tick.volume or 0.0), 0.0)

    # -- bar -> window bar ----------------------------------------------

    def update_bar(self, bar: BarData) -> None:
        self.last_bar = bar
        if self.interval == Interval.MINUTE:
            self._update_minute_window(bar)
        elif self.interval == Interval.HOUR:
            self._update_hour_window(bar)
        elif self.interval == Interval.DAILY:
            self._update_daily_window(bar)
        else:  # WEEKLY / TICK intervals degrade to passthrough
            self.on_bar(bar)

    def _update_minute_window(self, bar: BarData) -> None:
        if self.window == 1:
            self.on_bar(bar)
            return
        if bar.datetime.minute % self.window == 0:
            if self.window_bar is not None:
                self.on_window_bar(self.window_bar)
                self.window_bar = None
            self.window_bar = self._copy_bar(bar, bar.datetime)
            return  # the starting bar is already merged in via the copy
        if self.window_bar is None:
            # Engine started mid-window: begin the window from this bar.
            self.window_bar = self._copy_bar(bar, bar.datetime)
            return
        self._merge_into(self.window_bar, bar)

    def _update_hour_window(self, bar: BarData) -> None:
        finished = self._synth_hour_bar(bar)
        if finished is None:
            return
        if self.window == 1:
            self.on_window_bar(finished)
            return
        if self.window_hours == 0:
            self.window_hours = 1
            self.window_bar = finished
        else:
            self._merge_into(self.window_bar, finished)
            self.window_hours += 1
        if self.window_hours >= self.window:
            self.on_window_bar(self.window_bar)
            self.window_bar = None
            self.window_hours = 0

    def _update_daily_window(self, bar: BarData) -> None:
        finished = self._synth_day_bar(bar)
        if finished is None:
            return
        if self.window == 1:
            self.on_window_bar(finished)
            return
        if self.window_days == 0:
            self.window_days = 1
            self.window_bar = finished
        else:
            self._merge_into(self.window_bar, finished)
            self.window_days += 1
        if self.window_days >= self.window:
            self.on_window_bar(self.window_bar)
            self.window_bar = None
            self.window_days = 0

    # -- internal helpers -------------------------------------------------

    def _synth_hour_bar(self, bar: BarData) -> BarData | None:
        """Merge a minute bar into ``hour_bar``; emit on hour rollover."""

        hour_dt = bar.datetime.replace(minute=0, second=0, microsecond=0)
        if self.hour_bar is None:
            self.hour_bar = self._copy_bar(bar, hour_dt)
            return None
        if bar.datetime.hour != self.hour_bar.datetime.hour:
            finished = self.hour_bar
            self.hour_bar = self._copy_bar(bar, hour_dt)
            return finished
        self._merge_into(self.hour_bar, bar)
        return None

    def _synth_day_bar(self, bar: BarData) -> BarData | None:
        day_dt = bar.datetime.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.day_bar is None:
            self.day_bar = self._copy_bar(bar, day_dt)
            return None
        if bar.datetime.date() != self.day_bar.datetime.date():
            finished = self.day_bar
            self.day_bar = self._copy_bar(bar, day_dt)
            return finished
        self._merge_into(self.day_bar, bar)
        return None

    @staticmethod
    def _copy_bar(bar: BarData, datetime_override: datetime) -> BarData:
        return BarData(
            symbol=bar.symbol,
            exchange=bar.exchange,
            datetime=datetime_override,
            interval=bar.interval,
            open_price=bar.open_price,
            high_price=bar.high_price,
            low_price=bar.low_price,
            close_price=bar.close_price,
            volume=bar.volume,
            turnover=bar.turnover,
            open_interest=bar.open_interest,
        )

    @staticmethod
    def _merge_into(target: BarData, bar: BarData) -> None:
        target.high_price = max(target.high_price, bar.high_price)
        target.low_price = min(target.low_price, bar.low_price)
        target.close_price = bar.close_price
        target.volume += float(bar.volume or 0.0)
        target.turnover += float(bar.turnover or 0.0)
        target.open_interest = bar.open_interest


class ArrayManager:
    """Rolling OHLCV buffer with the VNpy indicator surface.

    Mirrors ``vnpy.trader.utility.ArrayManager``: fixed-size numpy
    buffers filled oldest→newest via :meth:`update_bar`, ``inited``
    once ``count >= size``, and indicator helpers returning either a
    scalar (last value) or the full array (``array=True``).
    """

    def __init__(self, size: int = 100) -> None:
        self.count: int = 0
        self.size: int = max(int(size), 1)
        self.inited: bool = False

        self.open_array: np.ndarray = np.zeros(self.size)
        self.high_array: np.ndarray = np.zeros(self.size)
        self.low_array: np.ndarray = np.zeros(self.size)
        self.close_array: np.ndarray = np.zeros(self.size)
        self.volume_array: np.ndarray = np.zeros(self.size)
        self.turnover_array: np.ndarray = np.zeros(self.size)
        self.open_interest_array: np.ndarray = np.zeros(self.size)

    def update_bar(self, bar: BarData) -> None:
        self.count += 1
        if not self.inited and self.count >= self.size:
            self.inited = True
        self.open_array[:-1] = self.open_array[1:]
        self.high_array[:-1] = self.high_array[1:]
        self.low_array[:-1] = self.low_array[1:]
        self.close_array[:-1] = self.close_array[1:]
        self.volume_array[:-1] = self.volume_array[1:]
        self.turnover_array[:-1] = self.turnover_array[1:]
        self.open_interest_array[:-1] = self.open_interest_array[1:]

        self.open_array[-1] = bar.open_price
        self.high_array[-1] = bar.high_price
        self.low_array[-1] = bar.low_price
        self.close_array[-1] = bar.close_price
        self.volume_array[-1] = bar.volume
        self.turnover_array[-1] = bar.turnover
        self.open_interest_array[-1] = bar.open_interest

    @property
    def open(self) -> np.ndarray:
        return self.open_array

    @property
    def high(self) -> np.ndarray:
        return self.high_array

    @property
    def low(self) -> np.ndarray:
        return self.low_array

    @property
    def close(self) -> np.ndarray:
        return self.close_array

    @property
    def volume(self) -> np.ndarray:
        return self.volume_array

    def get(self, size: int | None = None) -> int:
        return self.size if size is None else int(size)

    # -- indicators -------------------------------------------------------

    def sma(self, n: int, array: bool = False) -> Any:
        return _last_or_array(_rolling(self.close_array, n, lambda x: np.nanmean(x)), array)

    def ema(self, n: int, array: bool = False) -> Any:
        values = _ema_series(self.close_array, n)
        return values if array else float(values[-1])

    def std(self, n: int, array: bool = False) -> Any:
        return _last_or_array(_rolling(self.close_array, n, lambda x: np.nanstd(x)), array)

    def macd(
        self,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
        array: bool = False,
    ) -> tuple[Any, Any, Any]:
        macd_line = _ema_series(self.close_array, slow_period) - _ema_series(
            self.close_array, fast_period
        )
        signal_line = _ema_series(macd_line, signal_period)
        hist = macd_line - signal_line
        if array:
            return macd_line, signal_line, hist
        return float(macd_line[-1]), float(signal_line[-1]), float(hist[-1])

    def atr(self, n: int, array: bool = False) -> Any:
        tr = _true_range(self.high_array, self.low_array, self.close_array)
        values = _rolling(tr, n, lambda x: np.nanmean(x))
        return _last_or_array(values, array)

    def rsi(self, n: int, array: bool = False) -> Any:
        values = _rsi_series(self.close_array, n)
        return values if array else float(values[-1])

    def boll(
        self,
        n: int,
        dev: float = 2,
        array: bool = False,
    ) -> tuple[Any, Any, Any]:
        mid = _rolling(self.close_array, n, lambda x: np.nanmean(x))
        std = _rolling(self.close_array, n, lambda x: np.nanstd(x))
        up = mid + std * float(dev)
        down = mid - std * float(dev)
        if array:
            return up, down, mid
        return float(up[-1]), float(down[-1]), float(mid[-1])

    def kdj(
        self,
        k_period: int = 9,
        d_period: int = 3,
        array: bool = False,
    ) -> tuple[Any, Any, Any]:
        low_list = _rolling(self.low_array, k_period, lambda x: np.nanmin(x))
        high_list = _rolling(self.high_array, k_period, lambda x: np.nanmax(x))
        denom = np.where((high_list - low_list) == 0, np.nan, high_list - low_list)
        rsv = (self.close_array - low_list) / denom * 100

        k_values = _smooth(rsv, d_period, kind="sma")
        d_values = _smooth(k_values, d_period, kind="sma")
        j_values = 3 * k_values - 2 * d_values
        if array:
            return k_values, d_values, j_values
        return float(k_values[-1]), float(d_values[-1]), float(j_values[-1])

    def cci(self, n: int, array: bool = False) -> Any:
        tp = (self.high_array + self.low_array + self.close_array) / 3.0
        ma = _rolling(tp, n, lambda x: np.nanmean(x))
        md = _rolling(tp, n, lambda x: np.nanmean(np.abs(x - np.nanmean(x))))
        denom = np.where(md == 0, np.nan, 0.015 * md)
        values = (tp - ma) / denom
        return _last_or_array(values, array)

    def donchian(self, n: int, array: bool = False) -> tuple[Any, Any]:
        up = _rolling(self.high_array, n, lambda x: np.nanmax(x))
        down = _rolling(self.low_array, n, lambda x: np.nanmin(x))
        if array:
            return up, down
        return float(up[-1]), float(down[-1])


# ---------------------------------------------------------------------------
# indicator helpers (numpy equivalents of the talib calls VNpy uses)
# ---------------------------------------------------------------------------


def _rolling(values: np.ndarray, n: int, fn: Callable[[np.ndarray], float]) -> np.ndarray:
    """Rolling ``fn`` over the last ``n`` values; NaN before warm-up."""

    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    n = max(int(n), 1)
    if n == 1:
        return np.where(np.isnan(values), np.nan, values)
    for i in range(n - 1, values.shape[0]):
        window = values[i - n + 1 : i + 1]
        if np.all(np.isnan(window)):
            continue
        out[i] = fn(window)
    return out


def _ema_series(values: np.ndarray, n: int) -> np.ndarray:
    """Talib-style EMA: SMA seed for the first ``n`` points, then EMA."""

    values = np.asarray(values, dtype=float)
    n = max(int(n), 1)
    out = np.full(values.shape, np.nan)
    if values.shape[0] == 0:
        return out
    first = min(n, values.shape[0])
    seed_window = values[:first]
    if np.all(np.isnan(seed_window)):
        return out
    out[first - 1] = np.nanmean(seed_window)
    alpha = 2.0 / (n + 1.0)
    for i in range(first, values.shape[0]):
        prev = out[i - 1]
        cur = values[i]
        out[i] = cur * alpha + prev * (1.0 - alpha) if not np.isnan(cur) else prev
    return out


def _smooth(values: np.ndarray, n: int, *, kind: str = "sma") -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if kind == "sma":
        return _rolling(values, max(int(n), 1), lambda x: np.nanmean(x))
    return _ema_series(values, max(int(n), 1))


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    prev_close = np.roll(close, 1)
    prev_close[0] = np.nan
    ranges = np.stack(
        [
            high - low,
            np.abs(high - prev_close),
            np.abs(low - prev_close),
        ],
        axis=1,
    )
    return np.nanmax(ranges, axis=1)


def _rsi_series(values: np.ndarray, n: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    n = max(int(n), 1)
    out = np.full(values.shape, np.nan)
    if values.shape[0] < n + 1:
        return out
    delta = np.diff(values)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.nanmean(gain[:n])
    avg_loss = np.nanmean(loss[:n])
    for i in range(n, values.shape[0]):
        gain_i, loss_i = gain[i - 1], loss[i - 1]
        if i > n:
            avg_gain = (avg_gain * (n - 1) + gain_i) / n
            avg_loss = (avg_loss * (n - 1) + loss_i) / n
        if avg_loss == 0:
            out[i] = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def _last_or_array(values: np.ndarray, array: bool) -> Any:
    values = np.asarray(values, dtype=float)
    if array:
        return values
    return float(values[-1]) if values.shape[0] else float("nan")


# ---------------------------------------------------------------------------
# vnpy_ctastrategy — CtaTemplate
# ---------------------------------------------------------------------------


class EngineType(Enum):
    LIVE = "实盘"
    BACKTESTING = "回测"


class CtaTemplate:
    """VNpy CTA strategy base — same public surface as the real one.

    The adapter (:mod:`nerya.strategies.compat.vnpy_adapter`) supplies a
    lightweight ``cta_engine`` implementing the handful of engine calls
    templates make: ``send_order``, ``cancel_all``, ``write_log``,
    ``load_bar``, ``get_engine_type``, ``sync_data``.
    """

    author: str = ""
    parameters: list[str] = []
    settings: list[dict[str, Any]] = []
    variables: list[str] = ["pos"]

    def __init__(
        self,
        cta_engine: Any,
        strategy_name: str,
        vt_symbol: str,
        setting: dict[str, Any] | None = None,
    ) -> None:
        self.cta_engine = cta_engine
        self.strategy_name = strategy_name
        self.vt_symbol = vt_symbol
        self.symbol, self.exchange = _extract_vt_symbol(vt_symbol)

        self.data: dict[str, Any] = {}
        self.pos: int = 0
        self.trading: bool = False
        self.inited: bool = False

        if setting:
            self.update_setting(setting)

    def update_setting(self, setting: dict[str, Any]) -> None:
        for name in self.parameters:
            if name in setting:
                setattr(self, name, setting[name])

    # -- lifecycle hooks (strategies override these) ---------------------

    def on_init(self) -> None:
        self.write_log("策略初始化")

    def on_tick(self, tick: TickData) -> None:
        pass

    def on_bar(self, bar: BarData) -> None:
        pass

    def on_order(self, order: OrderData) -> None:
        pass

    def on_trade(self, trade: TradeData) -> None:
        pass

    def on_stop_order(self, order: StopOrder) -> None:
        pass

    def on_stop(self) -> None:
        self.write_log("策略停止")

    # -- engine calls ------------------------------------------------------

    def load_bar(
        self,
        days: int,
        hour: int = 0,
        minute: int = 0,
        interval: Interval = Interval.MINUTE,
        callback: Callable[[BarData], None] | None = None,
    ) -> None:
        loader = getattr(self.cta_engine, "load_bar", None)
        if loader is None:
            return
        loader(self, days=days, hour=hour, minute=minute, interval=interval, callback=callback)

    def load_tick(self, days: int) -> None:
        # Tick replay requires a tick source; Nerya adapters feed bars.
        pass

    def send_order(
        self,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        stop: bool = False,
        lock: bool = False,
        net: bool = False,
    ) -> list[str]:
        if self.trading:
            sender = getattr(self.cta_engine, "send_order", None)
            if sender is not None:
                return sender(
                    self, str(direction.value), str(offset.value), float(price), float(volume),
                    stop=stop, lock=lock, net=net,
                )
        return []

    def buy(
        self,
        price: float,
        volume: float,
        stop: bool = False,
        lock: bool = False,
        net: bool = False,
    ) -> list[str]:
        """Buy to open a long position."""

        return self.send_order(Direction.LONG, Offset.OPEN, price, volume, stop, lock, net)

    def sell(
        self,
        price: float,
        volume: float,
        stop: bool = False,
        lock: bool = False,
        net: bool = False,
    ) -> list[str]:
        """Sell to close a long position."""

        return self.send_order(Direction.SHORT, Offset.CLOSE, price, volume, stop, lock, net)

    def short(
        self,
        price: float,
        volume: float,
        stop: bool = False,
        lock: bool = False,
        net: bool = False,
    ) -> list[str]:
        """Sell to open a short position."""

        return self.send_order(Direction.SHORT, Offset.OPEN, price, volume, stop, lock, net)

    def cover(
        self,
        price: float,
        volume: float,
        stop: bool = False,
        lock: bool = False,
        net: bool = False,
    ) -> list[str]:
        """Buy to close a short position."""

        return self.send_order(Direction.LONG, Offset.CLOSE, price, volume, stop, lock, net)

    def cancel_order(self, vt_orderid: str) -> None:
        canceller = getattr(self.cta_engine, "cancel_order", None)
        if canceller is not None:
            canceller(self, vt_orderid)

    def cancel_all(self) -> None:
        canceller = getattr(self.cta_engine, "cancel_all", None)
        if canceller is not None:
            canceller(self)

    def write_log(self, msg: str) -> None:
        logger = getattr(self.cta_engine, "write_log", None)
        if logger is not None:
            logger(msg, strategy=self)

    def get_engine_type(self) -> EngineType:
        getter = getattr(self.cta_engine, "get_engine_type", None)
        if getter is not None:
            return getter()
        return EngineType.BACKTESTING

    def get_data(self) -> dict[str, Any]:
        return self.data

    def put_event(self) -> None:
        # UI event pump is a no-op under the Nerya adapter.
        syncer = getattr(self.cta_engine, "sync_data", None)
        if syncer is not None:
            syncer(self)

    def sync_data(self) -> None:
        syncer = getattr(self.cta_engine, "sync_data", None)
        if syncer is not None:
            syncer(self)


# ---------------------------------------------------------------------------
# sys.modules installation
# ---------------------------------------------------------------------------


def _module_available(root: str) -> bool:
    try:
        return importlib.util.find_spec(root) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _register(name: str, module: types.ModuleType, *, only_if_missing: bool = True) -> None:
    if only_if_missing and name in sys.modules:
        return
    sys.modules[name] = module


def install(*, force: bool = False) -> dict[str, bool]:
    """Register VNpy stand-in modules when the real framework is absent.

    Returns a mapping of registered module name → whether we installed
    a shim (``False`` means the real module was already importable and
    was left untouched, or the module was already registered).
    """

    installed: dict[str, bool] = {}
    only_if_missing = not force

    real_vnpy = _module_available("vnpy")
    real_cta = _module_available("vnpy_ctastrategy")
    if real_vnpy and real_cta and not force:
        return installed

    # vnpy + vnpy.trader packages (namespace parents must exist for the
    # attribute-style imports below to resolve).
    for name in ("vnpy", "vnpy.trader"):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__path__ = []  # mark as package
            _register(name, mod, only_if_missing=only_if_missing)
            installed[name] = True

    constant = types.ModuleType("vnpy.trader.constant")
    constant.Direction = Direction
    constant.Offset = Offset
    constant.Status = Status
    constant.Interval = Interval
    constant.Exchange = Exchange
    _register("vnpy.trader.constant", constant, only_if_missing=only_if_missing)
    installed["vnpy.trader.constant"] = True

    objects = types.ModuleType("vnpy.trader.object")
    objects.BarData = BarData
    objects.TickData = TickData
    objects.TradeData = TradeData
    objects.OrderData = OrderData
    objects.StopOrder = StopOrder
    objects.Direction = Direction
    objects.Offset = Offset
    objects.Status = Status
    objects.Interval = Interval
    objects.Exchange = Exchange
    _register("vnpy.trader.object", objects, only_if_missing=only_if_missing)
    installed["vnpy.trader.object"] = True

    utility = types.ModuleType("vnpy.trader.utility")
    utility.BarGenerator = BarGenerator
    utility.ArrayManager = ArrayManager
    utility.extract_vt_symbol = _extract_vt_symbol
    _register("vnpy.trader.utility", utility, only_if_missing=only_if_missing)
    installed["vnpy.trader.utility"] = True

    template = types.ModuleType("vnpy_ctastrategy.template")
    template.CtaTemplate = CtaTemplate
    template.StopOrder = StopOrder
    template.BarData = BarData
    template.TickData = TickData
    template.TradeData = TradeData
    template.OrderData = OrderData
    template.Direction = Direction
    template.Offset = Offset
    template.Status = Status
    template.Interval = Interval

    cta = types.ModuleType("vnpy_ctastrategy")
    cta.__path__ = []
    cta.CtaTemplate = CtaTemplate
    cta.template = template
    cta.StopOrder = StopOrder
    cta.BarData = BarData
    cta.TickData = TickData
    cta.TradeData = TradeData
    cta.OrderData = OrderData
    cta.Direction = Direction
    cta.Offset = Offset
    cta.Status = Status
    cta.Interval = Interval
    cta.Exchange = Exchange
    cta.EngineType = EngineType
    cta.STOP_ORDER_PREFIX = STOP_ORDER_PREFIX
    _register("vnpy_ctastrategy.template", template, only_if_missing=only_if_missing)
    _register("vnpy_ctastrategy", cta, only_if_missing=only_if_missing)
    installed["vnpy_ctastrategy"] = True
    installed["vnpy_ctastrategy.template"] = True

    # Attribute wiring so `import vnpy; vnpy.trader.utility...` resolves.
    sys.modules["vnpy"].trader = sys.modules.get("vnpy.trader")  # type: ignore[union-attr]
    sys.modules["vnpy.trader"].constant = constant  # type: ignore[union-attr]
    sys.modules["vnpy.trader"].object = objects  # type: ignore[union-attr]
    sys.modules["vnpy.trader"].utility = utility  # type: ignore[union-attr]

    return installed

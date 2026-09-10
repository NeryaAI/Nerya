"""Freqtrade stand-ins for running Freqtrade strategies on Nerya.

Freqtrade strategy code imports::

    from freqtrade.strategy import IStrategy, IntParameter
    from freqtrade.persistence import Trade

Installing the full Freqtrade engine to execute a strategy's *logic*
on Nerya is unnecessary — the strategy only sees the objects we hand
it. This module registers dependency-free stand-ins under the real
module names:

* :mod:`freqtrade` / :mod:`freqtrade.strategy` — :class:`IStrategy`,
  hyperopt-style ``IntParameter`` / ``DecimalParameter`` /
  ``BooleanParameter`` / ``CategoricalParameter``, and the
  ``informative`` decorator (accepted, marking-only).
* :mod:`freqtrade.strategy.interface` — legacy ``IStrategy`` import
  path.
* :mod:`freqtrade.persistence` — a duck-typed :class:`Trade` object
  the adapter constructs for ``custom_exit`` / ``custom_stoploss`` /
  ``confirm_trade_entry`` hooks (``open_rate``, ``amount``,
  ``stake_amount``, ``is_short``, ``calc_profit_ratio``, …).
* :mod:`freqtrade.exchange` — ``timeframe_to_minutes`` and friends
  (pure helpers strategies sometimes call).

``install()`` only registers a module when the real Freqtrade package
is not importable, and is idempotent. It deliberately does *not* shim
TA-Lib (``talib``) — strategies that need it require the real wheel,
and the resulting ImportError is surfaced as a validation error.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd


# ---------------------------------------------------------------------------
# freqtrade.strategy — hyperopt-style parameters
# ---------------------------------------------------------------------------


class BaseParameter:
    """Minimal hyperopt parameter: strategies read ``self.<name>.value``."""

    default: Any = None

    def __init__(self, default: Any = None, **kwargs: Any) -> None:
        self.default = default
        self.value = default
        self.low = kwargs.get("low", kwargs.get("low_", None))
        self.high = kwargs.get("high", kwargs.get("high_", None))
        self.optimize = bool(kwargs.get("optimize", True))

    def __call__(self) -> Any:  # some strategies call the parameter
        return self.value

    def __float__(self) -> float:
        return float(self.value)

    def __int__(self) -> int:
        return int(self.value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value={self.value!r})"


class IntParameter(BaseParameter):
    def __init__(self, low: int = 0, high: int = 100, *, default: int | None = None, **kw: Any) -> None:
        super().__init__(default if default is not None else low, low=low, high=high, **kw)


class DecimalParameter(BaseParameter):
    def __init__(
        self,
        low: float = 0.0,
        high: float = 1.0,
        *,
        default: float | None = None,
        decimals: int = 3,
        **kw: Any,
    ) -> None:
        super().__init__(default if default is not None else low, low=low, high=high, **kw)
        self.decimals = decimals


class RealParameter(DecimalParameter):
    pass


class BooleanParameter(BaseParameter):
    def __init__(self, *, default: bool = False, **kw: Any) -> None:
        super().__init__(default, **kw)


class CategoricalParameter(BaseParameter):
    def __init__(self, categories: list[Any] | None = None, *, default: Any = None, **kw: Any) -> None:
        self.categories = list(categories or [])
        super().__init__(default if default is not None else (self.categories[0] if self.categories else None), **kw)


def informative(
    timeframe: str,
    pair: str | None = None,
    *,
    fmt_column: Callable[[str], str] | None = None,
    candle_type: str = "",
    ffill: bool = True,
) -> Callable[[Any], Any]:
    """Marker decorator — informative merge is engine-side on Freqtrade.

    Nerya compat packages run single-market, so decorated methods are
    left untouched; the importer records a warning when it sees them.
    """

    def decorator(func: Any) -> Any:
        setattr(func, "_ft_informative", {"timeframe": timeframe, "pair": pair})
        return func

    return decorator


def stoploss_from_open(
    open_relative_stop: float,
    current_profit: float,
    is_short: bool = False,
    leverage: float = 1.0,
) -> float:
    """Stoploss relative to current_rate for a target vs-open stop.

    Same contract as ``freqtrade.strategy.stoploss_from_open``: given
    the desired stop as a ratio of open price and the trade's current
    profit, return the value to return from ``custom_stoploss`` (a
    negative-or-zero stoploss relative to the current rate). Returns 0
    when the requested stop is already at/beyond the current price.
    """

    _open_relative_stop = open_relative_stop / leverage
    _current_profit = current_profit / leverage

    if (_current_profit == -1 and not is_short) or (_open_relative_stop == -1 and is_short):
        return 1

    if is_short is True:
        stoploss = -1 + ((1 - _open_relative_stop) / (1 - _current_profit))
    else:
        stoploss = 1 - ((1 + _open_relative_stop) / (1 + _current_profit))

    # Negative results mean the requested stop price is beyond the
    # current rate — the profit is already locked, so exit.
    return max(stoploss * leverage, 0.0)


def stoploss_from_absolute(
    stop_rate: float,
    current_rate: float,
    is_short: bool = False,
    leverage: float = 1.0,
) -> float:
    """Stoploss relative to current_rate for an absolute stop price.

    Same contract as ``freqtrade.strategy.stoploss_from_absolute``.
    """

    if not current_rate:
        return 1
    stoploss = 1 - (stop_rate / current_rate)
    if is_short:
        stoploss = -stoploss
    return max(stoploss, 0.0) * leverage


# ---------------------------------------------------------------------------
# freqtrade.persistence — Trade shim
# ---------------------------------------------------------------------------


class Trade:
    """Duck-typed stand-in for ``freqtrade.persistence.Trade``.

    Carries the attributes hooks commonly read. Constructed by the
    adapter from the tracked Nerya-side position state.
    """

    def __init__(
        self,
        *,
        pair: str = "",
        open_rate: float = 0.0,
        amount: float = 0.0,
        stake_amount: float = 0.0,
        is_short: bool = False,
        open_date: datetime | None = None,
        leverage: float = 1.0,
        stoploss: float = 0.0,
        id: int = 1,
    ) -> None:
        self.pair = pair
        self.open_rate = float(open_rate)
        self.amount = float(amount)
        self.stake_amount = float(stake_amount)
        self.is_short = bool(is_short)
        self.open_date = open_date or datetime.now(timezone.utc)
        self.open_date_utc = self.open_date
        self.leverage = float(leverage or 1.0)
        self.stop_loss = float(stoploss)
        self.initial_stop_loss = float(stoploss)
        self.id = id

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (
            f"Trade(pair={self.pair!r}, open_rate={self.open_rate}, "
            f"amount={self.amount}, is_short={self.is_short})"
        )

    def calc_profit_ratio(self, rate: float, *, fee: float = 0.0) -> float:
        if not self.open_rate:
            return 0.0
        if self.is_short:
            ratio = (self.open_rate - rate) / self.open_rate
        else:
            ratio = (rate - self.open_rate) / self.open_rate
        return ratio * self.leverage - 2 * float(fee or 0.0)

    def calc_profit(self, rate: float, *, fee: float = 0.0) -> float:
        return self.calc_profit_ratio(rate, fee=fee) * self.stake_amount

    @property
    def nr_of_successful_exits(self) -> int:
        return 0


# ---------------------------------------------------------------------------
# freqtrade.strategy — IStrategy
# ---------------------------------------------------------------------------


class IStrategy:
    """Minimal ``IStrategy`` base.

    Strategy subclasses override the three ``populate_*`` dataframe
    methods and any hooks they need. Defaults mirror Freqtrade's
    permissive base so a subclass only declares what it cares about.
    """

    # Engine-facing class attributes (strategies override freely).
    INTERFACE_VERSION: int = 3
    timeframe: str = "5m"
    can_short: bool = False
    stoploss: float = -0.99
    minimal_roi: dict[str, float] = {}
    trailing_stop: bool = False
    trailing_stop_positive: float | None = None
    trailing_stop_positive_offset: float | None = None
    trailing_only_offset_is_reached: bool = False
    process_only_new_candles: bool = True
    startup_candle_count: int = 30
    position_adjustment_enable: bool = False
    max_entry_position_adjustment: int = -1
    order_types: dict[str, Any] = {}
    order_time_in_force: dict[str, Any] = {}
    plot_config: dict[str, Any] = {}
    informational_pairs: list[tuple[str, str]] = []

    # Filled in by the adapter at host time.
    config: dict[str, Any] = {}
    dp: Any = None  # DataProvider — unsupported on Nerya; raises if used
    wallets: Any = None

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        if config is not None:
            self.config = dict(config)

    # -- informative pairs -------------------------------------------------

    def informative_pairs(self) -> list[tuple[str, str]]:
        return []

    # -- dataframe pipeline (the three methods strategies must define) ----

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    # Legacy (pre-2023) method names — supported transparently.
    def populate_buy_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    def populate_sell_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    # -- hooks (opt-in) ------------------------------------------------------

    def bot_loop_start(self, current_time: datetime, **kwargs: Any) -> None:
        return None

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool = False,
        **kwargs: Any,
    ) -> float | None:
        return None

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs: Any,
    ) -> str | None:
        return None

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        return True

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Trade,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        exit_reason: str,
        **kwargs: Any,
    ) -> bool:
        return True

    def custom_entry_price(self, pair: str, trade: Trade, current_time: datetime, proposed_rate: float, **kwargs: Any) -> float:
        return proposed_rate

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        return 1.0

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: float | None,
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs: Any,
    ) -> float | None:
        return None

    # -- versioning helpers (some strategies call these) -------------------

    @staticmethod
    def version() -> str | None:
        return None

    def get_strategy_name(self) -> str:
        return type(self).__name__


# ---------------------------------------------------------------------------
# freqtrade.exchange — pure helpers
# ---------------------------------------------------------------------------


_TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "1d": 1440, "3d": 4320, "1w": 10080, "1M": 43200,
}


def timeframe_to_minutes(timeframe: str) -> int:
    return _TIMEFRAME_MINUTES.get(str(timeframe), 5)


def timeframe_to_seconds(timeframe: str) -> int:
    return timeframe_to_minutes(timeframe) * 60


def timeframe_to_prev_date(timeframe: str, date: datetime | None = None) -> datetime:
    tf_sec = timeframe_to_seconds(timeframe)
    dt = date or datetime.now(timezone.utc)
    ts = int(dt.timestamp()) // tf_sec * tf_sec
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def timeframe_to_next_date(timeframe: str, date: datetime | None = None) -> datetime:
    return timeframe_to_prev_date(timeframe, date) + timedelta(seconds=timeframe_to_seconds(timeframe))


# ---------------------------------------------------------------------------
# sys.modules installation
# ---------------------------------------------------------------------------


def _module_available(root: str) -> bool:
    try:
        return importlib.util.find_spec(root) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _register(name: str, module: types.ModuleType) -> bool:
    if name in sys.modules:
        return False
    sys.modules[name] = module
    return True


def install(*, force: bool = False) -> dict[str, bool]:
    """Register Freqtrade stand-ins when the real framework is absent."""

    installed: dict[str, bool] = {}
    if _module_available("freqtrade") and not force:
        return installed

    for pkg in ("freqtrade", "freqtrade.persistence", "freqtrade.exchange"):
        mod = types.ModuleType(pkg)
        if pkg == "freqtrade":
            mod.__path__ = []
        if _register(pkg, mod):
            installed[pkg] = True

    strategy_mod = types.ModuleType("freqtrade.strategy")
    strategy_mod.IStrategy = IStrategy
    strategy_mod.InformativePair = tuple
    strategy_mod.informative = informative
    strategy_mod.IntParameter = IntParameter
    strategy_mod.DecimalParameter = DecimalParameter
    strategy_mod.RealParameter = RealParameter
    strategy_mod.BooleanParameter = BooleanParameter
    strategy_mod.CategoricalParameter = CategoricalParameter
    strategy_mod.timeframe_to_minutes = timeframe_to_minutes
    strategy_mod.timeframe_to_seconds = timeframe_to_seconds
    strategy_mod.merge_informative_pair = _merge_informative_pair_noop
    strategy_mod.stoploss_from_open = stoploss_from_open
    strategy_mod.stoploss_from_absolute = stoploss_from_absolute
    if _register("freqtrade.strategy", strategy_mod):
        installed["freqtrade.strategy"] = True

    interface = types.ModuleType("freqtrade.strategy.interface")
    interface.IStrategy = IStrategy
    if _register("freqtrade.strategy.interface", interface):
        installed["freqtrade.strategy.interface"] = True

    persistence = sys.modules.get("freqtrade.persistence")
    if persistence is not None:
        persistence.Trade = Trade
        installed["freqtrade.persistence"] = True

    exchange = sys.modules.get("freqtrade.exchange")
    if exchange is not None:
        exchange.timeframe_to_minutes = timeframe_to_minutes
        exchange.timeframe_to_seconds = timeframe_to_seconds
        exchange.timeframe_to_prev_date = timeframe_to_prev_date
        exchange.timeframe_to_next_date = timeframe_to_next_date
        installed["freqtrade.exchange"] = True

    freqtrade = sys.modules.get("freqtrade")
    if freqtrade is not None:
        freqtrade.strategy = strategy_mod
        freqtrade.persistence = persistence
        freqtrade.exchange = exchange

    return installed


def _merge_informative_pair_noop(*args: Any, **kwargs: Any) -> pd.DataFrame:
    """``merge_informative_pair`` without a data provider.

    Strategies calling this in ``populate_indicators`` cannot run under
    a single-market adapter; we raise a clear error instead of
    silently returning wrong data.
    """

    raise NotImplementedError(
        "merge_informative_pair requires informative data feeds, which the "
        "Nerya single-market compat runtime does not provide"
    )


__all__ = [
    "BooleanParameter",
    "CategoricalParameter",
    "DecimalParameter",
    "IStrategy",
    "IntParameter",
    "RealParameter",
    "Trade",
    "informative",
    "install",
    "stoploss_from_absolute",
    "stoploss_from_open",
    "timeframe_to_minutes",
    "timeframe_to_seconds",
]

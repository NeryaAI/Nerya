"""qtpylib stand-in for Freqtrade strategies.

Freqtrade strategies commonly start with::

    import qtpylib.indicators as qtpylib
    import talib.abstract as ta

The vendor copy Freqtrade ships (``freqtrade.vendor.qtpylib``) is a
small pandas helper library, so we reimplement the commonly-used
surface with pandas. Registered as both ``qtpylib`` and
``qtpylib.indicators`` when the real package is absent. TA-Lib itself
is *not* shimmed — a strategy that needs ``talib`` requires the real
library and fails validation with a clear ImportError otherwise.
"""

from __future__ import annotations

import importlib.util
import sys
import types

import pandas as pd


def crossed(series1: pd.Series, series2: pd.Series | float, direction: str = "above") -> bool:
    """Whether ``series1`` crossed ``series2`` on the last bar."""

    s1 = pd.Series(series1).astype(float)
    if isinstance(series2, (int, float)):
        s2 = pd.Series(float(series2), index=s1.index)
    else:
        s2 = pd.Series(series2).astype(float)
    if len(s1) < 2:
        return False
    prev1, last1 = float(s1.iloc[-2]), float(s1.iloc[-1])
    prev2, last2 = float(s2.iloc[-2]), float(s2.iloc[-1])
    if direction == "above":
        return bool(prev1 <= prev2 and last1 > last2)
    if direction == "below":
        return bool(prev1 >= prev2 and last1 < last2)
    return bool(prev1 == prev2 and last1 == last2)


def crossed_above(series1: pd.Series, series2: pd.Series | float) -> bool:
    return crossed(series1, series2, "above")


def crossed_below(series1: pd.Series, series2: pd.Series | float) -> bool:
    return crossed(series1, series2, "below")


def rolling_std(series: pd.Series, window: int = 3) -> pd.Series:
    return series.rolling(window=window).std()


def rolling_mean(series: pd.Series, window: int = 3) -> pd.Series:
    return series.rolling(window=window).mean()


def bollinger_bands(
    series: pd.Series,
    window: int = 20,
    stds: float = 2,
) -> dict[str, pd.Series]:
    mean = series.rolling(window=window).mean()
    std = series.rolling(window=window).std(ddof=0)
    return {
        "mid": mean,
        "upper": mean + std * float(stds),
        "lower": mean - std * float(stds),
        "bandwidth": (mean + std * float(stds) - (mean - std * float(stds))) / mean,
        "percent": (series - (mean - std * float(stds)))
        / ((mean + std * float(stds)) - (mean - std * float(stds))),
    }


def weighted_average_price(
    dataframe: pd.DataFrame,
    window: int = 20,
    fresh_volume: pd.Series | None = None,
) -> pd.Series:
    df = dataframe
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = fresh_volume if fresh_volume is not None else df.get("volume", pd.Series(1.0, index=tp.index))
    return (tp * vol).rolling(window=window).sum() / vol.rolling(window=window).sum()


vwap = weighted_average_price
rolling_vwap = weighted_average_price


def typical_price(dataframe: pd.DataFrame) -> pd.Series:
    return (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3.0


def true_range(dataframe: pd.DataFrame) -> pd.Series:
    df = dataframe
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1)


def average_directional_movement_index(
    dataframe: pd.DataFrame,
    n: int = 14,
    n_ADX: int = 14,
) -> dict[str, pd.Series]:
    df = dataframe
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np_where(up, down), index=df.index)
    minus_dm = pd.Series(np_where(down, up), index=df.index)
    tr = true_range(df)
    tr_smooth = tr.rolling(window=n).sum()
    plus_di = 100 * plus_dm.rolling(window=n).sum() / tr_smooth
    minus_di = 100 * minus_dm.rolling(window=n).sum() / tr_smooth
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di) * 100
    adx = dx.rolling(window=n_ADX).mean()
    return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}


def np_where(up: pd.Series, down: pd.Series) -> pd.Series:
    import numpy as np

    return pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=up.index)


def zscore(series: pd.Series, window: int = 20) -> pd.Series:
    mean = series.rolling(window=window).mean()
    std = series.rolling(window=window).std(ddof=0)
    return (series - mean) / std


def pct_change(series: pd.Series, periods: int = 1) -> pd.Series:
    return series.pct_change(periods=periods)


def log_return(series: pd.Series, periods: int = 1) -> pd.Series:
    import numpy as np

    return pd.Series(np.log(series)).pct_change(periods=periods)


def install(*, force: bool = False) -> dict[str, bool]:
    """Register the qtpylib stand-ins when the real package is absent."""

    installed: dict[str, bool] = {}
    try:
        available = importlib.util.find_spec("qtpylib") is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        available = False
    if available and not force:
        return installed

    root = types.ModuleType("qtpylib")
    indicators = types.ModuleType("qtpylib.indicators")
    public = {
        name: obj
        for name, obj in globals().items()
        if callable(obj) and not name.startswith("_") and name != "install"
    }
    for name, obj in public.items():
        setattr(root, name, obj)
        setattr(indicators, name, obj)
    root.indicators = indicators
    if "qtpylib" not in sys.modules:
        sys.modules["qtpylib"] = root
        installed["qtpylib"] = True
    if "qtpylib.indicators" not in sys.modules:
        sys.modules["qtpylib.indicators"] = indicators
        installed["qtpylib.indicators"] = True
    return installed

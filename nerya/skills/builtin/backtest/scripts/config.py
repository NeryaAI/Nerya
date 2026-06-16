"""Configuration schema and loader for the backtest skill."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .....core import yaml_io


class BacktestConfigError(ValueError):
    """Raised when backtest configuration is invalid."""


@dataclass
class MockSurfaceCfg:
    mode: str = "error"
    payload: Any = None

    @classmethod
    def from_raw(cls, raw: Any) -> "MockSurfaceCfg":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise BacktestConfigError("mock surface config must be a mapping")
        mode = str(raw.get("mode") or "error").strip().lower()
        if mode not in {"error", "stub", "replay"}:
            raise BacktestConfigError(f"invalid mock surface mode: {mode}")
        return cls(mode=mode, payload=raw.get("payload"))


@dataclass
class StakeAmountCfg:
    mode: str = "unlimited"
    fixed_usd: float | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "StakeAmountCfg":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise BacktestConfigError("stake_amount must be a mapping")
        mode = str(raw.get("mode") or "unlimited").strip().lower()
        if mode not in {"unlimited", "fixed"}:
            raise BacktestConfigError("stake_amount.mode must be unlimited or fixed")
        fixed = raw.get("fixed_usd")
        fixed_usd = None if fixed in (None, "") else float(fixed)
        if mode == "fixed" and (fixed_usd is None or fixed_usd <= 0):
            raise BacktestConfigError("stake_amount.fixed_usd must be positive when mode=fixed")
        return cls(mode=mode, fixed_usd=fixed_usd)


@dataclass
class ThresholdCfg:
    drawdown_episode_min_pct: float = 3.0
    missed_profit_min_move_pct: float = 5.0
    missed_profit_max_noise_pct: float = 2.0

    @classmethod
    def from_raw(cls, raw: Any) -> "ThresholdCfg":
        raw = raw or {}
        if not isinstance(raw, dict):
            raise BacktestConfigError("thresholds must be a mapping")
        return cls(
            drawdown_episode_min_pct=float(raw.get("drawdown_episode_min_pct", 3.0)),
            missed_profit_min_move_pct=float(raw.get("missed_profit_min_move_pct", 5.0)),
            missed_profit_max_noise_pct=float(raw.get("missed_profit_max_noise_pct", 2.0)),
        )


@dataclass
class BacktestConfig:
    initial_capital_usd: float = 10000.0
    warmup_bars: int = 50
    min_backtest_days: int = 0
    window_days: int = 180
    short_lived_window_days: int = 7
    tf: str = "1h"
    timeframes: list[str] = field(default_factory=list)
    markets: list[str] = field(default_factory=list)
    indicators: dict[str, list[int]] = field(default_factory=lambda: {
        "sma": [20, 50],
        "ema": [12, 26],
        "rsi": [14],
        "atr": [14],
    })
    fee_bps_by_venue: dict[str, float] = field(default_factory=lambda: {
        "BINANCE": 2.0,
        "COINBASE": 5.0,
        "YAHOO": 5.0,
        "ALPACA": 1.0,
        "DEX": 30.0,
    })
    slip_bps_by_venue: dict[str, float] = field(default_factory=lambda: {
        "BINANCE": 1.0,
        "COINBASE": 2.0,
        "YAHOO": 2.0,
        "ALPACA": 1.0,
        "DEX": 10.0,
    })
    fill_mode: str = "entry_current_open__exit_next_open"
    # Default to permitting shorts so a long/short strategy backtests faithfully
    # (its own open_position(side="short") intents fill). Set false to force
    # long-only (e.g. spot-only) simulations.
    allow_short: bool = True
    max_open_trades: int = 1
    kill_switch: bool = False
    max_slippage_bps: float | None = None
    max_drawdown_pct: float = 30.0
    stake_amount: StakeAmountCfg = field(default_factory=StakeAmountCfg)
    mock_surfaces: dict[str, MockSurfaceCfg] = field(default_factory=lambda: {
        "news": MockSurfaceCfg(),
        "llm": MockSurfaceCfg(),
        "onchain": MockSurfaceCfg(),
        "subagents": MockSurfaceCfg(),
        "team": MockSurfaceCfg(),
        "messages": MockSurfaceCfg(),
    })
    thresholds: ThresholdCfg = field(default_factory=ThresholdCfg)
    risk_free_daily: float = 0.0
    benchmark_mode: str = "buy_hold_equal_weight"
    cache_root: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "BacktestConfig":
        raw = dict(raw or {})
        mock_raw = raw.get("mock_surfaces") or {}
        if not isinstance(mock_raw, dict):
            raise BacktestConfigError("mock_surfaces must be a mapping")
        cfg = cls(
            initial_capital_usd=float(raw.get("initial_capital_usd", 10000.0)),
            warmup_bars=int(raw.get("warmup_bars", 50)),
            min_backtest_days=int(raw.get("min_backtest_days", 0)),
            window_days=int(raw.get("window_days", 180)),
            short_lived_window_days=int(raw.get("short_lived_window_days", 7)),
            tf=str(raw.get("tf", "1h")),
            timeframes=_str_list(raw.get("timeframes")),
            markets=[str(m) for m in (raw.get("markets") or [])],
            indicators=_int_list_map(raw.get("indicators")),
            fee_bps_by_venue=_float_map(raw.get("fee_bps_by_venue"), cls().fee_bps_by_venue),
            slip_bps_by_venue=_float_map(raw.get("slip_bps_by_venue"), cls().slip_bps_by_venue),
            fill_mode=str(raw.get("fill_mode", "entry_current_open__exit_next_open")),
            allow_short=bool(raw.get("allow_short", True)),
            max_open_trades=int(raw.get("max_open_trades", 1)),
            kill_switch=bool(raw.get("kill_switch", False)),
            max_slippage_bps=(
                None if raw.get("max_slippage_bps") in (None, "") else float(raw.get("max_slippage_bps"))
            ),
            max_drawdown_pct=float(raw.get("max_drawdown_pct", 30.0)),
            stake_amount=StakeAmountCfg.from_raw(raw.get("stake_amount")),
            mock_surfaces={
                name: MockSurfaceCfg.from_raw(mock_raw.get(name))
                for name in {"news", "llm", "onchain", "subagents", "team", "messages"}
            },
            thresholds=ThresholdCfg.from_raw(raw.get("thresholds")),
            risk_free_daily=float(raw.get("risk_free_daily", 0.0)),
            benchmark_mode=str(raw.get("benchmark_mode", "buy_hold_equal_weight")),
            cache_root=(str(raw.get("cache_root")) if raw.get("cache_root") else None),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.initial_capital_usd <= 0:
            raise BacktestConfigError("initial_capital_usd must be positive")
        if self.min_backtest_days < 0:
            raise BacktestConfigError("min_backtest_days must be >= 0")
        if self.window_days <= 0:
            raise BacktestConfigError("window_days must be positive")
        if self.short_lived_window_days <= 0:
            raise BacktestConfigError("short_lived_window_days must be positive")
        if self.warmup_bars < 0:
            raise BacktestConfigError("warmup_bars must be >= 0")
        if self.max_open_trades <= 0:
            raise BacktestConfigError("max_open_trades must be positive")
        if self.fill_mode != "entry_current_open__exit_next_open":
            raise BacktestConfigError("unsupported fill_mode")
        if not self.timeframes:
            self.timeframes = [self.tf]
        else:
            self.timeframes = _unique([self.tf, *self.timeframes])

    def asdict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mock_surfaces"] = {
            key: asdict(value) for key, value in self.mock_surfaces.items()
        }
        data["stake_amount"] = asdict(self.stake_amount)
        data["thresholds"] = asdict(self.thresholds)
        return data


def preset_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "references" / f"config.{name}.yml"


def load_config(
    *,
    preset: str = "default",
    markets: list[str] | tuple[str, ...] | None = None,
    config_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> BacktestConfig:
    raw: dict[str, Any] = {}
    if preset:
        path = preset_path(preset)
        if path.exists():
            loaded = yaml_io.load(path, default={})
            if not isinstance(loaded, dict):
                raise BacktestConfigError(f"{path}: preset must be a mapping")
            raw = _deep_merge(raw, loaded)
    if config_path:
        loaded = yaml_io.load(Path(config_path), default={})
        if not isinstance(loaded, dict):
            raise BacktestConfigError(f"{config_path}: config must be a mapping")
        raw = _deep_merge(raw, loaded)
    if overrides:
        raw = _deep_merge(raw, dict(overrides))
    if markets:
        raw["markets"] = list(markets)
    return BacktestConfig.from_raw(raw)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _int_list_map(raw: Any) -> dict[str, list[int]]:
    defaults = BacktestConfig().indicators
    if raw is None:
        return defaults
    if not isinstance(raw, dict):
        raise BacktestConfigError("indicators must be a mapping")
    out: dict[str, list[int]] = {}
    for key, value in raw.items():
        if not isinstance(value, (list, tuple)):
            raise BacktestConfigError(f"indicators.{key} must be a list")
        out[str(key)] = [int(v) for v in value]
    return out


def _str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, (list, tuple)):
        raise BacktestConfigError("timeframes must be a string or list")
    return [str(v) for v in raw if str(v).strip()]


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = str(value).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _float_map(raw: Any, defaults: dict[str, float]) -> dict[str, float]:
    out = dict(defaults)
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise BacktestConfigError("bps maps must be mappings")
    for key, value in raw.items():
        out[str(key).upper()] = float(value)
    return out

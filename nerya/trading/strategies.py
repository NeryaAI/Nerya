"""Strategy registry — reads workspace/strategies/*/strategy.yml + limits.yml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import yaml_io
from ..core.errors import TradingError
from ..core.paths import WorkspacePaths
from .strategy_lifecycle import STATES as _STATES, is_tradable as _is_tradable


@dataclass
class StrategyLimits:
    allowed_markets: list[str] = field(default_factory=list)
    max_single_order_usd: float = 0.0
    max_total_exposure_usd: float = 0.0
    daily_loss_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    min_confidence: float = 0.5
    max_slippage_bps: int = 50
    max_stale_seconds: int = 30
    approval_threshold_usd: float = 0.0
    kill_switch: bool = False


@dataclass
class Strategy:
    id: str
    title: str
    status: str  # one of nerya.trading.strategy_lifecycle.STATES
    account_id: str
    markets: list[str]
    paper_trading_enabled: bool
    live_trading_enabled: bool
    trigger_kinds: list[str]
    subagents: list[str]
    path: Path
    limits: StrategyLimits

    @property
    def is_tradable(self) -> bool:
        return _is_tradable(self.status)


def load_strategy(paths: WorkspacePaths, strategy_id: str) -> Strategy:
    """Load a strategy descriptor from ``strategies/<id>/``.

    Tolerates both the legacy manifest schema (top-level ``id`` /
    ``account_id`` / ``status`` / ``markets``) and the agent-generated
    package schema (``strategy_id`` / ``mode`` / ``accounts: [...]``)
    so a single registry surface works for both. Anything missing
    defaults to a tradable paper-mode shape that won't blow up the
    dashboard listing.
    """

    root = paths.strategy(strategy_id)
    if not (root / "strategy.yml").exists():
        raise TradingError(f"unknown strategy: {strategy_id}")
    s = yaml_io.load(root / "strategy.yml") or {}
    limits_raw = yaml_io.load(root / "limits.yml", default={}) or {}
    limits = StrategyLimits(
        allowed_markets=list(limits_raw.get("allowed_markets") or []),
        max_single_order_usd=float(limits_raw.get("max_single_order_usd", 0)),
        max_total_exposure_usd=float(limits_raw.get("max_total_exposure_usd", 0)),
        daily_loss_usd=float(limits_raw.get("daily_loss_usd", 0)),
        max_drawdown_pct=float(limits_raw.get("max_drawdown_pct", 0)),
        min_confidence=float(limits_raw.get("min_confidence", 0.5)),
        max_slippage_bps=int(limits_raw.get("max_slippage_bps", 50)),
        max_stale_seconds=int(limits_raw.get("max_stale_seconds", 30)),
        approval_threshold_usd=float(limits_raw.get("approval_threshold_usd", 0)),
        kill_switch=bool(limits_raw.get("kill_switch", False)),
    )

    sid = str(s.get("id") or s.get("strategy_id") or strategy_id)
    title = str(s.get("title") or sid)
    # ``status`` (legacy lifecycle) vs ``mode`` (new package schema:
    # ``paper`` / ``live``). Map ``mode`` onto a lifecycle bucket so
    # downstream gates still see a recognisable value.
    status = str(
        s.get("status")
        or {"paper": "paper", "live": "live"}.get(str(s.get("mode") or "").lower(), "paper")
    )
    account_id = str(
        s.get("account_id")
        or (s.get("accounts") or [None])[0]
        or "paper_main"
    )
    markets = list(s.get("markets") or [])
    paper_enabled = bool(
        s.get("paper_trading_enabled", True)
        if "paper_trading_enabled" in s
        else (str(s.get("mode") or "paper").lower() == "paper")
    )
    live_enabled = bool(
        s.get("live_trading_enabled", False)
        if "live_trading_enabled" in s
        else (str(s.get("mode") or "").lower() == "live")
    )
    # Newer manifests carry an explicit ``trigger_kinds`` list; older
    # agent-generated packages only set ``schedule.*``. Infer the kind
    # from the schedule shape so the dashboard badge stays informative
    # for legacy packages without forcing a regeneration cycle.
    raw_kinds = list(s.get("trigger_kinds") or [])
    if not raw_kinds:
        sched = s.get("schedule") or {}
        if isinstance(sched, dict) and (
            sched.get("cron") or sched.get("every_seconds")
        ):
            raw_kinds = ["schedule"]
        if s.get("news_sources"):
            raw_kinds.append("event")
    return Strategy(
        id=sid, title=title,
        status=status,
        account_id=account_id,
        markets=markets,
        paper_trading_enabled=paper_enabled,
        live_trading_enabled=live_enabled,
        trigger_kinds=raw_kinds,
        subagents=list(s.get("subagents") or []),
        path=root, limits=limits,
    )


def list_strategies(paths: WorkspacePaths) -> list[Strategy]:
    """Walk ``strategies/`` and return every readable :class:`Strategy`.

    Broken packages (missing manifest, malformed YAML, schema mismatch)
    are skipped rather than fatally aborting the listing — the dashboard
    and the model both depend on this never raising. Operators see a
    repair hint via ``strategy_validate`` instead.
    """

    out = []
    if not paths.strategies.exists():
        return out
    for d in sorted(p for p in paths.strategies.iterdir() if p.is_dir()):
        if not (d / "strategy.yml").exists():
            continue
        try:
            out.append(load_strategy(paths, d.name))
        except Exception:
            continue
    return out

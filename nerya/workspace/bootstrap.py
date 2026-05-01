"""Seed example strategies on `nerya init`."""

from __future__ import annotations

from pathlib import Path

from ..core import yaml_io
from ..core.paths import WorkspacePaths


def seed_example_strategies(paths: WorkspacePaths) -> None:
    _seed_btc_momentum(paths)
    _seed_btc_grid_script(paths)
    _seed_manual_agent(paths)


def _seed_manual_agent(paths: WorkspacePaths) -> None:
    """Seed the ``manual_agent`` bucket used by ad-hoc agent-initiated trades.

    ``trading_skill.submit_trade_intent`` falls back to ``strategy_id =
    "manual_agent"`` whenever the agent (or an operator) submits a trade
    that is not wired to a formal strategy. Without a real strategy record
    behind that id, the risk gate rejects every such intent with
    ``strategy_unknown`` and the paper-trade path never exercises.

    We therefore seed a conservative, paper-only, ``manual_agent`` strategy
    at workspace bootstrap. The limits are intentionally tight so
    operator-issued direct trades cannot accidentally route huge notional
    or touch live markets.
    """
    sid = "manual_agent"
    root = paths.strategy(sid)
    root.mkdir(parents=True, exist_ok=True)
    (paths.strategy_history(sid)).mkdir(parents=True, exist_ok=True)
    (paths.strategy_sessions(sid)).mkdir(parents=True, exist_ok=True)
    (root / "prompts").mkdir(exist_ok=True)
    strategy = {
        "id": sid,
        "title": "Manual / agent-initiated paper trades",
        "status": "paper",
        "account_id": "paper_main",
        "markets": [
            "PAPER:BTCUSDT", "PAPER:ETHUSDT", "PAPER:SOLUSDT",
        ],
        "paper_trading_enabled": True,
        "live_trading_enabled": False,
        "subagents": [],
        "trigger_kinds": ["manual.intent"],
        "driver": "manual",
        "notes": (
            "Fallback strategy id used by ``submit_trade_intent`` when the "
            "caller does not pin a formal strategy. Keep paper-only."
        ),
    }
    config = {
        "min_confidence": 0.0,
        "position_size_usd": 500.0,
    }
    limits = {
        "allowed_markets": [
            "PAPER:BTCUSDT", "PAPER:ETHUSDT", "PAPER:SOLUSDT",
        ],
        "max_single_order_usd": 1000.0,
        "max_total_exposure_usd": 2500.0,
        "daily_loss_usd": 500.0,
        "max_drawdown_pct": 0.10,
        "min_confidence": 0.0,
        "max_slippage_bps": 50,
        "max_stale_seconds": 60,
        "approval_threshold_usd": 1500.0,
    }
    _write_yaml(root / "strategy.yml", strategy)
    _write_yaml(root / "config.yml", config)
    _write_yaml(root / "limits.yml", limits)
    _seed_text(
        root / "learnings.md",
        f"# Learnings — {sid}\n\n- (empty)\n",
    )
    for name in (
        "triggers", "skill_calls", "subagents", "decisions", "intents",
        "risk", "orders", "fills", "pnl", "messages", "reviews",
    ):
        p = paths.strategy_history(sid) / f"{name}.jsonl"
        if not p.exists():
            p.touch()


def _seed_btc_momentum(paths: WorkspacePaths) -> None:
    sid = "btc_momentum"
    root = paths.strategy(sid)
    root.mkdir(parents=True, exist_ok=True)
    hist = paths.strategy_history(sid)
    sess = paths.strategy_sessions(sid)
    hist.mkdir(parents=True, exist_ok=True)
    sess.mkdir(parents=True, exist_ok=True)
    (root / "prompts").mkdir(exist_ok=True)
    strategy = {
        "id": sid,
        "title": "BTC breakout momentum",
        "status": "paper",
        "account_id": "paper_main",
        "markets": ["PAPER:BTCUSDT"],
        "paper_trading_enabled": True,
        "live_trading_enabled": False,
        "subagents": ["market_analyst", "risk_critic"],
        "trigger_kinds": ["price.breakout"],
        "notes": (
            "PAPER:* markets are explicit paper-trading presets; live "
            "promotion must repoint to a real VENUE:SYMBOL."
        ),
    }
    config = {
        "min_confidence": 0.55,
        "position_size_usd": 1000.0,
        "take_profit_pct": 0.05,
        "stop_loss_pct": 0.02,
    }
    limits = {
        "allowed_markets": ["PAPER:BTCUSDT"],
        "max_single_order_usd": 2500.0,
        "max_total_exposure_usd": 10000.0,
        "daily_loss_usd": 1500.0,
        "max_drawdown_pct": 0.15,
        "min_confidence": 0.55,
        "max_slippage_bps": 30,
        "max_stale_seconds": 30,
        "approval_threshold_usd": 5000.0,
    }
    _write_yaml(root / "strategy.yml", strategy)
    _write_yaml(root / "config.yml", config)
    _write_yaml(root / "limits.yml", limits)
    _seed_text(root / "learnings.md", f"# Learnings — {sid}\n\n- (empty)\n")
    for name in (
        "triggers", "skill_calls", "subagents", "decisions", "intents",
        "risk", "orders", "fills", "pnl", "messages", "reviews",
    ):
        p = hist / f"{name}.jsonl"
        if not p.exists():
            p.touch()
    _seed_text(root / "prompts" / "main.agent.md",
               f"# {sid} main agent\n\nBTC momentum strategy. Be conservative.\n")
    _seed_text(root / "prompts" / "market_analyst.agent.md",
               f"# {sid} market analyst\n\nFocus on BTC structure and volume.\n")
    _seed_text(root / "prompts" / "risk_critic.agent.md",
               f"# {sid} risk critic\n\nChallenge every intent with a counter-scenario.\n")
    _seed_text(root / "prompts" / "execution_planner.agent.md",
               f"# {sid} execution planner\n\nSingle-leg execution for now.\n")


def _seed_btc_grid_script(paths: WorkspacePaths) -> None:
    sid = "btc_grid_script"
    root = paths.strategy(sid)
    root.mkdir(parents=True, exist_ok=True)
    (paths.strategy_history(sid)).mkdir(parents=True, exist_ok=True)
    (paths.strategy_sessions(sid)).mkdir(parents=True, exist_ok=True)
    (root / "prompts").mkdir(exist_ok=True)
    strategy = {
        "id": sid,
        "title": "BTC grid via SDK script",
        "status": "paper",
        "account_id": "paper_main",
        "markets": ["PAPER:BTCUSDT"],
        "paper_trading_enabled": True,
        "live_trading_enabled": False,
        "subagents": [],
        "trigger_kinds": ["sdk.trade_intent"],
        "driver": "script",
    }
    config = {
        "grid_size_usd": 500.0,
        "levels": 5,
    }
    limits = {
        "allowed_markets": ["PAPER:BTCUSDT"],
        "max_single_order_usd": 1000.0,
        "max_total_exposure_usd": 5000.0,
        "daily_loss_usd": 500.0,
        "max_drawdown_pct": 0.10,
        "min_confidence": 0.50,
        "max_slippage_bps": 50,
        "max_stale_seconds": 60,
        "approval_threshold_usd": 2500.0,
    }
    _write_yaml(root / "strategy.yml", strategy)
    _write_yaml(root / "config.yml", config)
    _write_yaml(root / "limits.yml", limits)
    _seed_text(root / "learnings.md", f"# Learnings — {sid}\n")
    for name in (
        "triggers", "skill_calls", "subagents", "decisions", "intents",
        "risk", "orders", "fills", "pnl", "messages", "reviews",
    ):
        p = paths.strategy_history(sid) / f"{name}.jsonl"
        if not p.exists():
            p.touch()


def _write_yaml(path: Path, data: dict) -> None:
    if not path.exists():
        yaml_io.dump(path, data)


def _seed_text(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")

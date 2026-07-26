"""Workspace lifecycle: init, load, journal accessors, state store accessors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import yaml_io
from ..core.config import Config, load_config
from ..core.paths import WorkspacePaths, resolve_workspace
from .journal import Journal
from .layout import REQUIRED_JOURNALS, required_dirs
from .prompt_bundles import DEFAULT_BUNDLE_ID, load_bundle, seed_bundle
from .state_store import StateStore


@dataclass
class WorkspaceManager:
    config: Config

    @property
    def paths(self) -> WorkspacePaths:
        return self.config.paths

    @classmethod
    def load(cls, workspace: Path | str | None = None) -> "WorkspaceManager":
        return cls(config=load_config(workspace))

    @classmethod
    def init(cls, workspace: Path | str | None = None) -> "WorkspaceManager":
        paths = resolve_workspace(workspace)
        paths.root.mkdir(parents=True, exist_ok=True)
        for d in required_dirs(paths):
            d.mkdir(parents=True, exist_ok=True)
        # default nerya.yml
        if not paths.config.exists():
            from ..core.config import DEFAULT_CONFIG
            yaml_io.dump(paths.config, DEFAULT_CONFIG)
        # seed journals so tail works
        for name in REQUIRED_JOURNALS:
            p = paths.journal(name)
            if not p.exists():
                p.touch()
        # seed config files
        _seed_yaml(paths.skills_enabled, {"version": 1, "enabled": _DEFAULT_ENABLED_SKILLS})
        _seed_yaml(paths.exchanges_file, {"version": 1, "exchanges": {}})
        _seed_yaml(paths.accounts_file, {"version": 1, "accounts": [
            {"id": "paper_main", "exchange": "mock", "mode": "paper",
             "live_trading_enabled": False, "initial_balance_usd": 100000.0}
        ]})
        _seed_yaml(paths.secrets_refs_file, {"version": 1, "refs": {}})
        _seed_yaml(paths.triggers_routes_file, {"version": 1, "routes": _DEFAULT_ROUTES})
        _seed_yaml(paths.triggers_schedules_file, {"version": 1, "schedules": []})
        _seed_yaml(paths.messages_channels, {"version": 1, "channels": {
            "dashboard": {"kind": "dashboard"},
        }})
        # seed memory
        _seed_text(paths.memory / "global.md",
                   "# Global memory\n\nAgent-wide notes go here.\n")
        _seed_text(paths.memory / "mistakes.md",
                   "# Mistakes\n\nReflected mistakes, one bullet per entry.\n")
        _seed_text(paths.memory / "market_regimes.md",
                   "# Market regimes\n")
        _seed_text(paths.memory / "skill_learnings.md",
                   "# Skill learnings\n")
        # workspace prompts now ship as a real prompt
        # bundle under ``nerya/workspace/_prompt_bundles/<id>``.  The
        # bundle loader records provenance (sha256, source path, bundle
        # version) into ``agents/_provenance.yml`` so future migrations
        # can detect operator-edited prompts and avoid silently reverting
        # them.  The previous Python-literal seeding flow (which baked a
        # trading personality into the bootstrap path) is replaced by
        # :func:`seed_bundle`.
        seed_bundle(
            paths,
            bundle=load_bundle(DEFAULT_BUNDLE_ID),
        )
        # seed example strategies
        from .bootstrap import seed_example_strategies
        seed_example_strategies(paths)
        return cls.load(paths.root)

    # ---------- accessors ----------
    def journal(self, name: str) -> Journal:
        return Journal(self.paths.journal(name))

    def state(self) -> StateStore:
        return StateStore(self.paths.runtime_state)


_DEFAULT_ENABLED_SKILLS = [
    # These are SKILL.md playbook ids. Native tool availability and approval
    # live in the tool registry; do not duplicate tool/action names here.
    "analysis", "backtest", "browser", "coding", "evolve",
    "expert_investors", "finance-creators", "llm", "market_data_routing",
    "market_research",
    "markets", "memory", "news_social", "notify", "quant-strategy-loop",
    "quant_research",
    "research", "research_report", "strategy_author", "tasks", "team",
    "trading", "triggers",
    # Integration-gated: listed here but loaded only after configuration.
    "dcf_valuation", "equity_research", "sec_filings",
]


_DEFAULT_ROUTES = [
    {"id": "btc_breakout_to_market_analyst",
     "match": {"kind": "price.breakout", "payload.symbol": "BTC"},
     "target": "subagent:market_analyst",
     "strategy_id": "btc_momentum",
     "cooldown_seconds": 60,
     "max_per_minute": 30,
     "max_payload_bytes": 4096},
    {"id": "news_alpha_to_main",
     "match": {"kind": "news.alpha"},
     "target": "main",
     "strategy_id": "btc_momentum",
     "cooldown_seconds": 15,
     "max_per_minute": 30},
    {"id": "news_keyword_to_news_interpreter",
     "match": {"kind": "news.keyword"},
     "target": "subagent:news_interpreter",
     "strategy_id": "btc_momentum",
     "cooldown_seconds": 0,
     "max_per_minute": 10,
     "max_payload_bytes": 8192},
    {"id": "funding_spike_to_main",
     "match": {"kind": "funding.spike"},
     "target": "main",
     "strategy_id": "btc_momentum",
     "cooldown_seconds": 30,
     "max_per_minute": 20},
    {"id": "whale_transfer_to_onchain_watcher",
     "match": {"kind": "whale.transfer"},
     "target": "subagent:onchain_watcher",
     "strategy_id": "btc_momentum",
     "cooldown_seconds": 10,
     "max_per_minute": 60},
    {"id": "sdk_order_to_trading",
     "match": {"kind": "sdk.trade_intent"},
     "target": "skill:trading.submit_trade_intent",
     "cooldown_seconds": 0},
]


def _seed_yaml(path: Path, data: dict[str, Any]) -> None:
    if not path.exists():
        yaml_io.dump(path, data)


def _seed_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")

"""Strategy package loader + typed manifest models.

Plan ref: ``2026-04-28-agent-generated-strategy-runtime-refactor.md`` §5.1.

A *strategy package* is what the agent generates and the operator
promotes into ``workspace/strategies/<strategy_id>/``. The runtime
never reads loose ``main.py`` files; it always loads the package
through this module so manifest schema, schedule, and policy are
validated up-front.

The shape mirrors the plan's "Minimum manifest" example:

```yaml
version: 1
strategy_id: btc_scalper
title: BTC short-cycle scalper
mode: paper
entrypoint: main.py:run
markets: [PAPER:BTCUSDT]
accounts: [paper_main]
schedule:
  type: cron
  cron: "*/1 * * * *"
policy:
  max_single_order_usd: 100
  max_daily_notional_usd: 1000
  max_open_positions: 1
  min_confidence: 0.55
  allow_direct_order: true
  require_subagent_before_order: false
llm_policy:
  default_tier: light
  allowed_tiers: [light]
  max_calls_per_run: 2
subagents: []
tuning:
  enabled: false
  schedule:
    type: cron
    cron: "0 */6 * * *"
  ...
```

The two breaking constraints versus older `strategy.yml` rows from
``nerya/trading/strategies.py``:

* The strategy package owns the cron *here*, not in
  ``triggers/schedules.yml``. The runtime compiles it down to a
  trigger schedule via :mod:`nerya.strategies.scheduler_bridge`.
* The package always carries a typed ``policy`` and ``llm_policy``
  block; the runner refuses to load packages with missing required
  fields. Older operator-authored ``strategy.yml`` rows under
  ``workspace/strategies/`` keep working because the loader fills in
  defaults for any optional sections the agent did not generate.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..core import yaml_io
from ..core.errors import TradingError
from ..core.paths import WorkspacePaths


_STRATEGY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_VALID_MODES: frozenset[str] = frozenset({"paper", "shadow", "live"})
_VALID_SCHEDULE_TYPES: frozenset[str] = frozenset({"cron", "interval"})
_VALID_LLM_TIERS: frozenset[str] = frozenset({"light", "medium", "high"})
_VALID_OBJECTIVES: frozenset[str] = frozenset({
    "risk_adjusted_return",
    "drawdown",
    "win_rate",
    "slippage",
    "execution_quality",
    "return",
    "sharpe",
    "sortino",
})


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategySchedule:
    """Normalized schedule block.

    Compiled into an :class:`~nerya.triggers.schedule.ScheduleEntry`
    by :mod:`nerya.strategies.scheduler_bridge`. We keep the data
    model independent of the trigger schema so a strategy can declare
    its cron without dragging trigger-routing concerns in.
    """

    type: str = "cron"  # "cron" | "interval"
    cron: Optional[str] = None
    every_seconds: Optional[int] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, where: str) -> "StrategySchedule":
        if not isinstance(raw, dict):
            raise TradingError(f"{where}: schedule must be a mapping, got {type(raw).__name__}")
        kind = str(raw.get("type") or "cron").strip().lower()
        if kind not in _VALID_SCHEDULE_TYPES:
            raise TradingError(
                f"{where}: schedule.type must be one of {sorted(_VALID_SCHEDULE_TYPES)!r}, "
                f"got {kind!r}"
            )
        cron_val = raw.get("cron")
        every = raw.get("every_seconds") or raw.get("interval_seconds")
        if kind == "cron":
            if not cron_val or not isinstance(cron_val, str):
                raise TradingError(f"{where}: cron schedule requires non-empty `cron`")
            return cls(
                type="cron",
                cron=cron_val.strip(),
                starts_at=_optional_str(raw.get("starts_at")),
                ends_at=_optional_str(raw.get("ends_at")),
                enabled=bool(raw.get("enabled", True)),
            )
        # interval
        if every is None:
            raise TradingError(f"{where}: interval schedule requires `every_seconds`")
        try:
            every_int = int(every)
        except Exception as exc:
            raise TradingError(f"{where}: interval `every_seconds` must be int") from exc
        if every_int <= 0:
            raise TradingError(f"{where}: interval `every_seconds` must be positive")
        return cls(
            type="interval",
            every_seconds=every_int,
            starts_at=_optional_str(raw.get("starts_at")),
            ends_at=_optional_str(raw.get("ends_at")),
            enabled=bool(raw.get("enabled", True)),
        )

    def asdict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "enabled": self.enabled}
        if self.cron:
            out["cron"] = self.cron
        if self.every_seconds is not None:
            out["every_seconds"] = self.every_seconds
        if self.starts_at:
            out["starts_at"] = self.starts_at
        if self.ends_at:
            out["ends_at"] = self.ends_at
        return out


# ---------------------------------------------------------------------------
# Policy / LLM policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyPolicy:
    """Strategy-scoped runtime policy.

    These are *upstream* of the trading kernel's risk gate. The runner
    enforces them before it ever calls ``submit_intent``. Anything the
    runner allows still goes through the kernel, so the kernel limits
    win when there is a conflict.
    """

    max_single_order_usd: float = 0.0
    max_daily_notional_usd: float = 0.0
    max_open_positions: int = 0
    min_confidence: float = 0.0
    allow_direct_order: bool = True
    require_subagent_before_order: bool = False
    default_order_usd: float = 0.0
    max_run_seconds: int = 60
    max_sdk_calls_per_run: int = 64
    max_subagent_calls_per_run: int = 4

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]], *, where: str) -> "StrategyPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise TradingError(f"{where}: policy must be a mapping, got {type(raw).__name__}")
        try:
            return cls(
                max_single_order_usd=float(raw.get("max_single_order_usd", 0.0) or 0.0),
                max_daily_notional_usd=float(raw.get("max_daily_notional_usd", 0.0) or 0.0),
                max_open_positions=int(raw.get("max_open_positions", 0) or 0),
                min_confidence=float(raw.get("min_confidence", 0.0) or 0.0),
                allow_direct_order=bool(raw.get("allow_direct_order", True)),
                require_subagent_before_order=bool(
                    raw.get("require_subagent_before_order", False)
                ),
                default_order_usd=float(raw.get("default_order_usd", 0.0) or 0.0),
                max_run_seconds=int(raw.get("max_run_seconds", 60) or 60),
                max_sdk_calls_per_run=int(raw.get("max_sdk_calls_per_run", 64) or 64),
                max_subagent_calls_per_run=int(raw.get("max_subagent_calls_per_run", 4) or 4),
            )
        except (TypeError, ValueError) as exc:
            raise TradingError(f"{where}: invalid policy field: {exc}") from exc

    def asdict(self) -> dict[str, Any]:
        return {
            "max_single_order_usd": self.max_single_order_usd,
            "max_daily_notional_usd": self.max_daily_notional_usd,
            "max_open_positions": self.max_open_positions,
            "min_confidence": self.min_confidence,
            "allow_direct_order": self.allow_direct_order,
            "require_subagent_before_order": self.require_subagent_before_order,
            "default_order_usd": self.default_order_usd,
            "max_run_seconds": self.max_run_seconds,
            "max_sdk_calls_per_run": self.max_sdk_calls_per_run,
            "max_subagent_calls_per_run": self.max_subagent_calls_per_run,
        }


@dataclass(frozen=True)
class StrategyLLMPolicy:
    """LLM tier / call-count policy enforced by ``ctx.llm``."""

    default_tier: str = "light"
    allowed_tiers: tuple[str, ...] = ("light",)
    max_calls_per_run: int = 8

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]], *, where: str) -> "StrategyLLMPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise TradingError(f"{where}: llm_policy must be a mapping")
        default_tier = str(raw.get("default_tier") or "light").strip().lower()
        if default_tier not in _VALID_LLM_TIERS:
            raise TradingError(
                f"{where}: llm_policy.default_tier must be one of "
                f"{sorted(_VALID_LLM_TIERS)!r}, got {default_tier!r}"
            )
        tiers_raw = raw.get("allowed_tiers")
        if tiers_raw is None:
            allowed = (default_tier,)
        else:
            if not isinstance(tiers_raw, (list, tuple)):
                raise TradingError(f"{where}: llm_policy.allowed_tiers must be a list")
            allowed = tuple(str(t).strip().lower() for t in tiers_raw if t)
            for t in allowed:
                if t not in _VALID_LLM_TIERS:
                    raise TradingError(
                        f"{where}: llm_policy.allowed_tiers contains invalid tier {t!r}"
                    )
        if default_tier not in allowed:
            allowed = tuple([default_tier, *allowed])
        try:
            cap = int(raw.get("max_calls_per_run", 8) or 8)
        except Exception as exc:
            raise TradingError(f"{where}: llm_policy.max_calls_per_run must be int") from exc
        return cls(default_tier=default_tier, allowed_tiers=allowed, max_calls_per_run=cap)

    def asdict(self) -> dict[str, Any]:
        return {
            "default_tier": self.default_tier,
            "allowed_tiers": list(self.allowed_tiers),
            "max_calls_per_run": self.max_calls_per_run,
        }


@dataclass(frozen=True)
class StrategyAgentSessionConfig:
    """Stable Agent session policy for prompt-driven strategy tasks."""

    policy: str = "per_strategy_market_timeframe"
    ttl_seconds: int = 86400
    max_turns: int = 500
    compact_every_turns: int = 20
    include_prior_messages: bool = True
    refresh_profile_on_change: bool = True

    @classmethod
    def from_dict(
        cls,
        raw: Optional[dict[str, Any]],
        *,
        where: str,
    ) -> "StrategyAgentSessionConfig":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise TradingError(f"{where}: agent_session must be a mapping")
        return cls(
            policy=str(raw.get("policy") or "per_strategy_market_timeframe"),
            ttl_seconds=int(raw.get("ttl_seconds", 86400) or 0),
            max_turns=int(raw.get("max_turns", 500) or 0),
            compact_every_turns=int(raw.get("compact_every_turns", 20) or 0),
            include_prior_messages=bool(raw.get("include_prior_messages", True)),
            refresh_profile_on_change=bool(raw.get("refresh_profile_on_change", True)),
        )

    def asdict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "ttl_seconds": self.ttl_seconds,
            "max_turns": self.max_turns,
            "compact_every_turns": self.compact_every_turns,
            "include_prior_messages": self.include_prior_messages,
            "refresh_profile_on_change": self.refresh_profile_on_change,
        }


@dataclass(frozen=True)
class StrategyAgentProfile:
    """Long-lived rules pinned into a strategy Agent session."""

    title: str = ""
    role: str = ""
    order_rules: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    attached_skills: tuple[str, ...] = ()
    default_trade_source: str = "strategy_agent"
    min_confidence_to_trade: float = 0.0
    risk_limits: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls,
        raw: Optional[dict[str, Any]],
        *,
        where: str,
    ) -> "StrategyAgentProfile":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise TradingError(f"{where}: agent_profile must be a mapping")
        handled = {
            "title", "role", "order_rules", "allowed_tools", "attached_skills",
            "default_trade_source", "min_confidence_to_trade", "risk_limits",
        }
        return cls(
            title=str(raw.get("title") or ""),
            role=str(raw.get("role") or ""),
            order_rules=tuple(str(x) for x in (raw.get("order_rules") or [])),
            allowed_tools=tuple(str(x) for x in (raw.get("allowed_tools") or [])),
            attached_skills=tuple(str(x) for x in (raw.get("attached_skills") or [])),
            default_trade_source=str(raw.get("default_trade_source") or "strategy_agent"),
            min_confidence_to_trade=float(raw.get("min_confidence_to_trade", 0.0) or 0.0),
            risk_limits=dict(raw.get("risk_limits") or {}),
            extras={k: v for k, v in raw.items() if k not in handled},
        )

    def asdict(self) -> dict[str, Any]:
        out = {
            "title": self.title,
            "role": self.role,
            "order_rules": list(self.order_rules),
            "allowed_tools": list(self.allowed_tools),
            "attached_skills": list(self.attached_skills),
            "default_trade_source": self.default_trade_source,
            "min_confidence_to_trade": self.min_confidence_to_trade,
            "risk_limits": dict(self.risk_limits),
        }
        out.update(self.extras)
        return out


# ---------------------------------------------------------------------------
# Tuning block (Phase 7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyTuningLookback:
    runs: int = 200
    min_closed_trades: int = 0
    max_age_hours: int = 168


@dataclass(frozen=True)
class StrategyTuningSubagent:
    name: str = "strategy_tuner"
    prompt_file: str = "subagents/strategy_tuner.agent.md"
    tier: str = "high"


@dataclass(frozen=True)
class StrategyTuningGuardrails:
    max_patch_files: int = 5
    max_position_size_change_pct: float = 25.0
    require_backtest: bool = True
    require_shadow_run: bool = False
    require_operator_approval: bool = True


@dataclass(frozen=True)
class StrategyTuningConfig:
    """Per-strategy tuning configuration.

    Lives at ``strategy.yml::tuning`` (see plan §7.1). Drives an
    independent self-evolution cron — separate from the trading
    schedule — that generates ``PatchProposal`` objects with code /
    config / prompt updates.
    """

    enabled: bool = False
    schedule: Optional[StrategySchedule] = None
    lookback: StrategyTuningLookback = field(default_factory=StrategyTuningLookback)
    subagent: StrategyTuningSubagent = field(default_factory=StrategyTuningSubagent)
    objectives: tuple[str, ...] = ()
    guardrails: StrategyTuningGuardrails = field(default_factory=StrategyTuningGuardrails)
    allowed_targets: tuple[str, ...] = (
        "strategy.yml",
        "main.py",
        "subagents/*.agent.md",
    )
    forbidden_targets: tuple[str, ...] = (
        "accounts/*",
        "limits.yml",
        "secrets/*",
        "live_trading_enabled",
    )
    tuning_prompt: str = ""

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]], *, where: str) -> "StrategyTuningConfig":
        if raw is None or raw is False:
            return cls(enabled=False)
        if not isinstance(raw, dict):
            raise TradingError(f"{where}: tuning must be a mapping")
        enabled = bool(raw.get("enabled", False))
        schedule_raw = raw.get("schedule") or None
        schedule = (
            StrategySchedule.from_dict(schedule_raw, where=f"{where}.schedule")
            if schedule_raw is not None else None
        )
        if enabled and schedule is None:
            raise TradingError(f"{where}: tuning.enabled requires a schedule block")
        lb_raw = raw.get("lookback") or {}
        lookback = StrategyTuningLookback(
            runs=int((lb_raw or {}).get("runs", 200) or 200),
            min_closed_trades=int((lb_raw or {}).get("min_closed_trades", 0) or 0),
            max_age_hours=int((lb_raw or {}).get("max_age_hours", 168) or 168),
        )
        sa_raw = raw.get("subagent") or {}
        sa_tier = str((sa_raw or {}).get("tier", "high") or "high").strip().lower()
        if sa_tier not in _VALID_LLM_TIERS:
            raise TradingError(
                f"{where}: tuning.subagent.tier must be one of "
                f"{sorted(_VALID_LLM_TIERS)!r}"
            )
        subagent = StrategyTuningSubagent(
            name=str((sa_raw or {}).get("name", "strategy_tuner") or "strategy_tuner"),
            prompt_file=str(
                (sa_raw or {}).get("prompt_file", "subagents/strategy_tuner.agent.md")
                or "subagents/strategy_tuner.agent.md"
            ),
            tier=sa_tier,
        )
        objectives_raw = raw.get("objectives")
        if objectives_raw is None:
            objectives: tuple[str, ...] = ()
        elif isinstance(objectives_raw, dict):
            primary = str(objectives_raw.get("primary") or "").strip()
            secondary = objectives_raw.get("secondary") or []
            ordered = [primary] if primary else []
            ordered.extend(str(s).strip() for s in secondary if s)
            objectives = tuple(o for o in ordered if o)
        elif isinstance(objectives_raw, (list, tuple)):
            objectives = tuple(str(o).strip() for o in objectives_raw if o)
        else:
            raise TradingError(f"{where}: tuning.objectives must be list or mapping")
        for obj in objectives:
            if obj not in _VALID_OBJECTIVES:
                raise TradingError(
                    f"{where}: tuning objective {obj!r} not in "
                    f"{sorted(_VALID_OBJECTIVES)!r}"
                )
        gr_raw = raw.get("guardrails") or {}
        guardrails = StrategyTuningGuardrails(
            max_patch_files=int((gr_raw or {}).get("max_patch_files", 5) or 5),
            max_position_size_change_pct=float(
                (gr_raw or {}).get("max_position_size_change_pct", 25.0) or 25.0
            ),
            require_backtest=bool((gr_raw or {}).get("require_backtest", True)),
            require_shadow_run=bool((gr_raw or {}).get("require_shadow_run", False)),
            require_operator_approval=bool(
                (gr_raw or {}).get("require_operator_approval", True)
            ),
        )
        proposal_policy = raw.get("proposal_policy") or {}
        allowed_targets = tuple(
            str(p).strip()
            for p in (proposal_policy.get("allowed_targets") or [
                "strategy.yml", "main.py", "subagents/*.agent.md",
            ])
            if p
        )
        forbidden_targets = tuple(
            str(p).strip()
            for p in (proposal_policy.get("forbidden_targets") or [
                "accounts/*", "limits.yml", "secrets/*", "live_trading_enabled",
            ])
            if p
        )
        return cls(
            enabled=enabled,
            schedule=schedule,
            lookback=lookback,
            subagent=subagent,
            objectives=objectives,
            guardrails=guardrails,
            allowed_targets=allowed_targets,
            forbidden_targets=forbidden_targets,
            tuning_prompt=str(raw.get("tuning_prompt") or "").strip(),
        )

    def asdict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled": self.enabled,
            "lookback": {
                "runs": self.lookback.runs,
                "min_closed_trades": self.lookback.min_closed_trades,
                "max_age_hours": self.lookback.max_age_hours,
            },
            "subagent": {
                "name": self.subagent.name,
                "prompt_file": self.subagent.prompt_file,
                "tier": self.subagent.tier,
            },
            "objectives": list(self.objectives),
            "guardrails": {
                "max_patch_files": self.guardrails.max_patch_files,
                "max_position_size_change_pct": self.guardrails.max_position_size_change_pct,
                "require_backtest": self.guardrails.require_backtest,
                "require_shadow_run": self.guardrails.require_shadow_run,
                "require_operator_approval": self.guardrails.require_operator_approval,
            },
            "proposal_policy": {
                "allowed_targets": list(self.allowed_targets),
                "forbidden_targets": list(self.forbidden_targets),
            },
            "tuning_prompt": self.tuning_prompt,
        }
        if self.schedule is not None:
            out["schedule"] = self.schedule.asdict()
        return out


# ---------------------------------------------------------------------------
# Manifest + package
# ---------------------------------------------------------------------------


@dataclass
class StrategyManifest:
    """Typed view of ``strategy.yml``."""

    version: int
    strategy_id: str
    title: str
    description: str
    mode: str  # "paper" | "shadow" | "live"
    entrypoint: str  # "main.py:run"
    markets: tuple[str, ...]
    accounts: tuple[str, ...]
    schedule: StrategySchedule
    policy: StrategyPolicy
    llm_policy: StrategyLLMPolicy
    agent_session: StrategyAgentSessionConfig = field(default_factory=StrategyAgentSessionConfig)
    agent_profile: StrategyAgentProfile = field(default_factory=StrategyAgentProfile)
    subagents: tuple[str, ...] = ()
    news_sources: tuple[str, ...] = ()
    tuning: StrategyTuningConfig = field(default_factory=StrategyTuningConfig)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def entrypoint_module(self) -> str:
        return self.entrypoint.split(":", 1)[0].strip() or "main.py"

    @property
    def entrypoint_func(self) -> str:
        if ":" in self.entrypoint:
            return self.entrypoint.split(":", 1)[1].strip() or "run"
        return "run"

    def asdict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "strategy_id": self.strategy_id,
            "title": self.title,
            "description": self.description,
            "mode": self.mode,
            "entrypoint": self.entrypoint,
            "markets": list(self.markets),
            "accounts": list(self.accounts),
            "schedule": self.schedule.asdict(),
            "policy": self.policy.asdict(),
            "llm_policy": self.llm_policy.asdict(),
            "agent_session": self.agent_session.asdict(),
            "agent_profile": self.agent_profile.asdict(),
            "subagents": list(self.subagents),
            "news_sources": list(self.news_sources),
            "tuning": self.tuning.asdict(),
            "extras": dict(self.extras),
        }


@dataclass
class StrategyPackage:
    """Loaded strategy package — manifest + on-disk paths + content hash."""

    manifest: StrategyManifest
    root: Path
    files: tuple[str, ...]
    content_hash: str

    @property
    def strategy_id(self) -> str:
        return self.manifest.strategy_id

    @property
    def main_path(self) -> Path:
        return self.root / self.manifest.entrypoint_module

    @property
    def manifest_path(self) -> Path:
        return self.root / "strategy.yml"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def reviews_dir(self) -> Path:
        return self.root / "reviews"

    @property
    def versions_dir(self) -> Path:
        return self.root / "versions"

    @property
    def subagents_dir(self) -> Path:
        return self.root / "subagents"

    @property
    def tests_dir(self) -> Path:
        return self.root / "tests"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_package(paths: WorkspacePaths, strategy_id: str) -> StrategyPackage:
    """Load a strategy package by id, validating the manifest as we go."""

    root = paths.strategy(strategy_id)
    if not root.exists() or not root.is_dir():
        raise TradingError(f"unknown strategy: {strategy_id!r}")
    return _load_from_dir(root)


def load_packages(paths: WorkspacePaths) -> list[StrategyPackage]:
    """Load every package under ``workspace/strategies/<id>/``."""

    out: list[StrategyPackage] = []
    if not paths.strategies.exists():
        return out
    for d in sorted(p for p in paths.strategies.iterdir() if p.is_dir()):
        if not (d / "strategy.yml").exists():
            continue
        try:
            out.append(_load_from_dir(d))
        except TradingError:
            # Tolerate broken packages — operator must fix manifest.
            continue
    return out


def _load_from_dir(root: Path) -> StrategyPackage:
    manifest_path = root / "strategy.yml"
    if not manifest_path.exists():
        raise TradingError(f"{root}: missing strategy.yml")
    raw = yaml_io.load(manifest_path)
    if not isinstance(raw, dict):
        raise TradingError(f"{manifest_path}: must be a YAML mapping")
    manifest = _parse_manifest(raw, source=manifest_path)

    main_path = root / manifest.entrypoint_module
    if not main_path.exists():
        raise TradingError(f"{root}: missing entrypoint {manifest.entrypoint_module}")

    files = _collect_files(root)
    content_hash = _hash_files(root, files)
    return StrategyPackage(
        manifest=manifest,
        root=root,
        files=files,
        content_hash=content_hash,
    )


def _parse_manifest(raw: dict[str, Any], *, source: Path) -> StrategyManifest:
    version = int(raw.get("version", 1) or 1)
    sid = str(raw.get("strategy_id") or raw.get("id") or "").strip()
    if not sid or not _STRATEGY_ID_RE.match(sid):
        raise TradingError(
            f"{source}: strategy_id must match {_STRATEGY_ID_RE.pattern}, got {sid!r}"
        )
    title = str(raw.get("title") or sid)
    description = str(raw.get("description") or "").strip()
    mode = str(raw.get("mode") or "paper").strip().lower()
    if mode not in _VALID_MODES:
        raise TradingError(
            f"{source}: mode must be one of {sorted(_VALID_MODES)!r}, got {mode!r}"
        )
    entrypoint = str(raw.get("entrypoint") or "main.py:run").strip()
    markets = _str_tuple(raw.get("markets"), where=f"{source}::markets", required=False)
    accounts = _str_tuple(raw.get("accounts"), where=f"{source}::accounts", required=False)
    sched_raw = raw.get("schedule")
    if not sched_raw:
        raise TradingError(f"{source}: schedule block is required")
    schedule = StrategySchedule.from_dict(sched_raw, where=f"{source}::schedule")
    policy = StrategyPolicy.from_dict(raw.get("policy"), where=f"{source}::policy")
    llm_policy = StrategyLLMPolicy.from_dict(
        raw.get("llm_policy"), where=f"{source}::llm_policy"
    )
    agent_session = StrategyAgentSessionConfig.from_dict(
        raw.get("agent_session"), where=f"{source}::agent_session"
    )
    agent_profile = StrategyAgentProfile.from_dict(
        raw.get("agent_profile"), where=f"{source}::agent_profile"
    )
    subagents = _str_tuple(raw.get("subagents"), where=f"{source}::subagents", required=False)
    news_sources = _str_tuple(
        raw.get("news_sources"), where=f"{source}::news_sources", required=False
    )
    tuning = StrategyTuningConfig.from_dict(raw.get("tuning"), where=f"{source}::tuning")

    handled = {
        "version", "strategy_id", "id", "title", "description", "mode",
        "entrypoint", "markets", "accounts", "schedule", "policy",
        "llm_policy", "agent_session", "agent_profile",
        "subagents", "news_sources", "tuning",
    }
    extras = {k: v for k, v in raw.items() if k not in handled}
    return StrategyManifest(
        version=version,
        strategy_id=sid,
        title=title,
        description=description,
        mode=mode,
        entrypoint=entrypoint,
        markets=markets,
        accounts=accounts,
        schedule=schedule,
        policy=policy,
        llm_policy=llm_policy,
        agent_session=agent_session,
        agent_profile=agent_profile,
        subagents=subagents,
        news_sources=news_sources,
        tuning=tuning,
        extras=extras,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _str_tuple(value: Any, *, where: str, required: bool) -> tuple[str, ...]:
    if value is None or value == "":
        if required:
            raise TradingError(f"{where}: required")
        return ()
    if isinstance(value, str):
        items: Iterable[str] = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        raise TradingError(f"{where}: must be list of strings")
    out = tuple(s.strip() for s in items if s and str(s).strip())
    if required and not out:
        raise TradingError(f"{where}: required")
    return out


def _collect_files(root: Path) -> tuple[str, ...]:
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        # Skip per-run state we don't want in the content hash.
        first = rel.split("/", 1)[0]
        if first in {"runs", "state", "versions", "reviews"}:
            continue
        out.append(rel)
    return tuple(out)


def _hash_files(root: Path, files: tuple[str, ...]) -> str:
    h = hashlib.sha256()
    for rel in files:
        p = root / rel
        try:
            blob = p.read_bytes()
        except OSError:
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(hashlib.sha256(blob).digest())
        h.update(b"\x00")
    return h.hexdigest()


__all__ = [
    "StrategyLLMPolicy",
    "StrategyManifest",
    "StrategyPackage",
    "StrategyPolicy",
    "StrategySchedule",
    "StrategyTuningConfig",
    "StrategyTuningGuardrails",
    "StrategyTuningLookback",
    "StrategyTuningSubagent",
    "load_package",
    "load_packages",
]

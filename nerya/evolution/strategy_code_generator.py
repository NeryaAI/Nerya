"""Generate strategy package proposals.

The agent calls this module to *propose* a new strategy package; the
generator never mutates ``workspace/strategies/`` directly. It builds:

* ``strategy.md``                — operator-readable playbook.
* ``strategy.yml``               — typed manifest validated by
  :mod:`nerya.strategies.package`.
* ``main.py``                    — entrypoint scaffolded for one of
  the strategy classes (``scalping`` / ``trend`` / ``news`` /
  ``agent`` / ``agent_team``).
* ``subagents/<name>.agent.md``  — strategy-scoped subagent prompts
  (only when the manifest declares them).
* ``tests/test_contract.py``     — minimal contract test that the
  validator+CI re-runs.

Why we ship a typed generator
-----------------------------
The agent could in principle just dump arbitrary Python and YAML. In
practice we want every generated package to:

* parse cleanly through :func:`nerya.strategies.package._parse_manifest`
  (so the runner doesn't have to keep schema migrations forever);
* pass :func:`nerya.strategies.validator.validate_proposal_files`
  (so promotion can be a one-click action for operators);
* read like the templates in runtime spec so reviewers don't have to
  context-switch between three writing styles.

This generator centralises those defaults. The agent can override
any field via :class:`StrategyGenerationRequest`; the generator
fills in conservative defaults for anything missing.

Output shape
------------
The generator returns a :class:`StrategyGenerationResult` with two
parts:

* ``files``     — a flat ``rel_path -> content`` map ready for the
  validator and for ``create_proposal(extra_files={"after/" + rel: c, ...})``.
* ``proposal``  — the ``Proposal`` returned by
  :func:`nerya.evolution.patch_proposal.create_proposal`.

The proposal is created with kind ``strategy_package_proposal``;
:mod:`nerya.evolution.promotion` already understands ``after/<rel>``
files and copies them into the workspace on approval.
"""

from __future__ import annotations

import logging
import re
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Optional

from ..core import yaml_io
from ..core.errors import NeryaError
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from ..strategies.validator import StrategyValidation, validate_proposal_files
from .patch_proposal import Proposal, create_proposal


_LOG = logging.getLogger(__name__)

_VALID_CLASSES: frozenset[str] = frozenset({
    "scalping",
    "trend",
    "news",
    "agent",
    "agent_team",
})
_VALID_MODES: frozenset[str] = frozenset({"paper", "shadow", "live"})
_VALID_EXECUTION_MODES: frozenset[str] = frozenset({
    "",
    "script",
    "agent",
    "agent_task",
    "agent_team",
    "team",
})
_STRATEGY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_VALID_TUNING_OBJECTIVES: frozenset[str] = frozenset({
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
# Request / result types
# ---------------------------------------------------------------------------


@dataclass
class StrategyGenerationRequest:
    """Inputs from the agent.

    The minimum a caller must provide is ``strategy_id``, ``markets``,
    and ``accounts``; everything else has a sensible default keyed
    off ``strategy_class``. The agent is encouraged to pass through
    its raw natural-language prompt as ``prompt`` so it lands inside
    the generated ``strategy.md`` for the operator to review.
    """

    strategy_id: str
    title: str = ""
    description: str = ""
    prompt: str = ""
    strategy_class: str = "scalping"  # scalping | trend | news | agent | agent_team
    execution_mode: str = ""  # script | agent | agent_team; defaults from class/prompt
    mode: str = "paper"
    markets: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()
    schedule_cron: str = ""
    schedule_every_seconds: Optional[int] = None
    news_sources: tuple[str, ...] = ()
    subagents: tuple[str, ...] = ()
    policy_overrides: dict[str, Any] = field(default_factory=dict)
    llm_policy_overrides: dict[str, Any] = field(default_factory=dict)
    create_tuning: bool = True
    tuning_prompt: str = ""
    tuning_cron: str = "0 */6 * * *"
    tuning_objectives: tuple[str, ...] = ()
    extra_subagent_prompts: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    """Optional package-rooted file overrides.

    Keys are paths relative to ``strategies/<id>/`` (e.g. ``main.py``,
    ``tests/test_main.py``, ``strategy.md``). Values fully replace the
    default template output for that path. Use this to inject the
    actual strategy logic, custom backtests, or a richer ``strategy.md``
    when the agent has already drafted them — the templates are a
    fallback, not a constraint.
    """


@dataclass
class StrategyGenerationResult:
    """Generator output.

    ``files`` are package-rooted (``strategy.yml``, ``main.py``,
    ``subagents/foo.agent.md`` ...). The proposal copy already
    includes the ``after/strategies/<id>/`` prefix.
    """

    request: StrategyGenerationRequest
    files: dict[str, str]
    proposal: Optional[Proposal] = None
    validation: Optional[StrategyValidation] = None

    def asdict(self) -> dict[str, Any]:
        return {
            "request": asdict(self.request),
            "files": dict(self.files),
            "proposal_id": (self.proposal.id if self.proposal else None),
            "validation": (self.validation.asdict() if self.validation else None),
        }


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class StrategyCodeGenerator:
    """Produce strategy package files + the resulting :class:`Proposal`.

    Stateless on purpose — every call builds files from the request
    and writes a fresh proposal. We don't cache anything between
    calls because the agent regenerates with new prompts on each
    iteration; caching would just risk stale output.
    """

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths

    def generate(
        self,
        request: StrategyGenerationRequest,
        *,
        validate: bool = True,
        create_proposal_record: bool = True,
    ) -> StrategyGenerationResult:
        """Build files for ``request`` and (optionally) write the proposal."""

        self._validate_request(request)
        request = replace(
            request,
            tuning_objectives=_normalize_tuning_objectives(
                request.tuning_objectives,
            ),
        )
        files = self._build_files(request)

        validation: Optional[StrategyValidation] = None
        if validate:
            validation = validate_proposal_files(
                strategy_id=request.strategy_id, files=files
            )

        proposal: Optional[Proposal] = None
        if create_proposal_record:
            extra_files: dict[str, str] = {}
            for rel, content in files.items():
                # PROMOTION COPIES `after/<rel>` -> `<workspace>/<rel>`.
                # Strategies live under `strategies/<id>/`, so prepend that.
                extra_files[f"after/strategies/{request.strategy_id}/{rel}"] = content
            if validation is not None:
                extra_files["validation_report.json"] = _json_dumps(
                    validation.asdict()
                )
            summary = (
                request.title
                or f"Strategy package proposal for {request.strategy_id}"
            )
            proposal = create_proposal(
                self.paths,
                kind="strategy_package_proposal",
                summary=summary,
                rationale=_rationale(request),
                test_plan=_test_plan(request, validation),
                rollback=_rollback(request),
                target=f"strategies/{request.strategy_id}",
                extra_files=extra_files,
                initial_state="pending_review",
            )

        return StrategyGenerationResult(
            request=request,
            files=files,
            proposal=proposal,
            validation=validation,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_request(req: StrategyGenerationRequest) -> None:
        if not _STRATEGY_ID_RE.match(req.strategy_id or ""):
            raise NeryaError(
                f"strategy_id must match {_STRATEGY_ID_RE.pattern}, got {req.strategy_id!r}"
            )
        if req.strategy_class not in _VALID_CLASSES:
            raise NeryaError(
                f"strategy_class must be one of {sorted(_VALID_CLASSES)!r}, "
                f"got {req.strategy_class!r}"
            )
        if req.mode not in _VALID_MODES:
            raise NeryaError(
                f"mode must be one of {sorted(_VALID_MODES)!r}, got {req.mode!r}"
            )
        if req.execution_mode not in _VALID_EXECUTION_MODES:
            raise NeryaError(
                "execution_mode must be one of "
                f"{sorted(_VALID_EXECUTION_MODES - {''})!r}, "
                f"got {req.execution_mode!r}"
            )
        if not req.markets:
            raise NeryaError("strategy must declare at least one market")
        if not req.accounts:
            raise NeryaError("strategy must declare at least one account")
        if req.schedule_cron and req.schedule_every_seconds:
            raise NeryaError(
                "set schedule_cron OR schedule_every_seconds, not both"
            )

    def _build_files(self, req: StrategyGenerationRequest) -> dict[str, str]:
        files: dict[str, str] = {}
        manifest = self._render_manifest(req)
        files["strategy.yml"] = yaml_io.dumps(manifest)
        files["strategy.md"] = self._render_playbook(req)
        files["main.py"] = self._render_main(req)
        files["tests/test_contract.py"] = self._render_contract_test(req)
        # The manifest may have *added* subagents on top of req.subagents
        # (e.g. defaulting market_analyst for trend strategies). Honour
        # whatever ended up in the manifest so prompt files line up.
        manifest_subagents = list(manifest.get("subagents") or [])
        for name in manifest_subagents:
            prompt = req.extra_subagent_prompts.get(name) or _default_subagent_prompt(
                name=name,
                strategy_id=req.strategy_id,
                strategy_class=req.strategy_class,
                markets=req.markets,
            )
            files[f"subagents/{name}.agent.md"] = prompt
        if req.create_tuning:
            files["subagents/strategy_tuner.agent.md"] = _tuning_subagent_prompt(req)
        # Apply caller-supplied overrides last so the agent's draft of
        # `main.py` / `tests/test_main.py` / `strategy.md` always wins
        # over the stock template. The defaults above stay in for any
        # path the caller didn't override.
        for rel, content in (req.files or {}).items():
            rel_norm = str(rel).replace("\\", "/").lstrip("/")
            if not rel_norm:
                continue
            # Block escapes — proposal writers must not break out of the
            # strategy package.
            if rel_norm.startswith("../") or "/../" in rel_norm:
                raise NeryaError(
                    f"file path escapes strategy package: {rel!r}"
                )
            files[rel_norm] = str(content)
        return files

    @staticmethod
    def _render_manifest(req: StrategyGenerationRequest) -> dict[str, Any]:
        execution_mode = _execution_mode(req)
        sched = _build_schedule(req)
        policy = _default_policy(_template_class(req))
        policy.update(req.policy_overrides or {})
        llm_policy = _default_llm_policy(_template_class(req))
        llm_policy.update(req.llm_policy_overrides or {})
        needs_agent = execution_mode in {"agent", "agent_team"}
        needs_agent_team = execution_mode == "agent_team"
        if needs_agent:
            policy["allow_direct_order"] = False
            policy["max_run_seconds"] = max(
                int(policy.get("max_run_seconds") or 0),
                300,
            )
            llm_policy["max_calls_per_run"] = max(
                int(llm_policy.get("max_calls_per_run") or 0),
                4,
            )
        if needs_agent_team:
            policy["max_run_seconds"] = max(int(policy.get("max_run_seconds") or 0), 600)
            policy["max_subagent_calls_per_run"] = max(
                int(policy.get("max_subagent_calls_per_run") or 0),
                max(4, len(req.subagents)),
            )
            policy["allow_direct_order"] = False
            llm_policy["max_calls_per_run"] = max(
                int(llm_policy.get("max_calls_per_run") or 0),
                8,
            )

        # Surface trigger_kinds so the dashboard list view can render
        # the badge ("schedule" / "event") even when the operator has
        # not yet sync'd schedules.yml. The runtime schedule itself
        # still lives under ``schedule:``; this is purely the dashboard
        # taxonomy field.
        kinds: list[str] = ["schedule"]
        if req.news_sources:
            kinds.append("event")

        # subagents are only added when the caller asks for them: a
        # strategy that lists a role *must* actually call
        # ``ctx.subagents.run(...)`` (or ``ctx.team.run(...)``) inside
        # main.py during the tick, otherwise the role just adds noise.
        # The author skill is responsible for deciding which roles the
        # strategy needs and either reusing one from the workspace
        # Agents library or shipping a per-strategy override.
        effective_subagents: list[str] = (
            _agent_team_roles_for_request(req)
            if needs_agent_team
            else list(req.subagents)
        )

        manifest: dict[str, Any] = {
            "version": 1,
            "strategy_id": req.strategy_id,
            "title": req.title or req.strategy_id,
            "description": req.description or _default_description(req),
            "mode": req.mode,
            "entrypoint": "main.py:run",
            "markets": list(req.markets),
            "accounts": list(req.accounts),
            "schedule": sched,
            "strategy_class": req.strategy_class,
            "execution_mode": execution_mode,
            "trigger_kinds": kinds,
            "policy": policy,
            "llm_policy": llm_policy,
            "subagents": effective_subagents,
            "news_sources": list(req.news_sources),
        }
        if req.create_tuning:
            manifest["tuning"] = _tuning_block(req)
        if needs_agent:
            manifest["agent_task"] = {
                "enabled": True,
                "mode": execution_mode,
            }
            manifest["agent_session"] = _agent_session_block(req, execution_mode)
            manifest["agent_profile"] = _agent_profile_block(req, policy, execution_mode)
        if needs_agent_team:
            manifest["agent_task"] = {
                "enabled": True,
                "mode": "agent_team",
            }
        return manifest

    @staticmethod
    def _render_playbook(req: StrategyGenerationRequest) -> str:
        body = req.prompt.strip() or _default_playbook(req)
        head = f"# {req.title or req.strategy_id}\n\n"
        head += f"_class: {req.strategy_class}; mode: {req.mode}; generated: {now_iso()}_\n\n"
        return head + body + "\n"

    @staticmethod
    def _render_main(req: StrategyGenerationRequest) -> str:
        execution_mode = _execution_mode(req)
        if execution_mode == "agent_team":
            return _agent_team_template(req)
        if execution_mode == "agent":
            return _agent_task_template(req)
        return _MAIN_TEMPLATES[_template_class(req)](req)

    @staticmethod
    def _render_contract_test(req: StrategyGenerationRequest) -> str:
        return _CONTRACT_TEST_TEMPLATE.format(strategy_id=req.strategy_id)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _default_description(req: StrategyGenerationRequest) -> str:
    return (
        f"Auto-generated {req.strategy_class} strategy for "
        f"{', '.join(req.markets) or '<markets>'}; mode={req.mode}."
    )


def _default_playbook(req: StrategyGenerationRequest) -> str:
    base = (
        "## Intent\n\n"
        f"This is an auto-generated {req.strategy_class} strategy targeting "
        f"`{', '.join(req.markets)}`.\n\n"
        "## Policy\n\n"
        "* Strategy may only place orders via `ctx.trading.submit_intent`.\n"
        "* All LLM calls are tier-policed by the runner.\n"
        "* Subagents return recommendations only; the runner submits trades.\n\n"
        "## Operator Notes\n\n"
        "Edit `strategy.yml` to tune risk caps, schedule, or LLM tiers.\n"
        "Edit `main.py` to refine the signal logic.\n"
    )
    return base


def _build_schedule(req: StrategyGenerationRequest) -> dict[str, Any]:
    if req.schedule_every_seconds:
        return {
            "type": "interval",
            "every_seconds": int(req.schedule_every_seconds),
            "enabled": True,
        }
    cron = req.schedule_cron or _default_cron(req.strategy_class)
    return {"type": "cron", "cron": cron, "enabled": True}


def _default_cron(strategy_class: str) -> str:
    return {
        "scalping": "*/1 * * * *",
        "trend": "*/15 * * * *",
        "news": "*/5 * * * *",
        "agent": "*/5 * * * *",
        "agent_team": "0 14 * * 1-5",
    }.get(strategy_class, "*/5 * * * *")


def _execution_mode(req: StrategyGenerationRequest) -> str:
    raw = (req.execution_mode or "").strip().lower().replace("-", "_")
    if raw == "agent_task":
        return "agent"
    if raw == "team":
        return "agent_team"
    if raw in {"script", "agent", "agent_team"}:
        return raw
    if req.strategy_class == "agent":
        return "agent"
    if req.strategy_class == "agent_team":
        return "agent_team"
    if _requires_agent_team(req):
        return "agent_team"
    return "script"


def _template_class(req: StrategyGenerationRequest) -> str:
    if req.strategy_class in _MAIN_TEMPLATES:
        return req.strategy_class
    if req.strategy_class == "agent_team":
        return "trend"
    if req.strategy_class == "agent":
        text = " ".join([req.strategy_id, req.title, req.description, req.prompt]).lower()
        if "news" in text or "headline" in text:
            return "news"
        if "scalp" in text or "剥头皮" in text:
            return "scalping"
        return "trend"
    return "scalping"


def _requires_agent_team(req: StrategyGenerationRequest) -> bool:
    if not req.subagents:
        return False
    text = " ".join([
        req.strategy_id,
        req.title,
        req.description,
        req.prompt,
    ]).lower()
    if (
        "agent team" in text
        or "agent_team" in text
        or "agent-team" in text
        or "_team_" in text
    ):
        return True
    return len(req.subagents) >= 2


def _agent_session_block(
    req: StrategyGenerationRequest,
    execution_mode: str,
) -> dict[str, Any]:
    policy = "per_strategy_market_timeframe"
    if execution_mode == "agent_team":
        policy = "per_signal"
    return {
        "policy": policy,
        "ttl_seconds": 86400,
        "max_turns": 500,
        "compact_every_turns": 20,
        "include_prior_messages": True,
        "refresh_profile_on_change": True,
    }


def _agent_profile_block(
    req: StrategyGenerationRequest,
    policy: dict[str, Any],
    execution_mode: str,
) -> dict[str, Any]:
    if execution_mode == "agent_team":
        allowed_tools = [
            "team_run",
            "role_list",
            "market_data",
            "portfolio_summary",
            "strategy_history",
            "risk_check",
            "trade_intent_submit",
        ]
        attached_skills = [
            "team",
            "trading",
            "market_research",
            "research",
            "market_data_routing",
        ]
        role = (
            "Use Agent Team research to analyze the configured market or basket, "
            "including technicals, fundamentals, macro/news, and risk before "
            "submitting any trade intent."
        )
        order_rules = [
            "Call team_run before risk_check or trade_intent_submit; use role_list only to diagnose missing roles.",
            "For basket tasks, rank candidates and trade only the best risk-adjusted setup.",
            "Submit buy/sell/reduce only when team confidence meets policy.",
            "Hold when evidence is stale, conflicting, or below min_confidence.",
        ]
        title = f"{req.title or req.strategy_id} Agent Team"
    else:
        allowed_tools = [
            "market_data",
            "portfolio_summary",
            "strategy_history",
            "risk_check",
            "trade_intent_submit",
        ]
        attached_skills = [
            "trading",
            "market_research",
            "research",
            "market_data_routing",
        ]
        role = (
            "Review the strategy script's prepared signal, recent candles, "
            "indicators, and news context, then submit only risk-gated trade intents."
        )
        order_rules = [
            "Treat the script-generated signal as evidence, not permission to trade.",
            "Call risk_check before trade_intent_submit.",
            "Hold when confidence is below policy or data quality is degraded.",
        ]
        title = f"{req.title or req.strategy_id} Strategy Agent"
    return {
        "title": title,
        "role": role,
        "order_rules": order_rules,
        "allowed_tools": allowed_tools,
        "attached_skills": attached_skills,
        "default_trade_source": "strategy_agent",
        "min_confidence_to_trade": float(policy.get("min_confidence") or 0.0),
    }


def _agent_team_roles_for_request(req: StrategyGenerationRequest) -> list[str]:
    roles: list[str] = []
    for raw in req.subagents:
        role = str(raw or "").strip()
        if role and role not in roles:
            roles.append(role)
    if not roles:
        roles = [
            "technical_analyst",
            "fundamentals_analyst",
            "macro_strategist",
            "news_interpreter",
            "risk_critic",
        ]
    equity_like = any(str(m).lower().startswith("yahoo:") for m in req.markets)
    text = " ".join([req.strategy_id, req.title, req.description, req.prompt]).lower()
    if (
        (equity_like or "fundamental" in text or "valuation" in text or "earnings" in text)
        and not any("fundamental" in r.lower() for r in roles)
    ):
        roles.insert(1 if roles else 0, "fundamentals_analyst")
    return roles


def _default_policy(strategy_class: str) -> dict[str, Any]:
    base = {
        "max_single_order_usd": 100.0,
        "max_daily_notional_usd": 1000.0,
        "max_open_positions": 1,
        "min_confidence": 0.55,
        "allow_direct_order": True,
        "require_subagent_before_order": False,
        "default_order_usd": 50.0,
        "max_run_seconds": 60,
        "max_sdk_calls_per_run": 64,
        "max_subagent_calls_per_run": 4,
    }
    if strategy_class == "trend":
        base["min_confidence"] = 0.6
    elif strategy_class == "news":
        base["min_confidence"] = 0.65
        base["max_subagent_calls_per_run"] = 6
    return base


def _default_llm_policy(strategy_class: str) -> dict[str, Any]:
    if strategy_class == "scalping":
        return {
            "default_tier": "light",
            "allowed_tiers": ["light"],
            "max_calls_per_run": 2,
        }
    if strategy_class == "trend":
        return {
            "default_tier": "medium",
            "allowed_tiers": ["light", "medium"],
            "max_calls_per_run": 4,
        }
    return {
        "default_tier": "light",
        "allowed_tiers": ["light", "medium"],
        "max_calls_per_run": 8,
    }


def _tuning_block(req: StrategyGenerationRequest) -> dict[str, Any]:
    return {
        "enabled": True,
        "schedule": {"type": "cron", "cron": req.tuning_cron, "enabled": True},
        "lookback": {"runs": 200, "min_closed_trades": 0, "max_age_hours": 168},
        "subagent": {
            "name": "strategy_tuner",
            "prompt_file": "subagents/strategy_tuner.agent.md",
            "tier": "medium",
        },
        "objectives": list(req.tuning_objectives) or ["risk_adjusted_return"],
        "guardrails": {
            "max_patch_files": 5,
            "max_position_size_change_pct": 25.0,
            "require_backtest": True,
            "require_shadow_run": False,
            "require_operator_approval": True,
        },
        "proposal_policy": {
            "allowed_targets": ["strategy.yml", "main.py", "subagents/*.agent.md"],
            "forbidden_targets": [
                "accounts/*",
                "limits.yml",
                "secrets/*",
                "live_trading_enabled",
            ],
        },
        "tuning_prompt": req.tuning_prompt or "",
    }


def _normalize_tuning_objectives(objectives: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []

    def add(value: str) -> None:
        if value in _VALID_TUNING_OBJECTIVES and value not in normalized:
            normalized.append(value)

    for raw in objectives:
        text = str(raw or "").strip()
        if not text:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        add(key)
        if "risk_adjust" in key or ("sharpe" in key and "sortino" in key):
            add("risk_adjusted_return")
        if "drawdown" in key or "max_dd" in key or "downside" in key:
            add("drawdown")
        if "win_rate" in key or ("win" in key and "rate" in key):
            add("win_rate")
        if "slippage" in key:
            add("slippage")
        if "execution" in key:
            add("execution_quality")
        if "sharpe" in key:
            add("sharpe")
        if "sortino" in key:
            add("sortino")
        if (
            "return" in key
            or "profit" in key
            or "pnl" in key
            or "收益" in text
            or "回报" in text
        ):
            add("return")
        if "回撤" in text:
            add("drawdown")
        if "胜率" in text:
            add("win_rate")
        if "滑点" in text:
            add("slippage")
    return tuple(normalized or ("risk_adjusted_return",))


# ----- main.py templates ---------------------------------------------------


def _scalping_template(req: StrategyGenerationRequest) -> str:
    market = req.markets[0]
    return (
        '"""Auto-generated scalping strategy.\n\n'
        f"Strategy id: {req.strategy_id}\n"
        f"Market: {market}\n"
        "\n"
        "Entry path uses ``ctx.trading.open_position(side=, sizing=, protection=)``\n"
        "so the order ships with a real bracket TP/SL: the trading kernel arms\n"
        "the protection at the exchange when running live and at the soft-\n"
        "runtime executor when running paper/shadow. The in-code momentum/RSI\n"
        "exit below acts as a *tactical* discretionary trim and calls\n"
        "``ctx.trading.close_position(...)`` so the bracket protection is\n"
        "released atomically and ``SizingPolicy(method='close_all')`` cannot\n"
        "leave dust.\n"
        "\n"
        "Side-aware: ``ctx.portfolio.positions(market)`` returns this strategy's\n"
        "own *share* (post-v6 merged-position contract). We read the **signed**\n"
        "size and route the exit on the *opposite* side so a close never doubles\n"
        "the position. PnL is normalised on the share direction so TP/SL\n"
        "thresholds compare apples to apples between long and short slices.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from nerya.strategies import StrategyContext, StrategyResult\n\n"
        "\n"
        "# Bracket parameters. Tactical exit thresholds below should stay just\n"
        "# *inside* these rails so the strategy trims before the hard bracket\n"
        "# fires — the bracket is the backstop, not the primary exit.\n"
        "_STOP_LOSS_PCT = 0.005   # 0.5% adverse → exchange bracket fires\n"
        "_TAKE_PROFIT_PCT = 0.008 # 0.8% favourable → exchange bracket books\n"
        "\n"
        "def run(ctx: StrategyContext) -> StrategyResult:\n"
        '    """Single tick entry point invoked by StrategyRunner."""\n'
        "    market = ctx.config.markets[0]\n"
        '    candles = ctx.market.candles(market, timeframe="1m", limit=120)\n'
        "    if len(candles) < 20:\n"
        '        return ctx.result.hold(reason="not enough candles")\n'
        "    closes = [float(row.get('close') or 0.0) for row in candles]\n"
        "    volumes = [float(row.get('volume') or 0.0) for row in candles]\n"
        "    last = closes[-1]\n"
        "    prev = closes[-2]\n"
        "    momentum = (last - prev) / prev if prev else 0.0\n"
        "    rsi = _rsi(closes, 14)\n"
        "    avg_volume = sum(volumes[-20:]) / 20.0 if len(volumes) >= 20 else 0.0\n"
        "    volume_ok = volumes[-1] >= avg_volume * 1.2 if avg_volume else True\n"
        "    positions = ctx.portfolio.positions(market)\n"
        "    position = positions[0] if positions else None\n"
        "    if position:\n"
        "        entry = float(position.get('avg_price') or position.get('entry_price') or last)\n"
        "        signed = float(position.get('size') or position.get('quantity') or position.get('qty') or 0.0)\n"
        "        qty = abs(signed)\n"
        "        # Side-aware PnL: long earns when price rises, short earns when price falls.\n"
        "        side_factor = 1.0 if signed >= 0 else -1.0\n"
        "        pnl_pct = ((last - entry) / entry) * side_factor if entry else 0.0\n"
        "        # Tactical exits — tighter than the bracket SL/TP so the\n"
        "        # strategy gets out on momentum reversal *before* the\n"
        "        # exchange bracket has to fire.\n"
        "        should_exit = (\n"
        "            momentum * side_factor < -0.00035\n"
        "            or pnl_pct >= (_TAKE_PROFIT_PCT - 0.002)\n"
        "            or pnl_pct <= -(_STOP_LOSS_PCT - 0.001)\n"
        "            or rsi >= 74.0\n"
        "        )\n"
        "        if should_exit and qty > 0:\n"
        "            position_side = 'long' if signed > 0 else 'short'\n"
        "            # ``close_position`` releases bracket protection and sizes\n"
        "            # at ``SizingPolicy(close_all)`` — no dust, no doubling.\n"
        "            return ctx.trading.close_position(\n"
        "                market=market,\n"
        "                side=position_side,\n"
        "                confidence=0.61,\n"
        "                reasoning_ref=(\n"
        "                    f\"scalp_exit side={position_side} \"\n"
        "                    f\"momentum={momentum:.6f} pnl={pnl_pct:.4f} rsi={rsi:.2f}\"\n"
        "                ),\n"
        "            )\n"
        '        return ctx.result.hold(reason="holding scalp position", metadata={"momentum": momentum, "rsi": rsi, "pnl_pct": pnl_pct})\n'
        "    trend = (last - (sum(closes[-60:]) / 60.0)) / last if len(closes) >= 60 and last else 0.0\n"
        "    if momentum <= 0.0005 or trend <= 0.0 or not volume_ok or rsi < 45.0 or rsi >= 68.0:\n"
        '        return ctx.result.hold(reason="no scalp entry", metadata={"momentum": momentum, "rsi": rsi, "volume_ok": volume_ok})\n'
        "    # Long-only scalper. ``open_position`` ships the entry market\n"
        "    # order *and* arms the bracket SL/TP in one atomic plan — for\n"
        "    # live CEX accounts the bracket lives at the exchange; for paper\n"
        "    # accounts the in-process protection executor enforces the same\n"
        "    # exit semantics.\n"
        "    return ctx.trading.open_position(\n"
        "        market=market,\n"
        '        side="long",\n'
        '        sizing={"method": "fixed_usd", "fixed_usd": ctx.policy.default_order_usd},\n'
        '        protection={\n'
        '            "stop_loss": {"type": "pct", "value": _STOP_LOSS_PCT},\n'
        '            "take_profit": {"type": "pct", "value": _TAKE_PROFIT_PCT},\n'
        "        },\n"
        "        confidence=0.6,\n"
        '        reasoning_ref=f"scalp_entry momentum={momentum:.6f} rsi={rsi:.2f}",\n'
        "    )\n"
        "\n\n"
        "def _rsi(values, window):\n"
        "    if len(values) <= window:\n"
        "        return 50.0\n"
        "    gains = []\n"
        "    losses = []\n"
        "    for i in range(-window, 0):\n"
        "        delta = values[i] - values[i - 1]\n"
        "        gains.append(max(delta, 0.0))\n"
        "        losses.append(abs(min(delta, 0.0)))\n"
        "    avg_gain = sum(gains) / window\n"
        "    avg_loss = sum(losses) / window\n"
        "    if avg_loss == 0:\n"
        "        return 100.0 if avg_gain > 0 else 50.0\n"
        "    rs = avg_gain / avg_loss\n"
        "    return 100.0 - (100.0 / (1.0 + rs))\n"
    )


def _trend_template(req: StrategyGenerationRequest) -> str:
    market = req.markets[0]
    subagent_call = ""
    if req.subagents:
        sa = req.subagents[0]
        subagent_call = (
            "    signal = _ma_cross_signal(candles)\n"
            f'    analysis = ctx.subagents.run("{sa}", payload={{"market": market, "signal": signal, "features": features}})\n'
            '    output = analysis.get("output") or {}\n'
            '    rec = output.get("recommendation")\n'
            '    if rec not in {"buy", "sell"}:\n'
            '        return ctx.result.hold(reason=output.get("thesis", "subagent declined"))\n'
            '    confidence = float(output.get("confidence", 0.0) or 0.0)\n'
            '    if confidence < ctx.policy.min_confidence:\n'
            '        return ctx.result.hold(reason="subagent confidence below policy")\n'
            "    return ctx.trading.submit_intent(\n"
            "        market=market,\n"
            "        side=rec,\n"
            "        size=ctx.policy.default_order_usd,\n"
            '        size_unit="usd",\n'
            '        order_type="market",\n'
            "        confidence=confidence,\n"
            '        reasoning=output.get("thesis", ""),\n'
            "    )\n"
        )
    else:
        subagent_call = (
            "    signal = _ma_cross_signal(candles)\n"
            '    if signal["cross"] == "none":\n'
            '        return ctx.result.hold(reason="no moving-average cross", metadata={"signal": signal, "features": features})\n'
            "    # v6 side-aware exit/entry. ``positions(market)`` returns this\n"
            "    # strategy's own share (NOT the merged total), so ``signed`` is\n"
            "    # the slice this strategy is responsible for.\n"
            "    positions = ctx.portfolio.positions(market)\n"
            "    position = positions[0] if positions else None\n"
            "    signed = (\n"
            "        float(position.get('size') or position.get('quantity') or position.get('qty') or 0.0)\n"
            "        if position else 0.0\n"
            "    )\n"
            "    cross_side = \"long\" if signal[\"cross\"] == \"golden_cross\" else \"short\"\n"
            "    # A cross that aligns with the existing share is a no-op for\n"
            "    # this template — doubling down on a trend without sizing logic\n"
            "    # is how strategies leak capital.\n"
            "    if signed > 0 and cross_side == \"long\":\n"
            '        return ctx.result.hold(reason="golden_cross already long", metadata={"signal": signal, "size": signed})\n'
            "    if signed < 0 and cross_side == \"short\":\n"
            '        return ctx.result.hold(reason="death_cross already short", metadata={"signal": signal, "size": signed})\n'
            "    # Opposing cross while holding a share → flatten the share.\n"
            "    # ``close_position`` releases protection and uses close_all\n"
            "    # sizing so a SHORT close emits a buy of |signed|, not a bare\n"
            "    # ``sell`` (which would grow the short — the v6 runaway bug).\n"
            "    if abs(signed) > 0:\n"
            "        position_side = 'long' if signed > 0 else 'short'\n"
            "        return ctx.trading.close_position(\n"
            "            market=market,\n"
            "            side=position_side,\n"
            "            confidence=0.65,\n"
            '            reasoning_ref=f"close on {signal[\'cross\']} fast_sma={signal[\'fast_now\']:.4f} slow_sma={signal[\'slow_now\']:.4f}",\n'
            "        )\n"
            "    # Fresh entry — no existing share for this strategy. Ship the\n"
            "    # entry market order with bracket TP/SL atomically.\n"
            "    confidence = 0.62 if cross_side == 'long' else 0.6\n"
            "    return ctx.trading.open_position(\n"
            "        market=market,\n"
            "        side=cross_side,\n"
            '        sizing={"method": "fixed_usd", "fixed_usd": ctx.policy.default_order_usd},\n'
            '        protection={\n'
            '            "stop_loss": {"type": "pct", "value": 0.02},\n'
            '            "take_profit": {"type": "pct", "value": 0.05},\n'
            "        },\n"
            "        confidence=confidence,\n"
            '        reasoning_ref=f"{signal[\'cross\']} fast_sma={signal[\'fast_now\']:.4f} slow_sma={signal[\'slow_now\']:.4f}",\n'
            "    )\n"
        )
    return (
        '"""Auto-generated trend-following strategy.\n\n'
        f"Strategy id: {req.strategy_id}\n"
        f"Market: {market}\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from nerya.strategies import StrategyContext, StrategyResult\n\n"
        "\n"
        "def run(ctx: StrategyContext) -> StrategyResult:\n"
        "    market = ctx.config.markets[0]\n"
        '    timeframe = ctx.trigger.get("timeframe") or ctx.trigger.get("interval") or "15m"\n'
        "    candles = ctx.market.candles(market, timeframe=timeframe, limit=160)\n"
        "    features = ctx.market.features(market, timeframe=timeframe, lookback=160)\n"
        '    if len(candles) < 55:\n'
        '        return ctx.result.hold(reason="insufficient history")\n'
        f"{subagent_call}"
        "\n\n"
        "def _sma(values, window):\n"
        "    if len(values) < window:\n"
        "        return None\n"
        "    return sum(values[-window:]) / float(window)\n\n"
        "\n"
        "def _ma_cross_signal(candles):\n"
        "    closes = [float(row.get('close') or 0.0) for row in candles]\n"
        "    fast_now = _sma(closes, 20)\n"
        "    slow_now = _sma(closes, 50)\n"
        "    fast_prev = _sma(closes[:-1], 20)\n"
        "    slow_prev = _sma(closes[:-1], 50)\n"
        "    cross = 'none'\n"
        "    if None not in (fast_prev, slow_prev, fast_now, slow_now):\n"
        "        if fast_prev <= slow_prev and fast_now > slow_now:\n"
        "            cross = 'golden_cross'\n"
        "        elif fast_prev >= slow_prev and fast_now < slow_now:\n"
        "            cross = 'death_cross'\n"
        "    return {\n"
        "        'cross': cross,\n"
        "        'fast_window': 20,\n"
        "        'slow_window': 50,\n"
        "        'fast_now': float(fast_now or 0.0),\n"
        "        'slow_now': float(slow_now or 0.0),\n"
        "        'last_close': closes[-1] if closes else 0.0,\n"
        "    }\n"
    )


def _news_template(req: StrategyGenerationRequest) -> str:
    sources = list(req.news_sources) or ["operator-configured"]
    subagent_line = (
        f'    analysis = ctx.subagents.run("{req.subagents[0]}", payload={{"item": item}})\n'
        if req.subagents else
        "    analysis = {}\n"
    )
    return (
        '"""Auto-generated news-following strategy.\n\n'
        f"Strategy id: {req.strategy_id}\n"
        f"News sources: {sources}\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from nerya.strategies import StrategyContext, StrategyResult\n\n"
        "\n"
        "def run(ctx: StrategyContext) -> StrategyResult:\n"
        '    items = ctx.news.fetch(since=ctx.state.get("last_seen"))\n'
        "    if not items:\n"
        '        return ctx.result.hold(reason="no news this tick")\n'
        "    items = ctx.dedupe.news(items)\n"
        "    if not items:\n"
        '        return ctx.result.hold(reason="all duplicates")\n'
        "    item = items[0]\n"
        "    cls = ctx.llm.classify(\n"
        '        prompt=item.get("summary") or item.get("title") or "",\n'
        '        labels=["alpha", "noise", "risk"],\n'
        "    )\n"
        '    if cls.get("label") != "alpha":\n'
        '        ctx.messages.send(text=f"news classified as {cls.get(\'label\')}")\n'
        '        ctx.state.set("last_seen", ctx.clock.now_iso())\n'
        '        return ctx.result.ok(reason="non-actionable news")\n'
        f"{subagent_line}"
        '    out = analysis.get("output") if isinstance(analysis, dict) else {}\n'
        "    out = out or {}\n"
        '    rec = out.get("recommendation")\n'
        '    if rec not in {"buy", "sell"}:\n'
        '        ctx.messages.send(text=out.get("summary", "subagent declined"))\n'
        '        ctx.state.set("last_seen", ctx.clock.now_iso())\n'
        '        return ctx.result.ok(reason="subagent declined")\n'
        '    ctx.state.set("last_seen", ctx.clock.now_iso())\n'
        "    return ctx.trading.submit_intent(\n"
        "        market=ctx.config.markets[0],\n"
        "        side=rec,\n"
        "        size=ctx.policy.default_order_usd,\n"
        '        size_unit="usd",\n'
        '        order_type="market",\n'
        '        confidence=float(out.get("confidence", 0.0) or 0.0),\n'
        '        reasoning=out.get("thesis", ""),\n'
        "    )\n"
    )


def _agent_task_template(req: StrategyGenerationRequest) -> str:
    market = req.markets[0]
    account = req.accounts[0]
    style = _template_class(req)
    return (
        '"""Auto-generated script-to-Agent strategy.\n\n'
        f"Strategy id: {req.strategy_id}\n"
        f"Primary market: {market}\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import json\n\n"
        "from nerya.strategies import StrategyAgentTask, StrategyContext, StrategyResult\n\n"
        f'_DEFAULT_MARKET = "{market}"\n'
        f'_DEFAULT_ACCOUNT = "{account}"\n'
        f'_STRATEGY_STYLE = "{style}"\n\n'
        "\n"
        "def run(ctx: StrategyContext) -> StrategyAgentTask | StrategyResult:\n"
        "    if getattr(ctx, 'runmode', '') == 'backtest':\n"
        "        return _backtest_signal_result(ctx)\n"
        "    return build_agent_task(ctx)\n\n"
        "\n"
        "def _backtest_signal_result(ctx: StrategyContext) -> StrategyResult:\n"
        "    market = ctx.trigger.get('market') or ctx.config.markets[0] or _DEFAULT_MARKET\n"
        "    timeframe = ctx.trigger.get('timeframe') or ctx.trigger.get('interval') or _default_timeframe()\n"
        "    candles = ctx.market.candles(market, timeframe=timeframe, limit=160)\n"
        "    signal = _strategy_signal(candles, {})\n"
        "    action = signal.get('action_hint')\n"
        "    confidence = float(signal.get('confidence_hint') or 0.0)\n"
        "    positions = ctx.portfolio.positions(market)\n"
        "    position = positions[0] if positions else None\n"
        "    signed = float(position.get('size') or position.get('quantity') or position.get('qty') or 0.0) if position else 0.0\n"
        "    # v6 contract: entries ship bracket TP/SL via open_position so the\n"
        "    # backtest harness exercises the same protection-armed path the\n"
        "    # live runtime uses. Exits route through close_position so the\n"
        "    # protection is released atomically and ``SizingPolicy(close_all)``\n"
        "    # picks up the full slice.\n"
        "    if action == 'buy' and not position and confidence >= max(0.0, ctx.policy.min_confidence):\n"
        "        return ctx.trading.open_position(\n"
        "            market=market,\n"
        "            side='long',\n"
        "            sizing={'method': 'fixed_usd', 'fixed_usd': ctx.policy.default_order_usd},\n"
        "            protection={\n"
        "                'stop_loss': {'type': 'pct', 'value': 0.01},\n"
        "                'take_profit': {'type': 'pct', 'value': 0.02},\n"
        "            },\n"
        "            confidence=confidence,\n"
        "            reasoning_ref=f\"backtest_script_signal {signal.get('name')}\",\n"
        "        )\n"
        "    if action == 'sell' and position and abs(signed) > 0:\n"
        "        position_side = 'long' if signed > 0 else 'short'\n"
        "        return ctx.trading.close_position(\n"
        "            market=market,\n"
        "            side=position_side,\n"
        "            confidence=max(confidence, 0.6),\n"
        "            reasoning_ref=f\"backtest_script_signal {signal.get('name')}\",\n"
        "        )\n"
        "    if position and abs(signed) > 0 and _exit_signal(candles, signal):\n"
        "        position_side = 'long' if signed > 0 else 'short'\n"
        "        return ctx.trading.close_position(\n"
        "            market=market,\n"
        "            side=position_side,\n"
        "            confidence=0.6,\n"
        "            reasoning_ref=f\"backtest_exit {signal.get('name')}\",\n"
        "        )\n"
        "    return ctx.result.hold(reason='backtest signal hold', metadata={'signal': signal})\n\n"
        "\n"
        "def build_agent_task(ctx: StrategyContext) -> StrategyAgentTask:\n"
        "    market = ctx.trigger.get('market') or ctx.config.markets[0] or _DEFAULT_MARKET\n"
        "    account_id = ctx.trigger.get('account_id') or ctx.config.accounts[0] or _DEFAULT_ACCOUNT\n"
        "    timeframe = ctx.trigger.get('timeframe') or ctx.trigger.get('interval') or _default_timeframe()\n"
        "    candles = []\n"
        "    features = {}\n"
        "    candle_error = ''\n"
        "    try:\n"
        "        candles = ctx.market.candles(market, timeframe=timeframe, limit=160)\n"
        "        features = ctx.market.features(market, timeframe=timeframe, lookback=160)\n"
        "    except Exception as exc:\n"
        "        candle_error = f'{type(exc).__name__}: {exc}'\n"
        "    news_items = []\n"
        "    news_error = ''\n"
        "    try:\n"
        "        news_items = ctx.news.fetch(limit=10)\n"
        "    except Exception as exc:\n"
        "        news_error = f'{type(exc).__name__}: {exc}'\n"
        "    signal = _strategy_signal(candles, features)\n"
        "    data_quality = {\n"
        "        'candles_available': len(candles),\n"
        "        'candle_error': candle_error,\n"
        "        'news_available': len(news_items),\n"
        "        'news_error': news_error,\n"
        "    }\n"
        "    prompt = '\\n'.join([\n"
        "        f'Strategy `{ctx.config.strategy_id}` prepared a script-built signal for Agent review.',\n"
        "        '',\n"
        "        f'Market: {market}',\n"
        "        f'Account: {account_id}',\n"
        "        f'Timeframe: {timeframe}',\n"
        "        f'Style: {_STRATEGY_STYLE}',\n"
        "        '',\n"
        "        'The script computed the signal and bundled recent K-line rows, indicators, and news.',\n"
        "        'Do not trade from the signal alone. Validate data quality and risk first.',\n"
        "        'Before any buy/sell/reduce action, call risk_check, then trade_intent_submit only if allowed.',\n"
        "        'Hold when the signal is weak, contradictory, stale, or below policy.min_confidence.',\n"
        "        '',\n"
        "        'IMPORTANT: every fresh entry MUST include a `protection` block with',\n"
        "        '`stop_loss` and `take_profit` so the bracket arms atomically with',\n"
        "        'the order. Bare market orders without a stop are a known way to',\n"
        "        'leak capital during gaps. For closes use `plan_action=\"close_position\"`',\n"
        "        'and omit `protection` — the close releases the existing bracket.',\n"
        "        'Example bracket entry payload:',\n"
        "        '  {\"side\": \"buy\", \"size\": 100, \"size_unit\": \"usd\",',\n"
        "        '   \"order_type\": \"market\", \"protection\": {',\n"
        "        '     \"stop_loss\": {\"type\": \"pct\", \"value\": 0.01},',\n"
        "        '     \"take_profit\": {\"type\": \"pct\", \"value\": 0.02}}}',\n"
        "        '',\n"
        "        'Script signal JSON:',\n"
        "        json.dumps(signal, ensure_ascii=False, indent=2, default=str),\n"
        "        '',\n"
        "        'Indicator/features JSON:',\n"
        "        json.dumps(features, ensure_ascii=False, indent=2, default=str),\n"
        "        '',\n"
        "        'Recent K-line tail JSON:',\n"
        "        json.dumps(list(candles[-24:]), ensure_ascii=False, indent=2, default=str),\n"
        "        '',\n"
        "        'Recent news JSON:',\n"
        "        json.dumps(list(news_items[:10]), ensure_ascii=False, indent=2, default=str),\n"
        "        '',\n"
        "        'Data quality JSON:',\n"
        "        json.dumps(data_quality, ensure_ascii=False, indent=2, default=str),\n"
        "        '',\n"
        "        'Final response contract:',\n"
        "        json.dumps({\n"
        "            'decision': 'buy|sell|reduce|hold',\n"
        "            'confidence': 0.0,\n"
        "            'market': market,\n"
        "            'account_id': account_id,\n"
        "            'signal': signal.get('name'),\n"
        "            'action_taken': 'none|risk_check|trade_intent_submit',\n"
        "            'reasoning': ['evidence-backed bullet'],\n"
        "        }, ensure_ascii=False, indent=2),\n"
        "    ])\n"
        "    return StrategyAgentTask.dispatch(\n"
        "        prompt=prompt,\n"
        "        session_key={'market': market, 'timeframe': timeframe},\n"
        "        metadata={\n"
        "            'market': market,\n"
        "            'timeframe': timeframe,\n"
        "            'account_id': account_id,\n"
        "            'signal': signal,\n"
        "            'data_quality': data_quality,\n"
        "            'execution_mode': 'agent',\n"
        "        },\n"
        "        attached_skills=['trading', 'market_research', 'research', 'market_data_routing'],\n"
        "        reason=f'script-built {_STRATEGY_STYLE} signal dispatched to Agent',\n"
        "    )\n\n"
        "\n"
        "def _default_timeframe() -> str:\n"
        "    if _STRATEGY_STYLE == 'scalping':\n"
        "        return '1m'\n"
        "    if _STRATEGY_STYLE == 'news':\n"
        "        return '5m'\n"
        "    return '15m'\n\n"
        "\n"
        "def _strategy_signal(candles, features):\n"
        "    if _STRATEGY_STYLE == 'scalping':\n"
        "        return _momentum_signal(candles)\n"
        "    if _STRATEGY_STYLE == 'news':\n"
        "        return {'name': 'news_agent_review', 'action_hint': 'hold', 'confidence_hint': 0.0}\n"
        "    return _ma_cross_signal(candles)\n\n"
        "\n"
        "def _exit_signal(candles, signal):\n"
        "    if signal.get('action_hint') == 'sell':\n"
        "        return True\n"
        "    if len(candles) < 3:\n"
        "        return False\n"
        "    last = float(candles[-1].get('close') or 0.0)\n"
        "    prev = float(candles[-2].get('close') or 0.0)\n"
        "    momentum = (last - prev) / prev if prev else 0.0\n"
        "    return momentum < -0.001\n\n"
        "\n"
        "def _momentum_signal(candles):\n"
        "    if len(candles) < 10:\n"
        "        return {'name': 'insufficient_candles', 'action_hint': 'hold', 'confidence_hint': 0.0}\n"
        "    last = float(candles[-1].get('close') or 0.0)\n"
        "    prev = float(candles[-2].get('close') or 0.0)\n"
        "    momentum = (last - prev) / prev if prev else 0.0\n"
        "    action = 'buy' if momentum > 0 else 'hold'\n"
        "    return {'name': 'scalping_momentum', 'action_hint': action, 'momentum': momentum, 'confidence_hint': 0.58 if action == 'buy' else 0.0}\n\n"
        "\n"
        "def _sma(values, window):\n"
        "    if len(values) < window:\n"
        "        return None\n"
        "    return sum(values[-window:]) / float(window)\n\n"
        "\n"
        "def _ma_cross_signal(candles):\n"
        "    closes = [float(row.get('close') or 0.0) for row in candles]\n"
        "    fast_now = _sma(closes, 20)\n"
        "    slow_now = _sma(closes, 50)\n"
        "    fast_prev = _sma(closes[:-1], 20)\n"
        "    slow_prev = _sma(closes[:-1], 50)\n"
        "    name = 'no_cross'\n"
        "    action = 'hold'\n"
        "    confidence = 0.0\n"
        "    if None not in (fast_prev, slow_prev, fast_now, slow_now):\n"
        "        if fast_prev <= slow_prev and fast_now > slow_now:\n"
        "            name, action, confidence = 'golden_cross', 'buy', 0.62\n"
        "        elif fast_prev >= slow_prev and fast_now < slow_now:\n"
        "            name, action, confidence = 'death_cross', 'sell', 0.6\n"
        "    return {\n"
        "        'name': name,\n"
        "        'action_hint': action,\n"
        "        'confidence_hint': confidence,\n"
        "        'fast_window': 20,\n"
        "        'slow_window': 50,\n"
        "        'fast_now': float(fast_now or 0.0),\n"
        "        'slow_now': float(slow_now or 0.0),\n"
        "        'last_close': closes[-1] if closes else 0.0,\n"
        "    }\n"
    )


def _agent_team_template(req: StrategyGenerationRequest) -> str:
    markets = list(req.markets)
    market = markets[0]
    account = req.accounts[0]
    roles = _agent_team_roles_for_request(req)
    roles_literal = json.dumps(roles, ensure_ascii=False)
    markets_literal = json.dumps(markets, ensure_ascii=False)
    return (
        '"""Auto-generated Agent Team strategy task.\n\n'
        f"Strategy id: {req.strategy_id}\n"
        f"Markets: {', '.join(markets)}\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import json\n\n"
        "from nerya.strategies import StrategyAgentTask, StrategyContext, StrategyResult\n\n"
        f"_TEAM_ROLES = {roles_literal}\n"
        f"_DEFAULT_MARKETS = {markets_literal}\n"
        f'_DEFAULT_MARKET = "{market}"\n'
        f'_DEFAULT_ACCOUNT = "{account}"\n'
        '_DEFAULT_TIMEFRAME = "1d"\n\n'
        "\n"
        "def run(ctx: StrategyContext) -> StrategyAgentTask | StrategyResult:\n"
        "    if getattr(ctx, 'runmode', '') == 'backtest':\n"
        "        return _backtest_basket_result(ctx)\n"
        "    return build_agent_task(ctx)\n\n"
        "\n"
        "def _backtest_basket_result(ctx: StrategyContext) -> StrategyResult:\n"
        "    markets = list(ctx.config.markets or _DEFAULT_MARKETS)\n"
        "    timeframe = ctx.trigger.get('timeframe') or ctx.trigger.get('interval') or _DEFAULT_TIMEFRAME\n"
        "    open_positions = []\n"
        "    for candidate in markets:\n"
        "        open_positions.extend(ctx.portfolio.positions(candidate))\n"
        "    # v6 contract: backtest exits go through close_position (releases\n"
        "    # protection + close_all sizing). Entries go through open_position\n"
        "    # with a bracket TP/SL so the live runtime and the backtest harness\n"
        "    # share the same protection-armed code path.\n"
        "    if open_positions:\n"
        "        position = open_positions[0]\n"
        "        pos_market = position.get('market') or markets[0]\n"
        "        score = _technical_score(ctx, pos_market, timeframe)\n"
        "        signed = float(position.get('size') or position.get('quantity') or position.get('qty') or 0.0)\n"
        "        if (score['score'] < -0.005 or score['rsi'] >= 75.0) and abs(signed) > 0:\n"
        "            position_side = 'long' if signed > 0 else 'short'\n"
        "            return ctx.trading.close_position(\n"
        "                market=pos_market,\n"
        "                side=position_side,\n"
        "                confidence=0.62,\n"
        "                reasoning_ref=f\"backtest_team_exit score={score['score']:.4f} rsi={score['rsi']:.2f}\",\n"
        "            )\n"
        "        return ctx.result.hold(reason='backtest team holds open candidate', metadata={'score': score})\n"
        "    ranked = sorted((_technical_score(ctx, m, timeframe) for m in markets), key=lambda row: row['score'], reverse=True)\n"
        "    best = ranked[0] if ranked else {'market': markets[0], 'score': 0.0, 'rsi': 50.0}\n"
        "    confidence = min(0.8, 0.55 + max(0.0, best['score']) * 5.0)\n"
        "    if best['score'] <= 0.008 or confidence < max(0.0, ctx.policy.min_confidence):\n"
        "        return ctx.result.hold(reason='backtest team no ranked setup', metadata={'ranked': ranked[:4]})\n"
        "    return ctx.trading.open_position(\n"
        "        market=best['market'],\n"
        "        side='long',\n"
        "        sizing={'method': 'fixed_usd', 'fixed_usd': ctx.policy.default_order_usd},\n"
        "        protection={\n"
        "            'stop_loss': {'type': 'pct', 'value': 0.02},\n"
        "            'take_profit': {'type': 'pct', 'value': 0.05},\n"
        "        },\n"
        "        confidence=confidence,\n"
        "        reasoning_ref=f\"backtest_team_rank score={best['score']:.4f} rsi={best['rsi']:.2f}\",\n"
        "    )\n\n"
        "\n"
        "def build_agent_task(ctx: StrategyContext) -> StrategyAgentTask:\n"
        "    trigger_markets = ctx.trigger.get('markets')\n"
        "    if isinstance(trigger_markets, str):\n"
        "        markets = [m.strip() for m in trigger_markets.split(',') if m.strip()]\n"
        "    elif isinstance(trigger_markets, list):\n"
        "        markets = [str(m).strip() for m in trigger_markets if str(m).strip()]\n"
        "    else:\n"
        "        single = ctx.trigger.get('market')\n"
        "        markets = [str(single).strip()] if single else list(ctx.config.markets or _DEFAULT_MARKETS)\n"
        "    markets = markets or list(_DEFAULT_MARKETS) or [_DEFAULT_MARKET]\n"
        "    market = markets[0]\n"
        "    account_id = ctx.trigger.get('account_id') or ctx.config.accounts[0] or _DEFAULT_ACCOUNT\n"
        "    timeframe = ctx.trigger.get('timeframe') or ctx.trigger.get('interval') or _DEFAULT_TIMEFRAME\n"
        "    snapshots = [_safe_technical_snapshot(ctx, m, timeframe) for m in markets[:12]]\n"
        "    roles = [\n"
        "        {'name': role, 'instructions': _role_task(role, markets)}\n"
        "        for role in _TEAM_ROLES\n"
        "    ]\n"
        "    prompt = '\\n'.join([\n"
        "        f'Strategy Agent Team task for `{ctx.config.strategy_id}`.',\n"
        "        '',\n"
        "        'Call team_run first with the roles JSON below; do not call role_list unless team_run reports a missing role.',\n"
        "        'When calling team_run, pass roles as the array itself; never pass a JSON-encoded roles string.',\n"
        "        'Do not answer only from this JSON payload; use Agent Team analysis before deciding.',\n"
        "        '',\n"
        "        'Shared objective:',\n"
        "        f'- Candidate markets: {\", \".join(markets)}',\n"
        "        f'- Account: {account_id}',\n"
        "        f'- Timeframe: {timeframe}',\n"
        "        '- Evaluate technical trend/momentum/volume, fundamentals, macro/news context, and risk for the full basket.',\n"
        "        '- Rank the candidates and choose the best risk-adjusted buy/sell/reduce setup, or hold the basket.',\n"
        "        '- Before any buy/sell/reduce, call risk_check and then trade_intent_submit if allowed.',\n"
        "        '- Hold when confidence is below policy.min_confidence or evidence conflicts.',\n"
        "        '- IMPORTANT: every fresh entry trade_intent_submit MUST include a `protection`',\n"
        "        '  block with `stop_loss` and `take_profit` so the bracket arms atomically.',\n"
        "        '  Use sizing-aware stops (e.g. 2*ATR or 1-3% pct) and 2-3R targets.',\n"
        "        '  Example bracket payload: `protection: {stop_loss: {type: pct, value: 0.02},',\n"
        "        '  take_profit: {type: pct, value: 0.05}}`. For closes use',\n"
        "        '  `plan_action: close_position` and omit `protection` (release bracket).',\n"
        "        '',\n"
        "        'Suggested team_run roles JSON:',\n"
        "        json.dumps(roles, ensure_ascii=False, indent=2),\n"
        "        '',\n"
        "        'Policy:',\n"
        "        json.dumps({\n"
        "            'min_confidence': ctx.policy.min_confidence,\n"
        "            'default_order_usd': ctx.policy.default_order_usd,\n"
        "            'max_single_order_usd': ctx.policy.max_single_order_usd,\n"
        "            'max_daily_notional_usd': ctx.policy.max_daily_notional_usd,\n"
        "            'mode': ctx.config.mode,\n"
        "        }, ensure_ascii=False, indent=2),\n"
        "        '',\n"
        "        'Technical snapshots already gathered by the strategy facade:',\n"
        "        json.dumps(snapshots, ensure_ascii=False, indent=2, default=str),\n"
        "        '',\n"
        "        'Final response contract:',\n"
        "        json.dumps({\n"
            "            'decision': 'buy|sell|reduce|hold',\n"
            "            'confidence': 0.0,\n"
            "            'team_run_id': '<id from team_run>',\n"
        "            'selected_market': '<best candidate or null>',\n"
        "            'ranked_candidates': [{'market': '<symbol>', 'rank': 1, 'reason': '...'}],\n"
            "            'account_id': account_id,\n"
            "            'technical': 'summary',\n"
            "            'fundamental': 'summary',\n"
        "            'macro_news': 'summary',\n"
        "            'risk': 'summary',\n"
        "            'action_taken': 'none|risk_check|trade_intent_submit',\n"
        "            'reasoning': ['evidence-backed bullet'],\n"
        "        }, ensure_ascii=False, indent=2),\n"
        "    ])\n"
        "    return StrategyAgentTask.dispatch(\n"
        "        prompt=prompt,\n"
        "        session_key={'markets': markets, 'timeframe': timeframe},\n"
        "        metadata={\n"
        "            'market': market,\n"
        "            'markets': markets,\n"
        "            'timeframe': timeframe,\n"
        "            'account_id': account_id,\n"
            "            'roles': list(_TEAM_ROLES),\n"
        "            'execution_mode': 'agent_team',\n"
        "        },\n"
        "        attached_skills=['team', 'trading', 'market_research', 'research', 'market_data_routing'],\n"
        "        reason='scheduled Agent Team market-analysis task',\n"
        "    )\n\n"
        "\n"
        "def _technical_score(ctx: StrategyContext, market: str, timeframe: str) -> dict:\n"
        "    candles = ctx.market.candles(market, timeframe=timeframe, limit=80)\n"
        "    closes = [float(row.get('close') or 0.0) for row in candles]\n"
        "    if len(closes) < 21:\n"
        "        return {'market': market, 'score': 0.0, 'rsi': 50.0, 'reason': 'insufficient_history'}\n"
        "    sma_20 = sum(closes[-20:]) / 20.0\n"
        "    ret_3 = (closes[-1] - closes[-4]) / closes[-4] if len(closes) >= 4 and closes[-4] else 0.0\n"
        "    trend = (closes[-1] - sma_20) / sma_20 if sma_20 else 0.0\n"
        "    rsi = _rsi(closes, 14)\n"
        "    overbought_penalty = max(0.0, rsi - 70.0) / 100.0\n"
        "    score = ret_3 + trend * 0.5 - overbought_penalty\n"
        "    return {'market': market, 'score': score, 'rsi': rsi, 'ret_3': ret_3, 'trend': trend}\n\n"
        "\n"
        "def _rsi(values, window):\n"
        "    if len(values) <= window:\n"
        "        return 50.0\n"
        "    gains = []\n"
        "    losses = []\n"
        "    for i in range(-window, 0):\n"
        "        delta = values[i] - values[i - 1]\n"
        "        gains.append(max(delta, 0.0))\n"
        "        losses.append(abs(min(delta, 0.0)))\n"
        "    avg_gain = sum(gains) / window\n"
        "    avg_loss = sum(losses) / window\n"
        "    if avg_loss == 0:\n"
        "        return 100.0 if avg_gain > 0 else 50.0\n"
        "    rs = avg_gain / avg_loss\n"
        "    return 100.0 - (100.0 / (1.0 + rs))\n\n"
        "\n"
        "def _safe_technical_snapshot(ctx: StrategyContext, market: str, timeframe: str) -> dict:\n"
        "    snapshot = {'market': market, 'timeframe': timeframe}\n"
        "    try:\n"
        "        snapshot['features'] = ctx.market.features(market, timeframe=timeframe, lookback=160)\n"
        "    except Exception as exc:\n"
        "        snapshot['features_error'] = f'{type(exc).__name__}: {exc}'\n"
        "    try:\n"
        "        candles = ctx.market.candles(market, timeframe=timeframe, limit=40)\n"
        "        snapshot['candles_count'] = len(candles)\n"
        "        snapshot['recent_candles'] = list(candles[-12:])\n"
        "    except Exception as exc:\n"
        "        snapshot['candles_error'] = f'{type(exc).__name__}: {exc}'\n"
        "    try:\n"
        "        snapshot['ticker'] = ctx.market.ticker(market)\n"
        "    except Exception as exc:\n"
        "        snapshot['ticker_error'] = f'{type(exc).__name__}: {exc}'\n"
        "    return snapshot\n\n"
        "\n"
        "def _role_task(role: str, markets: list[str]) -> str:\n"
        "    key = role.lower()\n"
        "    universe = ', '.join(markets)\n"
        "    if 'technical' in key or 'quant' in key:\n"
        "        return f'Rank technical trend, indicators, momentum, volume, and levels across: {universe}.'\n"
        "    if 'fundamental' in key or 'valuation' in key:\n"
        "        return f'Compare fundamentals, valuation, earnings, and moat across: {universe}.'\n"
        "    if 'macro' in key:\n"
        "        return f'Analyze macro, rates, sector, and risk regime implications for the basket: {universe}.'\n"
        "    if 'news' in key or 'sentiment' in key:\n"
        "        return f'Analyze recent news, filings, sentiment, and event risk for the basket: {universe}. If a dedicated news skill is unavailable, use research or websearch; do not stop at a missing optional skill.'\n"
        "    if 'risk' in key or 'critic' in key:\n"
        "        return f'Challenge selected candidate, sizing, invalidation, concentration, and downside risks for: {universe}.'\n"
        "    return f'Contribute an evidence-backed basket view for: {universe}.'\n"
    )


_MAIN_TEMPLATES = {
    "scalping": _scalping_template,
    "trend": _trend_template,
    "news": _news_template,
}


_CONTRACT_TEST_TEMPLATE = (
    '"""Smoke test that the strategy package imports cleanly.\n\n'
    "The validator already runs this check; the file lives in the\n"
    "package so operators can run ``pytest`` against the package on\n"
    "their own.\n"
    '"""\n\n'
    "from __future__ import annotations\n\n"
    "\n"
    "def test_strategy_imports():\n"
    "    import importlib.util\n"
    "    import sys\n"
    "    from pathlib import Path\n"
    "\n"
    '    main_path = Path(__file__).resolve().parent.parent / "main.py"\n'
    '    spec = importlib.util.spec_from_file_location("_smoke_{strategy_id}", main_path)\n'
    "    assert spec is not None and spec.loader is not None\n"
    "    module = importlib.util.module_from_spec(spec)\n"
    "    sys.modules[spec.name] = module\n"
    "    spec.loader.exec_module(module)\n"
    '    assert callable(getattr(module, "run", None))\n'
)


def _default_subagent_prompt(
    *,
    name: str,
    strategy_id: str,
    strategy_class: str,
    markets: Iterable[str],
) -> str:
    market_list = ", ".join(markets) or "<markets>"
    return (
        f"# {name}\n\n"
        f"Strategy: `{strategy_id}` ({strategy_class}); markets: {market_list}.\n\n"
        "## Role\n\n"
        f"You are the `{name}` subagent. Return a structured trade recommendation;\n"
        "the strategy runner decides whether to execute it.\n\n"
        "## Output schema\n\n"
        "```json\n"
        "{\n"
        '  "recommendation": "buy|sell|hold|reduce|avoid",\n'
        '  "confidence": 0.0,\n'
        '  "time_horizon": "minutes|hours|days",\n'
        f'  "market": "{next(iter(markets), "<market>")}",\n'
        '  "thesis": "...",\n'
        '  "invalidation": "...",\n'
        '  "risk_flags": [],\n'
        '  "evidence": [{ "source": "...", "summary": "..." }]\n'
        "}\n"
        "```\n\n"
        "## Constraints\n\n"
        "* Never place orders.\n"
        "* Cite evidence sources you actually used.\n"
        "* Respect the strategy's `policy.min_confidence`.\n"
    )


def _tuning_subagent_prompt(req: StrategyGenerationRequest) -> str:
    objectives = ", ".join(req.tuning_objectives) or "risk_adjusted_return"
    custom = req.tuning_prompt.strip() or "Focus on improving risk-adjusted return."
    return (
        "# strategy_tuner\n\n"
        f"Strategy: `{req.strategy_id}` ({req.strategy_class}).\n"
        f"Objectives: {objectives}.\n\n"
        "## Role\n\n"
        "You are this strategy's tuning subagent. Review recent runs, propose\n"
        "code/config/prompt changes that improve the objective, and return a\n"
        "structured proposal. **Do not** mutate live files; the runner converts\n"
        "your output into a `PatchProposal`.\n\n"
        "The payload includes `performance.market_context` with recent K-line\n"
        "tails and computed indicators, plus `performance.news_context` with\n"
        "recent matched news when available. Cite those fields when they affect\n"
        "your recommendation; explicitly say when data is unavailable or degraded.\n\n"
        "## Operator brief\n\n"
        f"{custom}\n\n"
        "## Output schema\n\n"
        "```json\n"
        "{\n"
        '  "summary": "...",\n'
        '  "evidence": [{ "source": "strategy_runs", "finding": "..." }],\n'
        '  "proposed_changes": [\n'
        '    {"file": "main.py", "kind": "code_patch", "rationale": "..."},\n'
        '    {"file": "strategy.yml", "kind": "config_patch", "rationale": "..."}\n'
        "  ],\n"
        '  "expected_effect": {"return": "neutral_or_better", "drawdown": "lower"},\n'
        '  "validation_plan": ["unit", "fixture_replay", "backtest", "shadow_run"],\n'
        '  "risk_flags": []\n'
        "}\n"
        "```\n"
    )


# ---------------------------------------------------------------------------
# Proposal helpers
# ---------------------------------------------------------------------------


def _rationale(req: StrategyGenerationRequest) -> str:
    parts = [
        f"# Strategy package: {req.strategy_id}",
        "",
        f"Class: **{req.strategy_class}**, mode: **{req.mode}**.",
        f"Markets: {', '.join(req.markets)}.",
        f"Accounts: {', '.join(req.accounts)}.",
    ]
    if req.prompt.strip():
        parts.extend(["", "## Operator prompt", "", req.prompt.strip()])
    return "\n".join(parts) + "\n"


def _test_plan(
    req: StrategyGenerationRequest,
    validation: Optional[StrategyValidation],
) -> str:
    parts = [
        "# Test plan",
        "",
        "1. Validator passes (see `validation_report.json`).",
        "2. Run `nerya strategy validate {sid}` after promotion.".format(
            sid=req.strategy_id
        ),
        "3. Run one paper tick: `nerya strategy run {sid} --dry-run`.".format(
            sid=req.strategy_id
        ),
    ]
    if validation is not None and not validation.ok:
        parts.append("")
        parts.append("## Outstanding blockers")
        for issue in validation.blockers:
            parts.append(f"* `{issue.code}`: {issue.message} ({issue.where})")
    if validation is not None and validation.warnings:
        parts.append("")
        parts.append("## Warnings")
        for issue in validation.warnings:
            parts.append(f"* `{issue.code}`: {issue.message} ({issue.where})")
    return "\n".join(parts) + "\n"


def _rollback(req: StrategyGenerationRequest) -> str:
    return (
        "# Rollback\n\n"
        f"Disable the trading schedule for `{req.strategy_id}` "
        "(`nerya strategy pause`), then remove the strategy package "
        f"directory `workspace/strategies/{req.strategy_id}/` "
        "(or restore the previous version from `versions/`).\n"
    )


def _json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, indent=2, default=str)


__all__ = [
    "StrategyCodeGenerator",
    "StrategyGenerationRequest",
    "StrategyGenerationResult",
]

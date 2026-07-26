"""Native tools for the agent-generated strategy runtime.

These tools let the agent author and operate strategy packages from
inside the loop:

* :func:`strategy_generate_proposal_handler` — wrap
  :class:`~nerya.evolution.strategy_code_generator.StrategyCodeGenerator`
  and return the proposal id + validation outcome.
* :func:`strategy_validate_handler` — re-run the validator against a
  promoted package (or against an in-flight proposal's ``after/``
  files when a ``proposal_id`` is given).
* :func:`strategy_promote_handler` — flip a strategy proposal to
  ``approved`` and apply it via :func:`nerya.evolution.promotion.apply_proposal`.
* :func:`strategy_run_tick_handler` — invoke
  :class:`~nerya.strategies.runner.StrategyRunner` for a single tick;
  the returned record is what the dashboard's *Runs & Trades* panel
  pulls from.
* :func:`strategy_kill_switch_handler` — set / clear / inspect the
  per-strategy kill switch.
* :func:`strategy_run_history_handler` — list recent strategy runs
  with paging.

Why a separate module
---------------------
The "trading" native tools (:mod:`nerya.tools.native.trading`) drive
the safety-critical `submit_intent` pipeline; the strategy runtime
tools drive *the package's* lifecycle (generation → validation →
promotion → execution). Keeping them apart means the trading bundle
keeps its narrow surface, and the strategy bundle can grow with the
runtime without dragging the trading deps along.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any, Optional

from ...core import yaml_io
from ...core.config import Config
from ...core.errors import NeryaError, TradingError
from ...evolution.patch_proposal import (
    create_proposal,
    delete_proposal,
    list_proposals,
    set_state,
)
from ...evolution.promotion import apply_proposal
from ...evolution.strategy_code_generator import (
    StrategyCodeGenerator,
    StrategyGenerationRequest,
)
from ...evolution.strategy_tuning_generator import (
    StrategyTuningGenerationRequest,
    StrategyTuningGenerator,
)
from ...strategies.evolution import StrategyEvolutionRunner
from ...strategies.package import load_package
from ...trading.accounts import load_account_profiles
from ...strategies.proposal_files import read_proposal_strategy_files
from ...strategies.scheduler_bridge import apply_strategy_schedules
from ...strategies.performance import build_snapshot
# NOTE: ``StrategyRunner`` is imported lazily inside the handlers below
# to break a transitive import cycle introduced when the agent kernel
# loads via ``nerya.charting`` (the kernel imports
# ``nerya.tools.native``, which used to pull in ``StrategyRunner`` →
# ``nerya.strategies.context`` while ``context`` was still being
# initialised by the strategies package init). Keeping the import lazy
# means the cycle only resolves when the tool is actually invoked, by
# which point ``nerya.strategies.context`` has finished loading.
# from ...strategies.runner import StrategyRunner  # noqa: ERA001 (lazy import)
from ...strategies.state import StrategyKillSwitch, StrategyRunStore
from ...strategies.validator import (
    StrategyValidation,
    validate_proposal_files,
    validate_strategy_package,
)
from ..tool_errors import schema_validation_result as _usage_error
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


STRATEGY_GENERATE_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {
            "type": "string",
            "description": "Lowercase identifier; matches ^[a-z][a-z0-9_]+$.",
        },
        "title": {"type": "string"},
        "description": {"type": "string"},
        "strategy_class": {
            "type": "string",
            "enum": ["scalping", "trend", "news", "agent", "agent_task", "agent_team"],
            "default": "scalping",
            "description": (
                "Strategy family. `agent_task` is accepted as an alias for "
                "`strategy_class=agent` plus `execution_mode=agent_task`."
            ),
        },
        "execution_mode": {
            "type": "string",
            "enum": ["script", "agent", "agent_task", "agent_team", "team"],
            "description": (
                "Explicit runtime mode. `script` runs main.py directly; "
                "`agent` lets main.py build a StrategyAgentTask prompt; "
                "`agent_team` schedules the Agent Team research/trade path."
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["paper", "shadow", "live"],
            "default": "paper",
        },
        "markets": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "VENUE:SYMBOL market ids. Use BYBIT_PERPETUAL:SOLUSDT for "
                "Bybit linear/perpetual/swap/futures contracts; use "
                "BYBIT:SOLUSDT only for Bybit spot."
            ),
        },
        "accounts": {"type": "array", "items": {"type": "string"}},
        "files.main.py": {
            "type": "string",
            "description": (
                "Compact top-level alias for files['main.py']. Prefer this "
                "over a large nested files object when authoring custom SDK "
                "strategies through OpenAI-compatible providers."
            ),
        },
        "files.strategy.md": {
            "type": "string",
            "description": (
                "Compact top-level alias for files['strategy.md']. Keep it "
                "concise; detailed tuning notes can be added after the "
                "proposal exists."
            ),
        },
        "prompt": {
            "type": "string",
            "description": (
                "Concise operator brief; lands in strategy.md. Keep this short "
                "when files are supplied, and put runnable logic in SDK files."
            ),
        },
        "schedule_cron": {"type": "string"},
        "schedule_every_seconds": {"type": "integer", "minimum": 1},
        "news_sources": {"type": "array", "items": {"type": "string"}},
        "subagents": {"type": "array", "items": {"type": "string"}},
        "policy_overrides": {"type": "object"},
        "llm_policy_overrides": {"type": "object"},
        "create_tuning": {
            "type": "boolean",
            "default": True,
            "description": (
                "When true (the default), the package ships a per-strategy "
                "self-evolution lane: a strategy_tuner subagent prompt, a "
                "tuning cron row, and a strategy.yml `tuning` block. Only "
                "set this to false if the operator explicitly wants a "
                "static strategy."
            ),
        },
        "tuning_prompt": {
            "type": "string",
            "description": (
                "Reflection / tuner prompt body for "
                "subagents/strategy_tuner.agent.md. The auto-evolution "
                "loop loads this each tuning cycle. Provide a real "
                "rubric: what counts as 'good performance', what "
                "knobs the tuner is allowed to propose, and what "
                "guardrails it must respect."
            ),
        },
        "tuning_cron": {
            "type": "string",
            "description": (
                "Cron expression for the tuning lane (default '0 */6 * * *'). "
                "Use a longer cycle than the trading tick — e.g. "
                "'0 8 * * *' for daily reflection."
            ),
        },
        "tuning_objectives": {"type": "array", "items": {"type": "string"}},
        "extra_subagent_prompts": {
            "type": "object",
            "description": "Map of subagent name -> agent.md body.",
        },
        "files": {
            "type": "object",
            "description": (
                "Optional inline file overrides keyed by package-relative "
                "path (e.g. 'main.py', 'tests/test_main.py', "
                "'strategy.md'). Values fully replace the default "
                "template for that path. Use this only when you can keep "
                "main.py inside the StrategyContext contract: read candles "
                "through ctx.market.candles/features, read positions through "
                "ctx.portfolio.positions, place orders through ctx.trading, "
                "and return ctx.result/StrategyResult. Do not use native "
                "market_data tool shapes such as get_candles, legacy "
                "portfolio.get_positions/get_account, ctx.account_id, or raw "
                "order-list returns."
            ),
            "additionalProperties": {"type": "string"},
        },
        "validate": {"type": "boolean", "default": True},
    },
    "required": ["strategy_id", "markets", "accounts"],
}


STRATEGY_VALIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {"type": "string"},
        "proposal_id": {
            "type": "string",
            "description": (
                "Validate the in-flight proposal's after/ files instead of "
                "the promoted package."
            ),
        },
    },
}


STRATEGY_DELETE_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proposal_id": {
            "type": "string",
            "description": (
                "The prp_* id of the pending strategy package (or tuning) "
                "proposal to delete."
            ),
        },
        "force": {
            "type": "boolean",
            "default": False,
            "description": (
                "Delete even an already-applied proposal record. Off by "
                "default; applied proposals are the audit trail of a change "
                "that already landed in the workspace."
            ),
        },
    },
    "required": ["proposal_id"],
}


STRATEGY_DRAFT_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {
            "type": "string",
            "description": "Lowercase identifier; matches ^[a-z][a-z0-9_]+$.",
        },
        "title": {"type": "string"},
        "description": {"type": "string"},
        "prompt": {
            "type": "string",
            "description": (
                "Concise operator brief; lands in strategy.md. Keep it short — "
                "you will author the runnable logic by editing the scaffolded "
                "files, not by passing code here."
            ),
        },
        "strategy_class": {
            "type": "string",
            "enum": ["scalping", "trend", "news", "agent", "agent_task", "agent_team"],
            "default": "scalping",
            "description": (
                "Strategy family used to pick the scaffold template. "
                "`agent_task` is an alias for `strategy_class=agent` plus "
                "`execution_mode=agent_task`."
            ),
        },
        "execution_mode": {
            "type": "string",
            "enum": ["script", "agent", "agent_task", "agent_team", "team"],
            "description": (
                "Explicit runtime mode for the scaffold. `script` runs main.py "
                "directly; `agent` lets main.py build a StrategyAgentTask; "
                "`agent_team` schedules the Agent Team path."
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["paper", "shadow", "live"],
            "default": "paper",
        },
        "markets": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "VENUE:SYMBOL market ids. Use BYBIT_PERPETUAL:SOLUSDT for "
                "Bybit linear/perpetual/swap/futures contracts; use "
                "BYBIT:SOLUSDT only for Bybit spot."
            ),
        },
        "accounts": {"type": "array", "items": {"type": "string"}},
        "schedule_cron": {"type": "string"},
        "schedule_every_seconds": {"type": "integer", "minimum": 1},
        "news_sources": {"type": "array", "items": {"type": "string"}},
        "subagents": {"type": "array", "items": {"type": "string"}},
        "policy_overrides": {"type": "object"},
        "llm_policy_overrides": {"type": "object"},
        "create_tuning": {"type": "boolean", "default": True},
        "tuning_prompt": {"type": "string"},
        "tuning_cron": {"type": "string"},
        "tuning_objectives": {"type": "array", "items": {"type": "string"}},
        "extra_subagent_prompts": {
            "type": "object",
            "description": "Map of subagent name -> agent.md body.",
        },
        "from_strategy_id": {
            "type": "string",
            "description": (
                "Seed the draft from an already-promoted strategy package "
                "instead of stock templates. Use this to iterate on an "
                "existing strategy: the live files are copied into the "
                "proposal's after/strategies/<id>/ tree so you can edit and "
                "re-submit them without mutating the live workspace."
            ),
        },
    },
    "required": ["strategy_id"],
}


STRATEGY_SUBMIT_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proposal_id": {
            "type": "string",
            "description": (
                "The prp_* id of the draft strategy proposal whose "
                "after/strategies/<id>/ files you have finished editing."
            ),
        },
        "note": {
            "type": "string",
            "description": "Optional reviewer note recorded on the proposal.",
        },
    },
    "required": ["proposal_id"],
}


STRATEGY_BACKTEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {
            "type": "string",
            "description": "Backtest an already-promoted strategy package.",
        },
        "proposal_id": {
            "type": "string",
            "description": (
                "Backtest an in-flight strategy proposal's after/ files "
                "before promotion."
            ),
        },
        "preset": {"type": "string", "default": "default"},
        "config_path": {"type": "string"},
        "allow_mock": {
            "type": "boolean",
            "default": False,
            "description": (
                "Allow mock candles when no real historical data is "
                "available. Use only for smoke/proposal verification."
            ),
        },
    },
}


STRATEGY_PROMOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proposal_id": {"type": "string"},
        "note": {"type": "string"},
        "require_backtest": {
            "type": "boolean",
            "default": True,
            "description": (
                "Refuse promotion unless the proposal already has a "
                "strategy_backtest artifact or an approved flexible "
                "meme/on-chain replay policy."
            ),
        },
        "backtest_policy": {
            "type": "string",
            "enum": ["standard_required", "flexible_meme", "operator_waiver"],
            "default": "standard_required",
            "description": (
                "standard_required accepts only normal strategy_backtest "
                "artifacts. flexible_meme accepts custom/event replay for "
                "meme or on-chain markets and otherwise requires explicit "
                "operator approval. operator_waiver requires explicit "
                "operator approval and records the standard-backtest waiver."
            ),
        },
        "operator_approved": {
            "type": "boolean",
            "default": False,
            "description": (
                "True only when the operator explicitly approves promoting "
                "without a standard backtest. The agent must not set this "
                "during an ordinary create/review request."
            ),
        },
        "approval_note": {
            "type": "string",
            "description": (
                "Required when operator_approved is true; state why the "
                "standard backtest is unsuitable or unavailable."
            ),
        },
        "operator": {"type": "string"},
    },
    "required": ["proposal_id"],
}


STRATEGY_RUN_TICK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {"type": "string"},
        "trigger_event_id": {"type": "string"},
        "trigger_payload": {"type": "object"},
        "operator": {"type": "string"},
        "note": {"type": "string"},
        "mode_override": {
            "type": "string",
            "enum": ["paper", "shadow", "live"],
        },
    },
    "required": ["strategy_id"],
}


STRATEGY_KILL_SWITCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {"type": "string"},
        "action": {
            "type": "string",
            "enum": ["get", "assert", "clear"],
            "default": "get",
        },
        "reason": {
            "type": "string",
            "description": "Required when action=='assert'.",
        },
        "by": {"type": "string"},
    },
    "required": ["strategy_id"],
}


STRATEGY_RUN_HISTORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "default": 50},
    },
    "required": ["strategy_id"],
}


STRATEGY_TUNING_GENERATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {"type": "string"},
        "prompt": {"type": "string"},
        "cron": {"type": "string", "default": "0 */6 * * *"},
        "every_seconds": {"type": "integer", "minimum": 1},
        "objectives": {"type": "array", "items": {"type": "string"}},
        "require_backtest": {"type": "boolean", "default": True},
        "require_shadow_run": {"type": "boolean", "default": False},
    },
    "required": ["strategy_id"],
}


STRATEGY_TUNING_RUN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {"type": "string"},
        "dry_run": {"type": "boolean", "default": False},
        "operator": {"type": "string"},
        "note": {"type": "string"},
        "trigger_event_id": {"type": "string"},
        "evidence_run_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "evidence_session_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["strategy_id"],
}


STRATEGY_TUNING_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {"type": "string"},
        "lookback_runs": {"type": "integer", "minimum": 1, "default": 200},
    },
    "required": ["strategy_id"],
}


STRATEGY_TUNING_SNAPSHOT_SCHEMA = STRATEGY_TUNING_STATUS_SCHEMA


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _execution_error(call: ToolCall, message: str) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message=message),
    )


def _strategy_validation_blockers_error(
    call: ToolCall,
    *,
    strategy_id: str,
    validation: StrategyValidation,
    files: list[str],
) -> ToolResult:
    """Return the create-time validation blockers without a pending proposal.

    The package failed validation, so no proposal was written. We hand the
    structured blockers back to the agent as a schema-validation error so the
    loop fixes ``files`` and re-calls ``strategy_generate_proposal``; the
    proposal only enters the pending-review queue once validation passes.
    """

    blockers = validation.blockers
    lines: list[str] = []
    for issue in blockers:
        where = f" [{issue.where}]" if issue.where else ""
        lines.append(f"- {issue.message}{where}")
    detail = "\n".join(lines) if lines else "- (no blocker detail reported)"
    message = (
        "strategy_generate_proposal did not create a pending proposal: the "
        f"generated package for {strategy_id!r} has {len(blockers)} validation "
        "blocker(s). Fix the strategy files and call strategy_generate_proposal "
        "again with the corrected `files`; the proposal only enters the "
        "pending-review queue once validation passes.\n"
        f"Blockers:\n{detail}"
    )
    return _usage_error(
        call,
        message,
        recovery_hint={
            "action": "fix_validation_blockers_and_retry",
            "tool_name": "strategy_generate_proposal",
            "strategy_id": strategy_id,
            "files": files,
            "validation": validation.asdict(),
            "blockers": [issue.asdict() for issue in blockers],
        },
    )


def _manifest_execution_mode(content: str) -> str:
    try:
        raw = yaml_io.loads(content, default={}) or {}
    except Exception:
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("execution_mode") or "").strip().lower().replace("-", "_")


def _auto_bind_default_accounts(
    paths: Any, request: StrategyGenerationRequest
) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    """Resolve default accounts for a new draft when the caller omitted them.

    Returns ``(resolved_account_ids, available_accounts)``. We auto-bind every
    *active, non-real-money* account whose mode matches the request and whose
    venue matches one of the requested markets — this saves the agent an extra
    ``account_list`` round-trip on the common paper-draft flow. Real-money
    modes (live/canary) are never auto-bound; the caller falls back to a
    directive error that lists the available accounts so the agent can pick in
    a single step. ``available_accounts`` is always returned (even on success)
    so callers can surface it in recovery hints.
    """
    try:
        profiles = load_account_profiles(paths)
    except Exception:  # pragma: no cover - defensive: missing/corrupt roster
        return (), []
    req_mode = (request.mode or "paper").strip().lower()
    venues = {
        str(market).split(":")[0].strip().lower()
        for market in request.markets
        if str(market).strip()
    }
    venues.discard("")
    available: list[dict[str, str]] = []
    matches: list[str] = []

    def venue_matches(profile_venue: str) -> bool:
        if not venues:
            return True
        if profile_venue in venues:
            return True
        if profile_venue == "bybit" and venues.intersection(
            {"bybit_perpetual", "bybit_perp", "bybit_linear"}
        ):
            return True
        return False

    for profile in profiles.values():
        if str(getattr(profile, "status", "")).strip().lower() != "active":
            continue
        p_mode = str(getattr(profile, "mode", "")).strip().lower()
        p_venue = str(getattr(profile, "venue", "")).strip().lower()
        available.append({"id": profile.id, "venue": p_venue, "mode": p_mode})
        # Only auto-bind safe, mode-matched accounts whose venue is in scope.
        if getattr(profile, "is_real_money", False):
            continue
        if p_mode != req_mode:
            continue
        if not venue_matches(p_venue):
            continue
        matches.append(profile.id)
    matches.sort()
    available.sort(key=lambda row: str(row.get("id") or ""))
    return tuple(matches), available


def _missing_account_error(
    call: ToolCall,
    *,
    request: StrategyGenerationRequest,
    available: list[dict[str, str]],
) -> ToolResult:
    req_mode = (request.mode or "paper").strip().lower()
    venues = sorted(
        {
            str(market).split(":")[0].strip().lower()
            for market in request.markets
            if str(market).strip()
        }
        - {""}
    )
    if available:
        listing = ", ".join(
            f"{row['id']} ({row['venue']}/{row['mode']})" for row in available[:8]
        )
        avail_text = f"Available accounts: {listing}."
    else:
        avail_text = (
            "No accounts are configured yet — create a paper one with "
            "account_upsert."
        )
    venue_text = f" for venue(s) {venues}" if venues else ""
    return _usage_error(
        call,
        (
            "strategy_draft_proposal needs an account for the new strategy. No "
            f"active {req_mode} account matched the requested market(s){venue_text}. "
            f"{avail_text} Pass accounts=[...] (or from_strategy_id to iterate "
            "an existing strategy)."
        ),
        recovery_hint={
            "action": "select_account_and_retry",
            "tool_name": "strategy_draft_proposal",
            "requested_mode": req_mode,
            "requested_venues": venues,
            "available_accounts": available,
        },
    )


def _request_from_args(args: dict[str, Any]) -> StrategyGenerationRequest:
    args = _normalise_raw_strategy_args(args)
    args = _normalise_strategy_market_venues(args)
    strategy_class = str(args.get("strategy_class") or "scalping").strip().lower()
    execution_mode = str(args.get("execution_mode") or "").strip().lower()
    if strategy_class == "agent_task":
        strategy_class = "agent"
        execution_mode = execution_mode or "agent_task"
    elif strategy_class == "custom":
        execution_key = execution_mode.replace("-", "_")
        strategy_class = (
            "agent"
            if execution_key in {"agent", "agent_task", "agent_team", "team"}
            else "trend"
        )
    return StrategyGenerationRequest(
        strategy_id=str(args.get("strategy_id") or "").strip(),
        title=str(args.get("title") or ""),
        description=str(args.get("description") or ""),
        prompt=str(args.get("prompt") or ""),
        strategy_class=strategy_class,
        execution_mode=execution_mode,
        mode=str(args.get("mode") or "paper").strip().lower(),
        markets=tuple(str(m) for m in (args.get("markets") or ())),
        accounts=tuple(str(a) for a in (args.get("accounts") or ())),
        schedule_cron=str(args.get("schedule_cron") or "").strip(),
        schedule_every_seconds=(
            int(args.get("schedule_every_seconds"))
            if args.get("schedule_every_seconds") is not None
            else None
        ),
        news_sources=tuple(str(s) for s in (args.get("news_sources") or ())),
        subagents=tuple(str(s) for s in (args.get("subagents") or ())),
        policy_overrides=dict(args.get("policy_overrides") or {}),
        llm_policy_overrides=dict(args.get("llm_policy_overrides") or {}),
        create_tuning=bool(args.get("create_tuning", True)),
        tuning_prompt=str(args.get("tuning_prompt") or ""),
        tuning_cron=str(args.get("tuning_cron") or "0 */6 * * *"),
        tuning_objectives=tuple(
            str(o) for o in (args.get("tuning_objectives") or ())
        ),
        extra_subagent_prompts=dict(args.get("extra_subagent_prompts") or {}),
        files={
            str(k): str(v)
            for k, v in (args.get("files") or {}).items()
            if isinstance(k, str)
        },
    )


def _normalise_strategy_market_venues(args: dict[str, Any]) -> dict[str, Any]:
    markets = args.get("markets")
    if not isinstance(markets, list):
        return args
    text = " ".join(
        str(args.get(key) or "")
        for key in ("title", "description", "prompt", "strategy_id")
    ).lower()
    wants_bybit_perp = (
        "bybit" in text
        and any(token in text for token in ("perp", "perpetual", "linear", "swap", "futures", "contract"))
    )
    changed = False
    fixed: list[str] = []
    for market in markets:
        value = str(market)
        if wants_bybit_perp and value.upper().startswith("BYBIT:"):
            fixed.append("BYBIT_PERPETUAL:" + value.split(":", 1)[1])
            changed = True
        elif value.upper().startswith("BYREAL:"):
            fixed_value = _normalise_byreal_market(value)
            fixed.append(fixed_value)
            changed = changed or fixed_value != value
        else:
            fixed.append(value)
    if not changed:
        return args
    out = dict(args)
    out["markets"] = fixed
    return out


_BYREAL_GENERIC_MARKET_TAILS = {
    "",
    "sol",
    "solana",
    "meme",
    "memes",
    "meme_pool",
    "meme_pools",
    "sol_meme_pool",
    "sol_memepool",
    "new_pool",
    "new_pools",
    "new-pool",
    "new-pools",
    "pool",
    "pools",
    "scan",
    "scanner",
    "universe",
}


def _normalise_byreal_market(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return raw
    upper = raw.upper()
    if upper.startswith("BYREAL_ONCHAIN:"):
        return "BYREAL_ONCHAIN:" + raw.split(":", 1)[1]
    if not upper.startswith("BYREAL:"):
        return raw
    tail = raw.split(":", 1)[1].strip() if ":" in raw else ""
    tail_key = tail.strip().lower().replace("-", "_")
    if (
        tail_key in _BYREAL_GENERIC_MARKET_TAILS
        or "meme_pool" in tail_key
        or "new_pool" in tail_key
    ):
        return "BYREAL_ONCHAIN:solana"
    if tail.lower().startswith("solana:"):
        return "BYREAL_ONCHAIN:" + tail
    return "BYREAL_ONCHAIN:solana:" + tail


def _normalise_raw_strategy_args(args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("_raw") if isinstance(args, dict) else None
    if not isinstance(raw, str):
        return args
    raw = raw.strip()
    if not raw.startswith("{"):
        return args
    parsed = _parse_raw_strategy_args(raw)
    truncated_fields: list[str] = []
    if parsed is None:
        recovered = _recover_truncated_raw_strategy_args(raw)
        if recovered is not None:
            parsed = recovered
            truncated_fields = [
                key
                for key in ("files", "files.main.py", "files.strategy.md")
                if f'"{key}"' in raw and key not in recovered
            ]
    if parsed is None:
        return args
    merged = dict(args)
    merged.pop("_raw", None)
    merged.update(parsed)
    if truncated_fields:
        merged["_provider_raw_truncated"] = True
        merged["_provider_raw_truncated_fields"] = truncated_fields
    return merged


def _parse_raw_strategy_args(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _recover_truncated_raw_strategy_args(raw: str) -> dict[str, Any] | None:
    """Recover provider-truncated JSON tool args without inventing fields."""

    cut = raw.rfind(",")
    attempts = 0
    while cut > 0 and attempts < 20:
        attempts += 1
        candidate = raw[:cut].rstrip() + "}"
        parsed = _parse_raw_strategy_args(candidate)
        if isinstance(parsed, dict) and all(
            parsed.get(key) for key in ("strategy_id", "markets", "accounts")
        ):
            return parsed
        cut = raw.rfind(",", 0, cut)
    return None


def _request_mentions_markers(
    request: StrategyGenerationRequest,
    markers: tuple[str, ...],
) -> bool:
    body = "\n".join(
        [
            request.strategy_id,
            request.title,
            request.description,
            request.prompt,
            request.strategy_class,
            request.execution_mode,
            *request.markets,
        ]
    ).lower()
    return _text_contains_marker(body, markers)


def _text_mentions_markers(text: str, markers: tuple[str, ...]) -> bool:
    return _text_contains_marker(str(text or "").lower(), markers)


def _text_contains_marker(lowered: str, markers: tuple[str, ...]) -> bool:
    tokens: set[str] | None = None
    for marker in markers:
        if marker in _TOKEN_MATCH_ONLY_MARKERS:
            if tokens is None:
                tokens = _lower_tokens(lowered)
            if marker in tokens:
                return True
            continue
        if marker in lowered:
            return True
    return False


def _lower_tokens(lowered: str) -> set[str]:
    token_chars: list[str] = []
    tokens: set[str] = set()
    for char in lowered:
        if char.isalnum() or char == "_":
            token_chars.append(char)
            continue
        if token_chars:
            tokens.add("".join(token_chars))
            token_chars.clear()
    if token_chars:
        tokens.add("".join(token_chars))
    return tokens


def _request_mentions_hard_to_replay(request: StrategyGenerationRequest) -> bool:
    return _request_mentions_markers(request, _HARD_TO_REPLAY_MARKERS)


def _request_mentions_custom_signal_logic(request: StrategyGenerationRequest) -> bool:
    return _request_mentions_markers(request, _CUSTOM_SIGNAL_MARKERS)


def _request_mentions_agent_decision(request: StrategyGenerationRequest) -> bool:
    body = "\n".join(
        [
            request.strategy_id,
            request.title,
            request.description,
            request.prompt,
            *request.markets,
        ]
    ).lower()
    return _text_contains_marker(body, _AGENT_DECISION_MARKERS)


def _has_package_file(request: StrategyGenerationRequest, rel_path: str) -> bool:
    wanted = rel_path.replace("\\", "/")
    return any(str(path).replace("\\", "/") == wanted for path in request.files)


def _requires_explicit_sdk_files(request: StrategyGenerationRequest) -> bool:
    """Return true when template fallback would erase the requested thesis.

    The generic generator is useful for simple CEX trend/scalping examples,
    but wallet/on-chain/custom-data strategies need the agent to author the
    SDK package logic itself. Otherwise the tool can silently turn a smart
    money prompt into the default trend template.
    """

    if not _request_mentions_hard_to_replay(request):
        return False
    has_main = _has_package_file(request, "main.py")
    has_strategy_doc = _has_package_file(
        request,
        "strategy.md",
    ) or _has_package_file(request, "README.md")
    return not (has_main and has_strategy_doc)


def _raw_payload_truncated_before_sdk_files(args: dict[str, Any]) -> bool:
    if not args.get("_provider_raw_truncated"):
        return False
    fields = args.get("_provider_raw_truncated_fields")
    if isinstance(fields, list):
        return any(str(field) in {"files", "files.main.py", "files.strategy.md"} for field in fields)
    return False


def _truncated_sdk_files_payload_error() -> str:
    return (
        "strategy_generate_proposal tool arguments were truncated before the "
        "SDK files arrived. Re-call the tool with a compact payload: include "
        "only strategy_id, title, strategy_class, execution_mode, mode, "
        "markets, accounts, plus top-level `files.main.py` and "
        "`files.strategy.md` string arguments. Omit long prompt, tuning_prompt, "
        "policy_overrides, and llm_policy_overrides until after the proposal "
        "exists."
    )


def _requires_custom_signal_main_py(
    request: StrategyGenerationRequest,
    *,
    operator_prompt: str = "",
) -> bool:
    if not _request_mentions_custom_signal_logic(request):
        return False
    if operator_prompt and not _text_mentions_markers(
        operator_prompt,
        _CUSTOM_SIGNAL_MARKERS,
    ):
        return False
    if _has_package_file(request, "main.py"):
        return False
    raw = request.execution_mode.strip().lower().replace("-", "_")
    if raw == "agent_task":
        raw = "agent"
    elif raw == "team":
        raw = "agent_team"
    if raw in {"script", "agent", "agent_team"}:
        return True
    return request.strategy_class in {"scalping", "trend", "news", "agent", "agent_team"}


def _custom_signal_main_py_coverage_error(
    request: StrategyGenerationRequest,
    *,
    operator_prompt: str = "",
) -> str:
    main_py = request.files.get("main.py", "")
    if not main_py:
        return ""
    requested = _requested_custom_signal_requirements(
        request,
        operator_prompt=operator_prompt,
    )
    if not requested:
        return ""
    main_l = main_py.lower()
    missing = [
        label
        for label, code_markers in requested
        if not _text_contains_marker(main_l, code_markers)
    ]
    if not missing:
        return ""
    return (
        "`files.main.py` is missing requested custom signal logic: "
        + ", ".join(missing)
        + ". Put the indicator/data-source reads, trigger preconditions, or "
        "StrategyAgentTask prompt inputs in main.py; strategy.md alone is not "
        "a runnable strategy contract."
    )


def _requested_custom_signal_requirements(
    request: StrategyGenerationRequest,
    *,
    operator_prompt: str = "",
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    source = operator_prompt.strip()
    if not source:
        source = "\n".join(
            [
                request.strategy_id,
                request.title,
                request.description,
                request.prompt,
                request.strategy_class,
                request.execution_mode,
                *request.markets,
            ]
        )
    source_l = source.lower()
    requested: list[tuple[str, tuple[str, ...]]] = []
    for label, request_markers, code_markers in _CUSTOM_SIGNAL_TERM_REQUIREMENTS:
        if _text_contains_marker(source_l, request_markers):
            requested.append((label, code_markers))
    return tuple(requested)


def _normalise_model_invented_strategy_scope(
    request: StrategyGenerationRequest,
    *,
    operator_prompt: str = "",
) -> StrategyGenerationRequest:
    """Undo model-invented agent mode when the operator asked for a plain strategy."""

    if not operator_prompt:
        return request
    if _text_mentions_markers(operator_prompt, _AGENT_DECISION_MARKERS):
        return request
    strategy_class = request.strategy_class.strip().lower().replace("-", "_")
    execution_mode = request.execution_mode.strip().lower().replace("-", "_")
    if strategy_class not in {"agent", "agent_task", "agent_team"} and execution_mode not in {
        "agent",
        "agent_task",
        "agent_team",
        "team",
    }:
        return request
    if request.files:
        return request
    return replace(request, strategy_class="trend", execution_mode="script")


def _agent_decision_contract_error(
    request: StrategyGenerationRequest,
    *,
    operator_prompt: str = "",
) -> str:
    if not _request_mentions_agent_decision(request):
        return ""
    if operator_prompt and not _text_mentions_markers(
        operator_prompt,
        _AGENT_DECISION_MARKERS,
    ):
        return ""
    raw = request.execution_mode.strip().lower().replace("-", "_")
    if raw == "agent_task":
        raw = "agent"
    elif raw == "team":
        raw = "agent_team"
    strategy_class = request.strategy_class.strip().lower().replace("-", "_")
    main_py = request.files.get("main.py", "")
    uses_agent_task = "StrategyAgentTask" in main_py
    if raw in {"", "agent"} and strategy_class in {"agent", "agent_task"}:
        if not main_py or uses_agent_task:
            return ""
    if raw == "agent" and uses_agent_task:
        return ""
    if raw == "agent_team" and strategy_class in {"agent", "agent_team"}:
        if not main_py or uses_agent_task:
            return ""
    return (
        "strategy requests that ask an Agent to decide, judge, arbitrate, "
        "check news, size risk, or choose skip/error paths must be packaged "
        "as an Agent-task strategy: set `strategy_class` to `agent` with "
        "`execution_mode=agent` for a single-agent decision workflow, or use "
        "`execution_mode=agent_team` when the request requires a coordinated "
        "Agent Team. `files.main.py` must return "
        "`StrategyAgentTask.dispatch(...)` when the Agent should decide, "
        "`StrategyAgentTask.skip(...)` when preconditions are not met, or "
        "`StrategyAgentTask.error(...)` for unrecoverable data errors. Do "
        "not submit script-mode `ctx.result.*` logic for an Agent-decision "
        "request."
    )


_PLACEHOLDER_BACKTEST_MARKERS = (
    "示例输出",
    "示例框架",
    "模拟回测结果",
    "模拟交易",
    "实际应",
    "placeholder",
    "demo framework",
    "synthetic",
    "fake candle",
    "random price",
)


def _sdk_files_placeholder_error(request: StrategyGenerationRequest) -> str:
    if not _request_mentions_hard_to_replay(request):
        return ""
    for path, content in request.files.items():
        rel = str(path).replace("\\", "/").lower()
        if not rel.startswith("backtests/"):
            continue
        lowered = str(content or "").lower()
        for marker in _PLACEHOLDER_BACKTEST_MARKERS:
            if marker.lower() in lowered:
                return (
                    "wallet/on-chain/meme/prediction-market strategy backtests cannot be "
                    f"placeholder or simulated examples ({marker!r} found in "
                    f"{path}). Use real wallet/on-chain evidence already "
                    "gathered in the conversation, emit a minimal "
                    "NERYA_FREEFORM_RESULT_JSON payload with equity_curve and "
                    "trades, and state limitations explicitly."
                )
    return ""


def _read_proposal_files(
    paths,
    proposal_id: str,
) -> tuple[Optional[str], dict[str, str]]:
    """Return ``(strategy_id, files)`` for ``proposal_id``.

    The proposal stores files under ``after/strategies/<strategy_id>/<rel>``;
    we strip the ``after/strategies/<id>/`` prefix so the validator
    sees the same shape it does for promoted packages.
    """

    return read_proposal_strategy_files(paths, proposal_id)


def _normalise_bybit_perpetual_manifest_files(
    paths,
    *,
    proposal_id: str,
    strategy_id: str,
    files: dict[str, str],
) -> dict[str, str]:
    manifest_text = files.get("strategy.yml")
    if not manifest_text:
        return files
    try:
        manifest = yaml_io.loads(manifest_text, default={}) or {}
    except Exception:
        return files
    if not isinstance(manifest, dict):
        return files
    text = " ".join(
        str(manifest.get(key) or "")
        for key in ("strategy_id", "title", "description", "strategy_class", "execution_mode")
    )
    strategy_md = files.get("strategy.md")
    if strategy_md:
        text += " " + strategy_md
    lowered = text.lower()
    wants_bybit_perp = (
        "bybit" in lowered
        and any(token in lowered for token in ("perp", "perpetual", "linear", "swap", "futures", "contract"))
    )
    markets = manifest.get("markets")
    if isinstance(markets, str):
        values = [markets]
    elif isinstance(markets, list):
        values = [str(item) for item in markets]
    else:
        values = []
    changed = False
    fixed: list[str] = []
    for market in values:
        if wants_bybit_perp and market.upper().startswith("BYBIT:"):
            fixed.append("BYBIT_PERPETUAL:" + market.split(":", 1)[1])
            changed = True
        elif market.upper().startswith("BYREAL:"):
            fixed_value = _normalise_byreal_market(market)
            fixed.append(fixed_value)
            changed = changed or fixed_value != market
        else:
            fixed.append(market)
    if not changed:
        return files
    manifest["markets"] = fixed
    updated = yaml_io.dumps(manifest)
    out = dict(files)
    out["strategy.yml"] = updated
    manifest_path = (
        paths.evolution
        / "proposals"
        / proposal_id
        / "after"
        / "strategies"
        / strategy_id
        / "strategy.yml"
    )
    try:
        if manifest_path.exists():
            manifest_path.write_text(updated, encoding="utf-8")
    except Exception:
        _LOG.debug("failed to persist normalized strategy market", exc_info=True)
    return out


_FREEFORM_BACKTEST_KINDS = {
    "freeform_backtest",
    "custom_backtest",
    "research_backtest",
}
_NONSTANDARD_BACKTEST_KINDS = {
    "custom_replay",
    "event_replay",
    *_FREEFORM_BACKTEST_KINDS,
}


def _proposal_backtest_artifacts(paths, proposal_id: str, strategy_id: str | None) -> list[dict[str, Any]]:
    if not proposal_id or not strategy_id:
        return []
    root = paths.evolution / "proposals" / proposal_id / "after" / "strategies" / strategy_id / "backtests"
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for metrics_path in sorted(root.glob("*/metrics.json")):
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except Exception:
            metrics = {}
        if str(metrics.get("backtest_kind") or "") in _NONSTANDARD_BACKTEST_KINDS:
            continue
        out.append({
            "out_dir": str(metrics_path.parent),
            "metrics_path": str(metrics_path),
            "report_path": str(metrics_path.parent / "report.md"),
            "verdict": metrics.get("verdict"),
            "total_return_pct": metrics.get("total_return_pct"),
            "max_drawdown_pct": metrics.get("max_drawdown_pct"),
            "sharpe_ratio": metrics.get("sharpe_ratio"),
            "total_trades": metrics.get("total_trades"),
            "backtest_days": metrics.get("backtest_days"),
            "coverage_ok": metrics.get("coverage_ok"),
            "recommended_coverage_ok": metrics.get("recommended_coverage_ok"),
            "coverage_message": metrics.get("coverage_message"),
        })
    return out


_MEME_OR_ONCHAIN_MARKERS = (
    "meme",
    "memecoin",
    "pump.fun",
    "pumpfun",
    "smart money",
    "smart_money",
    "top_trader",
    "token_top_trader",
    "onchain",
    "on-chain",
    "onchainos",
    "okx_onchain",
    "byreal",
    "byreal_onchain",
    "goat",
    "dex",
    "dexscreener",
    "decentralized exchange",
    "decentralised exchange",
    "swap history",
    "swap_history",
    "token swap",
    "wallet swap",
    "solana:",
    "base:",
    "ethereum:",
)

_TOKEN_MATCH_ONLY_MARKERS = frozenset({"dex"})

_PREDICTION_MARKET_MARKERS = (
    "polymarket",
    "prediction market",
    "prediction_market",
    "clob",
)

_HARD_TO_REPLAY_MARKERS = (
    *_MEME_OR_ONCHAIN_MARKERS,
    *_PREDICTION_MARKET_MARKERS,
)

_CUSTOM_SIGNAL_MARKERS = (
    "macd",
    "rsi",
    "bollinger",
    "布林",
    "donchian",
    "atr",
    "kdj",
    "fibonacci",
    "支撑",
    "阻力",
    "support",
    "resistance",
    "funding",
    "资金费率",
    "cvd",
    "order flow",
    "订单流",
)

_CUSTOM_SIGNAL_TERM_REQUIREMENTS = (
    (
        "rsi",
        ("rsi", "relative strength index", "相对强弱"),
        ("rsi", "relative strength index", "相对强弱"),
    ),
    (
        "macd",
        ("macd",),
        ("macd",),
    ),
    (
        "bollinger",
        ("bollinger", "布林"),
        ("bollinger", "bbands", "布林"),
    ),
    (
        "donchian",
        ("donchian",),
        ("donchian",),
    ),
    (
        "atr",
        ("atr", "average true range"),
        ("atr", "average true range"),
    ),
    (
        "kdj",
        ("kdj",),
        ("kdj",),
    ),
    (
        "fibonacci",
        ("fibonacci", "fib"),
        ("fibonacci", "fib"),
    ),
    (
        "support_resistance",
        ("support", "resistance", "支撑", "阻力"),
        ("support", "resistance", "支撑", "阻力"),
    ),
    (
        "funding_rate",
        ("funding", "funding rate", "资金费率"),
        ("funding", "funding_rate", "funding rate", "资金费率"),
    ),
    (
        "order_flow",
        ("cvd", "order flow", "订单流", "大单流向", "大单", "whale flow"),
        (
            "cvd",
            "order_flow",
            "order flow",
            "订单流",
            "大单流向",
            "large_trade",
            "large trade",
            "whale",
            "taker",
        ),
    ),
)

_AGENT_DECISION_MARKERS = (
    "agent",
    "agentic",
    "仲裁",
    "决策",
    "判断",
    "决定",
    "新闻",
    "news",
    "news_social",
    "portfolio",
    "仓位",
    "风险预算",
    "skip",
    "error",
)
_NEWS_CONTEXT_MARKERS = (
    "news",
    "headline",
    "headlines",
    "catalyst",
    "catalysts",
    "social",
    "sentiment",
    "macro event",
    "market event",
    "新闻",
    "消息",
    "事件",
    "大宗事件",
    "舆情",
    "情绪",
)
_CRYPTO_CONTEXT_MARKERS = (
    "btc",
    "eth",
    "sol",
    "bnb",
    "usdt",
    "crypto",
    "defi",
    "dex",
    "链上",
    "加密",
)
_SOCIAL_CONTEXT_MARKERS = (
    "social",
    "sentiment",
    "x/twitter",
    "twitter",
    "reddit",
    "舆情",
    "社媒",
    "情绪",
)


def _with_inferred_news_sources(
    request: StrategyGenerationRequest,
    *,
    operator_prompt: str,
) -> StrategyGenerationRequest:
    if request.news_sources:
        return request
    text = " ".join(
        str(part or "")
        for part in (
            operator_prompt,
            request.prompt,
            request.title,
            request.description,
            " ".join(request.markets),
        )
    ).lower()
    if not any(marker in text for marker in _NEWS_CONTEXT_MARKERS):
        return request
    sources: list[str] = []
    if any(marker in text for marker in _CRYPTO_CONTEXT_MARKERS):
        sources.append("crypto")
    else:
        sources.append("news")
    if any(marker in text for marker in _SOCIAL_CONTEXT_MARKERS):
        sources.append("social")
    return replace(request, news_sources=tuple(dict.fromkeys(sources)))


def _proposal_is_meme_or_onchain(
    strategy_id: str | None,
    files: dict[str, str],
) -> bool:
    body = "\n".join(
        str(part)
        for part in (
            strategy_id or "",
            files.get("strategy.yml", ""),
            files.get("strategy.md", ""),
            files.get("config.yml", ""),
            files.get("main.py", ""),
        )
    ).lower()
    return _text_contains_marker(body, _MEME_OR_ONCHAIN_MARKERS)


def _proposal_generic_onchain_markets(files: dict[str, str]) -> list[str]:
    try:
        data = yaml_io.loads(files.get("strategy.yml", ""), default={}) or {}
    except Exception:
        data = {}
    markets = data.get("markets") if isinstance(data, dict) else None
    if isinstance(markets, str):
        values = [markets]
    elif isinstance(markets, list):
        values = [str(item) for item in markets]
    else:
        values = []
    generic: list[str] = []
    for market in values:
        parts = str(market or "").split(":")
        venue = parts[0].upper() if parts else ""
        if venue in {"BYREAL_ONCHAIN", "OKX_ONCHAIN", "BITGET_ONCHAIN", "ONCHAIN"} and len(parts) == 2:
            generic.append(market)
    return generic


def _target_has_freeform_backtest_script(
    paths,
    *,
    strategy_id: str | None,
    proposal_id: str | None,
) -> bool:
    try:
        from ...skills.builtin.backtest.scripts.freeform_run import (
            has_freeform_backtest_script,
        )

        if proposal_id:
            sid, _files = _read_proposal_files(paths, proposal_id)
            if not sid:
                return False
            root = paths.evolution / "proposals" / proposal_id / "after" / "strategies" / sid
        elif strategy_id:
            root = paths.strategy(strategy_id)
        else:
            return False
        return has_freeform_backtest_script(root)
    except Exception:
        return False


def _read_json_file(path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _artifact_exists(path: Any) -> bool:
    try:
        return bool(path) and bool(path.exists())
    except Exception:
        return False


def _custom_replay_result_summary(
    *,
    kind: str,
    result_path: Any = None,
    report_path: Any = None,
    out_dir: Any = None,
    metrics_path: Any = None,
    equity_path: Any = None,
    trades_path: Any = None,
    chart_path: Any = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = result or {}
    results = payload.get("results")
    if not isinstance(results, list):
        results = []
    equity_curve = payload.get("equity_curve")
    if not isinstance(equity_curve, list):
        equity_curve = []
    trades = payload.get("trades")
    if not isinstance(trades, list):
        trades = []
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    buy_count = int(summary.get("buy") or 0) if summary else 0
    watch_count = int(summary.get("watch") or 0) if summary else 0
    if not summary and results:
        buy_count = sum(1 for row in results if str(row.get("decision") or "").upper() == "BUY")
        watch_count = sum(1 for row in results if str(row.get("decision") or "").upper() == "WATCH")
    decision_sample = []
    for row in results[:5]:
        if not isinstance(row, dict):
            continue
        decision_sample.append(
            {
                "symbol": row.get("symbol"),
                "decision": row.get("decision"),
                "score": row.get("score"),
                "risk_level": row.get("risk_level"),
                "liquidity_usd": row.get("liquidity_usd"),
                "top10_hold_pct": row.get("top10_hold_pct"),
                "unique_traders": row.get("unique_traders"),
                "smart_money_inflow_usd": row.get("smart_money_inflow_usd"),
                "volume_24h_usd": row.get("volume_24h_usd"),
                "reason": row.get("reason"),
            }
        )
    events_seen = payload.get("events_seen")
    if events_seen is None and results:
        events_seen = len(results)
    signals = payload.get("signals")
    if signals is None:
        signals = buy_count + watch_count
    simulated_trades = payload.get("simulated_trades")
    if simulated_trades is None:
        simulated_trades = payload.get("total_trades")
    if simulated_trades is None:
        simulated_trades = len(trades) if trades else buy_count
    equity_points = payload.get("equity_points")
    if equity_points is None:
        equity_points = len(equity_curve) if equity_curve else None
    has_equity_curve = (
        bool(payload.get("has_equity_curve"))
        or bool(equity_points)
        or _artifact_exists(equity_path)
    )
    has_trade_details = (
        bool(payload.get("has_trade_details"))
        or _artifact_exists(trades_path)
        or isinstance(payload.get("trades"), list)
    )
    limitations = payload.get("limitations") or []
    return {
        "kind": kind,
        "result_path": str(result_path) if result_path else None,
        "report_path": str(report_path) if report_path else None,
        "out_dir": str(out_dir) if out_dir else None,
        "metrics_path": str(metrics_path) if metrics_path else None,
        "equity_path": str(equity_path) if equity_path else None,
        "trades_path": str(trades_path) if trades_path else None,
        "chart_path": str(chart_path) if chart_path else None,
        "ok": bool(payload.get("ok")) or bool(results) or bool(has_equity_curve and has_trade_details),
        "replay_kind": payload.get("replay_kind") or payload.get("data_source"),
        "window": payload.get("window"),
        "events_seen": events_seen,
        "signals": signals,
        "simulated_trades": simulated_trades,
        "total_trades": payload.get("total_trades"),
        "equity_points": equity_points,
        "has_equity_curve": has_equity_curve,
        "has_trade_details": has_trade_details,
        "total_return_pct": payload.get("total_return_pct"),
        "max_drawdown_pct": payload.get("max_drawdown_pct"),
        "final_equity_usd": payload.get("final_equity_usd"),
        "summary": summary,
        "decision_sample": decision_sample,
        "limitations": limitations if isinstance(limitations, list) else [limitations],
    }


def _proposal_nonstandard_backtest_artifacts(
    paths,
    proposal_id: str,
    strategy_id: str | None,
) -> list[dict[str, Any]]:
    if not proposal_id or not strategy_id:
        return []
    root = (
        paths.evolution
        / "proposals"
        / proposal_id
        / "after"
        / "strategies"
        / strategy_id
    )
    if not root.exists():
        return []

    out: list[dict[str, Any]] = []
    patterns = (
        ("custom_replay", "custom_replay_result.json", "custom_replay_report"),
        ("event_replay", "event_replay_result.json", "event_replay_report"),
        ("freeform_backtest", "freeform_backtest_result.json", "freeform_backtest_report"),
        ("custom_backtest", "custom_backtest_result.json", "custom_backtest_report"),
        ("research_backtest", "research_backtest_result.json", "research_backtest_report"),
    )
    for kind, result_name, report_stem in patterns:
        seen: set[str] = set()
        for result_path in sorted(root.rglob(result_name)):
            key = str(result_path)
            if key in seen:
                continue
            seen.add(key)
            result = _read_json_file(result_path)
            report_path = result_path.with_name(f"{report_stem}.md")
            metrics_path = result_path.parent / "metrics.json"
            equity_path = result_path.parent / "equity.csv"
            trades_path = result_path.parent / "trades.csv"
            chart_path = result_path.parent / "chart.json"
            out.append(
                _custom_replay_result_summary(
                    kind=kind,
                    result_path=result_path,
                    report_path=report_path if report_path.exists() else None,
                    out_dir=result_path.parent,
                    metrics_path=metrics_path if metrics_path.exists() else None,
                    equity_path=equity_path if equity_path.exists() else None,
                    trades_path=trades_path if trades_path.exists() else None,
                    chart_path=chart_path if chart_path.exists() else None,
                    result=result,
                )
            )
        for report_path in sorted(root.rglob(f"{report_stem}.json")):
            key = str(report_path)
            if key in seen:
                continue
            seen.add(key)
            report = _read_json_file(report_path)
            metrics_path = report_path.parent / "metrics.json"
            equity_path = report_path.parent / "equity.csv"
            trades_path = report_path.parent / "trades.csv"
            chart_path = report_path.parent / "chart.json"
            out.append(
                _custom_replay_result_summary(
                    kind=kind,
                    report_path=report_path,
                    out_dir=report_path.parent,
                    metrics_path=metrics_path if metrics_path.exists() else None,
                    equity_path=equity_path if equity_path.exists() else None,
                    trades_path=trades_path if trades_path.exists() else None,
                    chart_path=chart_path if chart_path.exists() else None,
                    result=report,
                )
            )
    return out


def _qualifying_standard_backtests(backtests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(backtests)


def _qualifying_nonstandard_backtests(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not bool(artifact.get("ok")):
            continue
        if artifact.get("kind") in _FREEFORM_BACKTEST_KINDS and not (
            artifact.get("has_equity_curve") and artifact.get("has_trade_details")
        ):
            continue
        out.append(artifact)
    return out


def _proposal_backtest_status(
    *,
    policy: str,
    is_meme_or_onchain: bool,
    standard_backtests: list[dict[str, Any]],
    nonstandard_backtests: list[dict[str, Any]],
    waiver_approved: bool,
    waiver_note: str,
) -> dict[str, Any]:
    standard_ok = bool(_qualifying_standard_backtests(standard_backtests))
    nonstandard_ok = bool(_qualifying_nonstandard_backtests(nonstandard_backtests))
    waiver_ok = bool(waiver_approved and waiver_note.strip())
    if standard_ok:
        accepted_kind = "backtest"
    elif nonstandard_ok:
        accepted_kind = _qualifying_nonstandard_backtests(nonstandard_backtests)[0]["kind"]
    elif waiver_ok:
        accepted_kind = "backtest_waiver"
    else:
        accepted_kind = None
    return {
        "policy": policy,
        "is_meme_or_onchain": is_meme_or_onchain,
        "standard_ok": standard_ok,
        "nonstandard_ok": nonstandard_ok,
        "waiver_ok": waiver_ok,
        "accepted_kind": accepted_kind,
        "standard_backtests": standard_backtests,
        "nonstandard_backtests": nonstandard_backtests,
        "approval_required": not bool(accepted_kind),
        "approval_note": waiver_note if waiver_ok else "",
        "risk": (
            "standard_backtest_missing"
            if accepted_kind in {*_NONSTANDARD_BACKTEST_KINDS, "backtest_waiver"}
            else None
        ),
    }


def _operator_backtest_approval_next_action(
    proposal_id: str,
    *,
    policy: str = "flexible_meme",
) -> dict[str, Any]:
    return {
        "tool": "strategy_promote",
        "arguments": {
            "proposal_id": proposal_id,
            "backtest_policy": policy,
            "operator_approved": True,
            "approval_note": "<operator-approved reason>",
        },
        "message": (
            "Use only after the operator explicitly approves promoting this "
            "meme/on-chain strategy without a standard OHLCV backtest. Prefer "
            "a custom/event replay artifact when one can be built from real "
            "historical data."
        ),
    }


def _proposal_strategy_paths(paths, proposal_id: str | None, strategy_id: str | None) -> dict[str, str]:
    if not proposal_id or not strategy_id:
        return {}
    root = paths.evolution / "proposals" / proposal_id / "after" / "strategies" / strategy_id

    def _rel(p) -> str:
        # Workspace-relative: that is the contract read_file/list_dir expect,
        # and it keeps host filesystem layout (absolute C:\... paths) out of
        # tool results that get quoted verbatim in operator-facing replies.
        try:
            return p.relative_to(paths.root).as_posix()
        except ValueError:
            return str(p)

    return {
        "strategy_root": _rel(root),
        "strategy_yml_path": _rel(root / "strategy.yml"),
        "strategy_md_path": _rel(root / "strategy.md"),
        "main_path": _rel(root / "main.py"),
        "tests_path": _rel(root / "tests"),
    }


def _normalised_strategy_key(value: str | None) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _matching_proposal_strategy_candidates(paths, strategy_id: str) -> list[dict[str, Any]]:
    target = _normalised_strategy_key(strategy_id)
    if not target:
        return []
    matches: list[dict[str, Any]] = []
    try:
        proposals = list_proposals(paths)
    except Exception:
        return []
    for proposal in proposals:
        root = proposal.path / "after" / "strategies"
        if not root.exists():
            continue
        for strategy_dir in root.iterdir():
            if not strategy_dir.is_dir():
                continue
            candidate_id = strategy_dir.name
            if _normalised_strategy_key(candidate_id) != target:
                continue
            matches.append(
                {
                    "proposal_id": proposal.id,
                    "strategy_id": candidate_id,
                    "state": proposal.state,
                    "summary": proposal.summary,
                    "ts": proposal.ts,
                    "path": str(strategy_dir),
                }
            )
    return sorted(matches, key=lambda item: str(item.get("ts") or ""), reverse=True)


def _promoted_strategy_has_placeholder_market(paths, strategy_id: str) -> bool:
    strategy_yml = paths.strategies / strategy_id / "strategy.yml"
    if not strategy_yml.exists():
        return True
    try:
        data = yaml_io.load(strategy_yml, default={}) or {}
    except Exception:
        return False
    markets = data.get("markets")
    if not markets:
        selection = data.get("selection") if isinstance(data.get("selection"), dict) else {}
        markets = selection.get("markets")
    if isinstance(markets, str):
        market_values = [markets]
    elif isinstance(markets, list):
        market_values = [str(item) for item in markets]
    else:
        market_values = []
    if not market_values:
        return True
    joined = " ".join(market_values).lower()
    return any(marker in joined for marker in (":unknown", ":scan", "unknown"))


def _proposal_backtest_next_action(proposal_id: str) -> dict[str, Any]:
    return {
        "tool": "strategy_backtest",
        "arguments": {
            "proposal_id": proposal_id,
            "preset": "default",
            "allow_mock": False,
        },
    }


def _proposal_requires_standard_backtest(
    strategy_id: str | None,
    files: dict[str, str],
) -> bool:
    try:
        manifest = yaml_io.loads(files.get("strategy.yml", ""), default={}) or {}
    except Exception:
        manifest = {}
    markets = manifest.get("markets") if isinstance(manifest, dict) else None
    if isinstance(markets, str):
        market_values = [markets]
    elif isinstance(markets, list):
        market_values = [str(item) for item in markets if str(item).strip()]
    else:
        market_values = []
    if not market_values:
        return False
    if _proposal_is_meme_or_onchain(strategy_id, files):
        return False
    return True


def _proposal_nonstandard_replay_next_action() -> dict[str, Any]:
    return {
        "type": "paper_replay_or_custom_evidence",
        "message": (
            "This strategy is for meme/on-chain/new-pool discovery, so a "
            "standard OHLCV backtest is not required before review. Provide "
            "a paper replay plan or custom replay evidence from real pool, "
            "wallet, liquidity, concentration, and slippage observations."
        ),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def strategy_generate_proposal_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = _normalise_raw_strategy_args(call.arguments or {})
    try:
        request = _request_from_args(args)
    except Exception as exc:  # type errors etc.
        return _usage_error(call, f"invalid request: {type(exc).__name__}: {exc}")
    metadata = call.metadata if isinstance(call.metadata, dict) else {}
    operator_prompt = str(metadata.get("original_user_prompt") or "")
    request = _normalise_model_invented_strategy_scope(
        request,
        operator_prompt=operator_prompt,
    )
    request = _with_inferred_news_sources(
        request,
        operator_prompt=operator_prompt,
    )
    if _requires_explicit_sdk_files(request):
        if _raw_payload_truncated_before_sdk_files(args):
            return _usage_error(call, _truncated_sdk_files_payload_error())
        return _usage_error(
            call,
            (
                "wallet/on-chain/custom-data or prediction-market strategy "
                "proposals must include "
                "`files.main.py` and `files.strategy.md` authored with the "
                "Nerya Strategy SDK. The proposal tool only packages and "
                "validates that code; it must not fall back to the default "
                "trend/scalping generator for this scope."
            ),
        )
    if _requires_custom_signal_main_py(request, operator_prompt=operator_prompt):
        return _usage_error(
            call,
            (
                "strategy requests with named custom signal logic must include "
                "`files.main.py` authored with the Nerya Strategy SDK. Draft "
                "the indicator/trigger logic directly in "
                "strategy_generate_proposal.files instead of generating a stock "
                "template and editing proposal files afterwards."
            ),
            recovery_hint={
                "action": "retry_with_required_arguments",
                "tool_name": "strategy_generate_proposal",
                "required_arguments": ["files.main.py"],
                "avoid_arguments": ["files"],
                "reason": "custom_signal_logic_requires_sdk_file",
            },
        )
    agent_contract_error = _agent_decision_contract_error(
        request,
        operator_prompt=operator_prompt,
    )
    if agent_contract_error:
        return _usage_error(call, agent_contract_error)
    signal_coverage_error = _custom_signal_main_py_coverage_error(
        request,
        operator_prompt=operator_prompt,
    )
    if signal_coverage_error:
        return _usage_error(call, signal_coverage_error)
    placeholder_error = _sdk_files_placeholder_error(request)
    if placeholder_error:
        return _usage_error(call, placeholder_error)

    do_validate = bool(args.get("validate", True))
    try:
        generator = StrategyCodeGenerator(config.paths)
        result = generator.generate(
            request,
            validate=do_validate,
            create_proposal_record=True,
            # Validate immediately on create: when blockers exist we do not
            # leave a pending proposal behind, we hand the blockers straight
            # back to the agent so it can fix the package and call this tool
            # again. A proposal only enters the pending-review queue once the
            # generated files pass validation.
            require_valid=do_validate,
        )
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(
            call, f"generator failed: {type(exc).__name__}: {exc}"
        )

    if (
        do_validate
        and result.proposal is None
        and result.validation is not None
        and not result.validation.ok
    ):
        return _strategy_validation_blockers_error(
            call,
            strategy_id=result.request.strategy_id,
            validation=result.validation,
            files=list(result.files.keys()),
        )

    payload = {
        "strategy_id": result.request.strategy_id,
        "strategy_class": result.request.strategy_class,
        "execution_mode": result.files.get("strategy.yml")
        and _manifest_execution_mode(result.files["strategy.yml"])
        or result.request.execution_mode,
        "proposal_id": result.proposal.id if result.proposal else None,
        "validation": (
            result.validation.asdict() if result.validation is not None else None
        ),
        "files": list(result.files.keys()),
    }
    if result.proposal:
        payload["proposal_paths"] = _proposal_strategy_paths(
            config.paths,
            result.proposal.id,
            request.strategy_id,
        )
    if result.proposal and result.validation is not None and result.validation.ok:
        standard_backtest_required = _proposal_requires_standard_backtest(
            result.request.strategy_id,
            result.files,
        )
        payload["backtest_required"] = True
        if standard_backtest_required:
            payload["next_required_action"] = _proposal_backtest_next_action(result.proposal.id)
        else:
            payload["backtest_required"] = False
            payload["next_required_action"] = _proposal_nonstandard_replay_next_action()
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)


# ---------------------------------------------------------------------------
# File-authoring lane: strategy_draft_proposal + strategy_submit_proposal
#
# Instead of dumping every package file inline through one large tool call
# (high-context and brittle), the agent scaffolds a *draft* proposal, edits
# the staged files in place with read_file/edit_file/write_file (they live
# under evolution/proposals/<pid>/after/strategies/<id>/ which the workspace
# mutation guard allows), validates, and submits. A proposal only enters the
# pending-review queue once it passes validation.
# ---------------------------------------------------------------------------


_DRAFT_SEED_SKIP_DIRS = {"runs", "logs", "state", "versions", "backtests"}


def _draft_next_steps(proposal_id: str, paths_map: dict[str, str]) -> list[str]:
    main_path = paths_map.get("main_path") or "<proposal>/after/strategies/<id>/main.py"
    return [
        (
            "The scaffold is a GENERIC momentum example, not your strategy yet. "
            f"Edit the staged files in place with edit_file (e.g. {main_path}); "
            "they live under the proposal's after/strategies tree and are NOT "
            "live. Keep the contract scaffolding (the run() signature, the "
            "open_position / close_position calls, the signed-position handling "
            "and indicator helpers) and replace just the signal/indicator logic "
            "to match the requested idea — that is far faster than rewriting the "
            "whole file."
        ),
        (
            "Author real SDK logic in main.py using StrategyContext / "
            "StrategyResult (and StrategyAgentTask for agent decisions). Read "
            "positions via ctx.portfolio.positions(market) — it returns a list, "
            "so iterate or select a row, never call .get on it — and the "
            "configured account via ctx.config.accounts[0] (there is no "
            "ctx.account_id)."
        ),
        (
            f'Run strategy_validate({{"proposal_id": "{proposal_id}"}}) and fix '
            "any blockers by editing the files."
        ),
        (
            "When validation passes, call "
            f'strategy_submit_proposal({{"proposal_id": "{proposal_id}"}}) to '
            "move it into the pending-review queue."
        ),
    ]


def _read_promoted_strategy_files(paths, strategy_id: str) -> dict[str, str]:
    """Return ``rel_path -> content`` for a promoted strategy package.

    Runtime artifacts (runs/, logs/, state/, versions/, backtests/) and the
    cached validation report are skipped so the seeded draft only contains the
    authored package, not per-run output.
    """

    root = paths.strategies / strategy_id
    if not root.exists() or not root.is_dir():
        return {}
    files: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        top = rel.parts[0] if rel.parts else ""
        if top in _DRAFT_SEED_SKIP_DIRS:
            continue
        if rel.name == "validation_report.json":
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            continue
        files[rel.as_posix()] = content
    return files


def _draft_from_promoted(
    call: ToolCall,
    paths,
    *,
    strategy_id: str,
    from_strategy_id: str,
    args: dict[str, Any],
) -> ToolResult:
    seed = _read_promoted_strategy_files(paths, from_strategy_id)
    if not seed:
        return _usage_error(
            call,
            (
                f"cannot iterate: promoted strategy {from_strategy_id!r} has no "
                f"files under strategies/{from_strategy_id}/. Promote it first, "
                "or omit from_strategy_id to scaffold a brand-new strategy."
            ),
        )
    try:
        validation: Optional[StrategyValidation] = validate_proposal_files(
            strategy_id=strategy_id, files=seed
        )
    except Exception:
        validation = None
    extra_files: dict[str, str] = {}
    for rel, content in seed.items():
        extra_files[f"after/strategies/{strategy_id}/{rel}"] = content
    if validation is not None:
        extra_files["validation_report.json"] = json.dumps(
            validation.asdict(), indent=2, ensure_ascii=False
        )
    title = str(args.get("title") or "").strip() or f"Iterate strategy {strategy_id}"
    try:
        proposal = create_proposal(
            paths,
            kind="strategy_package_proposal",
            summary=title,
            rationale=(
                f"# {title}\n\nSeeded from promoted strategy "
                f"`{from_strategy_id}` for edit-based iteration.\n"
            ),
            test_plan=(
                "# Test plan\n\nValidate + backtest the edited package before "
                "promotion.\n"
            ),
            rollback=(
                f"# Rollback\n\nThe promoted `{from_strategy_id}` package stays "
                "live until this proposal is promoted.\n"
            ),
            target=f"strategies/{strategy_id}",
            extra_files=extra_files,
            initial_state="draft",
            metadata={
                "strategy_id": strategy_id,
                "iterated_from": from_strategy_id,
                "seeded_from": "promoted",
            },
        )
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(
            call, f"draft scaffold failed: {type(exc).__name__}: {exc}"
        )
    paths_map = _proposal_strategy_paths(paths, proposal.id, strategy_id)
    payload = {
        "action": "strategy_draft_proposal",
        "proposal_id": proposal.id,
        "strategy_id": strategy_id,
        "state": "draft",
        "kind": "strategy_package_proposal",
        "seeded_from": "promoted",
        "iterated_from": from_strategy_id,
        "files": sorted(seed.keys()),
        "validation": validation.asdict() if validation is not None else None,
        "proposal_paths": paths_map,
        "next_steps": _draft_next_steps(proposal.id, paths_map),
    }
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)


def strategy_draft_proposal_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = _normalise_raw_strategy_args(call.arguments or {})
    strategy_id = str(args.get("strategy_id") or "").strip()
    from_strategy_id = str(args.get("from_strategy_id") or "").strip()
    if not strategy_id:
        return _usage_error(call, "strategy_draft_proposal requires strategy_id")

    paths = config.paths
    if from_strategy_id:
        return _draft_from_promoted(
            call,
            paths,
            strategy_id=strategy_id,
            from_strategy_id=from_strategy_id,
            args=args,
        )

    try:
        request = _request_from_args(args)
    except Exception as exc:
        return _usage_error(call, f"invalid request: {type(exc).__name__}: {exc}")
    if not request.markets:
        return _usage_error(
            call,
            (
                "strategy_draft_proposal requires at least one market for a new "
                "strategy (or pass from_strategy_id to iterate an existing one)."
            ),
        )
    auto_selected_accounts: list[str] = []
    if not request.accounts:
        resolved, available = _auto_bind_default_accounts(paths, request)
        if resolved:
            request = replace(request, accounts=resolved)
            auto_selected_accounts = list(resolved)
        else:
            return _missing_account_error(
                call, request=request, available=available
            )

    try:
        generator = StrategyCodeGenerator(paths)
        gen = generator.generate(
            request,
            validate=True,
            create_proposal_record=True,
            require_valid=False,
            initial_state="draft",
        )
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(
            call, f"draft scaffold failed: {type(exc).__name__}: {exc}"
        )

    proposal = gen.proposal
    if proposal is None:
        return _execution_error(call, "draft proposal was not created")
    paths_map = _proposal_strategy_paths(paths, proposal.id, strategy_id)
    payload = {
        "action": "strategy_draft_proposal",
        "proposal_id": proposal.id,
        "strategy_id": strategy_id,
        "state": "draft",
        "kind": "strategy_package_proposal",
        "seeded_from": "template",
        "strategy_class": gen.request.strategy_class,
        "files": list(gen.files.keys()),
        "validation": gen.validation.asdict() if gen.validation is not None else None,
        "proposal_paths": paths_map,
        "next_steps": _draft_next_steps(proposal.id, paths_map),
    }
    if auto_selected_accounts:
        payload["auto_selected_accounts"] = auto_selected_accounts
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)


def _strategy_submit_validation_blockers_error(
    call: ToolCall,
    *,
    proposal_id: str,
    strategy_id: str,
    validation: StrategyValidation,
    files: list[str],
    paths_map: dict[str, str],
) -> ToolResult:
    blockers = validation.blockers
    lines: list[str] = []
    for issue in blockers:
        where = f" [{issue.where}]" if issue.where else ""
        lines.append(f"- {issue.message}{where}")
    detail = "\n".join(lines) if lines else "- (no blocker detail reported)"
    main_path = paths_map.get("main_path") or "the after/strategies files"
    message = (
        f"strategy_submit_proposal kept proposal {proposal_id!r} in draft: the "
        f"package for {strategy_id!r} still has {len(blockers)} validation "
        f"blocker(s). Edit the staged files (e.g. {main_path}) with "
        "edit_file / write_file, re-run strategy_validate, and call "
        "strategy_submit_proposal again once validation passes. The proposal "
        "only enters the pending-review queue when validation is clean.\n"
        f"Blockers:\n{detail}"
    )
    return _usage_error(
        call,
        message,
        recovery_hint={
            "action": "fix_validation_blockers_and_resubmit",
            "tool_name": "strategy_submit_proposal",
            "proposal_id": proposal_id,
            "strategy_id": strategy_id,
            "proposal_paths": paths_map,
            "files": files,
            "validation": validation.asdict(),
            "blockers": [issue.asdict() for issue in blockers],
        },
    )


def strategy_submit_proposal_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = call.arguments or {}
    proposal_id = str(args.get("proposal_id") or "").strip()
    note = str(args.get("note") or "").strip()
    if not proposal_id:
        return _usage_error(call, "strategy_submit_proposal requires proposal_id")

    paths = config.paths
    target = None
    for proposal in list_proposals(paths):
        if proposal.id == proposal_id:
            target = proposal
            break
    if target is None:
        return _usage_error(
            call,
            f"proposal {proposal_id!r} not found",
            recovery_hint={"action": "strategy_draft_proposal"},
        )
    if target.kind != "strategy_package_proposal":
        return _usage_error(
            call,
            (
                f"proposal {proposal_id!r} is a {target.kind}, not a strategy "
                "package proposal; strategy_submit_proposal only submits "
                "strategy packages."
            ),
        )

    try:
        sid, files = _read_proposal_files(paths, proposal_id)
    except Exception as exc:
        return _execution_error(
            call, f"failed to read proposal files: {type(exc).__name__}: {exc}"
        )
    if not files:
        return _usage_error(
            call,
            (
                f"proposal {proposal_id!r} has no after/strategies/* files to "
                "submit. Scaffold it with strategy_draft_proposal first, then "
                "edit the staged files."
            ),
        )
    strategy_id = sid or "unknown"
    files = _normalise_bybit_perpetual_manifest_files(
        paths,
        proposal_id=proposal_id,
        strategy_id=strategy_id,
        files=files,
    )

    try:
        validation = validate_proposal_files(strategy_id=strategy_id, files=files)
    except Exception as exc:
        return _execution_error(
            call, f"validation failed: {type(exc).__name__}: {exc}"
        )

    paths_map = _proposal_strategy_paths(paths, proposal_id, strategy_id)
    if not validation.ok:
        return _strategy_submit_validation_blockers_error(
            call,
            proposal_id=proposal_id,
            strategy_id=strategy_id,
            validation=validation,
            files=sorted(files.keys()),
            paths_map=paths_map,
        )

    try:
        (target.path / "validation_report.json").write_text(
            json.dumps(validation.asdict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:  # pragma: no cover - report write is best-effort
        pass
    set_state(
        paths,
        proposal_id,
        "pending_review",
        note=note or "submitted for review via strategy_submit_proposal",
    )

    standard_backtest_required = _proposal_requires_standard_backtest(
        strategy_id,
        files,
    )
    payload = {
        "action": "strategy_submit_proposal",
        "proposal_id": proposal_id,
        "strategy_id": strategy_id,
        "kind": "strategy_package_proposal",
        "state": "pending_review",
        "summary": target.summary,
        "validation": validation.asdict(),
        "files": sorted(files.keys()),
        "proposal_paths": paths_map,
        "backtest_required": standard_backtest_required,
        "next_required_action": (
            _proposal_backtest_next_action(proposal_id)
            if standard_backtest_required
            else _proposal_nonstandard_replay_next_action()
        ),
    }
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)


def strategy_validate_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    proposal_id = (args.get("proposal_id") or "").strip()
    strategy_id = (args.get("strategy_id") or "").strip()
    if not proposal_id and not strategy_id:
        return _usage_error(call, "strategy_id or proposal_id is required")

    try:
        if proposal_id:
            sid, files = _read_proposal_files(config.paths, proposal_id)
            if not files:
                return _usage_error(
                    call, f"proposal {proposal_id!r} has no after/strategies/* tree"
                )
            target_sid = strategy_id or sid or "unknown"
            files = _normalise_bybit_perpetual_manifest_files(
                config.paths,
                proposal_id=proposal_id,
                strategy_id=target_sid,
                files=files,
            )
            validation = validate_proposal_files(
                strategy_id=target_sid, files=files
            )
        else:
            validation = validate_strategy_package(config.paths, strategy_id)
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(
            call, f"validation failed: {type(exc).__name__}: {exc}"
        )
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name, data=validation.asdict()
    )


def strategy_backtest_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    strategy_id = (args.get("strategy_id") or "").strip() or None
    proposal_id = (args.get("proposal_id") or "").strip() or None
    # Agents frequently pass BOTH ids right after submitting a proposal
    # (they now know each). Both point at the same staged strategy, so
    # prefer the in-flight proposal and drop the redundant strategy_id
    # rather than failing run_strategy_backtest's "exactly one" guard.
    if strategy_id and proposal_id:
        strategy_id = None
    if proposal_id:
        try:
            sid, files = _read_proposal_files(config.paths, proposal_id)
            if files:
                _normalise_bybit_perpetual_manifest_files(
                    config.paths,
                    proposal_id=proposal_id,
                    strategy_id=sid or strategy_id or "unknown",
                    files=files,
                )
        except Exception:
            _LOG.debug("pre-backtest proposal normalization failed", exc_info=True)
    if strategy_id and not proposal_id:
        matching_proposals = _matching_proposal_strategy_candidates(config.paths, strategy_id)
        if matching_proposals and _promoted_strategy_has_placeholder_market(
            config.paths,
            strategy_id,
        ):
            return ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data={
                    "ok": False,
                    "reason": "proposal_id_required_for_matching_inflight_proposal",
                    "strategy_id": strategy_id,
                    "message": (
                        "This promoted strategy package has placeholder or "
                        "missing markets, while matching in-flight strategy "
                        "proposals exist. If the operator named a prp_* "
                        "proposal id, rerun this review with that exact "
                        "proposal_id instead of this similarly named "
                        "promoted strategy_id."
                    ),
                    "matching_proposals": matching_proposals[:10],
                },
            )
    try:
        from ...skills.builtin.backtest.scripts.backtest_run import (
            run_strategy_backtest,
        )
        from ...skills.builtin.backtest.scripts.data_cache import (
            NoHistoricalDataError,
        )
        from ...skills.builtin.backtest.scripts.freeform_run import (
            run_freeform_backtest,
        )

        if _target_has_freeform_backtest_script(
            config.paths,
            strategy_id=strategy_id,
            proposal_id=proposal_id,
        ):
            result = run_freeform_backtest(
                strategy_id=strategy_id,
                proposal_id=proposal_id,
                workspace=config.paths.root,
            )
        else:
            result = run_strategy_backtest(
                strategy_id=strategy_id,
                proposal_id=proposal_id,
                preset=str(args.get("preset") or "default"),
                config_path=(args.get("config_path") or None),
                workspace=config.paths.root,
                allow_mock=bool(args.get("allow_mock", False)),
            )
    except NoHistoricalDataError as exc:
        next_required_action: dict[str, Any] = {
            "type": "report_data_gap",
            "message": (
                "No durable historical candles were available for the "
                "requested market/timeframe or fallback timeframes. Do "
                "not retry with mock, synthetic, random, or placeholder "
                "data; either choose a market with real historical "
                "candles or ask the operator for a data source."
            ),
        }
        if proposal_id:
            try:
                sid, files = _read_proposal_files(config.paths, proposal_id)
                generic_markets = _proposal_generic_onchain_markets(files)
                if generic_markets:
                    next_required_action = {
                        "type": "repair_concrete_market_and_rerun",
                        "message": (
                            "The proposal used a generic on-chain universe "
                            f"market ({', '.join(generic_markets)}) as the "
                            "manifest market. That is not proof that standard "
                            "OHLCV is unavailable. If market_data already "
                            "returned a concrete chain:token market, repair "
                            "the proposal so strategy.yml markets uses that "
                            "exact market and rerun strategy_backtest. If no "
                            "single token was selected, report this as a "
                            "generic scanner requiring custom/event replay "
                            "rather than a standard OHLCV result."
                        ),
                        "repair_hint": (
                            "Use the exact concrete market from the successful "
                            "market_data result, e.g. BYREAL_ONCHAIN:solana:<pool_address>."
                        ),
                    }
                elif _proposal_is_meme_or_onchain(sid or strategy_id, files):
                    next_required_action = {
                        "type": "custom_replay_or_operator_approval",
                        "message": (
                            "Standard OHLCV history was unavailable across "
                            "the attempted timeframes or not representative "
                            "for this meme/on-chain strategy. "
                            "Build a custom/event replay from real wallet, "
                            "DEX, holder, or trade history when possible. If "
                            "no durable replay source exists, promotion "
                            "requires explicit operator approval and must be "
                            "reported as a standard-backtest waiver."
                        ),
                        "custom_replay_template": (
                            "nerya/skills/builtin/backtest/references/"
                            "custom_replay_template.md"
                        ),
                        "approval_action": _operator_backtest_approval_next_action(
                            proposal_id
                        ),
                    }
            except Exception:
                pass
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "ok": False,
                "reason": "no_historical_data",
                "strategy_id": strategy_id,
                "proposal_id": proposal_id,
                "coverage_ok": False,
                "coverage_message": str(exc),
                "next_required_action": next_required_action,
            },
        )
    except (NeryaError, TradingError, ValueError) as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(
            call, f"backtest failed: {type(exc).__name__}: {exc}"
        )
    model_result = _model_facing_backtest_result(result)
    if proposal_id:
        target_sid = strategy_id or str(result.get("strategy_id") or "").strip() or None
        files: dict[str, str] = {}
        if not target_sid:
            try:
                target_sid, files = _read_proposal_files(config.paths, proposal_id)
            except Exception:
                target_sid = None
        elif target_sid:
            try:
                _, files = _read_proposal_files(config.paths, proposal_id)
            except Exception:
                files = {}
        nonstandard_backtests = _proposal_nonstandard_backtest_artifacts(
            config.paths,
            proposal_id,
            target_sid,
        )
        model_result["nonstandard_backtests"] = nonstandard_backtests
        is_meme_or_onchain = _proposal_is_meme_or_onchain(target_sid, files)
        if is_meme_or_onchain:
            paper_review_allowed = bool(nonstandard_backtests) or bool(result.get("ok"))
            result_kind = str(result.get("kind") or "")
            if result_kind in _FREEFORM_BACKTEST_KINDS:
                paper_review_basis = "freeform_sdk_backtest"
            elif nonstandard_backtests and result.get("ok"):
                paper_review_basis = "real_kline_standard_backtest_plus_custom_event_replay"
            elif nonstandard_backtests:
                paper_review_basis = "custom_event_replay"
            else:
                paper_review_basis = "real_standard_backtest"
            model_result["paper_review_allowed"] = paper_review_allowed
            model_result["paper_review_basis"] = paper_review_basis
            model_result["shadow_live_requires_user_approval"] = True
            model_result["paper_review_note"] = (
                "If paper_review_allowed is true, do not report paper review "
                "as blocked solely because the standard OHLCV verdict is "
                "FAIL/no_trades or because the evidence came from a freeform "
                "SDK backtest. Preserve the meme smart-money thesis and cite "
                "the custom/freeform replay evidence; shadow/live still "
                "requires explicit operator approval."
            )
            model_result["review_gate"] = {
                "paper_review_allowed": paper_review_allowed,
                "paper_review_basis": paper_review_basis,
                "shadow_live_requires_user_approval": True,
                "message": (
                    "For meme/on-chain smart-money strategies, a standard "
                    "OHLCV replay with no trades can be an engine fit limit, "
                    "not a reason to rewrite the thesis into trend/scalping. "
                    "Use real K-line coverage or freeform SDK custom/event "
                    "replay evidence for paper review; shadow/live "
                    "progression still requires explicit operator approval."
                ),
            }
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data=model_result,
    )


def _model_facing_backtest_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return a backtest payload that is hard for the model to misread.

    Raw percentage metrics remain available in ``metrics.json`` for
    machine consumers. The LLM-facing tool result intentionally exposes
    display strings first and removes duplicate top-level numeric pct
    fields because models repeatedly converted values such as ``0.0274``
    into ``2.74%`` despite explicit unit warnings.
    """

    out = dict(result)
    display = result.get("metrics_display")
    if isinstance(display, dict):
        out["metrics"] = dict(display)
    for key in (
        "total_return_pct",
        "max_drawdown_pct",
        "benchmark_buy_hold_return_pct",
        "alpha_vs_benchmark_pct",
    ):
        out.pop(key, None)
    out["raw_metrics_file"] = result.get("metrics_path")
    out["metrics_are_display_strings"] = True
    out["metrics_note"] = (
        "Summarise the backtest in plain language for the user. Reuse the "
        "exact numbers from metrics/operator_summary, but do not copy their "
        "field labels or any internal notes verbatim. Raw numeric metrics "
        "are only in raw_metrics_file."
    )
    return out


def strategy_promote_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Approve + apply a strategy package proposal.

    The validator runs on the proposal's ``after/`` files first; we
    refuse to promote when blockers exist so an operator can't
    accidentally green-light a package the generator already
    flagged. Warnings are surfaced in the response but don't block.
    """

    args = call.arguments or {}
    pid = (args.get("proposal_id") or "").strip()
    note = str(args.get("note") or "")
    if not pid:
        return _usage_error(call, "proposal_id is required")

    paths = config.paths
    sid, files = _read_proposal_files(paths, pid)
    if not files:
        return _usage_error(
            call, f"proposal {pid!r} not found or has no strategy files"
        )

    try:
        validation = validate_proposal_files(strategy_id=sid or pid, files=files)
    except Exception as exc:
        return _execution_error(
            call, f"validation failed: {type(exc).__name__}: {exc}"
        )
    if not validation.ok:
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "ok": False,
                "reason": "validation_blockers",
                "validation": validation.asdict(),
            },
        )
    try:
        (paths.evolution / "proposals" / pid / "validation_report.json").write_text(
            json.dumps(validation.asdict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass

    backtests = _proposal_backtest_artifacts(paths, pid, sid)
    nonstandard_backtests = _proposal_nonstandard_backtest_artifacts(paths, pid, sid)
    requested_policy = str(args.get("backtest_policy") or "standard_required")
    require_backtest = bool(args.get("require_backtest", True))
    if not require_backtest and requested_policy == "standard_required":
        requested_policy = "operator_waiver"
    if requested_policy not in {"standard_required", "flexible_meme", "operator_waiver"}:
        return _usage_error(call, f"invalid backtest_policy: {requested_policy!r}")
    approval_note = str(args.get("approval_note") or note or "").strip()
    operator_approved = bool(args.get("operator_approved", False))
    is_meme_or_onchain = _proposal_is_meme_or_onchain(sid, files)
    backtest_status = _proposal_backtest_status(
        policy=requested_policy,
        is_meme_or_onchain=is_meme_or_onchain,
        standard_backtests=backtests,
        nonstandard_backtests=nonstandard_backtests,
        waiver_approved=operator_approved,
        waiver_note=approval_note,
    )
    accepted_kind = backtest_status.get("accepted_kind")
    if accepted_kind != "backtest":
        if requested_policy == "standard_required":
            if require_backtest:
                return ToolResult.from_json(
                    tool_use_id=call.id,
                    name=call.name,
                    data={
                        "ok": False,
                        "proposal_id": pid,
                        "strategy_id": sid,
                        "reason": "backtest_required",
                        "message": (
                            "Run strategy_backtest on this proposal before "
                            "promotion. For meme/on-chain strategies with no "
                            "representative OHLCV history, use "
                            "backtest_policy=flexible_meme and provide a real "
                            "custom/freeform/event replay artifact or explicit "
                            "operator approval."
                        ),
                        "validation": validation.asdict(),
                        "backtest_status": backtest_status,
                        "next_required_action": _proposal_backtest_next_action(pid),
                    },
                )
        elif requested_policy == "flexible_meme":
            if not is_meme_or_onchain:
                return ToolResult.from_json(
                    tool_use_id=call.id,
                    name=call.name,
                    data={
                        "ok": False,
                        "proposal_id": pid,
                        "strategy_id": sid,
                        "reason": "flexible_meme_policy_not_applicable",
                        "message": (
                            "flexible_meme is reserved for meme, DEX, wallet, "
                            "or other on-chain strategies. Use "
                            "operator_waiver with explicit operator approval "
                            "for other non-standard markets."
                        ),
                        "validation": validation.asdict(),
                        "backtest_status": backtest_status,
                    },
                )
            if accepted_kind is None:
                return ToolResult.from_json(
                    tool_use_id=call.id,
                    name=call.name,
                    data={
                        "ok": False,
                        "proposal_id": pid,
                        "strategy_id": sid,
                        "reason": "operator_approval_required_for_backtest_waiver",
                        "message": (
                            "No qualifying standard backtest or "
                            "custom/freeform/event replay artifact was found. "
                            "The strategy can still be promoted only after the "
                            "operator explicitly approves a standard-backtest "
                            "waiver."
                        ),
                        "validation": validation.asdict(),
                        "backtest_status": backtest_status,
                        "next_required_action": _operator_backtest_approval_next_action(
                            pid
                        ),
                    },
                )
        elif requested_policy == "operator_waiver" and accepted_kind is None:
            return ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data={
                    "ok": False,
                    "proposal_id": pid,
                    "strategy_id": sid,
                    "reason": "operator_approval_required_for_backtest_waiver",
                    "message": (
                        "operator_waiver requires operator_approved=true and "
                        "a non-empty approval_note. The final report must say "
                        "this did not pass a standard backtest."
                    ),
                    "validation": validation.asdict(),
                    "backtest_status": backtest_status,
                    "next_required_action": _operator_backtest_approval_next_action(
                        pid, policy="operator_waiver"
                    ),
                },
            )

    if bool(args.get("require_backtest", True)) and not accepted_kind:
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "ok": False,
                "proposal_id": pid,
                "strategy_id": sid,
                "reason": "backtest_required",
                "message": (
                    "Run strategy_backtest on this proposal before promotion, "
                    "or pass require_backtest=false only when the operator "
                    "explicitly skips the backtest."
                ),
                "validation": validation.asdict(),
                "backtest_status": backtest_status,
                "next_required_action": _proposal_backtest_next_action(pid),
            },
        )

    set_state(paths, pid, "approved", note=note or "approved by agent native tool")
    try:
        outcome = apply_proposal(paths, pid)
    except Exception as exc:
        return _execution_error(
            call, f"apply_proposal failed: {type(exc).__name__}: {exc}"
        )

    schedule_outcome: dict[str, Any] = {
        "trading_id": None,
        "tuning_id": None,
        "added": [],
        "updated": [],
        "removed": [],
    }
    if bool(outcome.get("ok")) and sid:
        try:
            package = load_package(paths, sid)
            sched_result = apply_strategy_schedules(paths, package)
            schedule_outcome = {
                "trading_id": sched_result.trading_id,
                "tuning_id": sched_result.tuning_id,
                "added": list(sched_result.added),
                "updated": list(sched_result.updated),
                "removed": list(sched_result.removed),
            }
        except Exception as exc:
            schedule_outcome["error"] = (
                f"{type(exc).__name__}: {exc}"
            )

    evidence_record: dict[str, Any] | None = None
    if bool(outcome.get("ok")) and sid and accepted_kind in {
        *_NONSTANDARD_BACKTEST_KINDS,
        "backtest_waiver",
    }:
        try:
            from ...trading.promotion import EvidenceStore

            artifact_ref = None
            payload: dict[str, Any] = {
                "source": "strategy_promote",
                "proposal_id": pid,
                "policy": requested_policy,
                "standard_backtest_missing": True,
                "is_meme_or_onchain": is_meme_or_onchain,
                "approval_note": approval_note if accepted_kind == "backtest_waiver" else "",
            }
            if accepted_kind in _NONSTANDARD_BACKTEST_KINDS:
                replay = _qualifying_nonstandard_backtests(nonstandard_backtests)[0]
                artifact_ref = replay.get("result_path") or replay.get("report_path")
                payload["replay"] = replay
            evidence = EvidenceStore(paths).record(
                strategy_id=sid,
                kind=accepted_kind,  # type: ignore[arg-type]
                passed=True,
                payload=payload,
                artifact_ref=artifact_ref,
                operator=str(args.get("operator") or "operator"),
            )
            evidence_record = evidence.asdict()
        except Exception as exc:
            evidence_record = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "ok": bool(outcome.get("ok")),
            "proposal_id": pid,
            "strategy_id": sid,
            "validation": validation.asdict(),
            "backtests": backtests,
            "nonstandard_backtests": nonstandard_backtests,
            "backtest_status": backtest_status,
            "evidence_record": evidence_record,
            "promotion": outcome,
            "schedules": schedule_outcome,
        },
    )


def strategy_delete_proposal_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    """Delete a pending strategy package/tuning proposal.

    Lets the agent (and, via the API route, the chat UI) drop a proposal that
    should not stay in the pending-review queue. Already-applied proposals are
    the audit trail of a landed change, so removing one needs ``force=true``.
    """

    args = call.arguments or {}
    pid = (args.get("proposal_id") or "").strip()
    if not pid:
        return _usage_error(call, "proposal_id is required")
    force = bool(args.get("force", False))
    note = str(args.get("note") or "")

    try:
        result = delete_proposal(config.paths, pid, force=force, note=note)
    except Exception as exc:
        return _execution_error(
            call, f"delete_proposal failed: {type(exc).__name__}: {exc}"
        )

    if not result.get("ok"):
        reason = str(result.get("reason") or "")
        if reason == "not_found":
            return _usage_error(call, f"proposal {pid!r} not found")
        if reason == "applied_requires_force":
            return _usage_error(
                call,
                f"proposal {pid!r} is already applied; pass force=true to "
                "delete its record (this does not roll back the applied "
                "change — use strategy rollback for that).",
            )
        return _execution_error(
            call, f"could not delete proposal {pid!r}: {reason}"
        )

    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data=result,
    )


def strategy_run_tick_handler(
    call: ToolCall,
    *,
    config: Config,
    skills: Any = None,
) -> ToolResult:
    args = call.arguments or {}
    sid = (args.get("strategy_id") or "").strip()
    if not sid:
        return _usage_error(call, "strategy_id is required")
    from ...strategies.runner import StrategyRunner  # lazy: see top-of-file note
    runner = StrategyRunner(config=config, skills=skills)
    try:
        record = runner.run_tick(
            sid,
            trigger_payload=dict(args.get("trigger_payload") or {}) or None,
            trigger_event_id=(args.get("trigger_event_id") or None),
            operator=(args.get("operator") or None),
            note=str(args.get("note") or ""),
            mode_override=(
                str(args["mode_override"]).strip().lower()
                if args.get("mode_override")
                else None
            ),
        )
    except (NeryaError, TradingError) as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(
            call, f"runner failed: {type(exc).__name__}: {exc}"
        )
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name, data=record.asdict()
    )


def strategy_kill_switch_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = call.arguments or {}
    sid = (args.get("strategy_id") or "").strip()
    if not sid:
        return _usage_error(call, "strategy_id is required")
    action = str(args.get("action") or "get").strip().lower()
    ks = StrategyKillSwitch(config.paths, sid)
    try:
        if action == "get":
            state = ks.get()
        elif action == "assert":
            reason = str(args.get("reason") or "").strip()
            if not reason:
                return _usage_error(
                    call, "action=='assert' requires a non-empty reason"
                )
            by = str(args.get("by") or "agent")
            state = ks.assert_(reason=reason, by=by)
        elif action == "clear":
            by = str(args.get("by") or "agent")
            state = ks.clear(by=by)
        else:
            return _usage_error(
                call, f"unknown action {action!r}; allowed: get|assert|clear"
            )
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"strategy_id": sid, "action": action, "state": state.asdict()},
    )


def strategy_run_history_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = call.arguments or {}
    sid = (args.get("strategy_id") or "").strip()
    if not sid:
        return _usage_error(call, "strategy_id is required")
    limit = max(1, int(args.get("limit") or 50))
    store = StrategyRunStore(config.paths, sid)
    rows = [r.asdict() for r in store.list(limit=limit)]
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"strategy_id": sid, "count": len(rows), "runs": rows},
    )


# ---------------------------------------------------------------------------
# Tuning handlers
# ---------------------------------------------------------------------------


def strategy_tuning_generate_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = call.arguments or {}
    sid = (args.get("strategy_id") or "").strip()
    if not sid:
        return _usage_error(call, "strategy_id is required")
    request = StrategyTuningGenerationRequest(
        strategy_id=sid,
        tuning_prompt=str(args.get("prompt") or ""),
        cron=str(args.get("cron") or "0 */6 * * *"),
        every_seconds=(
            int(args.get("every_seconds"))
            if args.get("every_seconds") is not None
            else None
        ),
        objectives=tuple(
            str(o) for o in (args.get("objectives") or ("risk_adjusted_return",))
        ),
        require_backtest=bool(args.get("require_backtest", True)),
        require_shadow_run=bool(args.get("require_shadow_run", False)),
    )
    try:
        result = StrategyTuningGenerator(config.paths).generate(request)
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(
            call, f"tuning generator failed: {type(exc).__name__}: {exc}"
        )
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "strategy_id": sid,
            "proposal_id": result.proposal.id if result.proposal else None,
            "files": list(result.files.keys()),
        },
    )


def strategy_tuning_run_handler(
    call: ToolCall, *, config: Config, skills: Any = None
) -> ToolResult:
    args = call.arguments or {}
    sid = (args.get("strategy_id") or "").strip()
    if not sid:
        return _usage_error(call, "strategy_id is required")
    runner = StrategyEvolutionRunner(config=config, skills=skills)
    evidence_run_ids = args.get("evidence_run_ids") or []
    if isinstance(evidence_run_ids, str):
        evidence_run_ids = [evidence_run_ids]
    evidence_session_ids = args.get("evidence_session_ids") or []
    if isinstance(evidence_session_ids, str):
        evidence_session_ids = [evidence_session_ids]
    try:
        result = runner.run_once(
            sid,
            operator=(args.get("operator") or None),
            note=str(args.get("note") or ""),
            dry_run=bool(args.get("dry_run", False)),
            trigger_event_id=(args.get("trigger_event_id") or None),
            evidence_run_ids=tuple(str(value) for value in evidence_run_ids),
            evidence_session_ids=tuple(
                str(value) for value in evidence_session_ids
            ),
        )
    except (NeryaError, TradingError) as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(
            call, f"tuning runner failed: {type(exc).__name__}: {exc}"
        )
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name, data=result.asdict()
    )


def strategy_tuning_status_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = call.arguments or {}
    sid = (args.get("strategy_id") or "").strip()
    if not sid:
        return _usage_error(call, "strategy_id is required")
    lookback = max(1, int(args.get("lookback_runs") or 200))
    try:
        pkg = load_package(config.paths, sid)
    except TradingError as exc:
        return _usage_error(call, str(exc))
    snapshot = build_snapshot(
        config.paths,
        sid,
        lookback_runs=lookback,
        package=pkg,
        config_like=config,
    )
    pending = [
        {
            "id": p.id,
            "summary": p.summary,
            "state": p.state,
            "ts": p.ts,
        }
        for p in list_proposals(config.paths)
        if p.kind == "strategy_tuning_proposal"
        and (p.target or "").endswith(sid)
        and p.state in ("draft", "pending_review", "approved")
    ]
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "strategy_id": sid,
            "tuning": pkg.manifest.tuning.asdict(),
            "snapshot": snapshot.asdict(),
            "pending_proposals": pending,
        },
    )


def strategy_tuning_snapshot_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = call.arguments or {}
    sid = (args.get("strategy_id") or "").strip()
    if not sid:
        return _usage_error(call, "strategy_id is required")
    lookback = max(1, int(args.get("lookback_runs") or 200))
    snap = build_snapshot(
        config.paths,
        sid,
        lookback_runs=lookback,
        config_like=config,
    )
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"strategy_id": sid, "snapshot": snap.asdict()},
    )


__all__ = [
    "STRATEGY_BACKTEST_SCHEMA",
    "STRATEGY_DELETE_PROPOSAL_SCHEMA",
    "STRATEGY_DRAFT_PROPOSAL_SCHEMA",
    "STRATEGY_GENERATE_PROPOSAL_SCHEMA",
    "STRATEGY_KILL_SWITCH_SCHEMA",
    "STRATEGY_PROMOTE_SCHEMA",
    "STRATEGY_RUN_HISTORY_SCHEMA",
    "STRATEGY_RUN_TICK_SCHEMA",
    "STRATEGY_SUBMIT_PROPOSAL_SCHEMA",
    "STRATEGY_TUNING_GENERATE_SCHEMA",
    "STRATEGY_TUNING_RUN_SCHEMA",
    "STRATEGY_TUNING_SNAPSHOT_SCHEMA",
    "STRATEGY_TUNING_STATUS_SCHEMA",
    "STRATEGY_VALIDATE_SCHEMA",
    "strategy_backtest_handler",
    "strategy_delete_proposal_handler",
    "strategy_draft_proposal_handler",
    "strategy_generate_proposal_handler",
    "strategy_kill_switch_handler",
    "strategy_promote_handler",
    "strategy_run_history_handler",
    "strategy_run_tick_handler",
    "strategy_submit_proposal_handler",
    "strategy_tuning_generate_handler",
    "strategy_tuning_run_handler",
    "strategy_tuning_snapshot_handler",
    "strategy_tuning_status_handler",
    "strategy_validate_handler",
]

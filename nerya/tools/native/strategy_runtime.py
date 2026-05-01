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
from pathlib import Path
from typing import Any, Optional

from ...core.config import Config
from ...core.errors import NeryaError, TradingError
from ...evolution.patch_proposal import list_proposals, set_state
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
from ...strategies.scheduler_bridge import apply_strategy_schedules
from ...strategies.performance import build_snapshot
from ...strategies.runner import StrategyRunner
from ...strategies.state import StrategyKillSwitch, StrategyRunStore
from ...strategies.validator import (
    validate_proposal_files,
    validate_strategy_package,
)
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
        "prompt": {
            "type": "string",
            "description": "Operator brief; lands in strategy.md.",
        },
        "strategy_class": {
            "type": "string",
            "enum": ["scalping", "trend", "news"],
            "default": "scalping",
        },
        "mode": {
            "type": "string",
            "enum": ["paper", "shadow", "live"],
            "default": "paper",
        },
        "markets": {"type": "array", "items": {"type": "string"}},
        "accounts": {"type": "array", "items": {"type": "string"}},
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
                "template for that path. Use this to inject the actual "
                "strategy logic and tests rather than relying on the "
                "stock scaffold."
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


STRATEGY_PROMOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proposal_id": {"type": "string"},
        "note": {"type": "string"},
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


def _usage_error(call: ToolCall, message: str) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(kind=ToolErrorKind.SCHEMA_VALIDATION, message=message),
    )


def _execution_error(call: ToolCall, message: str) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message=message),
    )


def _request_from_args(args: dict[str, Any]) -> StrategyGenerationRequest:
    return StrategyGenerationRequest(
        strategy_id=str(args.get("strategy_id") or "").strip(),
        title=str(args.get("title") or ""),
        description=str(args.get("description") or ""),
        prompt=str(args.get("prompt") or ""),
        strategy_class=str(args.get("strategy_class") or "scalping").strip().lower(),
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
        create_tuning=bool(args.get("create_tuning", False)),
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


def _read_proposal_files(
    paths,
    proposal_id: str,
) -> tuple[Optional[str], dict[str, str]]:
    """Return ``(strategy_id, files)`` for ``proposal_id``.

    The proposal stores files under ``after/strategies/<strategy_id>/<rel>``;
    we strip the ``after/strategies/<id>/`` prefix so the validator
    sees the same shape it does for promoted packages.
    """

    for prop in list_proposals(paths):
        if prop.id != proposal_id:
            continue
        after_dir = prop.path / "after" / "strategies"
        if not after_dir.exists():
            return None, {}
        candidates = [d for d in after_dir.iterdir() if d.is_dir()]
        if not candidates:
            return None, {}
        # Take the only declared strategy in the proposal — the
        # generator always writes exactly one.
        sd = candidates[0]
        files: dict[str, str] = {}
        for p in sd.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(sd).as_posix()
            try:
                files[rel] = p.read_text(encoding="utf-8")
            except OSError:
                continue
        return sd.name, files
    return None, {}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def strategy_generate_proposal_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = call.arguments or {}
    try:
        request = _request_from_args(args)
    except Exception as exc:  # type errors etc.
        return _usage_error(call, f"invalid request: {type(exc).__name__}: {exc}")

    do_validate = bool(args.get("validate", True))
    try:
        generator = StrategyCodeGenerator(config.paths)
        result = generator.generate(
            request,
            validate=do_validate,
            create_proposal_record=True,
        )
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(
            call, f"generator failed: {type(exc).__name__}: {exc}"
        )

    payload = {
        "strategy_id": request.strategy_id,
        "proposal_id": result.proposal.id if result.proposal else None,
        "validation": (
            result.validation.asdict() if result.validation is not None else None
        ),
        "files": list(result.files.keys()),
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

    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "ok": bool(outcome.get("ok")),
            "proposal_id": pid,
            "strategy_id": sid,
            "validation": validation.asdict(),
            "promotion": outcome,
            "schedules": schedule_outcome,
        },
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
    try:
        result = runner.run_once(
            sid,
            operator=(args.get("operator") or None),
            note=str(args.get("note") or ""),
            dry_run=bool(args.get("dry_run", False)),
            trigger_event_id=(args.get("trigger_event_id") or None),
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
        config.paths, sid, lookback_runs=lookback, package=pkg
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
    snap = build_snapshot(config.paths, sid, lookback_runs=lookback)
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"strategy_id": sid, "snapshot": snap.asdict()},
    )


__all__ = [
    "STRATEGY_GENERATE_PROPOSAL_SCHEMA",
    "STRATEGY_KILL_SWITCH_SCHEMA",
    "STRATEGY_PROMOTE_SCHEMA",
    "STRATEGY_RUN_HISTORY_SCHEMA",
    "STRATEGY_RUN_TICK_SCHEMA",
    "STRATEGY_TUNING_GENERATE_SCHEMA",
    "STRATEGY_TUNING_RUN_SCHEMA",
    "STRATEGY_TUNING_SNAPSHOT_SCHEMA",
    "STRATEGY_TUNING_STATUS_SCHEMA",
    "STRATEGY_VALIDATE_SCHEMA",
    "strategy_generate_proposal_handler",
    "strategy_kill_switch_handler",
    "strategy_promote_handler",
    "strategy_run_history_handler",
    "strategy_run_tick_handler",
    "strategy_tuning_generate_handler",
    "strategy_tuning_run_handler",
    "strategy_tuning_snapshot_handler",
    "strategy_tuning_status_handler",
    "strategy_validate_handler",
]

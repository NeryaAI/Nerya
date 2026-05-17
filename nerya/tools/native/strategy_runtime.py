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
            "enum": ["scalping", "trend", "news", "agent", "agent_team"],
            "default": "scalping",
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
                "without a standard backtest."
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
        execution_mode=str(args.get("execution_mode") or "").strip().lower(),
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
    "xagent",
    "x_agent",
    "goat",
    "dex",
    "swap",
    "solana:",
    "base:",
    "ethereum:",
)


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
    return any(marker in body for marker in _MEME_OR_ONCHAIN_MARKERS)


def _read_json_file(path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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
        ("custom_replay", "custom_replay_result.json", "custom_replay_report.md"),
        ("event_replay", "event_replay_result.json", "event_replay_report.md"),
    )
    for kind, result_name, report_name in patterns:
        seen: set[str] = set()
        for result_path in sorted(root.rglob(result_name)):
            key = str(result_path)
            if key in seen:
                continue
            seen.add(key)
            result = _read_json_file(result_path)
            report_path = result_path.with_name(report_name)
            out.append(
                {
                    "kind": kind,
                    "result_path": str(result_path),
                    "report_path": str(report_path) if report_path.exists() else None,
                    "ok": bool(result.get("ok")),
                    "replay_kind": result.get("replay_kind"),
                    "window": result.get("window"),
                    "events_seen": result.get("events_seen"),
                    "signals": result.get("signals"),
                    "simulated_trades": result.get("simulated_trades"),
                    "limitations": result.get("limitations") or [],
                }
            )
    return out


def _qualifying_standard_backtests(backtests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [bt for bt in backtests if bt.get("coverage_ok") is not False]


def _qualifying_nonstandard_backtests(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [artifact for artifact in artifacts if bool(artifact.get("ok"))]


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
            if accepted_kind in {"custom_replay", "event_replay", "backtest_waiver"}
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
    return {
        "strategy_root": str(root),
        "strategy_yml_path": str(root / "strategy.yml"),
        "strategy_md_path": str(root / "strategy.md"),
        "main_path": str(root / "main.py"),
        "tests_path": str(root / "tests"),
    }


def _proposal_backtest_next_action(proposal_id: str) -> dict[str, Any]:
    return {
        "tool": "strategy_backtest",
        "arguments": {
            "proposal_id": proposal_id,
            "preset": "default",
            "allow_mock": False,
        },
    }


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
    if result.proposal:
        payload["proposal_paths"] = _proposal_strategy_paths(
            config.paths,
            result.proposal.id,
            request.strategy_id,
        )
    if result.proposal and result.validation is not None and result.validation.ok:
        payload["backtest_required"] = True
        payload["next_required_action"] = _proposal_backtest_next_action(result.proposal.id)
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


def strategy_backtest_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    strategy_id = (args.get("strategy_id") or "").strip() or None
    proposal_id = (args.get("proposal_id") or "").strip() or None
    try:
        from ...skills.builtin.backtest.scripts.backtest_run import (
            run_strategy_backtest,
        )
        from ...skills.builtin.backtest.scripts.data_cache import (
            NoHistoricalDataError,
        )

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
                "requested market/timeframe. Do not retry with mock, "
                "synthetic, random, or placeholder data; either choose "
                "a market with real historical candles or ask the "
                "operator for a data source."
            ),
        }
        if proposal_id:
            try:
                sid, files = _read_proposal_files(config.paths, proposal_id)
                if _proposal_is_meme_or_onchain(sid or strategy_id, files):
                    next_required_action = {
                        "type": "custom_replay_or_operator_approval",
                        "message": (
                            "Standard OHLCV history is unavailable or not "
                            "representative for this meme/on-chain strategy. "
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
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data=_model_facing_backtest_result(result),
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
        "Use metrics/operator_summary strings exactly in the final answer. "
        "Raw numeric metrics are only in raw_metrics_file."
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
                            "custom/event replay artifact or explicit "
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
                            "No qualifying standard backtest or custom/event "
                            "replay artifact was found. The strategy can still "
                            "be promoted only after the operator explicitly "
                            "approves a standard-backtest waiver."
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
        "custom_replay",
        "event_replay",
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
            if accepted_kind in {"custom_replay", "event_replay"}:
                replay = _qualifying_nonstandard_backtests(nonstandard_backtests)[0]
                artifact_ref = replay.get("result_path")
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
    "strategy_backtest_handler",
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

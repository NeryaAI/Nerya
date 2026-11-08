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

import hashlib
import json
import logging
import re
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from jsonschema import Draft7Validator

from ...core import yaml_io
from ...core.config import Config
from ...core.errors import NeryaError, TradingError
from ...core.normalize import norm_key
from ...harness.cancellation import CancelledError
from ...evolution.patch_proposal import (
    create_proposal,
    delete_proposal,
    list_proposals,
    reseal_candidate_bundle,
    set_state,
)
from ...evolution.promotion import apply_proposal
from ...evolution.strategy_code_generator import (
    StrategyCodeGenerator,
    StrategyGenerationInputError,
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
from ...db.sqlite import connect_readonly
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
from ..tool_errors import schema_validation_result as _usage_error
from .paths import to_workspace_relative
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult


_LOG = logging.getLogger(__name__)

_RETROSPECTIVE_POLICY_REASONS = frozenset({
    "retrospective_replay_requires_operator_authorization",
    "invalid_or_expired_retrospective_authorization",
    "retrospective_authorization_invalid",
    "retrospective_authorization_policy_changed",
    "retrospective_model_route_mismatch",
    "retrospective_backend_not_bounded",
})
_RETROSPECTIVE_CONSENT = "allow_current_retrospective_replay"
_RETROSPECTIVE_DECLINE = "decline_current_retrospective_replay"


def _retrospective_consent(config: Config) -> bool | None:
    """Read the current command's durable replay consent, if any."""
    cid = str(config.get("runtime.command_id") or "").strip()
    if not cid:
        return None
    from ...db.sqlite import connect_readonly

    with closing(connect_readonly(config.paths.db)) as con:
        row = con.execute(
            "SELECT request_json FROM agent_commands WHERE command_id=?", (cid,)
        ).fetchone()
        if not row:
            return None
        request = json.loads(row[0] or "{}")
        interaction_id = request.get("interaction_id")
        if not interaction_id:
            return None
        interaction = con.execute(
            "SELECT state,payload_json,response_json FROM agent_interactions WHERE interaction_id=? AND session_id=?",
            (interaction_id, str(config.get("runtime.command_session_id") or "")),
        ).fetchone()
        if not interaction or interaction["state"] not in {"answered", "resolved"}:
            return None
        response = json.loads(interaction["response_json"] or "{}")
        payload = json.loads(interaction["payload_json"] or "{}")
        if any(q.get("id") == _RETROSPECTIVE_CONSENT for q in payload.get("questions", [])):
            answer = (response.get("answers") or {}).get(_RETROSPECTIVE_CONSENT) or {}
            selected = answer.get("selected") or []
            if selected == ["允许本次模型历史回放"]:
                return True
            return False if selected == ["暂不继续"] else None
        selected = response.get("selected") or []
        if "允许本次模型历史回放" in payload.get("choices", []):
            return "允许本次模型历史回放" in selected
        return None


def _retrospective_authorization_interaction(
    call: ToolCall, config: Config, *, reason: str
) -> ToolResult | None:
    """Pause for a clickable, task-scoped replay authorization decision."""
    consent = _retrospective_consent(config)
    if consent is True or not config.get("runtime.command_id"):
        return None
    if consent is False:
        result = ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "ok": False,
                "reason": "operator_declined_retrospective_replay",
                "authorization_reason": reason,
                "message": "Operator chose not to continue the current-model retrospective replay.",
            },
            semantic_success=False,
        )
        result.is_error = True
        result.error = ToolError(
            kind=ToolErrorKind.EXECUTION_ERROR,
            message="Operator declined this retrospective replay.",
            retryable=False,
            recovery_hint={"action": "operator_declined_retrospective_replay"},
        )
        return result
    try:
        from ...agent.interactions import create_interaction

        item = create_interaction(
            config,
            sid=str(config.get("runtime.command_session_id") or ""),
            tid=str(config.get("runtime.command_turn_id") or ""),
            call_id=call.id,
            kind="question",
            payload={
                "title": "需要确认模型历史回放",
                "message": (
                    "本次脚本 Agent 需要使用当前模型对历史前缀做有限回放。"
                    "这不是当时的真实决策，也不代表实盘交易绩效；只会使用已有"
                    "operator-owned provider、模型、预算和有效期配置。"
                    f"当前阻塞原因：{reason}"
                ),
                "questions": [{
                    "id": _RETROSPECTIVE_CONSENT,
                    "question": "允许在当前任务中重新检查授权并继续吗？",
                    "options": ["允许本次模型历史回放", "暂不继续"],
                    "multiple": False,
                }],
            },
        )
    except Exception:
        _LOG.debug("retrospective authorization interaction unavailable", exc_info=True)
        return None
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "ok": False,
            "reason": "retrospective_replay_authorization_pending",
            "authorization_reason": reason,
            "interaction": item,
            "next_required_action": {
                "tool": "strategy_backtest",
                "arguments": {"policy_check_only": True,
                              "policy_recheck_interaction_id": item["interaction_id"]},
                "message": "After this interaction is answered, recheck the existing operator-owned policy once. This does not grant authorization or start replay.",
            } if (call.arguments or {}).get("policy_check_only") is True else None,
            "message": "等待用户确认后重新检查 operator-owned 回放授权。",
        },
        semantic_success=False,
        result_protocol="user_interaction_pending",
    )




STRATEGY_GENERATE_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_]+$",
            "description": "Lowercase identifier; matches ^[a-z][a-z0-9_]+$.",
        },
        "title": {"type": "string"},
        "description": {"type": "string"},
        "strategy_class": {
            "type": "string",
            "enum": ["scalping", "trend", "news", "agent", "agent_team"],
            "default": "scalping",
            "description": "Canonical strategy family.",
        },
        "execution_mode": {
            "type": "string",
            "enum": ["script", "agent", "agent_team"],
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
        "prompt": {
            "type": "string",
            "description": (
                "Concise operator brief; lands in strategy.md. Keep this short "
                "when files are supplied, and put runnable logic in SDK files."
            ),
        },
        "schedule_cron": {"type": "string", "description": "Cron schedule. Mutually exclusive with schedule_every_seconds; set only one schedule form."},
        "schedule_every_seconds": {"type": "integer", "minimum": 1, "description": "Interval schedule in seconds. Mutually exclusive with schedule_cron; set only one schedule form."},
        "news_sources": {"type": "array", "items": {"type": "string"}},
        "subagents": {"type": "array", "items": {"type": "string"}},
        "policy_overrides": {"type": "object"},
        "llm_policy_overrides": {"type": "object"},
        "create_tuning": {
            "type": "boolean",
            "default": False,
            "description": (
                "Generate a review-plan scaffold for strategy_author to tailor after "
                "implementing the strategy. Supply its tuning config and prompt file; "
                "the review schedule defaults off. Does not run a review or apply changes."
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
        "validate": {"type": "boolean", "default": False},
    },
    "required": ["strategy_id"],
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


STRATEGY_IMPORT_EXTERNAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Absolute or workspace-relative path(s) to the strategy "
                ".py file(s), or a single directory containing them."
            ),
        },
        "strategy_id": {
            "type": "string",
            "description": (
                "Package id (snake_case). Defaults to the strategy class "
                "name slugified."
            ),
        },
        "framework": {
            "type": "string",
            "enum": ["auto", "freqtrade", "vnpy"],
            "description": (
                "Foreign quant framework the source was written for. "
                "'auto' AST-scans the source; default 'auto'."
            ),
        },
        "title": {"type": "string"},
        "markets": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Nerya market ids to trade (e.g. PAPER:BTCUSDT). Defaults "
                "to PAPER:BTCUSDT with a warning when omitted."
            ),
        },
        "accounts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Execution accounts; defaults to [paper_main].",
        },
        "timeframe": {
            "type": "string",
            "description": (
                "Candle timeframe override (5m / 1h / ...). Defaults to "
                "the strategy's own declared timeframe."
            ),
        },
        "settings": {
            "type": "object",
            "description": (
                "Strategy parameters (vnpy settings dict / freqtrade "
                "hyperopt parameter overrides), e.g. {\"fast_window\": 8}."
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["paper", "shadow", "live"],
            "default": "paper",
        },
        "stake_amount": {
            "type": "number",
            "description": (
                "Per-entry notional in USD for freqtrade-style sizing; "
                "0 falls back to policy default_order_usd."
            ),
        },
        "overwrite": {
            "type": "boolean",
            "default": False,
            "description": "Replace an existing package with the same id.",
        },
    },
    "required": ["sources"],
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
    "additionalProperties": False,
    "properties": {
        "files": {
            "type": "object", "additionalProperties": {"type": "string"},
            "description": "Optional authored files keyed by safe package-relative paths (main.py, strategy.yml, strategy.md, tests/test_contract.py). Prefer omitting files to scaffold once, then author incrementally at the returned candidate paths with edit_file/write_file. A complete inline bundle is allowed when naturally small. Never put code in prompt."
        },
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
            "enum": ["scalping", "trend", "news", "agent", "agent_team"],
            "default": "scalping",
            "description": "Canonical strategy family used to pick the scaffold template.",
        },
        "execution_mode": {
            "type": "string",
            "enum": ["script", "agent", "agent_team"],
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
                "BYBIT:SOLUSDT only for Bybit spot. For lending rate policies, copy "
                "the discovered pool canonical_identity.market (chain-qualified asset, not a pair)."
            ),
        },
        "accounts": {"type": "array", "items": {"type": "string"}},
        "chain": {
            "type": "string", "pattern": r"\S",
            "description": "Optional structured chain identity retained in strategy.yml.",
        },
        "protocol": {
            "type": "string", "pattern": r"\S",
            "description": "Optional structured protocol identity retained in strategy.yml.",
        },
        "timeframe": {
            "type": "string", "pattern": r"\S",
            "description": (
                "Explicit candle timeframe persisted in strategy.yml; independent of scheduling. "
                "Omission preserves existing behavior. Must match files['strategy.yml'].timeframe "
                "when both are supplied; conflicts are rejected. For from_strategy_id, omit "
                "this field and edit the cloned candidate manifest instead."
            ),
        },
        "schedule_cron": {"type": "string", "description": "Cron schedule. Mutually exclusive with schedule_every_seconds; set only one schedule form."},
        "schedule_every_seconds": {"type": "integer", "minimum": 1, "description": "Interval schedule in seconds. Mutually exclusive with schedule_cron; set only one schedule form."},
        "news_sources": {"type": "array", "items": {"type": "string"}},
        "subagents": {"type": "array", "items": {"type": "string"}},
        "policy_overrides": {"type": "object"},
        "llm_policy_overrides": {"type": "object"},
        "create_tuning": {"type": "boolean", "default": False, "description": "Generate a review-plan scaffold, then tailor tuning and its prompt file to the completed strategy. The review schedule defaults off; no review execution or automatic application."},
        "tuning_prompt": {"type": "string"},
        "tuning_cron": {"type": "string"},
        "tuning_objectives": {"type": "array", "items": {"type": "string"}},
        "extra_subagent_prompts": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Map of subagent name -> agent.md body.",
        },
        "validate": {"type": "boolean", "default": False,
            "description": "Optional validation diagnostics; saving a draft does not require validation or execution."},
        "from_strategy_id": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_]+$",
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
        "policy_check_only": {
            "type": "boolean", "default": False,
            "description": "Read-only authorization check only for historical replay that actually calls a model. Candidate creation/edits and deterministic backtests do not need it. Pass true without a strategy/proposal or replay settings; after an answered model-replay authorization question, include policy_recheck_interaction_id for one fresh check. Does not start replay, inspect remaining budget, resolve secrets or grant authorization.",
        },
        "policy_recheck_interaction_id": {
            "type": "string",
            "description": "Only with policy_check_only=true, after the user answers the replay authorization question. Use the exact persisted interaction_id from that question for one bounded recheck, including when the original policy call is cached. Not a retry nonce or authorization grant.",
        },
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
        "expected_source_revision": {
            "type": "string", "pattern": "^[a-f0-9]{64}$",
            "description": "Bind replay to strategy_validate.verification.revision. After correcting candidate source or replay_inputs.json, validate and submit the same candidate, then use its new revision for one corrected retry. Not a retry nonce; unchanged calls remain suppressed.",
        },
        "preflight_only": {"type": "boolean", "default": False,
            "description": "Native engine only. Validate configuration, Python syntax, dependencies and known replay surfaces without downloading data or executing the strategy. Omit for freeform."},
        "data_mode": {"type": "string", "enum": ["download", "local"],
            "description": "Native engine only; omit for freeform. local reads prepared historical data without network. download prepares missing local ranges. Freeform obtains data from the saved replay_inputs.json instead."},
        "engine": {"type": "string", "enum": ["auto", "native", "freeform", "rates"], "default": "auto",
            "description": "native runs the saved candle entrypoint; freeform runs strategy-local research; rates runs the saved rate-policy entrypoint with host accounting and replay_inputs.json. auto selects an explicit params.rate_replay contract before custom scripts. Never switch engines to change the thesis."},
        "config_path": {"type": "string", "description": "Native engine only; omit for freeform. Optional existing advanced config; no separate file is required for normal native replay."},
        "override_candidate_backtest_defaults": {
            "type": "boolean",
            "default": False,
            "description": (
                "Compatibility field. Explicit settings already override candidate replay defaults."
            ),
        },
        "settings": {
            "type": "object", "additionalProperties": False,
            "description": (
                "Native-engine replay settings; omit for freeform. Explicit settings override candidate defaults, "
                "allowing separate runs for different windows and timeframes. One call preflights, "
                "reuses/downloads history, checks coverage and runs the strategy."
            ),
            "properties": {
                "window_days": {"type": "integer", "minimum": 1},
                "start_utc": {"type": "string"}, "end_utc": {"type": "string"},
                "tf": {"type": "string"},
                "timeframes": {"type": "array", "items": {"type": "string"}},
                "warmup_bars": {"type": "integer", "minimum": 0},
                "initial_capital_usd": {"type": "number", "exclusiveMinimum": 0},
                "max_open_trades": {"type": "integer", "minimum": 1},
                "allow_short": {"type": "boolean"},
                "data_mode": {"type": "string", "enum": ["download", "local"]},
                "coverage_policy": {"type": "string", "enum": ["strict", "allow_partial"]},
                "allow_timeframe_fallback": {"type": "boolean"},
                "agent_replay": {"type": "boolean", "description": "Use bounded current-model retrospective replay for a script-gated confirmation role. Requires operator-owned authorization; not contemporaneous or live performance evidence."},
                "max_run_seconds": {"type": "number", "exclusiveMinimum": 0},
                "fee_bps": {"type": "number", "minimum": 0,
                    "description": "Scalar fee shorthand applied to every venue unless fee_bps_by_venue overrides it."},
                "slippage_bps": {"type": "number", "minimum": 0,
                    "description": "Scalar slippage shorthand applied to every venue unless slip_bps_by_venue overrides it."},
                "fee_bps_by_venue": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0}},
                "slip_bps_by_venue": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0}}
            }
        },
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


def _backtest_public_message(exc: Exception, reason: str) -> str:
    """Return short operator-facing copy; raw diagnostics stay in the receipt."""
    if reason == "backtest_sdk_unsupported" and getattr(exc, "surface", None) == "subagents":
        return "历史回放不支持 Agent 确认面，已停止普通回放；需要受授权的 Agent 回放。"
    if reason == "decision_budget_exhausted":
        return "受授权的模型回放预算不可用，已停止重试；部分回放证据已保留。"
    if reason in {
        "retrospective_authorization_invalid",
        "retrospective_authorization_policy_changed",
        "invalid_or_expired_retrospective_authorization",
        "retrospective_replay_requires_operator_authorization",
    }:
        return "模型回放授权不可用或已变化，已停止重试；请先检查授权状态。"
    if reason == "decision_request_unconfirmed":
        return "模型回放存在未确认的请求，已停止重试；请先核对已有执行记录。"
    if reason == "lp_cadence_data_incomplete":
        return "LP 回放所需状态或区间证据尚未齐备，已停止重试；请先补齐缺失证据并核对状态冲突。"
    if reason == "model_quota_exhausted":
        return "范围核对所需的模型路由额度已耗尽，回放尚未执行；请恢复该模型路由后再继续。"
    return "回放未完成，已保留候选和执行证据；具体恢复动作见执行详情。"


def _backtest_artifact_refs(exc: Exception, workspace_root: Path) -> dict[str, str]:
    root = Path(workspace_root).resolve()
    refs: dict[str, str] = {}
    for key in ("run_receipt", "failure_path"):
        raw = getattr(exc, key, None)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            path = Path(raw).resolve()
            path.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if path.is_file():
            refs[key] = to_workspace_relative(path, root)
    return refs


def _backtest_bounded_diagnostics(exc: Exception) -> dict[str, Any]:
    from ...core.redaction import redact_display_dict
    from ...skills.builtin.backtest.scripts.lp_state_requirements import LPDataIncomplete
    from ...skills.builtin.backtest.scripts.process_replay import ReplayWorkerError
    receipt = getattr(exc, "receipt", None)
    if not isinstance(receipt, dict):
        return {}
    if isinstance(exc, LPDataIncomplete):
        return {key: receipt[key] for key in (
            "required_state_count", "matched_state_count", "missing_state_count",
            "conflicting_state_count", "interval_count", "verified_interval_count",
            "missing_interval_count", "pending_state_interval_count",
        ) if type(receipt.get(key)) is int and receipt[key] >= 0}
    allowed = {"provider", "action", "reason", "failure_stage", "rpc_provider",
               "rpc_method", "request_count", "http_status", "rpc_error_code",
               "coverage_complete", "safe_to_retry", "stop_reason"}
    if isinstance(exc, ReplayWorkerError):
        allowed.update({"type", "message", "surface"})
    diagnostics = redact_display_dict({key: receipt[key] for key in allowed if key in receipt
                                       and isinstance(receipt[key], (str, int, bool))})
    # A response digest is a public integrity identifier, not a credential.
    # Restore its bounded 64-hex representation after generic redaction so
    # recovery receipts remain comparable without exposing provider content.
    response_sha256 = receipt.get("response_sha256")
    if isinstance(response_sha256, str) and re.fullmatch(r"[0-9a-fA-F]{64}", response_sha256):
        diagnostics["response_sha256"] = response_sha256.lower()
    return {key: value[:1000] if isinstance(value, str) else value
            for key, value in diagnostics.items()}


def _project_backtest_error(result: ToolResult) -> ToolResult:
    """Keep recovery visible when a consumer projects only ToolError.message."""
    body = result.content[0].data
    error = result.error
    # The producer receipt and the compact recovery summary are distinct views.
    # Reprojection must not replace the already-sanitized receipt with the summary.
    body.setdefault('diagnostics', error.recovery_hint.get('diagnostics', {}))
    detail = {key: body[key] for key in (
        'reason', 'backtest_status', 'strategy_id', 'proposal_id', 'next_required_action')}
    detail.update({key: error.recovery_hint[key] for key in ('artifacts', 'diagnostics')
                   if key in error.recovery_hint})
    detail['retryable'] = error.retryable
    if error.retryable is False:
        detail['retry_instruction'] = 'Stop; do not repeat the unchanged replay.'
    body.update({key: detail[key] for key in ('retryable', 'retry_instruction') if key in detail})
    error.detail = detail
    # Authorization/reconciliation failures already expose their bounded
    # recovery contract through the structured tool body and recovery_hint.
    # Keep the public message short so consumers that render only
    # ToolError.message do not dump the same JSON diagnostic as prose.
    if body.get('reason') in {
        'retrospective_replay_requires_operator_authorization',
        'invalid_or_expired_retrospective_authorization',
        'retrospective_authorization_invalid',
        'retrospective_authorization_policy_changed',
        'decision_budget_exhausted',
        'retrospective_deadline',
    }:
        error.message = body['message']
    else:
        error.message = body['message'] + '\n' + json.dumps(detail, ensure_ascii=False)
    return result


def _backtest_receipt_diagnostics(exc: Exception, summary: dict[str, Any]) -> dict[str, Any]:
    from ...core.redaction import redact_display_dict
    from ...skills.builtin.backtest.scripts.data_cache import NoHistoricalDataError
    from ...skills.builtin.backtest.scripts.decision_producer import DecisionProducerError

    receipt = getattr(exc, 'receipt', None)
    if not isinstance(receipt, dict):
        return dict(summary)
    if isinstance(exc, DecisionProducerError):
        fields = {'reason', 'label', 'performance_evidence', 'authorization_id',
                  'calls', 'max_calls', 'reserved_tokens', 'max_tokens',
                  'remaining_calls', 'remaining_tokens', 'required_reservation_tokens',
                  'deadline', 'request_sent', 'deadline_source',
                  'requires_receipt_reconciliation'}
        diagnostics = {key: value for key, value in receipt.items()
                       if key in fields and isinstance(value, (str, int, float, bool))}
        if isinstance(receipt.get('exhausted_limits'), list):
            diagnostics['exhausted_limits'] = [value for value in receipt['exhausted_limits']
                                              if value in ('calls', 'tokens', 'deadline')]
    elif isinstance(exc, NoHistoricalDataError):
        fields = {'market', 'timeframe', 'status', 'start', 'end', 'end_exclusive',
                  'data_mode', 'requests', 'rows', 'missing_bars', 'expected_bars',
                  'cached_rows', 'derived_rows', 'downloaded_rows', 'offline', 'resumable',
                  'job_id', 'complete', 'first_timestamp', 'last_timestamp'}
        diagnostics = {key: value for key, value in receipt.items()
                       if key in fields and isinstance(value, (str, int, float, bool))}
        for key in ('markets', 'timeframes'):
            if isinstance(receipt.get(key), list):
                diagnostics[key] = [value for value in receipt[key][:100] if isinstance(value, str)]
        if isinstance(receipt.get('downloads'), dict):
            diagnostics['downloads'] = {market: {tf: {
                key: value for key, value in download.items()
                if key in fields and isinstance(value, (str, int, float, bool))
            } for tf, download in timeframes.items() if isinstance(download, dict)}
                for market, timeframes in receipt['downloads'].items() if isinstance(timeframes, dict)}
    else:
        return dict(summary)
    return {**redact_display_dict(diagnostics), **summary}


def _backtest_error(call: ToolCall, exc: Exception, *, reason: str, strategy_id: str | None,
                    proposal_id: str | None, workspace_root: Path | None = None) -> ToolResult:
    """Keep diagnostics and target identity even when no report was produced."""
    from ...skills.builtin.backtest.scripts.decision_producer import DecisionProducerError, ResponseFailure
    from ...skills.builtin.backtest.scripts.decision_tape import DecisionTapeError
    from ...skills.builtin.backtest.scripts.lp_state_requirements import LPDataIncomplete
    from ...strategies.execution_contract import IncompleteStrategyError
    from ...skills.builtin.backtest.scripts.preflight import BacktestPreflightError

    protocol_gap = isinstance(exc, BacktestPreflightError) and any(
        isinstance(row, dict) and row.get('code') == 'protocol_replay_model_required'
        for row in exc.receipt.get('blockers', []))
    if protocol_gap:
        reason = 'protocol_replay_model_required'
    history_gap = not protocol_gap and isinstance(exc, BacktestPreflightError) and any(
        isinstance(row, dict) and row.get('code') == 'incomplete_history'
        for row in exc.receipt.get('blockers', []))
    if history_gap:
        reason = 'backtest_data_incomplete'

    # Match producer-owned errors, never a strategy's arbitrary error text.
    producer_reasons = {
        "decision_budget_exhausted": "decision_budget_exhausted",
        "decision_provider_exceeded_budget": "decision_provider_exceeded_budget",
        "decision input exceeds per-call budget": "decision_input_budget_exceeded",
        "invalid decision output budget": "decision_output_budget_invalid",
        "invalid decision reasoning policy": "decision_reasoning_policy_invalid",
        "invalid retrospective replay authorization": "retrospective_authorization_invalid",
        "authorization policy changed": "retrospective_authorization_policy_changed",
    }
    policy_reasons = {
        "retrospective_replay_requires_operator_authorization",
        "invalid_or_expired_retrospective_authorization",
        "retrospective_model_route_mismatch",
        "retrospective_backend_not_bounded",
    }
    reconciliation_reasons = {
        "decision_request_unconfirmed; reconcile before retry": "decision_request_unconfirmed",
        "decision_response_ledger_conflict": "decision_response_ledger_conflict",
        "unsafe decision ledger": "decision_ledger_unsafe",
    }
    code = str(exc)
    retrospective_action = None
    if isinstance(exc, DecisionProducerError):
        if code in producer_reasons:
            reason = producer_reasons[code]
            retrospective_action = "review_retrospective_authorization"
        elif code in reconciliation_reasons:
            reason = reconciliation_reasons[code]
            retrospective_action = "reconcile_retrospective_request"
        else:
            reason = (code if code in {failure.value for failure in ResponseFailure}
                      else "decision_request_failed_response" if code == "decision_request_failed_response; no automatic retry"
                      else "decision_producer_failed")
            retrospective_action = "inspect_retrospective_response"
    elif (isinstance(exc, TradingError) and code in policy_reasons
          or isinstance(exc, DecisionTapeError) and exc.code == "retrospective_deadline"):
        reason = code
        retrospective_action = "review_retrospective_authorization"
    retrospective_blocked = retrospective_action in {
        "review_retrospective_authorization", "reconcile_retrospective_request",
    }
    incomplete = isinstance(exc, IncompleteStrategyError)
    if incomplete:
        reason = 'strategy_implementation_incomplete'
    lp_cadence_gap = isinstance(exc, LPDataIncomplete)
    if lp_cadence_gap:
        reason = 'lp_cadence_data_incomplete'
    message = _backtest_public_message(exc, reason)
    next_required_action = (
        {
            "type": "restart_service_before_replay",
            "message": (
                "The replay worker and API loaded different runtime versions. "
                "Restart the Nerya service, then rerun the same strategy_backtest "
                "request. Do not mix SDK versions or change the strategy thesis."
            ),
        }
        if reason == "runtime_version_changed"
        else {
            "type": "repair_replay_contract" if reason == "backtest_sdk_unsupported" else "inspect_backtest_error",
            "message": "Do not retry the unchanged call or invent performance. Preserve the strategy thesis and risk controls; fix the indicated contract or use a documented historical execution model, then rerun.",
        }
    )
    if reason == "model_quota_exhausted":
        next_required_action = {
            "type": "restore_scope_model_quota", "automatic": False,
            "preserve_candidate": True, "preserve_requested_constraints": True,
            "message": (
                "The model route used for request-scope extraction is quota exhausted. "
                "The backtest did not start. Restore the admitted model route or its quota, "
                "then retry this same request once; do not change the candidate, scope, "
                "window or replay inputs and do not switch routes automatically."
            ),
        }
    if reason == "backtest_sdk_unsupported" and getattr(exc, "surface", None) == "subagents":
        next_required_action = {
            "type": "inspect_agent_replay_policy",
            "tool": "Skill",
            "arguments": {"skill": "backtest", "file": "references/replay-capabilities.md"},
            "message": (
                "Load the documented script confirmation replay policy. Preserve the same candidate, "
                "window, markets, sizing and risk controls unchanged. Do not replace Agent decisions "
                "with a stub, a pure-signal strategy, or mock_surfaces.subagents.mode='replay'. "
                "Only with an existing operator-authorized bounded model replay, merge settings.agent_replay=true "
                "into the same strategy_backtest request; the producer validates authorization and limits. "
                "This hint does not establish authorization or enable a fallback. If authorization is absent "
                "or rejected, keep replay blocked and report that producer reason. Do not search source, "
                "credential files or ledgers, change policy, restart services, or repeat an unchanged failure."
            ),
        }
        target = {"proposal_id": proposal_id} if proposal_id else {"strategy_id": strategy_id} if strategy_id else {}
        if target:
            next_required_action["conditional_replay"] = {
                "tool": "strategy_backtest",
                "arguments_patch": {**target, "settings": {"agent_replay": True}},
                "merge_settings": True,
                "preserve_requested_constraints": True,
                "requires_operator_authorization": True,
                "automatic": False,
            }
    if history_gap:
        next_required_action = {
            'type': 'prepare_historical_data', 'tool': 'historical_data',
            'automatic': False, 'reason': reason,
            'preserve_candidate': True, 'preserve_requested_constraints': True,
            'message': 'Inspect the receipt datasets with historical_data action=inspect using their exact '
                       'market, timeframe and full start/end bounds, including warmup. With existing download '
                       'permission, use action=download for that same window; retained rows are reused and only '
                       'missing ranges are fetched. Preserve the candidate and replay config. Do not shorten '
                       'the window, fabricate history, enable mock data or substitute a proxy. Do not rerun '
                       'replay until the producer verifies complete coverage.',
        }
    if incomplete:
        next_required_action = {
            'type': 'implement_candidate', 'preserve_candidate': True,
            'files': [exc.path], 'skill': 'strategy_author', 'reference': exc.template,
            'message': 'Implement the requested strategy in the saved candidate, then call strategy_validate. '
                       'Its unchanged scaffold cannot be submitted or replayed. Preserve the requested resource, '
                       'window and risk controls. Do not retry unchanged code or change data sources to bypass this check.',
        }
    if protocol_gap:
        next_required_action = {
            'type': 'provide_protocol_replay', 'preserve_candidate': True,
            'reason': 'protocol_replay_model_required',
            'skill': 'backtest', 'file': 'references/replay-capabilities.md',
            'message': 'This replay requires protocol history and a matching execution model. '
                       'Preserve the candidate, market and requested window. Candles or APY alone '
                       'cannot establish pool/reserve state, fees, liquidity or cashflows. '
                       'Keep the replay blocked until a supported producer supplies those inputs; '
                       'do not retry unchanged or substitute a different protocol.',
        }
        from ...core.paths import WorkspacePaths
        if workspace_root is not None and _target_has_freeform_backtest_script(
                WorkspacePaths(workspace_root), strategy_id=strategy_id, proposal_id=proposal_id):
            next_required_action = {
                'type': 'select_existing_replay', 'tool': 'strategy_backtest',
                'reason': reason, 'automatic': False, 'preserve_candidate': True,
                'preserve_requested_constraints': True,
                'arguments_patch': {**({'proposal_id': proposal_id} if proposal_id else
                                      {'strategy_id': strategy_id}), 'engine': 'auto', 'allow_mock': False},
                'message': 'Native candle preflight rejected the selected engine before execution. '
                    'This same package already has a custom replay script. Use engine=auto on the '
                    'same candidate to select its saved implementation; no new protocol producer is needed '
                    'for this routing correction. Preserve the exact window and producer_ref with matching '
                    'original args. Normal input, scope and execution checks still apply; this grants '
                    'no acquisition, trading or model-call permission. Do not repeat engine=native.',
            }
    if lp_cadence_gap:
        target = {"proposal_id": proposal_id} if proposal_id else {"strategy_id": strategy_id} if strategy_id else {}
        next_required_action = {
            "type": "inspect_lp_cadence_data", "automatic": False,
            "preserve_candidate": True, "preserve_requested_constraints": True, "preserve_schedule": True,
            "skill": "backtest", "file": "references/history-data.md",
            "tool": "historical_data" if target else "Skill",
            "arguments": ({"action": "inspect", "dataset_kind": "lp_state_requirements", **target}
                          if target else {"skill": "backtest", "file": "references/history-data.md"}),
            "replay_model": {"selected": "cadence", "configured_by": "params.lp_replay.mode",
                "legacy_default": "saved execution cron when mode is absent",
                "supported": ["cadence", "sampled"],
                "sampled_reference": {"skill": "strategy_author", "file": "references/lp-sampled-research.md"}},
            "message": (
                "The selected replay model is cadence. If the admitted task is observation-driven sampled "
                "research rather than scheduled execution, declare params.lp_replay.mode=sampled in the same "
                "candidate, select a task-bound snapshot with matching original args, validate and replay. "
                "This produces partial research only, never cadence coverage or performance evidence. "
                "Preserve an explicitly requested cadence; changing modes cannot satisfy that request. "
                "Inspect the saved candidate's LP state requirements and read the backtest Skill's history-data reference. "
                "Preserve the candidate, window, schedule, pool, tick range and risk controls. "
                "Repair only through the inspector's bounded producer cursor (next_download_args), within existing authorization; "
                "do not invent timestamps, reset budgets or start a new job without authorization. "
                "Do not repeat the unchanged replay until the producer verifies all required states and intervals are ready."
            ),
        }
    if retrospective_action:
        guidance = {
            "review_retrospective_authorization": (
                "Retrospective replay is blocked by its authorization, budget or deadline. "
                "Stop replay and report the producer reason for operator review."
            ),
            "reconcile_retrospective_request": (
                "Reconcile the existing decision request and budget ledger with the provider outcome "
                "through the operator before any retry. A new authorization does not resolve this request."
            ),
            "inspect_retrospective_response": (
                "Inspect the producer's recorded response diagnostics and report the failed decision. "
                "A rejected response remains charged; it is not a strategy validation failure."
            ),
        }
        next_required_action = {
            "type": retrospective_action,
            "reason": reason,
            "automatic": False,
            "preserve_requested_constraints": True,
            "preserve_partial_artifacts": True,
            "message": guidance[retrospective_action] + (
                " Preserve the partial decision tape, run/failure "
                "receipts, ledger and candidate/window/risk constraints; replay verification is incomplete. "
                "Do not retry automatically, reset budgets, extend deadlines, create a new authorization, "
                "or change the strategy or time window. Further model calls require an explicit operator decision."
            ),
        }
        if retrospective_action == "review_retrospective_authorization":
            next_required_action["requires_operator_authorization"] = True
        elif retrospective_action == "reconcile_retrospective_request":
            next_required_action["requires_operator_reconciliation"] = True
    recovery_hint = {"action": next_required_action["type"], **next_required_action,
                     "next_required_action": next_required_action}
    if workspace_root is not None:
        artifacts = _backtest_artifact_refs(exc, workspace_root)
        if artifacts:
            recovery_hint["artifacts"] = artifacts
    diagnostics = _backtest_bounded_diagnostics(exc)
    if history_gap:
        diagnostics.update({key: exc.receipt[key] for key in (
            'requested_start_utc', 'requested_end_utc', 'requested_window_complete',
            'end_exclusive', 'coverage_policy',
        ) if isinstance(exc.receipt.get(key), (str, bool, int))})
        diagnostics['datasets'] = [{
            **{key: row[key] for key in (
                'market', 'timeframe', 'start', 'end', 'end_exclusive',
                'rows', 'expected_bars', 'missing_bars', 'complete',
            ) if isinstance(row.get(key), (str, bool, int))},
            'missing_ranges': [{key: gap[key] for key in ('start', 'end', 'bars')
                                if type(gap.get(key)) is int}
                               for gap in row.get('missing_ranges', [])[:32] if isinstance(gap, dict)],
            'download': {key: row.get('download', {})[key] for key in ('job_id', 'resumable')
                         if isinstance(row.get('download', {}).get(key), (str, bool))},
        } for row in exc.receipt.get('datasets', [])[:100] if isinstance(row, dict)]
    if diagnostics:
        recovery_hint["diagnostics"] = (
            {key: value for key, value in diagnostics.items() if key != 'reason'}
            if isinstance(exc, DecisionProducerError) else diagnostics)
    result = ToolResult.from_json(tool_use_id=call.id, name=call.name,
        semantic_success=False,
        data={"result_type": "backtest_result", "ok": False,
            "backtest_status": "blocked" if incomplete or protocol_gap or lp_cadence_gap or retrospective_blocked or reason in {"runtime_version_changed", "backtest_sdk_unsupported", "backtest_dependency_missing", "backtest_preflight_failed", "backtest_data_incomplete", "backtest_data_invalid", "backtest_invalid_input"} else "failed",
            "strategy_id": strategy_id, "proposal_id": proposal_id,
            "reason": reason, "message": message,
            "surface": getattr(exc, "surface", None),
            "phase": getattr(exc, "replay_phase", None),
            "run_receipt": getattr(exc, "run_receipt", None),
            "failure_path": getattr(exc, "failure_path", None),
            "diagnostics": _backtest_receipt_diagnostics(exc, diagnostics),
            "missing_module": getattr(exc, "module", None),
            "next_required_action": next_required_action})
    result.is_error = True
    result.error = ToolError(
        kind=ToolErrorKind.EXECUTION_ERROR,
        message=message,
        retryable=False if incomplete or protocol_gap or lp_cadence_gap or retrospective_action or reason == "runtime_version_changed" else None,
        recovery_hint=recovery_hint,
    )
    return _project_backtest_error(result)




def _manifest_execution_mode(content: str) -> str:
    try:
        raw = yaml_io.loads(content, default={}) or {}
    except Exception:
        return ""
    if not isinstance(raw, dict):
        return ""
    return norm_key(str(raw.get("execution_mode") or ""))




def _request_from_args(args: dict[str, Any]) -> StrategyGenerationRequest:
    strategy_class = str(args.get("strategy_class") or "scalping").strip().lower()
    execution_mode = str(args.get("execution_mode") or "").strip().lower()
    prompt = str(args.get("prompt") or "")
    requested_subagents = tuple(str(s) for s in (args.get("subagents") or ()))
    # The replay producer requires the role declared by the script-gated Agent
    # contract. Preserve ordinary scripts, but make the canonical role explicit
    # when the user's request names this workflow and the caller omitted it.
    prompt_key = prompt.casefold()
    script_agent_request = (
        execution_mode in {"", "script"}
        and strategy_class == "trend"
        and ("script-driven agent" in prompt_key or "脚本驱动 agent" in prompt_key)
    )
    if script_agent_request and not requested_subagents:
        requested_subagents = ("market_analyst",)
    return StrategyGenerationRequest(
        strategy_id=str(args.get("strategy_id") or "").strip(),
        title=str(args.get("title") or ""),
        description=str(args.get("description") or ""),
        prompt=prompt,
        strategy_class=strategy_class,
        execution_mode=execution_mode,
        mode=str(args.get("mode") or "paper").strip().lower(),
        markets=tuple(str(m) for m in (args.get("markets") or ())),
        accounts=tuple(str(a) for a in (args.get("accounts") or ())),
        chain=(str(args["chain"]).strip() if args.get("chain") is not None else None),
        protocol=(str(args["protocol"]).strip() if args.get("protocol") is not None else None),
        timeframe=args.get("timeframe"),
        schedule_cron=str(args.get("schedule_cron") or "").strip(),
        schedule_every_seconds=(
            int(args.get("schedule_every_seconds"))
            if args.get("schedule_every_seconds") is not None
            else None
        ),
        news_sources=tuple(str(s) for s in (args.get("news_sources") or ())),
        subagents=requested_subagents,
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

    return read_proposal_strategy_files(paths, proposal_id)


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


def _proposal_uses_event_replay(
    strategy_id: str | None,
    files: dict[str, str],
) -> bool:
    """Use the replay policy declared by canonical market identifiers."""
    del strategy_id
    try:
        manifest = yaml_io.loads(files.get("strategy.yml", ""), default={}) or {}
    except Exception:
        manifest = {}
    markets = manifest.get("markets") if isinstance(manifest, dict) else None
    values = [markets] if isinstance(markets, str) else markets if isinstance(markets, list) else []
    for market in values:
        venue = str(market or "").split(":", 1)[0].strip().upper()
        if venue.endswith("_ONCHAIN") or venue in {"ONCHAIN", "POLYMARKET"}:
            return True
    return False


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
        "workflow_path": _rel(root / "workflow.json"),
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


def _target_backtest_defaults(paths, *, strategy_id: str | None, proposal_id: str | None) -> dict[str, Any]:
    """Read candidate-owned backtest assumptions for native Agent calls."""
    manifest: dict[str, Any] = {}
    try:
        if proposal_id:
            _sid, files = _read_proposal_files(paths, proposal_id)
            manifest = yaml_io.loads(files.get("strategy.yml", ""), default={}) or {}
        elif strategy_id:
            path = paths.strategies / strategy_id / "strategy.yml"
            manifest = yaml_io.load(path, default={}) or {} if path.is_file() else {}
    except Exception:
        return {}
    raw = manifest.get("backtest") if isinstance(manifest, dict) else None
    return dict(raw) if isinstance(raw, dict) else {}




def strategy_generate_proposal_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    from ...strategies.tool_verification import attach_inline_validation
    return attach_inline_validation(_strategy_generate_proposal_handler(call, config=config), config=config)


def _strategy_generate_proposal_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = call.arguments or {}
    try:
        request = _request_from_args(args)
        result = StrategyCodeGenerator(config.paths).generate(
            request, validate=bool(args.get("validate", False)), create_proposal_record=True)
    except StrategyGenerationInputError as exc:
        return _draft_input_error(call, exc.field, exc.correction)
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(call, f"generator failed: {type(exc).__name__}: {exc}")
    payload = {
        "ok": True, "strategy_id": result.request.strategy_id,
        "strategy_class": result.request.strategy_class,
        "execution_mode": _manifest_execution_mode(result.files["strategy.yml"]),
        "proposal_id": result.proposal.id if result.proposal else None,
        "validation": result.validation.asdict() if result.validation is not None else None,
        "files": list(result.files), "backtest_required": False,
    }
    if result.proposal:
        payload["proposal_paths"] = _proposal_strategy_paths(
            config.paths, result.proposal.id, request.strategy_id)
    return ToolResult.from_json(tool_use_id=call.id, name=call.name,
                                data=payload, semantic_success=True)


_DRAFT_SEED_SKIP_DIRS = {"runs", "logs", "state", "versions", "backtests"}


def _draft_next_steps(
    proposal_id: str,
    paths_map: dict[str, str],
    file_paths: list[str] | None = None,
) -> list[str]:
    return [
        "Editable candidate saved. Use proposal_paths to implement or revise this same package.",
        f"Submit for review with strategy_submit_proposal(proposal_id='{proposal_id}') when useful; "
        "validation and backtests are available independently when requested.",
        "Saving or submitting does not promote, schedule, activate or trade.",
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
    seed.update(args.get("files") or {})
    manifest = yaml_io.loads(seed.get("strategy.yml", ""), default={}) or {}
    manifest["strategy_id"] = strategy_id
    if "id" in manifest:
        manifest["id"] = strategy_id
    if "timeframe" in args:
        manifest["timeframe"] = args["timeframe"]
    seed["strategy.yml"] = yaml_io.dumps(manifest)
    validation = (validate_proposal_files(strategy_id=strategy_id, files=seed)
                  if args.get("validate", False) else None)
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
        "ok": True,
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
        "file_paths": [
            f"{paths_map['strategy_root']}/{name}" for name in sorted(seed)
        ],
        "next_steps": _draft_next_steps(
            proposal.id,
            paths_map,
            [f"{paths_map['strategy_root']}/{name}" for name in sorted(seed)],
        ),
    }
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload, semantic_success=True)


def strategy_draft_proposal_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    from ...strategies.tool_verification import attach_inline_validation
    return attach_inline_validation(_strategy_draft_proposal_handler(call, config=config), config=config)




def _draft_input_error(
    call: ToolCall, field: list[str | int], correction: str
) -> ToolResult:
    return _usage_error(
        call,
        f"strategy_draft_proposal invalid field {field!r}: {correction} "
        "Correct only this field; retain strategy_id and all other supplied arguments/files.",
        recovery_hint={
            "action": "correct_arguments",
            "tool_name": "strategy_draft_proposal",
            "field": field,
            "correction": correction,
            "preserve_other_arguments": True,
        },
    )


def _strategy_draft_proposal_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = call.arguments if call.arguments is not None else {}
    error = next(Draft7Validator(STRATEGY_DRAFT_PROPOSAL_SCHEMA).iter_errors(args), None)
    if error is not None:
        field = list(error.absolute_path)
        if error.validator == "required":
            field.append(next(key for key in error.validator_value if key not in error.instance))
        elif error.validator == "additionalProperties":
            field.append(next(key for key in error.instance if key not in error.schema.get("properties", {})))
        return _draft_input_error(call, field, error.message)
    strategy_id = str(args.get("strategy_id") or "").strip()
    if not strategy_id:
        return _usage_error(call, "strategy_draft_proposal requires strategy_id")
    # A concrete manifest must not silently replace an admitted instrument.
    # Leave partial scaffolds editable; submission checks the saved package.
    supplied = (args.get("files") or {}).get("strategy.yml")
    if supplied and config.get("runtime.command_id"):
        from ...strategies.request_scope import guard
        denied, _ = guard(config, call, manifest=lambda _: yaml_io.loads(supplied), allow_incomplete=True)
        if denied is not None:
            return denied
    from_strategy_id = str(args.get("from_strategy_id") or "").strip()
    if from_strategy_id:
        return _draft_from_promoted(call, config.paths, strategy_id=strategy_id,
                                    from_strategy_id=from_strategy_id, args=args)
    try:
        request = _request_from_args(args)
        gen = StrategyCodeGenerator(config.paths).generate(
            request, validate=bool(args.get("validate", False)),
            create_proposal_record=True, initial_state="draft")
    except StrategyGenerationInputError as exc:
        return _draft_input_error(call, exc.field, exc.correction)
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(call, f"draft scaffold failed: {type(exc).__name__}: {exc}")
    if gen.proposal is None:
        return _execution_error(call, "draft proposal was not created")
    paths_map = _proposal_strategy_paths(config.paths, gen.proposal.id, strategy_id)
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, semantic_success=True, data={
        "ok": True, "action": "strategy_draft_proposal",
        "proposal_id": gen.proposal.id, "strategy_id": strategy_id,
        "state": "draft", "kind": "strategy_package_proposal",
        "seeded_from": "provided_files" if request.files else "template",
        "strategy_class": gen.request.strategy_class, "files": list(gen.files),
        "validation": gen.validation.asdict() if gen.validation is not None else None,
        "proposal_paths": paths_map,
        "file_paths": [f"{paths_map['strategy_root']}/{name}" for name in sorted(gen.files)],
        "next_steps": _draft_next_steps(gen.proposal.id, paths_map),
    })




def strategy_submit_proposal_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    from ...strategies.tool_verification import attach_inline_validation, source_binding
    before = source_binding(config, call)
    return attach_inline_validation(_strategy_submit_proposal_handler(call, config=config), config=config, before=before)


def _strategy_submit_proposal_handler(
    call: ToolCall, *, config: Config
) -> ToolResult:
    args = call.arguments or {}
    proposal_id = str(args.get("proposal_id") or "").strip()
    if not proposal_id:
        return _usage_error(call, "strategy_submit_proposal requires proposal_id")
    target = next((p for p in list_proposals(config.paths) if p.id == proposal_id), None)
    if target is None or target.kind != "strategy_package_proposal":
        return _usage_error(call, "strategy package proposal not found")
    try:
        sid, files = _read_proposal_files(config.paths, proposal_id)
        if not files:
            return _usage_error(call, "proposal has no strategy files to submit")
        if config.get("runtime.command_id"):
            from ...strategies.request_scope import guard
            denied, _ = guard(config, call, manifest=lambda _: yaml_io.loads(files["strategy.yml"]))
            if denied is not None:
                return denied
        validation = validate_proposal_files(strategy_id=sid, files=files,
                                             smoke_test=False, run_tests=False).asdict()
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(call, f"failed to inspect proposal: {type(exc).__name__}: {exc}")
    (target.path / "validation_report.json").write_text(
        json.dumps(validation, indent=2, ensure_ascii=False), encoding="utf-8")
    set_state(config.paths, proposal_id, "pending_review",
              note=str(args.get("note") or "").strip() or "submitted for review")
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, semantic_success=True, data={
        "ok": True, "action": "strategy_submit_proposal", "proposal_id": proposal_id,
        "strategy_id": sid, "kind": "strategy_package_proposal", "state": "pending_review",
        "summary": target.summary, "validation": validation, "files": sorted(files),
        "proposal_paths": _proposal_strategy_paths(config.paths, proposal_id, sid),
        "backtest_required": False,
    })


def strategy_validate_handler(call: ToolCall, *, config: Config) -> ToolResult:
    from ...strategies.tool_verification import source_binding, attach_strategy_checks, attach_saved_replay
    before = source_binding(config, call)
    result = _strategy_validate_handler(call, config=config)
    after = source_binding(config, call)
    result = attach_strategy_checks(result, before=before, after=after, kind="validation", config=config)
    return attach_saved_replay(result, config=config, binding=after) if before == after else result


def _strategy_validate_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    proposal_id = (args.get("proposal_id") or "").strip()
    strategy_id = (args.get("strategy_id") or "").strip()
    if not proposal_id and not strategy_id:
        return _usage_error(call, "strategy_id or proposal_id is required")

    try:
        if proposal_id:
            from ...strategies.validation_receipt import validate_proposal_receipt
            report = validate_proposal_receipt(config.paths, proposal_id, strategy_id=strategy_id)
        else:
            validation = validate_strategy_package(config.paths, strategy_id)
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(
            call, f"validation failed: {type(exc).__name__}: {exc}"
        )
    report = report if proposal_id else validation.asdict()
    passed = report.get("ok") is True
    result = ToolResult.from_json(
        tool_use_id=call.id, name=call.name, data=report, semantic_success=passed,
    )
    if not passed:
        hint = {
            "action": "repair_candidate_and_validate", "tool_name": "strategy_validate",
            "proposal_id": proposal_id or None,
            "strategy_id": report.get("strategy_id") or strategy_id,
            "preserve_candidate": True,
            "blockers": report.get("blockers", []),
            "message": "Edit the reported files in this same candidate, then validate the saved version. "
                       "Do not retry unchanged inputs or claim submission, replay, coverage or returns from this failed validation.",
        }
        report["next_required_action"] = hint
        detail = "; ".join(f"{b.get('code')}: {b.get('message')} [{b.get('where')}]"
                           for b in hint["blockers"] if isinstance(b, dict))[:3000]
        result.is_error = True
        result.error = ToolError(
            kind=ToolErrorKind.EXECUTION_ERROR,
            message=f"Strategy validation failed. {hint['message']} Blockers: {detail}",
            recovery_hint=hint,
        )
    return result


def strategy_import_external_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Import a Freqtrade / VNpy strategy source as a Nerya package.

    The agent-facing entry to
    :func:`nerya.strategies.compat.importer.import_external_strategy`:
    upload (or point at) a strategy file and get back a promoted-ready
    package whose manifest, entrypoint and settings the agent can then
    validate / backtest / schedule like any other strategy.
    """

    args = call.arguments or {}
    sources = args.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    if not sources:
        return _usage_error(call, "sources (strategy file path or directory) is required")

    settings = args.get("settings")
    if settings is not None and not isinstance(settings, dict):
        return _usage_error(call, "settings must be a JSON object")

    # Imported lazy: the compat layer pulls in the validator, which the
    # native-tools package already depends on elsewhere — keep this
    # module's import surface unchanged.
    from ...strategies.compat.importer import import_external_strategy

    try:
        result = import_external_strategy(
            config,
            [str(s) for s in sources],
            strategy_id=(args.get("strategy_id") or "").strip() or None,
            framework=str(args.get("framework") or "auto"),
            title=(args.get("title") or "").strip() or None,
            markets=[str(m) for m in (args.get("markets") or [])] or None,
            accounts=[str(a) for a in (args.get("accounts") or [])] or None,
            timeframe=(args.get("timeframe") or "").strip() or None,
            settings=settings,
            mode=str(args.get("mode") or "paper"),
            stake_amount=float(args.get("stake_amount") or 0.0),
            overwrite=bool(args.get("overwrite", False)),
        )
    except NeryaError as exc:
        return _usage_error(call, str(exc))
    except Exception as exc:
        return _execution_error(
            call, f"import failed: {type(exc).__name__}: {exc}"
        )
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name, data=result.asdict()
    )


def strategy_backtest_dedupe_context(
    call: ToolCall, prior: ToolResult | None, *, config: Config
) -> str | None:
    """Observe changed operator policy only for reads and known replay failures.

    The wire ledger still owns exact-once admission; completed and unconfirmed
    replay calls retain their original tool cache regardless of policy changes.
    """
    from ..result_contracts import tool_json_data

    args = call.arguments or {}
    if args.get("policy_check_only") is not True:
        if (args.get("settings") or {}).get("agent_replay") is not True:
            return None
        if prior is not None:
            reason = (tool_json_data(prior) or {}).get("reason")
            if not prior.is_error or reason not in _RETROSPECTIVE_POLICY_REASONS | {
                "decision_response_truncated", "decision_request_failed_response",
            }:
                return None
    policy = config.get("backtest.retrospective_replay") or {}
    if not isinstance(policy, dict):
        return None
    relevant = {key: policy.get(key) for key in (
        "enabled", "authorization_id", "provider", "model", "key_ref", "tier",
        "reasoning_effort", "max_calls", "max_tokens", "deadline",
    )}
    relevant["max_output_tokens"] = policy.get("max_output_tokens", 4096)
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def strategy_backtest_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    if args.get("policy_check_only") is True:
        if set(args) - {"policy_check_only", "policy_recheck_interaction_id"}:
            return _usage_error(call, "Pass policy_check_only=true alone, without candidate or replay arguments.")
        if "policy_recheck_interaction_id" in args:
            cid = str(config.get("runtime.command_id") or "")
            if not cid:
                return _usage_error(call, "Policy recheck requires a durable command.")
            with closing(connect_readonly(config.paths.db)) as con:
                row = con.execute("SELECT request_json FROM agent_commands WHERE command_id=?", (cid,)).fetchone()
            iid = json.loads(row[0] or "{}").get("interaction_id") if row else None
            if not iid or args["policy_recheck_interaction_id"] != iid or _retrospective_consent(config) is not True:
                return ToolResult.from_error(tool_use_id=call.id, name=call.name, error=ToolError(
                    kind=ToolErrorKind.EXECUTION_ERROR,
                    message="Policy recheck requires the current command's answered replay-consent interaction.",
                    retryable=False))
        from ...skills.builtin.backtest.scripts.authorized_replay import authorized_policy
        try:
            policy = authorized_policy(config)
        except TradingError as exc:
            result = _backtest_error(call, exc, reason=str(exc), strategy_id=None, proposal_id=None)
            pending = _retrospective_authorization_interaction(call, config, reason=str(exc))
            return pending or result
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data={
            "result_type": "retrospective_authorization_policy", "ok": True,
            "authorization_valid": True, "replay_started": False,
            "budget_remaining_verified": False, "execution_readiness_verified": False,
            "performance_evidence": False,
            "policy": {key: policy[key] for key in (
                "authorization_id", "provider", "model", "max_calls", "max_tokens", "deadline")
                } | {"max_output_tokens": policy.get("max_output_tokens", 4096)},
        })
    if "policy_recheck_interaction_id" in args:
        return _usage_error(call, "policy_recheck_interaction_id is only valid with policy_check_only=true.")
    from ...strategies.tool_verification import source_binding, attach_strategy_checks
    before = source_binding(config, call)
    expected = (call.arguments or {}).get("expected_source_revision")
    if expected is not None and (before["revision"] == "unbound" or expected != before["revision"]):
        return ToolResult.from_error(tool_use_id=call.id, name=call.name, error=ToolError(
            kind=ToolErrorKind.EXECUTION_ERROR, message="Candidate source changed; validate and submit the corrected candidate before replay.",
            retryable=False, recovery_hint={"reason": "source_revision_conflict",
                "action": "validate_corrected_candidate", "side_effects": "none",
                "preserve_candidate": True}))
    result = _strategy_backtest_handler(call, config=config)
    if result.is_error and result.error:
        body = result.content[0].data if result.content and isinstance(result.content[0].data, dict) else {}
        reason = str(body.get("reason") or result.error.recovery_hint.get("reason") or "")
        if reason in _RETROSPECTIVE_POLICY_REASONS:
            pending = _retrospective_authorization_interaction(call, config, reason=reason)
            if pending is not None:
                return pending
    if result.is_error and result.error and result.error.recovery_hint.get("reason") == "request_scope_mismatch":
        result.error.recovery_hint["corrected_retry"] = {
            "tool": "strategy_backtest", "revision_from": "strategy_validate.verification.revision",
            "argument": "expected_source_revision",
            "message": "Correct only the diagnosed candidate/replay_inputs.json fields, validate and submit the same candidate, then retry once with the new source revision. Preserve admitted scope; do not retry unchanged inputs or invent a revision.",
        }
    return attach_strategy_checks(result, before=before, after=source_binding(config, call), kind="backtest",
                                  preflight_only=bool((call.arguments or {}).get("preflight_only")), config=config)


def _strategy_backtest_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    if args.get("settings") is not None and not isinstance(args["settings"], dict):
        return _usage_error(call, "settings must be an object")
    engine = str(args.get("engine") or "auto").lower()
    if engine not in {"auto", "native", "freeform", "rates"}:
        return _usage_error(call, "engine must be auto, native, freeform or rates")
    strategy_id = (args.get("strategy_id") or "").strip() or None
    proposal_id = (args.get("proposal_id") or "").strip() or None
    # Agents frequently pass BOTH ids right after submitting a proposal
    # (they now know each). Both point at the same staged strategy, so
    # prefer the in-flight proposal and drop the redundant strategy_id
    # rather than failing run_strategy_backtest's "exactly one" guard.
    if strategy_id and proposal_id:
        strategy_id = None
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
    candidate_backtest_defaults = _target_backtest_defaults(
        config.paths,
        strategy_id=strategy_id,
        proposal_id=proposal_id,
    )
    use_rates = engine == 'rates'
    if engine == 'auto':
        from ...strategies.request_scope import target_manifest
        try:
            saved = target_manifest(config, {'strategy_id':strategy_id,'proposal_id':proposal_id})
            use_rates = isinstance((saved.get('params') or {}).get('rate_replay'), dict)
        except (KeyError, ValueError, NeryaError, OSError):
            pass  # The selected producer owns its normal missing-source error.
    use_freeform = engine == "freeform" or engine == "auto" and not use_rates and _target_has_freeform_backtest_script(
        config.paths, strategy_id=strategy_id, proposal_id=proposal_id,
    )
    effective_settings = args.get("settings")
    if config.get("runtime.command_id"):
        from ...strategies.request_scope import guard
        denied, pinned = guard(config, call, settings=effective_settings)
        if denied is not None:
            return denied
        if pinned and not (use_rates or use_freeform):
            effective_settings = {**(effective_settings or {}), **pinned}
    effective_data_mode = args.get("data_mode")
    try:
        from ...skills.builtin.backtest.scripts.backtest_run import (
            run_strategy_backtest,
        )
        from ...skills.builtin.backtest.scripts.data_cache import (
            NoHistoricalDataError,
        )
        from ...skills.builtin.backtest.scripts.freeform_run import (
            run_freeform_backtest,
            FreeformDependencyError,
        )
        from ...skills.builtin.backtest.scripts.mock_ctx import BacktestUnsupportedSurfaceError

        if use_rates:
            native_only = [key for key in ('data_mode','settings','config_path','preflight_only') if args.get(key) is not None]
            if native_only or args.get('allow_mock'):
                return _usage_error(call,'Rate replay uses the saved params.rate_replay and replay_inputs.json only; remove native-only settings and never enable mock input.',
                    recovery_hint={'action':'correct_tool_arguments','preserve_requested_constraints':True,
                                   'remove_fields':native_only,'side_effects':'none'})
            from ...skills.builtin.backtest.scripts.rate_replay import run_rate_backtest
            from .historical_data import tool_progress
            result = run_rate_backtest(workspace=config.paths.root,proposal_id=proposal_id,strategy_id=strategy_id,
                runtime_config=config,
                progress=tool_progress(call),cancel_token=call.metadata.get('cancel_token'))
        elif use_freeform:
            native_only = [key for key in ("preflight_only", "data_mode", "settings", "config_path") if args.get(key)]
            if native_only:
                # Do not silently discard history/fee constraints or change the
                # engine. Give the model the exact correction, not a generic
                # execution error that invites the same invalid retry.
                retry_args = {"proposal_id": proposal_id} if proposal_id else {"strategy_id": strategy_id}
                retry_args.update(engine="freeform", allow_mock=False)
                return _usage_error(call,
                    "Freeform replay did not start. Remove these native-only arguments: "
                    + ", ".join(native_only) + ". Preserve the candidate and requested history. "
                    "First ensure dates/data requests are saved in replay_inputs.json and calculation assumptions "
                    "are read from strategy.yml by the custom script; then call strategy_backtest with exactly "
                    + json.dumps(retry_args) + ". Do not switch to native merely to clear this error.",
                    recovery_hint={"action": "correct_tool_arguments", "tool": "strategy_backtest",
                        "remove_fields": native_only, "suggested_arguments": retry_args,
                        "preserve_requested_constraints": True, "side_effects": "none"})
            from .historical_data import tool_progress
            result = run_freeform_backtest(
                runtime_config=config,
                strategy_id=strategy_id,
                proposal_id=proposal_id,
                workspace=config.paths.root,
                cancel_token=call.metadata.get("cancel_token"),
                progress=tool_progress(call),
            )
        else:
            from .historical_data import tool_progress
            result = run_strategy_backtest(
                strategy_id=strategy_id,
                proposal_id=proposal_id,
                preset=str(args.get("preset") or "default"),
                config_path=(args.get("config_path") or None),
                workspace=config.paths.root,
                allow_mock=bool(args.get("allow_mock", False)),
                preflight_only=bool(args.get("preflight_only", False)),
                data_mode=effective_data_mode,
                settings=effective_settings,
                progress=tool_progress(call),
                cancel_token=call.metadata.get("cancel_token"),
                runtime_config=config,
            )
            if args.get("preflight_only"):
                if candidate_backtest_defaults:
                    result["candidate_backtest_defaults_applied"] = not bool(effective_settings or args.get("config_path"))
                return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=result)
        result["engine"] = "rates" if use_rates else "freeform" if use_freeform else "native"
        if candidate_backtest_defaults and not use_freeform:
            result["candidate_backtest_defaults_applied"] = not bool(effective_settings or args.get("config_path"))
    except CancelledError:
        raise
    except FreeformDependencyError as exc:
        return _backtest_error(call, exc, reason="backtest_dependency_missing",
            strategy_id=strategy_id or args.get("strategy_id"), proposal_id=proposal_id,
            workspace_root=config.paths.root)
    except NoHistoricalDataError as exc:
        # Capability is producer-owned; neither exception prose nor venue names
        # establish it. Keep the phase fallback for older download producers.
        unsupported = exc.reason == 'historical_provider_unsupported'
        incomplete = not unsupported and (exc.reason == 'backtest_data_incomplete'
            or getattr(exc, 'replay_phase', None) == 'preparing_data')
        reason = 'historical_provider_unsupported' if unsupported else 'backtest_data_incomplete' if incomplete else 'no_historical_data'
        retryable = False if unsupported else True if incomplete else exc.retryable
        next_required_action: dict[str, Any] = {
            "type": "report_data_gap",
            "message": (
                "No durable historical candles were available for the "
                "requested market/timeframe or fallback timeframes. Do "
                "not retry with mock, synthetic, random, or placeholder "
                "data. Preserve the same candidate, requested chain, "
                "market, timeframe and window. Inspect the data-source "
                "receipt and workspace provider capability; repair the "
                "missing adapter or author a supported strategy-local "
                "replay from real historical inputs. If no durable "
                "source exists, report the exact gap as blocked."
            ),
            "skill": "backtest",
            "file": "references/replay-capabilities.md",
        }
        if unsupported:
            next_required_action = {
                'type': 'provide_historical_capability',
                'message': 'The configured provider cannot supply the requested historical market. '
                           'Preserve the same candidate, chain, venue, pair, timeframe, window and risk controls. '
                           'Keep replay blocked until a provider capability change supplies exact historical inputs '
                           'and a supported execution model. Do not repeat the unchanged backtest or historical_data '
                           'download, search configuration/source broadly, enable mock data, map to token/USD, '
                           'or substitute another venue or proxy. This diagnostic grants no proxy permission. '
                           'Report the producer receipt as blocked if that capability is unavailable.',
                'skill': 'backtest', 'file': 'references/replay-capabilities.md',
            }
        elif incomplete:
            next_required_action = {
                'type': 'prepare_historical_data', 'tool': 'historical_data',
                'message': 'Inspect the requested local window, then download or resume only its missing ranges. '
                           'An empty cache or download timeout does not prove the source lacks history. '
                           'Keep the same candidate and replay config; do not silently shorten the window, '
                           'enable mock data or substitute a proxy. Rerun replay only after coverage is complete.',
            }
        if proposal_id and not incomplete:
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
                elif _proposal_uses_event_replay(sid or strategy_id, files):
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
        next_required_action.update(reason=reason, safe_to_retry=retryable is True,
            preserve_candidate=True, preserve_requested_constraints=True)
        data = {'ok': False, 'reason': reason, 'result_type': 'backtest_result',
                'backtest_status': 'blocked', 'strategy_id': strategy_id, 'proposal_id': proposal_id,
                'coverage_ok': False, 'coverage_message': _backtest_public_message(exc, reason),
                'next_required_action': next_required_action}
        if reason == 'no_historical_data':
            return ToolResult.from_json(tool_use_id=call.id, name=call.name,
                data=data, semantic_success=False)
        failed = _backtest_error(call, exc, reason=reason,
            strategy_id=strategy_id, proposal_id=proposal_id, workspace_root=config.paths.root)
        failed.content[0].data.update(data)
        failed.error.retryable = retryable
        failed.error.recovery_hint = {
            'action': next_required_action['type'], **next_required_action,
            'next_required_action': next_required_action,
        }
        if config.paths.root:
            artifact_refs = _backtest_artifact_refs(exc, config.paths.root)
            if artifact_refs:
                failed.error.recovery_hint['artifacts'] = artifact_refs
        diagnostics = _backtest_bounded_diagnostics(exc)
        if diagnostics:
            failed.error.recovery_hint['diagnostics'] = diagnostics
        return _project_backtest_error(failed)
    except BacktestUnsupportedSurfaceError as exc:
        return _backtest_error(call, exc, reason="backtest_sdk_unsupported",
            strategy_id=strategy_id or args.get("strategy_id"), proposal_id=proposal_id,
            workspace_root=config.paths.root)
    except (NeryaError, TradingError, ValueError) as exc:
        return _backtest_error(call, exc, reason=getattr(exc, "reason", "backtest_invalid_input"),
            strategy_id=strategy_id or args.get("strategy_id"), proposal_id=proposal_id,
            workspace_root=config.paths.root)
    except Exception as exc:
        return _backtest_error(call, exc, reason=getattr(exc, "reason", "backtest_execution_failed"),
            strategy_id=strategy_id or args.get("strategy_id"), proposal_id=proposal_id,
            workspace_root=config.paths.root)
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
        is_meme_or_onchain = _proposal_uses_event_replay(target_sid, files)
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

    raw_metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    raw_config = result.get("config") if isinstance(result.get("config"), dict) else {}
    out = dict(result)
    out["result_type"] = "backtest_result"
    out["backtest_status"] = "failed" if result.get("ok") is False else "completed"
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
    out["bar_accounting"] = {
        "trading_window_bars_total": raw_metrics.get("bars_total") or result.get("bars_total"),
        "warmup_bars_per_market": raw_config.get("warmup_bars"),
        "note": (
            "Coverage dataset row counts may include warmup candles. "
            "trading_window_bars_total excludes warmup and is the correct total "
            "when describing the evaluated one-year window. Do not multiply a "
            "per-market coverage row count that includes warmup and call it the "
            "trading-window bar total."
        ),
    }
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
    is_meme_or_onchain = _proposal_uses_event_replay(sid, files)
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

    # Backtest/replay evidence can be attached after generation but before
    # approval. Freeze that final review snapshot; once approved, apply CAS is
    # intentionally immutable.
    reseal_candidate_bundle(
        paths,
        pid,
        note=f"strategy_promote:{accepted_kind or 'standard_backtest'}",
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
    tool_registry: Any = None,
    executor: Any = None,
) -> ToolResult:
    args = call.arguments or {}
    sid = (args.get("strategy_id") or "").strip()
    if not sid:
        return _usage_error(call, "strategy_id is required")
    from ...strategies.runner import StrategyRunner  # lazy: see top-of-file note
    runner = StrategyRunner(
        config=config,
        skills=skills,
        tool_registry=tool_registry,
        executor=executor,
    )
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

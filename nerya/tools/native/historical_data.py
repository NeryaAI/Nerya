"""Thin native-tool adapters to the backtest Skill's data preparation script."""
from __future__ import annotations

import time
from typing import Any

from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult, RiskLevel, PermissionScope
from ..registry import make_native_descriptor
from ...data_api.uniswap_history import RPC_ORIGINS
from ...harness.cancellation import raise_if_cancelled
from ...skills.builtin.backtest.scripts.history_data import history_operation


HISTORICAL_DATA_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["list", "inspect", "download", "status", "start", "resume", "stop"]},
        "dataset_kind": {"type": "string", "enum": ["candles", "uniswap_pool_events", "lp_state_requirements", "lp_state_job"], "default": "candles",
                         "description": "Pool events support inspect/download with exact inclusive block bounds. LP state requirements inspect one saved candidate or acquire a bounded batch of its missing decision states; neither grants replay admission."},
        "proposal_id": {"type": "string", "description": "Saved LP candidate for lp_state_requirements or lp_state_job start; supply this or strategy_id."},
        "strategy_id": {"type": "string", "description": "Installed LP strategy for lp_state_requirements or lp_state_job start; supply this or proposal_id."},
        "expected_source_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "expected_next_timestamp": {"type": "string", "description": "Copy the next missing timestamp from the LP state inspector's next_download_args."},
        "max_states": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
        "max_states_total": {"type": "integer", "minimum": 1, "maximum": 10000,
                             "description": "Explicit lifetime attempt budget for an LP state background job; resume does not reset it."},
        "phase": {"type": "string", "enum": ["states", "intervals"],
                  "description": "lp_state_job start only: default states requires max_states_total; intervals copies inspector event cursors through the saved finite window, with optional max_requests. Separate jobs; no automatic transition."},
        "request_id": {"type": "string", "minLength": 8, "maxLength": 100,
                       "description": "Stable idempotency key for one deliberate LP data job. Keep it on start retries."},
        "pool_address": {"type": "string", "pattern": r"^0x[0-9a-fA-F]{40}$"},
        "rpc_provider": {"type": "string", "enum": list(RPC_ORIGINS)},
        "start_block": {"type": "integer", "minimum": 1},
        "end_block": {"type": "integer", "minimum": 1},
        "tick_lower": {"type": "integer", "minimum": -887272, "maximum": 887272,
                        "description": "Optional exact saved LP range; provide both tick bounds. Bounds are part of the archive identity."},
        "tick_upper": {"type": "integer", "minimum": -887272, "maximum": 887272},
        "chunk_blocks": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
        "max_chunks": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1},
        "expected_next_block": {"type": "integer", "minimum": 1,
                                "description": "Producer-returned event cursor for the next download. Copy next_download_args; keep the full target bounds unchanged. Old committed cursors do not fetch again."},
        "markets": {"type": "array", "items": {"type": "string"}},
        "timeframes": {"type": "array", "items": {"type": "string"}},
        "start": {"type": "string", "description": "Inclusive UTC date/ISO timestamp, only when the producer has pinned the bound, e.g. 2025-01-01."},
        "end": {"type": "string", "description": "Exclusive UTC date/ISO timestamp, only when the producer has pinned the bound; do not derive it from a local calendar date, e.g. 2026-01-01."},
        "days": {"type": "integer", "minimum": 1,
                 "description": "For a relative recent-N-days window, pass days and omit start/end so the producer resolves the current UTC bounds. Defaults to 180; that default is not evidence of the user's requested window."},
        "data_dir": {"type": "string", "description": "Optional workspace-relative local data directory."},
        "job_id": {"type": "string"},
        "max_requests": {"type": "integer", "minimum": 1, "maximum": 10000,
                         "description": "For lp_state_job phase=intervals, optional explicit lifetime event-operation attempt cap, including failures/retries; resume never resets it. Omit to collect the saved finite window without inventing a total budget. Not a state or interval count."},
        "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 3600,
                            "description": "For lp_state_job, integer seconds until the overall deadline; 0 means no overall deadline (returned deadline=0). Positive values and resume preserve the original deadline."},
    }, "required": ["action"],
}


def tool_progress(call: ToolCall):
    last = 0.0
    last_phase = ""
    def publish(details):
        nonlocal last, last_phase
        session_id = str(call.metadata.get("session_id") or "")
        if not session_id:
            return
        phase = str(details.get("phase") or details.get("status") or "")
        now = time.monotonic()
        if now - last < 0.5 and phase == last_phase:
            return
        last, last_phase = now, phase
        data = details.get("data_progress") or details
        labels = {"preflight": "Checking strategy and configuration", "preparing_data": "Preparing local history",
            "downloading": "Downloading missing history", "validating_data": "Checking data coverage",
            "replaying": "Replaying historical candles", "reporting": "Writing report and charts",
            "completed": "Completed", "ready": "Local history ready", "incomplete": "Historical data incomplete"}
        message = labels.get(phase, phase)
        completed = data.get("rows") if data.get("expected_bars") is not None else details.get("bars_processed")
        total = data.get("expected_bars") or details.get("bars_total")
        if details.get("unit") == "blocks":
            completed, total = details.get("completed"), details.get("total")
        if data.get("market"):
            message += f" · {data['market']} {data.get('timeframe', '')}"
        if completed is not None and total:
            message += f" · {completed}/{total} {details.get('unit', 'candles')}"
        if data.get("cached_rows") is not None:
            message += f" · reused {data['cached_rows']}, downloaded {data.get('downloaded_rows', 0)}"
        from ...agent.streaming import get_default_bus
        get_default_bus().publish("tool.progress", session_id=session_id, turn_id=call.turn_id,
            tool_call_id=call.id, call_id=call.id, action=call.name,
            phase=phase, status=details.get("status", "running"), message=message,
            progress=min(1.0, completed / total) if isinstance(completed, (int, float)) and total else None,
            details=details)
    return publish


def call_cancellation(call: ToolCall):
    return lambda: raise_if_cancelled(call.metadata.get("cancel_token"), call.metadata.get("turn_deadline_epoch"))


def historical_data_handler(call: ToolCall, *, config) -> ToolResult:
    try:
        args = dict(call.arguments or {})
        kind = args.pop("dataset_kind", "candles")
        if kind == "lp_state_job":
            from ...skills.builtin.backtest.scripts.lp_state_job import operation
            call_cancellation(call)()
            result = operation(config, args,
                session_id=call.metadata.get("session_id") or config.get("runtime.command_session_id"),
                actor_id=config.get("runtime.financial_actor_id"), turn_id=call.turn_id)
        elif kind == "lp_state_requirements":
            from ...skills.builtin.backtest.scripts.lp_state_requirements import inspect_lp_state_requirements, download_lp_states
            if args.get("action") == "download":
                result = download_lp_states(config, args, check_cancel=call_cancellation(call),
                    cancel_token=call.metadata.get("cancel_token"))
            else:
                result = inspect_lp_state_requirements(config, args, check_cancel=call_cancellation(call))
        elif kind == "uniswap_pool_events":
            from ...skills.builtin.backtest.scripts.uniswap_event_data import event_history_operation
            result = event_history_operation(config, args, progress=tool_progress(call),
                check_cancel=call_cancellation(call), cancel_token=call.metadata.get("cancel_token"))
        elif kind == "candles":
            if set(args) & {"phase", "proposal_id", "strategy_id", "expected_source_revision", "expected_next_timestamp", "max_states", "max_states_total", "request_id", "pool_address", "rpc_provider", "start_block", "end_block", "chunk_blocks", "max_chunks", "expected_next_block", "tick_lower", "tick_upper"}:
                raise ValueError("LP fields require the matching historical dataset_kind")
            result = history_operation(config, args,
                progress=tool_progress(call), check_cancel=call_cancellation(call))
        else:
            raise ValueError("Unsupported historical dataset_kind")
        result = ToolResult.from_json(tool_use_id=call.id, name=call.name,
                                   data=result, semantic_success=result.get("ok") is True)
        if (call.arguments or {}).get("action") in {"list", "inspect", "status"}:
            result.metadata["read_only"] = True
        return result
    except Exception as exc:
        downloading = (call.arguments or {}).get("action") in {"download", "start", "resume"}
        result = ToolResult.from_json(tool_use_id=call.id, name=call.name, semantic_success=False,
            data={"ok": False, "result_type": "historical_data", "status": "cancelled" if type(exc).__name__ == "CancelledError" else "failed",
                  "error": type(exc).__name__, "message": str(exc),
                  "resumable": downloading, "message_hint":
                  "Completed historical segments remain local. Fix the error, then resume the same requested data window."
                  if downloading else "Local inspection did not complete. No historical data was downloaded."})
        result.is_error = True
        result.error = ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message=str(exc))
        return result


def historical_data_descriptors(config):
    return [make_native_descriptor(name="historical_data",
        description="List, inspect or incrementally download reusable local historical candles without running a strategy. "
            "For background LP state acquisition, use dataset_kind=lp_state_job action=start with a stable request_id, one saved candidate, expected_source_revision, max_states_total and timeout_seconds (0 means no overall deadline). The host returns job_id/task_id and runs without model calls; action=status is read-only, stop is cooperative, resume preserves the original budget and deadline. A job reaching its cap is not full-window coverage. "
            "For background event intervals, explicitly start a separate phase=intervals job with the saved candidate/revision, stable request_id and timeout_seconds; max_requests is an optional explicit cost cap (omit for the saved finite window), and max_states_total is not accepted. It copies only next_interval_download_args and reinspects real continuity/cursor progress after each call; intervals_acquired proves data collection only. Default states and existing jobs never auto-transition. "
            "For saved fixed-range LP data requirements, use action=inspect,dataset_kind=lp_state_requirements and one proposal_id or strategy_id. This offline check binds the saved schedule and declared UTC window, counts required decision/accounting states and reports missing locally evidenced checkpoints; it never changes or enables the schedule. "
            "For authorized bounded LP state acquisition, copy its next_download_args with action=download and max_states<=4 (default one). The host selects missing times from the unchanged saved scope, resolves canonical blocks and persists state evidence; state samples do not establish interval fee continuity. "
            "For Uniswap v3 pool events, use dataset_kind=uniswap_pool_events with action=inspect/download, exact pool_address, rpc_provider and inclusive start_block/end_block. This stores resumable chunks (chunk_blocks<=100, max_chunks<=10 per call, default one); inspect is offline. event_range_complete concerns collected events only, never full LP replay or fee reconstruction. Read the backtest Skill's history-data reference before download. "
            "Explicitly propagate admitted start/end or days with markets/timeframes. For relative recent-N-days requests, pass days and omit start/end; the producer resolves the current UTC bounds. Never derive end from a local calendar date or clamp a future date. Use explicit start/end only when the producer has pinned UTC bounds. The default 180 days is not proof of the user's requested window. "
            "Start is inclusive and end exclusive in UTC. Only missing ranges are fetched; repeated/overlapping requests reuse stored rows. "
            "action=inspect/list never use network. Inspect ok means the query completed; coverage_complete and datasets describe only local candle coverage. "
            "Inspect does not establish a configured historical provider, permission to download, or exact protocol replay capability. "
            "Missing local coverage is not evidence that a provider is unsupported or that repeating the query will help. "
            "Use action=download only with an established historical provider and download permission; incomplete downloads retain rows for gap repair. "
            "For a normal strategy_backtest, prefer the native producer's own history preparation; use this tool only for explicit local inventory, preparation or recovery. "
            "Does not activate strategies, place account orders or change global configuration.",
        input_schema=HISTORICAL_DATA_SCHEMA,
        handler=lambda call: historical_data_handler(call, config=config),
        risk=RiskLevel.WRITE, permission_scope=PermissionScope.WORKSPACE,
        risk_classifier=lambda args: RiskLevel.WRITE if args.get("action") in {"download", "start", "resume", "stop"} else RiskLevel.READ,
        read_only=False, is_concurrency_safe=False, mutates_paths=True,
        tags=("data", "history", "backtest"), result_kind="json", auto_approve=True)]

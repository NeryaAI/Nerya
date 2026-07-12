"""Memory native tools — read/write the long-term memory store.

The agent reaches persistent recall directly through native tools rather
than the legacy skill bridge:

* **system_prompt_block** — compatibility wrapper over the runtime's
  stable Notebook snapshot and query-independent structured recall.
* **recall** / **remember** — explicit tools for the model when it
  wants to look something up or commit a learning. The fenced block
  covers the "what do I already know" implicit case.
* **journal_search** — cheap read for "what happened recently"
  questions, tailing workspace journals.

The handlers accept a trusted, turn-bound
:class:`nerya.memory.runtime.MemoryRuntime`; model-supplied identifiers never
select a filesystem path or memory partition.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...core import jsonl
from ...core.paths import WorkspacePaths
from ...core.config import Config
from ...evolution.events import EvolutionSignal
from ...evolution.event_store import append_signal
from ...evolution.quality import evaluate_learning_candidate
from ...memory.runtime import MemoryRuntime, MemoryScopeError
from ..tool_errors import schema_validation_result as _usage_error
from ..types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


MEMORY_RECALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "string",
            "enum": ["visible", "global", "strategy", "session"],
            "default": "visible",
            "description": "Recall only memory visible to the trusted active turn.",
        },
        "query": {
            "type": "string",
            "description": "What to look for. Results are ranked for this query.",
        },
        "strategy_id": {
            "type": "string",
            "description": "Deprecated compatibility field; must equal the active strategy.",
        },
        "max_chars": {
            "type": "integer",
            "minimum": 100,
            "default": 1200,
            "description": "Truncate the recalled body to this many trailing chars.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 10,
        },
    },
}

MEMORY_REMEMBER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "string",
            "enum": ["global", "strategy", "session"],
            "description": "Memory partition.",
        },
        "name": {
            "type": "string",
            "description": (
                "Global memory file id. One of: global.md, mistakes.md, "
                "market_regimes.md, skill_learnings.md."
            ),
        },
        "strategy_id": {
            "type": "string",
            "description": "Deprecated compatibility field; must equal the active strategy.",
        },
        "category": {
            "type": "string",
            "description": "Memory category governed by memory.write_rules.",
        },
        "key": {
            "type": "string",
            "description": "Stable key used to update an existing fact.",
        },
        "title": {
            "type": "string",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "note": {
            "type": "string",
            "description": "Plain-text note to append; will be timestamped.",
        },
    },
    "required": ["scope", "note"],
}

JOURNAL_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "journal": {
            "type": "string",
            "default": "agent",
            "description": (
                "Journal name (without .jsonl). Common values: 'agent', "
                "'risk', 'orders', 'triggers'."
            ),
        },
        "contains": {
            "type": "string",
            "description": "Optional substring filter (case-insensitive).",
        },
        "kind": {
            "type": "string",
            "description": "Optional 'kind' field filter (exact match).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 50,
            "description": "Max entries to return; tails the file.",
        },
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _validate_requested_strategy(
    call: ToolCall,
    *,
    runtime: MemoryRuntime,
    scope: str,
) -> ToolResult | None:
    requested = str((call.arguments or {}).get("strategy_id") or "").strip()
    if requested and requested != runtime.strategy_id:
        return _usage_error(
            call,
            "requested strategy_id does not match the trusted active strategy",
        )
    if scope == "strategy" and not runtime.strategy_id:
        return _usage_error(call, "strategy memory requires an active strategy")
    if scope == "session" and not runtime.session_id:
        return _usage_error(call, "session memory requires an active session")
    return None


def memory_recall_handler(call: ToolCall, *, runtime: MemoryRuntime) -> ToolResult:
    args = call.arguments or {}
    scope = str(args.get("scope") or "visible").strip().lower()
    mismatch = _validate_requested_strategy(call, runtime=runtime, scope=scope)
    if mismatch is not None:
        return mismatch
    max_chars = max(100, int(args.get("max_chars") or 1200))
    limit = max(1, int(args.get("limit") or 10))
    query = str(args.get("query") or "").strip()
    try:
        hits = runtime.recall(query, scope=scope, limit=limit)
    except MemoryScopeError as exc:
        return _usage_error(call, str(exc))
    rows: list[dict[str, Any]] = []
    used = 0
    for hit in hits:
        remaining = max_chars - used
        if remaining <= 0:
            break
        content = hit.content[:remaining]
        if not content:
            continue
        rows.append({
            "memory_id": hit.memory_id,
            "scope": hit.scope,
            "strategy_id": hit.strategy_id,
            "session_id": hit.session_id,
            "category": hit.category,
            "key": hit.stable_key,
            "content": content,
            "source": hit.source_ref,
            "created_at": hit.created_at,
            "score": hit.score,
        })
        used += len(content)
    body = "\n\n".join(row["content"] for row in rows)
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "scope": scope,
            "query": query,
            "count": len(rows),
            "chars": len(body),
            "body": body or "(memory is empty)",
            "results": rows,
        },
    )


def memory_remember_handler(call: ToolCall, *, runtime: MemoryRuntime) -> ToolResult:
    args = call.arguments or {}
    scope = str(args.get("scope") or "").strip().lower()
    note = (args.get("note") or "").strip()
    if not note:
        return _usage_error(call, "note must be non-empty")
    mismatch = _validate_requested_strategy(call, runtime=runtime, scope=scope)
    if mismatch is not None:
        return mismatch
    if scope not in {"global", "strategy", "session"}:
        return _usage_error(
            call, f"unknown scope {scope!r}; expected global|strategy|session",
        )
    name = str(args.get("name") or "global.md").strip()
    category = str(args.get("category") or "").strip().lower()
    if not category:
        category = {
            "mistakes.md": "error",
            "decisions.md": "decision",
        }.get(name, "learning")
    evidence_ref = (
        f"strategy:{runtime.strategy_id}"
        if scope == "strategy"
        else f"memory:{scope}"
    )
    quality = evaluate_learning_candidate(note, evidence_refs=[evidence_ref])
    if not quality.ok:
        try:
            append_signal(
                runtime.config.paths,
                EvolutionSignal.create(
                    source="memory",
                    kind="memory_low_value_write",
                    severity="info",
                    strategy_id=runtime.strategy_id if scope == "strategy" else None,
                    evidence_refs=[evidence_ref],
                    summary=f"Memory write scored {quality.score}.",
                    dedupe_key=f"memory_low_value:{scope}:{hash(note[:256])}",
                    confidence=1.0 - quality.score,
                    metadata={"quality": quality.asdict()},
                ),
                dedupe=True,
            )
        except Exception:
            pass
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "ok": False,
                "scope": scope,
                "blocked_reasons": quality.reasons,
                "quality_score": quality.score,
            },
        )
    try:
        result = runtime.remember(
            category=category,
            content=note,
            scope=scope,
            key=str(args.get("key") or "").strip(),
            title=str(args.get("title") or "").strip(),
            tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
            source=f"native:{call.turn_id or call.id}",
            source_turn_id=call.turn_id,
            evidence_refs=[evidence_ref],
            writer_id="native_tool",
            confidence=quality.score,
        )
    except MemoryScopeError as exc:
        return _usage_error(call, str(exc))
    record = result.record
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "ok": result.ok,
            "skipped": result.skipped,
            "skip_reason": result.skip_reason,
            "scope": scope,
            "strategy_id": runtime.strategy_id if scope == "strategy" else "",
            "session_id": runtime.session_id if scope == "session" else "",
            "memory_id": record.memory_id if record else "",
        },
    )


def journal_search_handler(call: ToolCall, *, paths: WorkspacePaths) -> ToolResult:
    args = call.arguments or {}
    name = (args.get("journal") or "agent").strip()
    contains = (args.get("contains") or "").strip().lower()
    kind = (args.get("kind") or "").strip()
    limit = max(1, int(args.get("limit") or 50))

    journal_path: Path = paths.journal(name)
    if not journal_path.exists():
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"journal": name, "entries": [], "count": 0},
        )

    rows: list[dict[str, Any]] = []
    try:
        for row in jsonl.tail(journal_path, n=max(limit * 4, limit)):
            if kind and str(row.get("kind") or "") != kind:
                continue
            if contains:
                blob = json.dumps(row, ensure_ascii=False, default=str).lower()
                if contains not in blob:
                    continue
            rows.append(row)
            if len(rows) >= limit:
                break
    except Exception as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR, message=str(exc),
            ),
        )

    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"journal": name, "entries": rows, "count": len(rows)},
    )


# ---------------------------------------------------------------------------
# System-prompt block (compatibility)
# ---------------------------------------------------------------------------


def build_system_prompt_block(
    paths: WorkspacePaths,
    *,
    config: Config | None = None,
    session_id: str | None = None,
    strategy_id: str | None = None,
    max_chars: int = 1200,
) -> str:
    """Render the always-on memory section spliced into the system prompt.

    Returns an empty string when there is nothing useful to share, so
    the kernel can drop the block cleanly. The fence + system note
    wording mirrors :func:`agent_runtime.agent.memory_manager.build_memory_context_block`,
    so the model treats the body as informational, not as user input.
    """

    runtime = MemoryRuntime(
        config or Config(paths=paths, data={}),
        session_id=str(session_id or ""),
        strategy_id=str(strategy_id or ""),
    )
    snapshot = runtime.context("", max_chars=max_chars)
    return "\n\n".join(
        block for block in (snapshot.stable, snapshot.dynamic) if block
    )


__all__ = [
    "JOURNAL_SEARCH_SCHEMA",
    "MEMORY_RECALL_SCHEMA",
    "MEMORY_REMEMBER_SCHEMA",
    "build_system_prompt_block",
    "journal_search_handler",
    "memory_recall_handler",
    "memory_remember_handler",
]

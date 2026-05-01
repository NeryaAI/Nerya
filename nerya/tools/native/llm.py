"""LLM native tools — let the agent delegate to the workspace's
:class:`LLMGateway` without going through the legacy ``llm_skill``
bridge.

Why a native surface for LLM calls? The host kernel itself is already
on a model — but it often wants to off-load:

* short, cheap classification work to a ``light`` tier,
* schema-bound JSON extraction (where the gateway enforces the
  schema and the provider's ``schema_json_mode`` capability),
* ``compress`` long blobs into bounded summaries before passing them
  to a more expensive reasoning step.

Routing through :class:`LLMGateway` (rather than the agent's own
provider) means these calls go through the workspace's tier policy,
budget gates, capability matrix, and the same telemetry path that the
rest of the runtime is audited through.

Caller identity is fixed to ``agent:native`` so the dashboard can
distinguish between "the kernel called itself" and "a script borrowed
the gateway via :class:`LLMSession`".
"""

from __future__ import annotations

from typing import Any

from ...core.config import Config
from ...llm import LLMGateway
from ..types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


_CALLER_ID = "agent:native"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


_TIER_ENUM = ["light", "core", "high"]


LLM_COMPLETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": (
                "Task tag for the tier policy (e.g. 'analysis', "
                "'summary'). Required by the gateway for routing + "
                "telemetry."
            ),
        },
        "prompt": {"type": "string"},
        "tier": {
            "type": "string",
            "enum": _TIER_ENUM,
            "description": "Override the tier policy's default tier.",
        },
        "schema": {
            "type": "object",
            "description": (
                "Optional JSON schema. Providers without "
                "schema_json_mode support will refuse the call."
            ),
        },
    },
    "required": ["task", "prompt"],
}

LLM_CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "labels": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "description": "Candidate labels (min 2).",
        },
        "tier": {"type": "string", "enum": _TIER_ENUM, "default": "light"},
    },
    "required": ["text", "labels"],
}

LLM_EXTRACT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "schema": {
            "type": "object",
            "description": "Target JSON schema (required for strict extraction).",
        },
        "tier": {"type": "string", "enum": _TIER_ENUM, "default": "light"},
        "task": {
            "type": "string",
            "default": "extract_json",
            "description": "Task tag (defaults to 'extract_json').",
        },
    },
    "required": ["text"],
}

LLM_COMPRESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "max_tokens": {
            "type": "integer",
            "minimum": 32,
            "default": 512,
            "description": "Target output budget in tokens.",
        },
    },
    "required": ["text"],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _usage_error(call: ToolCall, message: str) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=ToolErrorKind.SCHEMA_VALIDATION, message=message,
        ),
    )


def _exec_error(call: ToolCall, exc: Exception) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=ToolErrorKind.EXECUTION_ERROR,
            message=f"{type(exc).__name__}: {exc}",
        ),
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def llm_complete_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    task = (args.get("task") or "").strip()
    prompt = args.get("prompt") or ""
    if not task or not prompt:
        return _usage_error(call, "task and prompt are required")
    tier = args.get("tier")
    schema = args.get("schema") if isinstance(args.get("schema"), dict) else None
    try:
        result = LLMGateway(config).call(
            task=task,
            prompt=prompt,
            caller=_CALLER_ID,
            tier=tier,
            schema=schema,
        )
    except Exception as exc:
        return _exec_error(call, exc)
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name,
        data={
            "raw": result.raw,
            "parsed": result.parsed,
            "tier": result.tier,
            "usd": getattr(result, "usd", None),
            "task": task,
        },
    )


def llm_classify_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    text = args.get("text") or ""
    labels = args.get("labels") or []
    if not text or not isinstance(labels, list) or len(labels) < 2:
        return _usage_error(call, "text and labels (>=2) are required")
    tier = args.get("tier") or "light"
    try:
        parsed = LLMGateway(config).classify(
            caller=_CALLER_ID,
            text=text,
            labels=[str(label) for label in labels],
            tier=tier,
        )
    except Exception as exc:
        return _exec_error(call, exc)
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name,
        data={"result": parsed, "tier": tier},
    )


def llm_extract_json_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    text = args.get("text") or ""
    if not text:
        return _usage_error(call, "text is required")
    schema = args.get("schema") if isinstance(args.get("schema"), dict) else None
    tier = args.get("tier") or "light"
    task = (args.get("task") or "extract_json").strip()
    try:
        parsed = LLMGateway(config).extract_json(
            caller=_CALLER_ID,
            text=text,
            schema=schema,
            task=task,
            tier=tier,
        )
    except Exception as exc:
        return _exec_error(call, exc)
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name,
        data={"result": parsed, "tier": tier, "task": task},
    )


def llm_compress_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    text = args.get("text") or ""
    if not text:
        return _usage_error(call, "text is required")
    max_tokens = max(32, int(args.get("max_tokens") or 512))
    try:
        compressed = LLMGateway(config).compress(
            caller=_CALLER_ID,
            text=text,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        return _exec_error(call, exc)
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name,
        data={"text": compressed, "max_tokens": max_tokens},
    )


__all__ = [
    "LLM_CLASSIFY_SCHEMA",
    "LLM_COMPLETE_SCHEMA",
    "LLM_COMPRESS_SCHEMA",
    "LLM_EXTRACT_JSON_SCHEMA",
    "llm_classify_handler",
    "llm_complete_handler",
    "llm_compress_handler",
    "llm_extract_json_handler",
]

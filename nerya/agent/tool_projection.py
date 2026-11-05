"""Projection of typed tool results into transcript and event blocks.

Execution and presentation are separate boundaries: :mod:`tool_phase` decides
which calls run and updates the turn ledger, while this module renders the
resulting evidence for providers, persistence and live UI subscribers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any, Callable

from ..core.redaction import redact_display_dict
from ..tools.types import ToolResult
from ..tools.verification import public_verification_receipt
from .attachments import ATTACHMENT_BLOCK_TYPES, assistant_attachment_block
from .transcript_blocks import ApprovalRequestBlock, ToolResultBlock


RenderToolResult = Callable[[ToolResult], dict[str, Any]]
RenderedToolResultText = Callable[[dict[str, Any]], str | None]


@dataclass(frozen=True)
class ToolResultProjection:
    transcript_blocks: tuple[dict[str, Any], ...]
    event_blocks: tuple[dict[str, Any], ...]
    approval_blocks: tuple[dict[str, Any], ...]


def tool_display_result(result: ToolResult, rendered: dict[str, Any]) -> dict[str, Any] | None:
    """Keep native part types for UI without changing the model's observation.

    旧链路用 result.text() 把 diff/json/shell 混成正文，前端无法可靠恢复类型。
    仅为未压缩的小结果附带脱敏显示数据；大结果继续使用已有压缩与原始记录，
    不绕过 compaction，也不把图片或附件的二进制内容重复写入事件。
    """
    if result.is_error or rendered.get("compaction"):
        return None
    parts = [part for part in result.content if part.type in {"text", "diff", "code", "json", "shell"}]
    if not any(part.type != "text" for part in parts):
        return None
    try:
        candidate = {"content": [part.asdict() for part in parts]}
        # 先限制原始体积，避免把巨型工具结果再复制到显示通道；脱敏后仍需检查预算。
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > 64_000:
            return None
        display = redact_display_dict(candidate)
        if len(json.dumps(display, ensure_ascii=False).encode("utf-8")) > 64_000:
            return None
    except (TypeError, ValueError, RecursionError):
        return None
    return display


def project_tool_results(
    results: list[ToolResult],
    *,
    render_tool_result: RenderToolResult,
    rendered_tool_result_text: RenderedToolResultText,
) -> ToolResultProjection:
    """Create provider transcript blocks and ordered runtime event blocks."""

    transcript_blocks: list[dict[str, Any]] = []
    event_blocks: list[dict[str, Any]] = []
    approval_by_id: dict[str, dict[str, Any]] = {}

    for result in results:
        text_result = replace(result, content=[p for p in result.content if not (p.type == 'image' and p.metadata.get('provider_input'))])
        rendered = render_tool_result(text_result)
        transcript_blocks.append(rendered)
        visible = rendered_tool_result_text(rendered) if not result.is_error else None
        if visible is None and not result.is_error:
            visible = result.text()
        event_blocks.append(
            ToolResultBlock(
                call_id=result.tool_use_id,
                skill_id="native",
                action=result.name,
                ok=not result.is_error,
                result=visible,
                error=(result.error.message if result.error else None)
                if result.is_error
                else None,
                error_kind=(result.error.kind.value if result.error else None)
                if result.is_error
                else None,
                elapsed_ms=float(result.elapsed_ms),
                completed_at=result.completed_at,
                recovery=(
                    dict(result.error.recovery_hint)
                    if result.is_error
                    and result.error is not None
                    and isinstance(result.error.recovery_hint, dict)
                    and result.error.recovery_hint
                    else None
                ),
                compaction=rendered.get("compaction"),
                semantic_success=result.semantic_success,
                result_protocol=result.result_protocol,
                verification=public_verification_receipt(result.metadata.get("verification")),
            ).as_dict()
        )
        display = tool_display_result(result, rendered)
        # Successful file mutations invalidate earlier turn-scoped checks even
        # when their display result was compacted. Paths remain display-redacted.
        mutations = [modifier.path for modifier in result.context_modifiers if modifier.kind == "file_mutate"]
        if mutations and not result.is_error:
            event_blocks[-1]["modified_paths"] = redact_display_dict({"paths": mutations})["paths"]
        if display is not None:
            event_blocks[-1]["display_result"] = display

        if result.name == "todo_write" and not result.is_error:
            from ..tools.native.task import normalise_task_snapshot
            for part in result.content:
                snapshot = normalise_task_snapshot(part.data) if part.type == "json" else None
                if snapshot is not None:
                    event_blocks[-1]["task_state"] = snapshot
                    break

        approval_request = result.metadata.get("approval_request")
        if isinstance(approval_request, dict):
            approval_id = str(approval_request.get("approval_id") or "")
            if approval_id:
                approval_by_id[approval_id] = dict(approval_request)

        for part in result.content:
            if part.type not in ATTACHMENT_BLOCK_TYPES:
                continue
            payload = part.data if isinstance(part.data, dict) else {}
            event_blocks.append(
                assistant_attachment_block(
                    {
                        "type": part.type,
                        "source": payload.get("source") or payload,
                        "name": (
                            payload.get("name")
                            or part.metadata.get("name")
                            or result.name
                            or "tool-attachment"
                        ),
                        "mime_type": (
                            part.media_type
                            or payload.get("mime_type")
                            or payload.get("media_type")
                        ),
                        "text": part.text,
                        "source_kind": "tool",
                    }
                )
            )

    approval_blocks = tuple(
        ApprovalRequestBlock.from_dict(request).as_dict()
        for request in approval_by_id.values()
    )
    # Keep all tool-result pairs ahead of supplementary images. Images are
    # top-level user content so OpenAI/Gemini do not flatten them to tool text.
    for result in results:
        if result.is_error:
            continue
        for part in result.content:
            if part.type == 'image' and part.metadata.get('provider_input') and isinstance(part.data, dict):
                transcript_blocks.append({'type':'image', 'source':dict(part.data)})
    return ToolResultProjection(
        transcript_blocks=tuple(transcript_blocks),
        event_blocks=tuple(event_blocks),
        approval_blocks=approval_blocks,
    )


__all__ = ["ToolResultProjection", "project_tool_results"]

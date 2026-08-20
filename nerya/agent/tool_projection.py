"""Projection of typed tool results into transcript and event blocks.

Execution and presentation are separate boundaries: :mod:`tool_phase` decides
which calls run and updates the turn ledger, while this module renders the
resulting evidence for providers, persistence and live UI subscribers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..tools.types import ToolResult
from .attachments import ATTACHMENT_BLOCK_TYPES, assistant_attachment_block
from .transcript_blocks import ApprovalRequestBlock, ToolResultBlock


RenderToolResult = Callable[[ToolResult], dict[str, Any]]
RenderedToolResultText = Callable[[dict[str, Any]], str | None]


@dataclass(frozen=True)
class ToolResultProjection:
    transcript_blocks: tuple[dict[str, Any], ...]
    event_blocks: tuple[dict[str, Any], ...]
    approval_blocks: tuple[dict[str, Any], ...]


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
        rendered = render_tool_result(result)
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
            ).as_dict()
        )

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
    return ToolResultProjection(
        transcript_blocks=tuple(transcript_blocks),
        event_blocks=tuple(event_blocks),
        approval_blocks=approval_blocks,
    )


__all__ = ["ToolResultProjection", "project_tool_results"]

"""Nerya main agent loop.

The kernel runs one turn end-to-end through the workspace-native
``messages + tools`` loop (see :mod:`nerya.agent.loop`). Anything that
used to live behind a planner / output_parser / context_builder is now
expressed as a native :class:`~nerya.tools.types.ToolDescriptor` — the
model decides what to call, the kernel just records what happened.
"""

from .kernel import AgentKernel, AgentTurnResult
from .loop import LoopConfig, LoopOutcome, WorkspaceNativeAgentLoop
from .transcript_blocks import (
    BlockEnvelope,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from .transcript_compact import (
    CompactionReport,
    compact_transcript,
    validate_transcript,
)

__all__ = [
    "AgentKernel",
    "AgentTurnResult",
    "BlockEnvelope",
    "CompactionReport",
    "LoopConfig",
    "LoopOutcome",
    "TextBlock",
    "ThinkingBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "WorkspaceNativeAgentLoop",
    "compact_transcript",
    "validate_transcript",
]

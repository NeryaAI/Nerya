"""Nerya main agent loop public exports.

The package keeps these exports lazy so importing a leaf module such as
``nerya.agent.file_state`` does not initialize ``AgentKernel`` and the native
tool registry while either side is still partially imported.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AgentKernel": ".kernel",
    "AgentTurnResult": ".kernel",
    "LoopConfig": ".loop",
    "LoopOutcome": ".loop",
    "WorkspaceNativeAgentLoop": ".loop",
    "BlockEnvelope": ".transcript_blocks",
    "ExecutionStateItem": ".execution_state",
    "build_execution_state": ".execution_state",
    "TextBlock": ".transcript_blocks",
    "ThinkingBlock": ".transcript_blocks",
    "ToolResultBlock": ".transcript_blocks",
    "ToolUseBlock": ".transcript_blocks",
    "CompactionReport": ".transcript_compact",
    "compact_transcript": ".transcript_compact",
    "validate_transcript": ".transcript_compact",
    "AgentRuntime": ".runtime",
    "SharedAgentRuntime": ".runtime",
    "CompletionGate": ".runtime",
    "GateDecision": ".runtime",
    "GateStatus": ".runtime",
    "TurnSnapshot": ".runtime",
    "RuntimeRequest": ".runtime",
    "RuntimeResult": ".runtime",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value

"""Per-turn execution helpers for the workspace-native agent loop.

The workspace-native loop in :mod:`nerya.agent.loop` drives tools
directly through :class:`~nerya.tools.types.ToolRegistry`, so the only
helpers that survive here are the runtime-side concerns the loop still
delegates to:

* :mod:`nerya.harness.cancellation` — cooperative cancellation tokens
  the kernel registers on every long-running turn.
* :mod:`nerya.harness.result_store` — the overflow spool used by
  ``operator_skill`` (and any caller that wants to keep large tool
  results out of the rolling LLM context).

The legacy planner / output-parser / ``TurnHarness`` / ``ToolRunner``
stack lived here and is gone — see ``nerya.agent.loop`` for the
canonical replacement.
"""

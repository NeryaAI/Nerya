"""Scaffold a new script proposal.

Audit 5.3 / the previous skeleton referenced a ``Client`` class
and ``Client.local()`` constructor that no longer exist on the Python SDK
(``nerya_sdk``). Generated scripts therefore failed at import time and
turned the agent-facing ``generate_script_proposal`` action into a
dead-end.

The skeleton below is aligned with the current runtime: it uses the
narrow, read-only :class:`nerya.scripts.script_context.ScriptContext`
surface (so it matches what approved scripts can actually do at
runtime), and falls back to a minimal "no-op" entry point when a
``ctx`` is not supplied so the file can still be imported by the
static analyzer or previewed outside the sandbox.
"""

from __future__ import annotations

from ..core import yaml_io
from ..core.paths import WorkspacePaths
from .patch_proposal import Proposal, create_proposal


SKELETON_MANIFEST = {
    "id": "generated_script",
    "version": "0.1.0",
    "title": "Generated Script",
    "description": "Operator must review before approving.",
    "entry": "run",
    "state": "pending",
    "trigger_kinds": [],
    "llm_policy": {
        "allowed_tiers": ["light"],
        "allowed_tasks": ["classify", "compress"],
        "max_calls_per_run": 5,
        "max_tokens_per_run": 4000,
        "max_cost_usd_per_day": 1,
        "high_tier_requires_approval": True,
    },
}

SKELETON_SCRIPT = (
    '"""Generated strategy script scaffold.\n\n'
    'The approved-script sandbox runs this module with a narrow\n'
    ':class:`nerya.scripts.script_context.ScriptContext` as ``ctx``.\n'
    'Only read-only skill actions (``market_data.*``, ``onchain.*``,\n'
    '``news_social.*``) are reachable via ``ctx.skill_call``; trading,\n'
    'LLM and wallet paths must come from the agent or an operator-run\n'
    '``nerya_sdk`` client outside the sandbox.\n"""\n'
    "from __future__ import annotations\n"
    "\n"
    "from typing import Any\n"
    "\n"
    "\n"
    "def run(ctx: Any | None = None, **_: Any) -> dict[str, Any]:\n"
    '    """Entry point invoked by ``nerya.scripts.runner.run_script``."""\n'
    "    if ctx is None:\n"
    "        # Invoked outside the sandbox (e.g. import smoke test). Keep\n"
    "        # this branch side-effect free so static analyzers and\n"
    "        # previews do not need a workspace.\n"
    '        return {"ok": True, "sandbox": False}\n'
    "    # Example: fetch a read-only market feature. Replace with the\n"
    "    # real probe once the operator approves the script.\n"
    "    ticker = ctx.skill_call(\n"
    '        "market_data", "get_ticker",\n'
    '        market="BINANCE:BTCUSDT",\n'
    "    )\n"
    '    return {"ok": True, "sandbox": True, "ticker": ticker}\n'
    "\n"
    "\n"
    "if __name__ == \"__main__\":\n"
    "    # When run as a plain script (outside the sandbox), just smoke\n"
    "    # test that the module imports and the entry point returns.\n"
    "    print(run())\n"
)


def propose_script(paths: WorkspacePaths, *, script_id: str, summary: str,
                   script: str | None = None) -> Proposal:
    manifest = {**SKELETON_MANIFEST, "id": script_id, "title": script_id}
    return create_proposal(
        paths, kind="script_proposal", summary=summary,
        rationale=f"# Script proposal {script_id}\n\n{summary}\n",
        extra_files={
            f"new_script/{script_id}.yml": yaml_io.dumps(manifest),
            f"new_script/{script_id}.py": script or SKELETON_SCRIPT,
        },
    )

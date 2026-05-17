"""Draft a workflow-to-skill proposal.

This script is intentionally proposal-first: it writes a PatchProposal under
``workspace/evolution/proposals/`` and never activates the skill directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .....core.config import load_config
from .....evolution.skill_proposal import propose_skill_from_workflow
from .....skills.manifest import cli_main


def run(_ctx=None, **payload: Any) -> dict[str, Any]:
    workspace = payload.pop("workspace", None)
    config = load_config(Path(workspace).expanduser() if workspace else None)
    return propose_skill_from_workflow(config.paths, **payload)


if __name__ == "__main__":
    cli_main(run)

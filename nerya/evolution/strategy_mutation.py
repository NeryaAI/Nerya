"""Propose tweaks to strategy/config.yml (non-protected)."""

from __future__ import annotations

from ..core import yaml_io
from ..core.paths import WorkspacePaths
from .patch_proposal import Proposal, create_proposal


def propose_config_patch(paths: WorkspacePaths, *, strategy_id: str,
                         summary: str, config_after: dict) -> Proposal:
    return create_proposal(
        paths, kind="strategy_config_patch", summary=summary,
        rationale=f"# Strategy config patch for {strategy_id}\n\n{summary}\n",
        extra_files={
            f"after/strategies/{strategy_id}/config.yml": yaml_io.dumps(config_after),
            "target.yml": yaml_io.dumps({"target": f"strategies/{strategy_id}/config.yml"}),
        },
    )


def propose_risk_suggestion(paths: WorkspacePaths, *, strategy_id: str,
                            summary: str, advisory_limits: dict) -> Proposal:
    """A suggestion — never auto-applied because limits.yml is protected."""
    return create_proposal(
        paths, kind="risk_limit_suggestion", summary=summary,
        rationale=(f"# Risk limit SUGGESTION for {strategy_id}\n\n{summary}\n\n"
                   "`limits.yml` is a protected scope and must be edited manually."),
        extra_files={"suggested_limits.yml": yaml_io.dumps(advisory_limits)},
    )

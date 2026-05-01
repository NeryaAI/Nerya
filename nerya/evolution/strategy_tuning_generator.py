"""Add a tuning block to an existing strategy package.

The :mod:`nerya.evolution.strategy_code_generator` module already
emits a tuning block when ``create_tuning=True`` is set at *creation*
time. Operators routinely promote a strategy without a tuning block
("just see if the signal works first") and later want to enable the
self-evolution loop without re-creating the package.

This module produces a minimal :class:`Proposal` of kind
``strategy_tuning_proposal`` that:

* rewrites ``strategy.yml`` with a populated ``tuning`` block, and
* writes / overwrites ``subagents/strategy_tuner.agent.md`` with the
  per-strategy prompt.

Both files are placed under
``after/strategies/<strategy_id>/`` so :mod:`nerya.evolution.promotion`
copies them in on approval.

We deliberately keep the generated YAML minimal — the validator
rejects unknown keys at promotion, and operators routinely diff the
tuning block by hand. ``schedule_cron`` defaults to ``0 */6 * * *``
to match the plan example, but any caller can override it through
:class:`StrategyTuningGenerationRequest`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..core import yaml_io
from ..core.errors import NeryaError
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from ..strategies.package import (
    StrategyPackage,
    StrategyTuningConfig,
    load_package,
)
from .patch_proposal import Proposal, create_proposal


_VALID_OBJECTIVES: frozenset[str] = frozenset({
    "risk_adjusted_return",
    "drawdown",
    "win_rate",
    "slippage",
    "execution_quality",
    "return",
    "consistency",
})


@dataclass
class StrategyTuningGenerationRequest:
    """Inputs from the agent / operator."""

    strategy_id: str
    tuning_prompt: str = ""
    cron: str = "0 */6 * * *"
    every_seconds: Optional[int] = None
    objectives: tuple[str, ...] = ("risk_adjusted_return",)
    require_backtest: bool = True
    require_shadow_run: bool = False
    require_operator_approval: bool = True
    max_patch_files: int = 5
    max_position_size_change_pct: float = 25.0
    allowed_targets: tuple[str, ...] = (
        "strategy.yml", "main.py", "subagents/*.agent.md",
    )
    forbidden_targets: tuple[str, ...] = (
        "accounts/*", "limits.yml", "secrets/*", "live_trading_enabled",
    )
    extra_subagent_prompt: str = ""


@dataclass
class StrategyTuningGenerationResult:
    request: StrategyTuningGenerationRequest
    files: dict[str, str] = field(default_factory=dict)
    proposal: Optional[Proposal] = None

    def asdict(self) -> dict[str, Any]:
        return {
            "request": asdict(self.request),
            "files": dict(self.files),
            "proposal_id": self.proposal.id if self.proposal else None,
        }


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class StrategyTuningGenerator:
    """Compose a ``strategy_tuning_proposal`` for an existing package.

    ``generate`` reads the package, mutates the manifest in-memory,
    and writes a fresh proposal. Nothing on disk changes until the
    proposal is promoted.
    """

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths

    def generate(
        self, request: StrategyTuningGenerationRequest
    ) -> StrategyTuningGenerationResult:
        self._validate(request)
        try:
            pkg = load_package(self.paths, request.strategy_id)
        except Exception as exc:
            raise NeryaError(
                f"strategy {request.strategy_id!r} not found: {exc}"
            ) from exc

        manifest_dict = pkg.manifest.asdict()
        manifest_dict["tuning"] = _render_tuning_block(request)
        manifest_dict.pop("extras", None)

        files = {
            "strategy.yml": yaml_io.dumps(manifest_dict),
            "subagents/strategy_tuner.agent.md": (
                request.extra_subagent_prompt.strip()
                or _default_tuner_prompt(pkg, request)
            ),
        }
        extra_files = {
            f"after/strategies/{request.strategy_id}/{rel}": content
            for rel, content in files.items()
        }
        proposal = create_proposal(
            self.paths,
            kind="strategy_tuning_proposal",
            summary=(
                f"Enable tuning loop for {request.strategy_id} "
                f"({', '.join(request.objectives)})"
            )[:200],
            rationale=_rationale(pkg, request),
            test_plan=_test_plan(request),
            rollback="Revert strategy.yml + remove subagents/strategy_tuner.agent.md.",
            target=f"strategies/{request.strategy_id}",
            extra_files=extra_files,
            initial_state="pending_review",
        )
        return StrategyTuningGenerationResult(
            request=request, files=files, proposal=proposal
        )

    def _validate(self, request: StrategyTuningGenerationRequest) -> None:
        if not request.strategy_id:
            raise NeryaError("strategy_id required")
        if request.cron and request.every_seconds:
            raise NeryaError("set cron OR every_seconds, not both")
        for obj in request.objectives:
            if obj not in _VALID_OBJECTIVES:
                raise NeryaError(
                    f"objective {obj!r} not in {sorted(_VALID_OBJECTIVES)!r}"
                )


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def _render_tuning_block(request: StrategyTuningGenerationRequest) -> dict[str, Any]:
    if request.every_seconds:
        schedule = {
            "type": "interval",
            "every_seconds": int(request.every_seconds),
            "enabled": True,
        }
    else:
        schedule = {
            "type": "cron",
            "cron": request.cron,
            "enabled": True,
        }
    return {
        "enabled": True,
        "schedule": schedule,
        "lookback": {
            "runs": 200,
            "min_closed_trades": 0,
            "max_age_hours": 168,
        },
        "subagent": {
            "name": "strategy_tuner",
            "prompt_file": "subagents/strategy_tuner.agent.md",
            "tier": "high",
        },
        "objectives": list(request.objectives),
        "guardrails": {
            "max_patch_files": int(request.max_patch_files),
            "max_position_size_change_pct": float(request.max_position_size_change_pct),
            "require_backtest": bool(request.require_backtest),
            "require_shadow_run": bool(request.require_shadow_run),
            "require_operator_approval": bool(request.require_operator_approval),
        },
        "proposal_policy": {
            "allowed_targets": list(request.allowed_targets),
            "forbidden_targets": list(request.forbidden_targets),
        },
        "tuning_prompt": request.tuning_prompt or "",
    }


def _default_tuner_prompt(
    pkg: StrategyPackage, request: StrategyTuningGenerationRequest
) -> str:
    objectives = ", ".join(request.objectives) or "risk_adjusted_return"
    custom = request.tuning_prompt.strip() or "Focus on improving risk-adjusted return."
    return (
        "# strategy_tuner\n\n"
        f"Strategy: `{pkg.strategy_id}` ({pkg.manifest.mode}).\n"
        f"Objectives: {objectives}.\n\n"
        "## Custom guidance\n\n"
        f"{custom}\n\n"
        "## What you must produce\n\n"
        "Return strict JSON shaped like:\n\n"
        "```json\n"
        "{\n"
        '  "summary": "<one-line takeaway>",\n'
        '  "evidence": [{ "source": "strategy_runs", "finding": "..." }],\n'
        '  "proposed_changes": [\n'
        '    {"file": "main.py", "kind": "code_patch", "rationale": "..."}\n'
        '  ],\n'
        '  "expected_effect": {"return": "neutral_or_better", "drawdown": "lower"},\n'
        '  "validation_plan": ["unit", "fixture_replay", "backtest"],\n'
        '  "risk_flags": []\n'
        "}\n"
        "```\n\n"
        "## Constraints\n\n"
        "* Never directly mutate the strategy. You only propose patches.\n"
        "* Stay within `allowed_targets` and never touch `forbidden_targets`.\n"
        "* Cite evidence drawn from the performance snapshot you receive.\n"
    )


def _rationale(
    pkg: StrategyPackage, request: StrategyTuningGenerationRequest
) -> str:
    return (
        f"# Tuning enablement proposal\n\n"
        f"Strategy: `{pkg.strategy_id}`\n"
        f"Generated: {now_iso()}\n\n"
        "Adds a self-evolution tuning loop with the following objectives:\n"
        + "".join(f"* {o}\n" for o in request.objectives)
        + "\nThe tuning loop runs on its own cron and only **proposes** "
        "patches; promotion still requires operator approval.\n"
    )


def _test_plan(request: StrategyTuningGenerationRequest) -> str:
    plan = ["validate strategy.yml after merge", "verify schedule_status"]
    if request.require_backtest:
        plan.append("first tuning run should report a backtest gate")
    if request.require_shadow_run:
        plan.append("first tuning run should report a shadow-run gate")
    body = "\n".join(f"- {p}" for p in plan)
    return f"# Test plan\n\n{body}\n"


__all__ = [
    "StrategyTuningGenerationRequest",
    "StrategyTuningGenerationResult",
    "StrategyTuningGenerator",
]

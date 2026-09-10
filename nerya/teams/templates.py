"""Built-in :class:`TeamTemplate` definitions.

Each template wires Nerya's default subagents into a structured team with
quorum/required-artifact gates. The market/research templates are adapted
from the public research runtime swarm presets and multi-analyst research analyst /
researcher / risk-manager chain, while keeping Nerya as the runtime of
record.

Operators extend the catalogue without touching Python via
``<workspace>/teams/templates/*.yml`` — the same file-driven pattern as
``subagents/*.agent.md`` and ``hooks/<phase>.py``. The YAML shape is
exactly :meth:`TeamTemplate.asdict` output::

    id: overnight_watch_team
    description: Watch funding rates overnight and escalate anomalies.
    lead: watch-lead
    members:
      - {name: watch-lead, role: research_manager, subagent_name: technical_analyst, tier: medium}
    tasks:
      - {id: t-watch, owner: watch-lead, subagent_name: technical_analyst, subject: Poll funding and alert}
    gates:
      - {id: required_tasks_t_watch, kind: required_tasks, detail: {tasks: [t-watch]}}
    max_rounds: 1
    max_parallel: 2

Workspace templates may not shadow builtin ids — collisions are
skipped with a warning (fail-closed, same stance as tool/connector
registration).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from ..core import yaml_io
from .models import (
    TeamGateSpec,
    TeamMemberSpec,
    TeamTaskSpec,
    TeamTemplate,
)

log = logging.getLogger("nerya.teams.templates")


# ----------------------------------------------------------- gate helpers


def _required_tasks(*ids: str) -> TeamGateSpec:
    return TeamGateSpec(
        id=f"required_tasks_{'_'.join(ids)}",
        kind="required_tasks",
        detail={"tasks": list(ids)},
    )


def _required_artifacts(name: str, *kinds: str) -> TeamGateSpec:
    return TeamGateSpec(
        id=f"required_artifacts_{name}",
        kind="required_artifacts",
        detail={"kinds": list(kinds)},
    )


# ----------------------------------------------------------- market_analysis_team


def _market_analysis_team() -> TeamTemplate:
    members = [
        TeamMemberSpec(
            name="market-lead",
            role="research_manager",
            subagent_name="research_manager",
            required=True,
            tier="high",
            description="Synthesizes analyst evidence into the final market memo.",
        ),
        TeamMemberSpec(
            name="technical-analyst",
            role="technical_analyst",
            subagent_name="technical_analyst",
            required=True,
            tier="medium",
            description="Reads K-line / regime / volatility evidence.",
        ),
        TeamMemberSpec(
            name="fundamentals-analyst",
            role="fundamentals_analyst",
            subagent_name="fundamentals_analyst",
            required=False,
            tier="medium",
            description="Reads fundamentals, valuation, catalysts, and red flags.",
        ),
        TeamMemberSpec(
            name="macro-strategist",
            role="macro_strategist",
            subagent_name="macro_strategist",
            required=False,
            tier="medium",
            description="Reads macro cycle, policy, rates, FX, and cross-asset pressure.",
        ),
        TeamMemberSpec(
            name="quant-researcher",
            role="quant_researcher",
            subagent_name="quant_researcher",
            required=False,
            tier="high",
            description="Validates factors, backtest evidence, leakage, and costs.",
        ),
        TeamMemberSpec(
            name="onchain-analyst",
            role="onchain_watcher",
            subagent_name="onchain_watcher",
            required=False,
            tier="medium",
            description="Reads exchange inflow/outflow and whale movements.",
        ),
        TeamMemberSpec(
            name="sentiment-analyst",
            role="sentiment_analyst",
            subagent_name="sentiment_analyst",
            required=True,
            tier="light",
            description="Reads recent headlines and macro sentiment.",
        ),
        TeamMemberSpec(
            name="risk-critic",
            role="risk_critic",
            subagent_name="risk_critic",
            required=True,
            tier="medium",
            description="Stress-tests the call: invalidation, leverage, drawdown.",
        ),
    ]
    tasks = [
        TeamTaskSpec(
            id="t-tech",
            owner="technical-analyst",
            subagent_name="technical_analyst",
            subject="Technical regime / volatility / breakout assessment",
            description=(
                "Look at recent price/volume on the requested market, "
                "describe the regime, identify breakout or rejection levels, "
                "and emit a normalized signal (bullish|bearish|neutral) with "
                "confidence and invalidation."
            ),
            output_kinds=["signal", "evidence", "claim"],
            required=True,
        ),
        TeamTaskSpec(
            id="t-fundamentals",
            owner="fundamentals-analyst",
            subagent_name="fundamentals_analyst",
            subject="Fundamental, valuation, catalyst, and red-flag review",
            output_kinds=["evidence", "claim"],
            required=False,
        ),
        TeamTaskSpec(
            id="t-macro",
            owner="macro-strategist",
            subagent_name="macro_strategist",
            subject="Macro cycle, policy, and cross-asset pressure",
            output_kinds=["evidence", "claim"],
            required=False,
        ),
        TeamTaskSpec(
            id="t-quant",
            owner="quant-researcher",
            subagent_name="quant_researcher",
            subject="Quant validation: factor/backtest/leakage/cost checks",
            output_kinds=["evidence", "risk", "decision_input"],
            required=False,
        ),
        TeamTaskSpec(
            id="t-onchain",
            owner="onchain-analyst",
            subagent_name="onchain_watcher",
            subject="Exchange inflow/outflow + whale flow snapshot",
            depends_on=[],
            output_kinds=["evidence", "claim"],
            required=False,
        ),
        TeamTaskSpec(
            id="t-news",
            owner="sentiment-analyst",
            subagent_name="sentiment_analyst",
            subject="News / sentiment for the requested market",
            output_kinds=["evidence", "claim"],
            required=True,
        ),
        TeamTaskSpec(
            id="t-risk",
            owner="risk-critic",
            subagent_name="risk_critic",
            subject="Risk review: invalidation, drawdown, stale-data, leverage",
            depends_on=["t-tech", "t-news"],
            output_kinds=["risk", "decision_input"],
            required=True,
        ),
        TeamTaskSpec(
            id="t-synth",
            owner="market-lead",
            subagent_name="research_manager",
            subject="Synthesize evidence into final memo",
            depends_on=["t-tech", "t-news", "t-risk"],
            output_kinds=["decision_input"],
            required=True,
        ),
    ]
    gates = [
        _required_tasks("t-tech", "t-news", "t-risk"),
        _required_artifacts("evidence_quorum", "evidence"),
    ]
    return TeamTemplate(
        id="market_analysis_team",
        description=(
            "Multi-expert investment research team for a market or asset. "
            "Runs technical, sentiment, optional fundamentals/macro/quant/"
            "on-chain lanes, then risk and research-manager synthesis."
        ),
        lead="market-lead",
        members=members,
        tasks=tasks,
        gates=gates,
        max_rounds=1,
        max_parallel=4,
        output_schema={
            "signal": "bullish|bearish|neutral|none",
            "confidence": "0..1",
            "invalidation": "string",
            "key_risks": "list",
            "data_freshness": "string",
            "rating": "Buy|Overweight|Hold|Underweight|Sell",
        },
    )


# ----------------------------------------------------------- investment_committee_team


def _investment_committee_team() -> TeamTemplate:
    members = [
        TeamMemberSpec(
            name="bull-side",
            role="bull_researcher",
            subagent_name="bull_researcher",
            required=True,
            tier="medium",
            description="Builds the positive investment case.",
        ),
        TeamMemberSpec(
            name="bear-side",
            role="bear_researcher",
            subagent_name="bear_researcher",
            required=True,
            tier="medium",
            description="Builds the downside and risk case.",
        ),
        TeamMemberSpec(
            name="risk-officer",
            role="risk_critic",
            subagent_name="risk_critic",
            required=True,
            tier="medium",
            description="Reviews debate quality, sizing, stops, and tail risk.",
        ),
        TeamMemberSpec(
            name="committee-chair",
            role="research_manager",
            subagent_name="research_manager",
            required=True,
            tier="high",
            description="Makes the final research rating and position guidance.",
        ),
        TeamMemberSpec(
            name="report-editor",
            role="research_editor",
            subagent_name="research_editor",
            required=False,
            tier="medium",
            description="Formats the final decision as a professional report.",
        ),
    ]
    tasks = [
        TeamTaskSpec(
            id="t-bull",
            owner="bull-side",
            subagent_name="bull_researcher",
            subject="Build the full bull case",
            description=(
                "Use technical, fundamental, sentiment, macro, and flow "
                "evidence to build a strong upside thesis with catalysts."
            ),
            output_kinds=["claim", "evidence", "decision_input"],
            required=True,
        ),
        TeamTaskSpec(
            id="t-bear",
            owner="bear-side",
            subagent_name="bear_researcher",
            subject="Build the full bear / risk case",
            description=(
                "Identify downside drivers, valuation risk, technical "
                "breakdown risk, and what would disprove the bear case."
            ),
            output_kinds=["risk", "evidence", "decision_input"],
            required=True,
        ),
        TeamTaskSpec(
            id="t-risk",
            owner="risk-officer",
            subagent_name="risk_critic",
            subject="Independent risk review and sizing guidance",
            depends_on=["t-bull", "t-bear"],
            output_kinds=["risk", "decision_input"],
            required=True,
        ),
        TeamTaskSpec(
            id="t-decision",
            owner="committee-chair",
            subagent_name="research_manager",
            subject="Final investment committee decision",
            depends_on=["t-bull", "t-bear", "t-risk"],
            output_kinds=["decision_input"],
            required=True,
        ),
        TeamTaskSpec(
            id="t-report",
            owner="report-editor",
            subagent_name="research_editor",
            subject="Format final decision as a research report",
            depends_on=["t-decision"],
            output_kinds=["report"],
            required=False,
        ),
    ]
    gates = [
        _required_tasks("t-bull", "t-bear", "t-risk", "t-decision"),
        _required_artifacts("committee_decision", "decision_input"),
    ]
    return TeamTemplate(
        id="investment_committee_team",
        description=(
            "Buy-side investment committee workflow: bull case, bear "
            "case, independent risk review, final research-manager "
            "rating, and optional report formatting."
        ),
        lead="committee-chair",
        members=members,
        tasks=tasks,
        gates=gates,
        max_rounds=1,
        max_parallel=2,
        output_schema={
            "rating": "Buy|Overweight|Hold|Underweight|Sell",
            "thesis": "string",
            "position_guidance": "object",
            "invalidation": "string",
            "review_triggers": "list",
        },
    )


# ----------------------------------------------------------- strategy_design_team


def _strategy_design_team() -> TeamTemplate:
    members = [
        TeamMemberSpec(
            name="strategy-lead",
            role="plan_lane",
            subagent_name="plan_lane",
            required=True,
            tier="high",
            description="Owns the final strategy spec and gating decisions.",
        ),
        TeamMemberSpec(
            name="market-analyst",
            role="market_analyst",
            subagent_name="market_analyst",
            required=True,
            tier="medium",
            description="Provides regime, K-line, and volatility evidence.",
        ),
        TeamMemberSpec(
            name="news-analyst",
            role="news_interpreter",
            subagent_name="news_interpreter",
            required=False,
            tier="light",
            description="Provides macro / sentiment context.",
        ),
        TeamMemberSpec(
            name="risk-critic",
            role="risk_critic",
            subagent_name="risk_critic",
            required=True,
            tier="medium",
            description="Specifies sizing, stop, drawdown limits, stale-data rules.",
        ),
        TeamMemberSpec(
            name="execution-planner",
            role="execution_planner",
            subagent_name="execution_planner",
            required=True,
            tier="medium",
            description="Specifies venue, order types, sizing constraints.",
        ),
        TeamMemberSpec(
            name="strategy-reviewer",
            role="strategy_reviewer",
            subagent_name="strategy_reviewer",
            required=True,
            tier="high",
            description="Audits testability/replay and proposes improvements.",
        ),
        TeamMemberSpec(
            name="verification-lane",
            role="verification_lane",
            subagent_name="verification_lane",
            required=False,
            tier="high",
            description="Checks the proposal can run in shadow + backtest.",
        ),
    ]
    tasks = [
        TeamTaskSpec(
            id="t-market",
            owner="market-analyst",
            subagent_name="market_analyst",
            subject="K-line + regime analysis for the target market",
            output_kinds=["evidence", "signal"],
            required=True,
        ),
        TeamTaskSpec(
            id="t-news",
            owner="news-analyst",
            subagent_name="news_interpreter",
            subject="Sentiment + macro snapshot",
            output_kinds=["evidence"],
            required=False,
        ),
        TeamTaskSpec(
            id="t-risk",
            owner="risk-critic",
            subagent_name="risk_critic",
            subject="Risk plan: sizing, stop, max drawdown, leverage rules",
            depends_on=["t-market"],
            output_kinds=["risk", "decision_input"],
            required=True,
        ),
        TeamTaskSpec(
            id="t-exec",
            owner="execution-planner",
            subagent_name="execution_planner",
            subject="Execution plan: venue, order types, slippage assumptions",
            depends_on=["t-market"],
            output_kinds=["decision_input"],
            required=True,
        ),
        TeamTaskSpec(
            id="t-spec",
            owner="strategy-lead",
            subagent_name="plan_lane",
            subject=(
                "Compose the strategy spec: entry/exit/invalidation/data needs/"
                "limits/test-replay-plan."
            ),
            depends_on=["t-market", "t-risk", "t-exec"],
            output_kinds=["decision_input"],
            required=True,
        ),
        TeamTaskSpec(
            id="t-review",
            owner="strategy-reviewer",
            subagent_name="strategy_reviewer",
            subject="Audit the proposed spec for testability/replay",
            depends_on=["t-spec"],
            output_kinds=["decision_input", "risk"],
            required=True,
        ),
    ]
    gates = [
        _required_tasks("t-market", "t-risk", "t-exec", "t-spec", "t-review"),
        _required_artifacts("strategy_spec", "decision_input"),
    ]
    return TeamTemplate(
        id="strategy_design_team",
        description=(
            "Quantitative strategy design team. Researches the market, "
            "writes a strategy spec, and reviews testability/replay. "
            "Produces a strategy proposal payload usable by "
            "``strategy.create_or_update``."
        ),
        lead="strategy-lead",
        members=members,
        tasks=tasks,
        gates=gates,
        max_rounds=1,
        max_parallel=3,
        output_schema={
            "name": "string",
            "thesis": "string",
            "entry": "string",
            "exit": "string",
            "invalidation": "string",
            "risk_limits": "object",
            "test_plan": "string",
        },
    )


BUILTIN_TEMPLATES: dict[str, TeamTemplate] = {
    "market_analysis_team": _market_analysis_team(),
    "investment_committee_team": _investment_committee_team(),
    "strategy_design_team": _strategy_design_team(),
}


# --------------------------------------------------------- workspace templates


def workspace_templates_dir(paths: Any) -> Path:
    """``<workspace>/teams/templates/`` — operator-authored team YAML."""

    return Path(getattr(paths, "root", paths)) / "teams" / "templates"


_WS_CACHE: dict[Path, tuple[float, "TeamTemplate | None", str]] = {}
_WS_CACHE_LOCK = threading.Lock()


def template_from_dict(data: dict[str, Any]) -> TeamTemplate:
    """Validate and build a :class:`TeamTemplate` from its dict form.

    Accepts the exact shape :meth:`TeamTemplate.asdict` produces so a
    YAML file can be seeded by dumping an existing template. Raises
    ``ValueError`` describing the first problem found.
    """

    if not isinstance(data, dict):
        raise ValueError("template must be a mapping")
    template_id = data.get("id")
    if not isinstance(template_id, str) or not template_id:
        raise ValueError("template.id must be a non-empty string")
    for field in ("description", "lead"):
        if not isinstance(data.get(field), str) or not data[field]:
            raise ValueError(f"template.{field} must be a non-empty string")

    members: list[TeamMemberSpec] = []
    raw_members = data.get("members") or []
    if not isinstance(raw_members, list):
        raise ValueError("template.members must be a list")
    for i, m in enumerate(raw_members):
        if not isinstance(m, dict):
            raise ValueError(f"members[{i}] must be a mapping")
        for field in ("name", "role", "subagent_name"):
            if not isinstance(m.get(field), str) or not m[field]:
                raise ValueError(f"members[{i}].{field} must be a non-empty string")
        members.append(
            TeamMemberSpec(
                name=m["name"],
                role=m["role"],
                subagent_name=m["subagent_name"],
                required=bool(m.get("required", True)),
                allowed_skills=list(m.get("allowed_skills", []) or []),
                tier=str(m.get("tier", "medium")),
                description=str(m.get("description", "")),
                provider=str(m.get("provider", "")),
                model=str(m.get("model", "")),
                execution_policy=dict(m.get("execution_policy", {}) or {}),
            )
        )

    tasks: list[TeamTaskSpec] = []
    raw_tasks = data.get("tasks") or []
    if not isinstance(raw_tasks, list):
        raise ValueError("template.tasks must be a list")
    for i, t in enumerate(raw_tasks):
        if not isinstance(t, dict):
            raise ValueError(f"tasks[{i}] must be a mapping")
        for field in ("id", "owner", "subagent_name", "subject"):
            if not isinstance(t.get(field), str) or not t[field]:
                raise ValueError(f"tasks[{i}].{field} must be a non-empty string")
        tasks.append(
            TeamTaskSpec(
                id=t["id"],
                owner=t["owner"],
                subagent_name=t["subagent_name"],
                subject=t["subject"],
                description=str(t.get("description", "")),
                depends_on=list(t.get("depends_on", []) or []),
                required=bool(t.get("required", True)),
                output_kinds=list(t.get("output_kinds", []) or []),
            )
        )

    gates: list[TeamGateSpec] = []
    raw_gates = data.get("gates") or []
    if not isinstance(raw_gates, list):
        raise ValueError("template.gates must be a list")
    for i, g in enumerate(raw_gates):
        if not isinstance(g, dict):
            raise ValueError(f"gates[{i}] must be a mapping")
        gates.append(
            TeamGateSpec(
                id=str(g.get("id", f"gate_{i}")),
                kind=str(g.get("kind", "")),
                detail=dict(g.get("detail", {}) or {}),
            )
        )

    def _int_field(name: str, default: int) -> int:
        value = data.get(name, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(f"template.{name} must be an integer") from None

    return TeamTemplate(
        id=template_id,
        description=data["description"],
        lead=data["lead"],
        members=members,
        tasks=tasks,
        gates=gates,
        max_rounds=_int_field("max_rounds", 2),
        max_parallel=_int_field("max_parallel", 3),
        output_schema=dict(data.get("output_schema", {}) or {}),
    )


def load_workspace_templates(
    paths: Any, *, reread: bool = False
) -> tuple[dict[str, TeamTemplate], list[str]]:
    """Load ``teams/templates/*.yml`` with an mtime cache.

    Returns ``(templates, errors)``. Builtin-id collisions and parse
    failures are reported in ``errors`` and skipped — one bad file
    never hides the rest.
    """

    directory = workspace_templates_dir(paths)
    if not directory.is_dir():
        return {}, []

    result: dict[str, TeamTemplate] = {}
    errors: list[str] = []
    for file in sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml")):
        try:
            mtime = file.stat().st_mtime
        except OSError:
            continue
        with _WS_CACHE_LOCK:
            cached = _WS_CACHE.get(file)
        if cached is not None and not reread and cached[0] == mtime:
            template, error = cached[1], cached[2]
        else:
            error = ""
            template = None
            try:
                data = yaml_io.load(file, default=None)
                template = template_from_dict(data or {})
            except Exception as exc:
                error = f"{file.name}: {exc}"
            with _WS_CACHE_LOCK:
                _WS_CACHE[file] = (mtime, template, error)

        if error:
            errors.append(error)
            continue
        if template is None:
            continue
        if template.id in BUILTIN_TEMPLATES:
            errors.append(
                f"{file.name}: id {template.id!r} collides with a builtin template"
            )
            continue
        if template.id in result:
            errors.append(f"{file.name}: duplicate template id {template.id!r}")
            continue
        result[template.id] = template
    for err in errors:
        log.warning("workspace team template skipped: %s", err)
    return result, errors


def reload_workspace_templates() -> None:
    """Drop the mtime cache so the next read re-parses every file."""

    with _WS_CACHE_LOCK:
        _WS_CACHE.clear()


def get_template(template_id: str, paths: Any = None) -> Optional[TeamTemplate]:
    tpl = BUILTIN_TEMPLATES.get(template_id)
    if tpl is not None:
        return tpl
    if paths is None:
        return None
    templates, _ = load_workspace_templates(paths)
    return templates.get(template_id)


def list_templates(paths: Any = None) -> list[dict[str, str]]:
    out = [
        {"id": tpl.id, "description": tpl.description, "lead": tpl.lead,
         "members": len(tpl.members), "tasks": len(tpl.tasks),
         "source": "builtin"}
        for tpl in BUILTIN_TEMPLATES.values()
    ]
    if paths is not None:
        workspace, _ = load_workspace_templates(paths)
        out.extend(
            {"id": tpl.id, "description": tpl.description, "lead": tpl.lead,
             "members": len(tpl.members), "tasks": len(tpl.tasks),
             "source": "workspace"}
            for tpl in sorted(workspace.values(), key=lambda t: t.id)
        )
    return out


__all__ = [
    "BUILTIN_TEMPLATES",
    "get_template",
    "list_templates",
    "load_workspace_templates",
    "reload_workspace_templates",
    "template_from_dict",
    "workspace_templates_dir",
]

"""Pure data models for the Agent Team layer.

Every model is JSON-serialisable via :py:meth:`asdict` and reconstructible
via :py:meth:`from_dict` so persistence is just JSON read/write. No
business logic lives here — see ``store.py``, ``mailbox.py``, etc.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..core.time import now_iso


# ----------------------------------------------------------- specs (templates)


@dataclass
class TeamMemberSpec:
    """A member slot in a :class:`TeamTemplate`.

    ``subagent_name`` maps to a Nerya ``SubAgentSpec`` from
    ``nerya/subagents/registry.py``. ``required`` controls quorum gates.
    """

    name: str
    role: str
    subagent_name: str
    required: bool = True
    allowed_skills: list[str] = field(default_factory=list)
    tier: str = "medium"
    description: str = ""

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamTaskSpec:
    """A task slot in a :class:`TeamTemplate`."""

    id: str
    owner: str
    subagent_name: str
    subject: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    required: bool = True
    output_kinds: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamGateSpec:
    """A gate definition (quorum / artifact / risk / verification)."""

    id: str
    kind: str  # required_tasks | required_artifacts | risk_review | verification
    detail: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamTemplate:
    id: str
    description: str
    lead: str
    members: list[TeamMemberSpec]
    tasks: list[TeamTaskSpec]
    gates: list[TeamGateSpec] = field(default_factory=list)
    max_rounds: int = 2
    max_parallel: int = 3
    usd_budget: Optional[float] = None
    output_schema: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "lead": self.lead,
            "members": [m.asdict() for m in self.members],
            "tasks": [t.asdict() for t in self.tasks],
            "gates": [g.asdict() for g in self.gates],
            "max_rounds": self.max_rounds,
            "max_parallel": self.max_parallel,
            "usd_budget": self.usd_budget,
            "output_schema": dict(self.output_schema),
        }


# ----------------------------------------------------------- run-time records


@dataclass
class TeamMember:
    name: str
    role: str
    subagent_name: str
    required: bool = True
    allowed_skills: list[str] = field(default_factory=list)
    tier: str = "medium"
    status: str = "idle"
    last_task_id: Optional[str] = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_spec(cls, spec: TeamMemberSpec) -> "TeamMember":
        return cls(
            name=spec.name,
            role=spec.role,
            subagent_name=spec.subagent_name,
            required=spec.required,
            allowed_skills=list(spec.allowed_skills),
            tier=spec.tier,
        )


@dataclass
class TeamTask:
    id: str
    run_id: str
    owner: str
    subagent_name: str
    subject: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    required: bool = True
    status: str = "pending"  # pending|in_progress|completed|failed|blocked
    payload: dict[str, Any] = field(default_factory=dict)
    result_artifact: Optional[str] = None
    result_summary: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamMessage:
    id: str
    run_id: str
    type: str  # message|broadcast|task_assigned|task_completed|question|conflict|operator_note
    from_agent: str
    to: Optional[str]
    content: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    consumed_at: Optional[str] = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BlackboardEntry:
    id: str
    run_id: str
    kind: str  # evidence|claim|signal|risk|question|assumption|conflict|decision_input
    author: str
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    confidence: Optional[float] = None
    source_refs: list[str] = field(default_factory=list)
    task_id: Optional[str] = None
    created_at: str = field(default_factory=now_iso)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamRun:
    id: str
    template_id: str
    goal: str
    status: str = "pending"  # pending|running|synthesizing|completed|failed|cancelled
    phase: str = "plan"      # plan|research|risk_review|synthesis|close
    turn_id: Optional[str] = None
    trigger_event_id: Optional[str] = None
    strategy_id: Optional[str] = None
    session_id: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    final_context_ref: Optional[str] = None
    final_report_ref: Optional[str] = None
    metrics: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamRunResult:
    run_id: str
    template_id: str
    status: str
    phase: str
    final_context: dict[str, Any]
    final_report_path: Optional[str] = None
    final_report_excerpt: Optional[str] = None
    members: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    blackboard_size: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)

    def step_detail(self) -> dict[str, Any]:
        """Compact summary for kernel turn-step journals."""

        return {
            "run_id": self.run_id,
            "template_id": self.template_id,
            "status": self.status,
            "phase": self.phase,
            "tasks_completed": sum(1 for t in self.tasks if t.get("status") == "completed"),
            "tasks_total": len(self.tasks),
            "blackboard": self.blackboard_size,
            "report_path": self.final_report_path,
        }

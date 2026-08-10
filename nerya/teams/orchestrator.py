"""TeamOrchestrator — runs a :class:`TeamTemplate` to completion.

Orchestrator responsibilities:

* Materialise a :class:`TeamRun` and persist its template/members/tasks.
* Seed the blackboard with the goal + trigger context.
* Schedule tasks honoring ``depends_on`` and ``max_parallel``.
* Dispatch each task to a Nerya :class:`SubAgentRuntime` via the
  :class:`SubAgentDispatcher` (so the existing skill/denylist/budget
  rails apply unchanged).
* Append produced ``signal``/``evidence``/``risk``/``decision_input``
  records to the shared :class:`Blackboard`.
* Evaluate gates (``required_tasks`` / ``required_artifacts``) and
  emit synthesis artifacts via :class:`TeamAggregator`.

The orchestrator itself never calls an LLM; every reasoning step is
delegated to a subagent runtime.
"""

from __future__ import annotations

import json
import inspect
import tempfile
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core import jsonl
from ..core.config import Config
from ..core.paths import WorkspacePaths
from ..core.redaction import redact_display_dict
from ..core.time import now_iso
from ..skills.kernel import SkillKernel
from ..subagents.dispatcher import SubAgentDispatcher, SubAgentResult
from ..subagents.registry import build_inline_spec
from .aggregator import TeamAggregator
from .blackboard import Blackboard
from .gates import evaluate_gates
from .mailbox import Mailbox
from .models import (
    TeamMember,
    TeamMemberSpec,
    TeamRun,
    TeamRunResult,
    TeamTask,
    TeamTaskSpec,
    TeamGateSpec,
    TeamTemplate,
)
from .store import TeamStore
from .templates import get_template


@dataclass
class TeamRunRequest:
    """Typed boundary shared by native ``team_run`` and the durable runner.

    Native tools may still accept their historical JSON shape, but once the
    call crosses this boundary there is one execution owner: ``TeamOrchestrator``.
    """

    task: str
    template: TeamTemplate | str | None = None
    roles: list[str] = field(default_factory=list)
    role_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    role_specs: dict[str, Any] = field(default_factory=dict)
    role_assignment_prompts: dict[str, str] = field(default_factory=dict)
    shared_payload: dict[str, Any] = field(default_factory=dict)
    output_language: str = "the original user prompt language"
    analysis_language: str = "the original user prompt language"
    trigger: Optional[dict[str, Any]] = None
    memory_preview: Optional[str] = None
    strategy_id: Optional[str] = None
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    parent_call_id: Optional[str] = None
    delegation_depth: int = 0
    run_id: Optional[str] = None
    max_parallel: Optional[int] = None
    timeout_s: Optional[float] = None
    cancel_token: Any = None
    executor: Any = None

    def __post_init__(self) -> None:
        self.roles = [str(role) for role in (self.roles or []) if str(role).strip()]
        self.role_payloads = {
            str(name): dict(payload)
            for name, payload in (self.role_payloads or {}).items()
            if isinstance(payload, dict)
        }
        self.role_specs = dict(self.role_specs or {})
        self.role_assignment_prompts = {
            str(name): str(prompt)
            for name, prompt in (self.role_assignment_prompts or {}).items()
        }
        self.shared_payload = dict(self.shared_payload or {})


class _TeamCancelled(RuntimeError):
    """Internal cooperative stop used to close a durable team cleanly."""


def _token_is_set(token: Any) -> bool:
    if token is None:
        return False
    state = getattr(token, "is_set", False)
    try:
        return bool(state() if callable(state) else state)
    except Exception:
        return True


def _permission_pending_record(result: SubAgentResult) -> dict[str, Any] | None:
    if str(result.error_kind or "").strip() == "permission_pending":
        return {
            "error_kind": "permission_pending",
            "error": result.error or "approval required",
        }
    records = result.metrics.get("rejected_actions") if isinstance(result.metrics, dict) else None
    if not isinstance(records, list):
        return None
    return next(
        (
            record
            for record in records
            if isinstance(record, dict)
            and str(record.get("error_kind") or "").strip() == "permission_pending"
        ),
        None,
    )


@dataclass
class TeamOrchestrator:
    """Orchestrates a single :class:`TeamRun` end-to-end.

    Construct once per process. ``run`` is safe to call many times in
    series; concurrent runs are also supported because every per-run
    file path is namespaced by ``run_id``.
    """

    config: Config
    skills: SkillKernel
    dispatcher: Optional[SubAgentDispatcher] = None
    tool_registry: Any = None
    executor: Any = None
    cancel_token: Any = None

    def __post_init__(self) -> None:
        if self.dispatcher is None:
            kwargs: dict[str, Any] = {}
            if self.tool_registry is not None:
                kwargs["tool_registry"] = self.tool_registry
            if self.executor is not None:
                kwargs["executor"] = self.executor
            self.dispatcher = SubAgentDispatcher(self.config, self.skills, **kwargs)
        else:
            # A caller may inject a test/custom dispatcher. Keep the parent
            # executor on that same object so nested members cannot silently
            # fall back to an unguarded native path.
            if self.tool_registry is not None and not hasattr(self.dispatcher, "tool_registry"):
                try:
                    self.dispatcher.tool_registry = self.tool_registry
                except Exception:
                    pass
            if self.executor is not None:
                try:
                    self.dispatcher.executor = self.executor
                except Exception:
                    pass
        paths = getattr(self.config, "paths", None)
        if paths is None:
            # Keep lightweight injected test configs usable without writing to
            # the repository or a shared default workspace.
            paths = WorkspacePaths(root=Path(tempfile.mkdtemp(prefix="nerya-team-")))
        self.store = TeamStore(paths)
        self.aggregator = TeamAggregator(self.store)

    def run_request(self, request: TeamRunRequest) -> TeamRunResult:
        """Run a typed native/durable request through this orchestrator."""

        if request.executor is not None:
            self.executor = request.executor
            try:
                self.dispatcher.executor = request.executor  # type: ignore[union-attr]
            except Exception:
                pass
        if request.cancel_token is not None:
            self.cancel_token = request.cancel_token
        template: TeamTemplate | str
        if request.roles:
            template = self._template_from_request(request)
        else:
            template = request.template or "market_analysis_team"
        return self.run(
            template=template,
            goal=request.task,
            trigger=request.trigger,
            memory_preview=request.memory_preview,
            strategy_id=request.strategy_id,
            session_id=request.session_id,
            turn_id=request.turn_id,
            run_id=request.run_id,
            request=request,
        )

    def _template_from_request(self, request: TeamRunRequest) -> TeamTemplate:
        """Materialise one ephemeral template for a native role list."""

        names = list(dict.fromkeys(request.roles or []))
        if not names:
            raise ValueError("team request requires at least one role")
        members = []
        tasks = []
        required_task_ids: list[str] = []
        for name in names:
            spec = request.role_specs.get(name)
            policy = getattr(spec, "execution_policy", {}) or {}
            if hasattr(policy, "asdict"):
                policy = policy.asdict()
            required = True
            if isinstance(request.role_payloads.get(name), dict):
                required = request.role_payloads[name].get("required", True) is not False
            members.append(
                TeamMemberSpec(
                    name=name,
                    role=name,
                    subagent_name=name,
                    required=required,
                    allowed_skills=list(getattr(spec, "allowed_skills", []) or []),
                    tier=str(getattr(spec, "tier", "medium") or "medium"),
                    provider=str(getattr(spec, "provider", "") or ""),
                    model=str(getattr(spec, "model", "") or ""),
                    execution_policy=dict(policy) if isinstance(policy, dict) else {},
                    description="Native team member.",
                )
            )
            task_id = f"role-{name}"
            if required:
                required_task_ids.append(task_id)
            tasks.append(
                TeamTaskSpec(
                    id=task_id,
                    owner=name,
                    subagent_name=name,
                    subject=request.task,
                    description=str(
                        (request.role_payloads.get(name) or {}).get("__team_instructions")
                        or "Execute the assigned team lane and return evidence-first JSON."
                    ),
                    required=required,
                    output_kinds=["signal", "evidence", "claim", "risk", "decision_input"],
                )
            )
        gates = (
            [
                TeamGateSpec(
                    id="required_team_members",
                    kind="required_tasks",
                    detail={"tasks": required_task_ids},
                )
            ]
            if required_task_ids
            else []
        )
        return TeamTemplate(
            id=str(request.template or "ad_hoc_parallel_team"),
            description="Ephemeral native team request.",
            lead=names[0],
            members=members,
            tasks=tasks,
            gates=gates,
            max_rounds=1,
            max_parallel=max(1, int(request.max_parallel or len(names))),
            output_schema={"kind": "team_run"},
        )

    # ------------------------------------------------------------------ public
    def run(
        self,
        *,
        template: TeamTemplate | str,
        goal: str,
        trigger: Optional[dict[str, Any]] = None,
        memory_preview: Optional[str] = None,
        strategy_id: Optional[str] = None,
        session_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        run_id: Optional[str] = None,
        request: TeamRunRequest | None = None,
    ) -> TeamRunResult:
        cancel_token = (
            request.cancel_token if request is not None else self.cancel_token
        )
        if _token_is_set(cancel_token):
            # Materialise no tasks for a request cancelled before scheduling.
            tpl = self._resolve_template(template)
            cancelled_id = run_id or f"team-{tpl.id}-{uuid.uuid4().hex[:8]}"
            return TeamRunResult(
                run_id=cancelled_id,
                template_id=tpl.id,
                status="cancelled",
                phase="close",
                final_context={
                    "run_id": cancelled_id,
                    "template_id": tpl.id,
                    "goal": goal,
                    "status": "cancelled",
                    "phase": "close",
                    "cancel_reason": str(getattr(cancel_token, "reason", "") or "cancelled"),
                },
                error=str(getattr(cancel_token, "reason", "") or "cancelled"),
            )
        tpl = self._resolve_template(template)
        run_id = run_id or f"team-{tpl.id}-{uuid.uuid4().hex[:8]}"
        run = TeamRun(
            id=run_id,
            template_id=tpl.id,
            goal=goal,
            status="pending",
            phase="plan",
            turn_id=turn_id,
            strategy_id=strategy_id,
            session_id=session_id,
            trigger_event_id=(trigger or {}).get("trigger_event_id"),
        )
        members = [TeamMember.from_spec(m) for m in tpl.members]
        self.store.create_run(run, tpl, members)
        self.store.append_event(run.id, kind="phase.enter", phase="plan", goal=goal)
        bb = Blackboard(self.store, run.id)
        bb.seed(goal=goal, trigger=trigger or {}, memory_preview=memory_preview)

        tasks = self._materialise_tasks(run, tpl)
        for t in tasks:
            self.store.create_task(t)

        run.status = "running"
        run.phase = "research"
        self.store.update_run(run)
        self.store.append_event(run.id, kind="phase.enter", phase="research")

        member_results: list[SubAgentResult] = []
        try:
            member_results = self._execute_tasks(
                run=run, template=tpl, tasks=tasks,
                strategy_id=strategy_id, session_id=session_id,
                request=request, cancel_token=cancel_token,
            )
            run.phase = "risk_review"
            self.store.update_run(run)
            self.store.append_event(run.id, kind="phase.enter", phase="risk_review")

            outcomes = evaluate_gates(tpl.gates, tasks=tasks, blackboard=bb)
            self.store.append_event(
                run.id, kind="gates.evaluated",
                ok=all(g.ok for g in outcomes),
                outcomes=[g.asdict() for g in outcomes],
            )

            gates_ok = all(g.ok for g in outcomes) and not any(
                task.required and task.status == "blocked" for task in tasks
            )

            run.phase = "synthesis"
            self.store.update_run(run)
            self.store.append_event(run.id, kind="phase.enter", phase="synthesis")
            conflict_matrix = self.aggregator.build_conflict_matrix(run.id)
            run.final_context_ref = "synthesis/final_context.json"
            run.final_report_ref = "synthesis/final_report.md"
            # A report may still be useful when a gate blocks, but the run is
            # not complete until every declared gate passes. This keeps the
            # terminal status aligned with the evidence contract.
            run.status = "completed" if gates_ok else "blocked"
            run.phase = "close"
            run.metrics = {
                "tasks_total": len(tasks),
                "tasks_completed": sum(1 for t in tasks if t.status == "completed"),
                "blackboard_size": len(bb.list()),
                "conflicts": int(conflict_matrix.get("count", 0)),
                "gates_passed": sum(1 for g in outcomes if g.ok),
                "gates_total": len(outcomes),
            }
            self.store.update_run(run)
            final_context = self.aggregator.build_final_context(
                run=run, tasks=tasks, gates=outcomes,
            )
            report = self.aggregator.build_final_report(
                run=run, final_context=final_context,
            )
            self.store.append_event(
                run.id,
                kind="run.completed" if gates_ok else "run.blocked",
                metrics=run.metrics,
            )
        except _TeamCancelled as exc:
            run.status = "cancelled"
            run.phase = "close"
            run.error = str(exc) or "cancelled"
            run.metrics = {
                "tasks_total": len(tasks),
                "tasks_completed": sum(1 for t in tasks if t.status == "completed"),
                "tasks_cancelled": sum(1 for t in tasks if t.status == "cancelled"),
                "cancel_reason": run.error,
            }
            self.store.update_run(run)
            self.store.append_event(run.id, kind="run.cancelled", error=run.error)
            final_context = {
                "run_id": run.id,
                "template_id": tpl.id,
                "goal": run.goal,
                "phase": run.phase,
                "status": run.status,
                "cancel_reason": run.error,
                "tasks_summary": [
                    {"id": t.id, "owner": t.owner, "status": t.status, "error": t.error}
                    for t in tasks
                ],
            }
            return TeamRunResult(
                run_id=run.id,
                template_id=tpl.id,
                status=run.status,
                phase=run.phase,
                final_context=final_context,
                members=[m.asdict() for m in members],
                tasks=[t.asdict() for t in tasks],
                blackboard_size=len(bb.list()),
                metrics=run.metrics,
                error=run.error,
                member_results=[res.asdict() for res in member_results],
            )
        except Exception as exc:
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            self.store.update_run(run)
            self.store.append_event(run.id, kind="run.failed", error=run.error)
            return TeamRunResult(
                run_id=run.id, template_id=tpl.id,
                status=run.status, phase=run.phase,
                final_context={"error": run.error}, error=run.error,
                tasks=[t.asdict() for t in tasks],
                members=[m.asdict() for m in members],
                blackboard_size=len(bb.list()),
                metrics=run.metrics,
            )

        self._mirror_to_agent_journal(run, final_context)

        return TeamRunResult(
            run_id=run.id,
            template_id=tpl.id,
            status=run.status,
            phase=run.phase,
            final_context=final_context,
            final_report_path=str(self.store.synthesis_dir(run.id) / "final_report.md"),
            final_report_excerpt=report[:1200],
            members=[m.asdict() for m in members],
            tasks=[t.asdict() for t in tasks],
            blackboard_size=len(bb.list()),
            metrics=run.metrics,
            member_results=[res.asdict() for res in member_results],
        )

    # ------------------------------------------------------------------ helpers
    def _resolve_template(self, template: TeamTemplate | str) -> TeamTemplate:
        if isinstance(template, TeamTemplate):
            return template
        tpl = get_template(str(template))
        if tpl is None:
            raise ValueError(f"unknown team template: {template!r}")
        return tpl

    def _materialise_tasks(self, run: TeamRun, tpl: TeamTemplate) -> list[TeamTask]:
        out: list[TeamTask] = []
        for spec in tpl.tasks:
            out.append(TeamTask(
                id=spec.id,
                run_id=run.id,
                owner=spec.owner,
                subagent_name=spec.subagent_name,
                subject=spec.subject,
                description=spec.description,
                depends_on=list(spec.depends_on),
                required=spec.required,
                status="pending",
                payload={"output_kinds": list(spec.output_kinds)},
            ))
        return out

    # ------------------------------------------------------------------ exec
    def _execute_tasks(
        self,
        *,
        run: TeamRun,
        template: TeamTemplate,
        tasks: list[TeamTask],
        strategy_id: Optional[str],
        session_id: Optional[str],
        request: TeamRunRequest | None = None,
        cancel_token: Any = None,
    ) -> list[SubAgentResult]:
        bb = Blackboard(self.store, run.id)
        mailbox = Mailbox(self.store, run.id)
        max_parallel = max(1, int(template.max_parallel or 1))
        index = {t.id: t for t in tasks}
        remaining = {t.id for t in tasks}
        members = {member.name: member for member in template.members}
        member_results: list[SubAgentResult] = []
        deadline = None
        if request is not None and request.timeout_s and request.timeout_s > 0:
            deadline = time.monotonic() + float(request.timeout_s)

        def stop_reason() -> str | None:
            if _token_is_set(cancel_token):
                return str(getattr(cancel_token, "reason", "") or "cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                return "team_timeout"
            return None

        def cancel_remaining(reason: str, futures: Any = ()) -> None:
            for future in futures:
                if not future.done():
                    future.cancel()
            for tid in list(remaining):
                task = index[tid]
                if task.status in {"pending", "in_progress"}:
                    task.status = "cancelled"
                    task.error = reason
                    task.completed_at = now_iso()
                    self.store.update_task(task)
                remaining.discard(tid)
            raise _TeamCancelled(reason)

        while remaining:
            reason = stop_reason()
            if reason is not None:
                cancel_remaining(reason)
            ready = [
                tid for tid in (t.id for t in tasks)
                if tid in remaining
                if index[tid].status == "pending"
                and all(index[d].status == "completed" or
                        (not index[d].required and index[d].status in ("failed", "blocked"))
                        for d in index[tid].depends_on)
            ]
            if not ready:
                # Deadlock or unavailable required deps; mark blocked.
                for tid in list(remaining):
                    t = index[tid]
                    blocked_by = [d for d in t.depends_on
                                  if index[d].status not in ("completed",)]
                    if any(
                        index[b].required
                        and index[b].status in {"failed", "blocked", "cancelled"}
                        for b in blocked_by
                    ):
                        t.status = "blocked"
                        t.error = f"blocked by deps: {blocked_by}"
                        t.completed_at = now_iso()
                        self.store.update_task(t)
                        remaining.discard(tid)
                if remaining:
                    # Nothing made progress and nothing is blocked — break to avoid infinite loop.
                    break
                continue

            pool = ThreadPoolExecutor(max_workers=min(max_parallel, len(ready)))
            futures: dict[Future, str] = {}
            try:
                for tid in ready:
                    t = index[tid]
                    t.status = "in_progress"
                    t.started_at = now_iso()
                    payload = self._task_payload(
                        run=run, template=template, task=t,
                        blackboard=bb, mailbox=mailbox,
                        request=request,
                    )
                    t.payload = {
                        **(t.payload or {}),
                        "input_payload": redact_display_dict(payload),
                        "assignment_prompt": (
                            (request.role_assignment_prompts.get(t.owner) if request else None)
                            or self._assignment_prompt(
                                run=run, template=template, task=t, payload=payload,
                            )
                        ),
                    }
                    self.store.update_task(t)
                    futures[pool.submit(
                        self._run_task,
                        task=t, payload=payload,
                        trigger_event_id=run.trigger_event_id,
                        strategy_id=strategy_id, session_id=session_id,
                        member_spec=members.get(t.owner),
                        request=request,
                        turn_id=run.turn_id,
                        deadline=deadline,
                    )] = tid
                pending = set(futures)
                while pending:
                    reason = stop_reason()
                    if reason is not None:
                        cancel_remaining(reason, pending)
                    done, pending = wait(
                        pending,
                        timeout=0.05,
                        return_when=FIRST_COMPLETED,
                    )
                    for fut in done:
                        tid = futures[fut]
                        res: SubAgentResult = fut.result()
                        member_results.append(res)
                        self._integrate_result(
                            task=index[tid], result=res, blackboard=bb,
                            mailbox=mailbox,
                        )
                        remaining.discard(tid)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        return member_results

    def _task_payload(
        self,
        *,
        run: TeamRun,
        template: TeamTemplate,
        task: TeamTask,
        blackboard: Blackboard,
        mailbox: Mailbox,
        request: TeamRunRequest | None = None,
    ) -> dict[str, Any]:
        preview = blackboard.preview_for_agent(task.owner, max_entries=10)
        inbox = [m.asdict() for m in mailbox.peek(task.owner, limit=10)]
        payload = {
            "team_run_id": run.id,
            "team_template": template.id,
            "team_goal": run.goal,
            "task_id": task.id,
            "task_owner": task.owner,
            "task_subject": task.subject,
            "task_description": task.description,
            "expected_output_kinds": task.payload.get("output_kinds") or [],
            "blackboard_preview": preview,
            "inbox_messages": inbox,
            "instruction": (
                "Respond with structured JSON. Include `summary` (string), "
                "an optional `signal` (one of: bullish|bearish|neutral|none), "
                "`confidence` (0..1), `evidence` (list of {summary, source}), "
                "`risks` (list), and `output` (final structured payload). "
                "Use `signal_calls` to request follow-up evidence "
                "from your `allowed_skills`."
            ),
        }
        if request is not None:
            payload.update(request.shared_payload)
            payload.update(request.role_payloads.get(task.owner) or {})
            payload.update({
                "output_language": request.output_language,
                "analysis_language": request.analysis_language,
                "original_user_prompt": (
                    request.shared_payload.get("original_user_prompt")
                    or payload.get("original_user_prompt")
                ),
            })
            assignment = request.role_assignment_prompts.get(task.owner)
            if assignment:
                payload["assignment_prompt"] = assignment
        return payload

    def _assignment_prompt(
        self,
        *,
        run: TeamRun,
        template: TeamTemplate,
        task: TeamTask,
        payload: dict[str, Any],
    ) -> str:
        return "\n".join([
            "Agent Team task assignment",
            "",
            f"Team template: {template.id}",
            f"Team goal: {run.goal}",
            f"Task id: {task.id}",
            f"Owner: {task.owner}",
            f"Subagent: {task.subagent_name}",
            f"Subject: {task.subject}",
            f"Description: {task.description}",
            "",
            "Input payload:",
            json.dumps(redact_display_dict(payload), ensure_ascii=False, indent=2, default=str),
        ])

    def _run_task(
        self,
        *,
        task: TeamTask,
        payload: dict[str, Any],
        trigger_event_id: Optional[str],
        strategy_id: Optional[str],
        session_id: Optional[str],
        member_spec: Any = None,
        request: TeamRunRequest | None = None,
        turn_id: Optional[str] = None,
        deadline: float | None = None,
    ) -> SubAgentResult:
        assert self.dispatcher is not None
        inline_spec = (
            request.role_specs.get(task.owner)
            if request is not None and task.owner in request.role_specs
            else None
        )
        if inline_spec is None and member_spec is not None and request is None:
            inline_spec = build_inline_spec(
                self.config.paths,
                name=task.subagent_name,
                allowed_skills=list(member_spec.allowed_skills or []) or None,
                tier=member_spec.tier or None,
                provider=member_spec.provider or None,
                model=member_spec.model or None,
                execution_policy=member_spec.execution_policy or None,
            )
        kwargs = {
            "payload": payload,
            "trigger_event_id": trigger_event_id,
            "strategy_id": strategy_id,
            "session_id": session_id,
            "turn_id": request.turn_id if request is not None else turn_id,
            "parent_call_id": request.parent_call_id if request is not None else None,
            "inline_spec": inline_spec,
        }
        if request is not None:
            kwargs["delegation_depth"] = request.delegation_depth
            if deadline is not None:
                kwargs["max_wall_seconds"] = max(
                    0.0, deadline - time.monotonic()
                )
        dispatch = getattr(self.dispatcher, "dispatch", None)
        if callable(dispatch):
            target = f"subagent:{task.subagent_name}"
            if request is not None:
                kwargs["cancel_token"] = request.cancel_token
            envelope = dispatch(target, **self._supported_kwargs(dispatch, kwargs))
            if isinstance(envelope, SubAgentResult):
                return envelope
            if isinstance(envelope, dict):
                return SubAgentResult(
                    ok=bool(envelope.get("ok", True)),
                    subagent=task.subagent_name,
                    tier=str(envelope.get("tier") or "medium"),
                    provider=str(envelope.get("provider") or ""),
                    model=str(envelope.get("model") or ""),
                    output=dict(envelope.get("output") or {}),
                    tokens=int(envelope.get("tokens") or 0),
                    usd=float(envelope.get("usd") or 0.0),
                    wall_ms=int(envelope.get("wall_ms") or 0),
                    error=envelope.get("error"),
                    error_kind=envelope.get("error_kind"),
                    metrics=dict(envelope.get("metrics") or {}),
                    steps=list(envelope.get("steps") or []),
                    audit=dict(envelope.get("audit") or {}),
                )
            return SubAgentResult(
                ok=False,
                subagent=task.subagent_name,
                error="dispatcher returned an invalid envelope",
                error_kind="unknown",
            )
        return self.dispatcher._run_one(
            task.subagent_name,
            **self._supported_kwargs(self.dispatcher._run_one, kwargs),
        )

    @staticmethod
    def _supported_kwargs(callable_obj: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Keep compatibility with injected dispatchers that predate new kwargs."""
        try:
            params = inspect.signature(callable_obj).parameters.values()
        except (TypeError, ValueError):
            return kwargs
        if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params):
            return kwargs
        accepted = {
            param.name
            for param in params
            if param.kind is not inspect.Parameter.POSITIONAL_ONLY
        }
        return {name: value for name, value in kwargs.items() if name in accepted}

    def _integrate_result(
        self,
        *,
        task: TeamTask,
        result: SubAgentResult,
        blackboard: Blackboard,
        mailbox: Mailbox,
    ) -> None:
        pending = _permission_pending_record(result)
        if pending is not None:
            task.status = "blocked"
            task.error = str(pending.get("error") or result.error or "permission_pending")
            task.payload = {
                **(task.payload or {}),
                "permission_pending": redact_display_dict(dict(pending)),
            }
            task.completed_at = now_iso()
            self.store.update_task(task)
            return
        if not result.ok:
            task.status = "failed"
            task.error = result.error or "subagent_run_failed"
            task.completed_at = now_iso()
            self.store.update_task(task)
            return

        out = result.output if isinstance(result.output, dict) else {}
        summary = str(out.get("summary") or "")[:512] or task.subject
        signal = out.get("signal")
        confidence = out.get("confidence")
        evidence = out.get("evidence") or []
        risks = out.get("risks") or []
        artifact_id = self.store.write_artifact(
            blackboard.run_id,
            payload={
                "task_id": task.id,
                "owner": task.owner,
                "subagent": result.subagent,
                "summary": summary,
                "signal": signal,
                "confidence": confidence,
                "raw_output": out,
                "metrics": result.metrics,
            },
            kind="task_result",
        )
        task.result_artifact = artifact_id
        task.result_summary = summary
        task.status = "completed"
        task.completed_at = now_iso()
        self.store.update_task(task)

        # Append blackboard entries derived from the subagent's output.
        wanted = set(task.payload.get("output_kinds") or [])
        if signal:
            blackboard.append(
                kind="signal", author=task.owner,
                summary=summary,
                payload={"signal": str(signal).lower(), "raw": out},
                confidence=_safe_float(confidence),
                source_refs=[artifact_id],
                task_id=task.id,
            )
        if evidence and ("evidence" in wanted or not wanted):
            for ev in (evidence if isinstance(evidence, list) else [evidence]):
                if isinstance(ev, dict):
                    blackboard.append(
                        kind="evidence", author=task.owner,
                        summary=str(ev.get("summary") or ev.get("text") or "")[:512],
                        payload=ev, task_id=task.id,
                        source_refs=[artifact_id],
                    )
                else:
                    blackboard.append(
                        kind="evidence", author=task.owner,
                        summary=str(ev)[:512], task_id=task.id,
                        source_refs=[artifact_id],
                    )
        if risks and ("risk" in wanted or not wanted):
            for risk in (risks if isinstance(risks, list) else [risks]):
                if isinstance(risk, dict):
                    blackboard.append(
                        kind="risk", author=task.owner,
                        summary=str(risk.get("summary") or risk.get("text") or "")[:512],
                        payload=risk, task_id=task.id,
                        source_refs=[artifact_id],
                    )
                else:
                    blackboard.append(
                        kind="risk", author=task.owner,
                        summary=str(risk)[:512], task_id=task.id,
                        source_refs=[artifact_id],
                    )
        if "decision_input" in wanted:
            blackboard.append(
                kind="decision_input", author=task.owner,
                summary=summary,
                payload={"output": out, "task_id": task.id},
                source_refs=[artifact_id], task_id=task.id,
            )

        # Notify dependent tasks via the mailbox (best-effort, audit only).
        downstream = [t for t in self.store.list_tasks(blackboard.run_id)
                      if task.id in (t.depends_on or [])]
        for dep in downstream:
            mailbox.send(
                from_agent=task.owner, to=dep.owner,
                type="task_completed",
                content=f"{task.id} completed: {summary}",
                artifact_refs=[artifact_id],
            )

    # ------------------------------------------------------------------ journal
    def _mirror_to_agent_journal(
        self, run: TeamRun, final_context: dict[str, Any],
    ) -> None:
        try:
            jsonl.append(self.config.paths.journal("agent"), {
                "kind": "team.run",
                "run_id": run.id,
                "template_id": run.template_id,
                "status": run.status,
                "phase": run.phase,
                "metrics": run.metrics,
                "consensus_signal": final_context.get("consensus_signal"),
                "actionable": final_context.get("actionable"),
                "report_ref": run.final_report_ref,
            })
        except Exception:
            pass


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["TeamOrchestrator", "TeamRunRequest"]

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
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass
from typing import Any, Optional

from ..core import jsonl
from ..core.config import Config
from ..core.time import now_iso
from ..skills.kernel import SkillKernel
from ..subagents.dispatcher import SubAgentDispatcher, SubAgentResult
from .aggregator import TeamAggregator
from .blackboard import Blackboard
from .gates import GateOutcome, evaluate_gates
from .mailbox import Mailbox
from .models import (
    TeamGateSpec,
    TeamMember,
    TeamRun,
    TeamRunResult,
    TeamTask,
    TeamTemplate,
)
from .store import TeamStore
from .templates import get_template


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

    def __post_init__(self) -> None:
        if self.dispatcher is None:
            self.dispatcher = SubAgentDispatcher(self.config, self.skills)
        self.store = TeamStore(self.config.paths)
        self.aggregator = TeamAggregator(self.store)

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
    ) -> TeamRunResult:
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

        try:
            self._execute_tasks(
                run=run, template=tpl, tasks=tasks,
                strategy_id=strategy_id, session_id=session_id,
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

            run.phase = "synthesis"
            self.store.update_run(run)
            self.store.append_event(run.id, kind="phase.enter", phase="synthesis")
            conflict_matrix = self.aggregator.build_conflict_matrix(run.id)
            run.final_context_ref = "synthesis/final_context.json"
            run.final_report_ref = "synthesis/final_report.md"
            run.status = "completed"
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
            self.store.append_event(run.id, kind="run.completed",
                                    metrics=run.metrics)
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
    ) -> None:
        bb = Blackboard(self.store, run.id)
        mailbox = Mailbox(self.store, run.id)
        max_parallel = max(1, int(template.max_parallel or 1))
        index = {t.id: t for t in tasks}
        remaining = {t.id for t in tasks}
        while remaining:
            ready = [
                tid for tid in remaining
                if index[tid].status == "pending"
                and all(index[d].status == "completed" or
                        (not index[d].required and index[d].status in ("failed", "blocked"))
                        for d in index[tid].depends_on)
            ]
            if not ready:
                # Deadlock or all blocked by failed required deps; mark blocked.
                for tid in list(remaining):
                    t = index[tid]
                    blocked_by = [d for d in t.depends_on
                                  if index[d].status not in ("completed",)]
                    if any(index[b].required and index[b].status == "failed"
                           for b in blocked_by):
                        t.status = "blocked"
                        t.error = f"blocked by failed deps: {blocked_by}"
                        t.completed_at = now_iso()
                        self.store.update_task(t)
                        remaining.discard(tid)
                if remaining:
                    # Nothing made progress and nothing is blocked — break to avoid infinite loop.
                    break
                continue

            with ThreadPoolExecutor(max_workers=min(max_parallel, len(ready))) as pool:
                futures: dict[Future, str] = {}
                for tid in ready:
                    t = index[tid]
                    t.status = "in_progress"
                    t.started_at = now_iso()
                    self.store.update_task(t)
                    payload = self._task_payload(
                        run=run, template=template, task=t,
                        blackboard=bb, mailbox=mailbox,
                    )
                    futures[pool.submit(
                        self._run_task,
                        task=t, payload=payload,
                        trigger_event_id=run.trigger_event_id,
                        strategy_id=strategy_id, session_id=session_id,
                    )] = tid
                for fut in futures:
                    tid = futures[fut]
                    res: SubAgentResult = fut.result()
                    self._integrate_result(
                        task=index[tid], result=res, blackboard=bb,
                        mailbox=mailbox,
                    )
                    remaining.discard(tid)

    def _task_payload(
        self,
        *,
        run: TeamRun,
        template: TeamTemplate,
        task: TeamTask,
        blackboard: Blackboard,
        mailbox: Mailbox,
    ) -> dict[str, Any]:
        preview = blackboard.preview_for_agent(task.owner, max_entries=10)
        inbox = [m.asdict() for m in mailbox.peek(task.owner, limit=10)]
        return {
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

    def _run_task(
        self,
        *,
        task: TeamTask,
        payload: dict[str, Any],
        trigger_event_id: Optional[str],
        strategy_id: Optional[str],
        session_id: Optional[str],
    ) -> SubAgentResult:
        assert self.dispatcher is not None
        return self.dispatcher._run_one(
            task.subagent_name,
            payload=payload,
            trigger_event_id=trigger_event_id,
            strategy_id=strategy_id,
            session_id=session_id,
        )

    def _integrate_result(
        self,
        *,
        task: TeamTask,
        result: SubAgentResult,
        blackboard: Blackboard,
        mailbox: Mailbox,
    ) -> None:
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
        if signal and ("signal" in wanted or not wanted):
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


__all__ = ["TeamOrchestrator"]

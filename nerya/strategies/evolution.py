"""Self-evolution loop for one strategy package.

The trading tick (:class:`StrategyRunner`) and the tuning loop
intentionally live in two different runtimes:

* :class:`StrategyRunner` ships market intents on every cron beat.
* :class:`StrategyEvolutionRunner` (this module) runs on a much
  slower cron, snapshots performance, asks a per-strategy
  ``strategy_tuner`` subagent for a recommendation, and writes a
  :class:`Proposal` with the recommended diff. The proposal **never**
  applies itself; promotion still goes through the operator's
  approval flow.

Boundaries
----------
* The runner only needs read access to the package and the strategy's
  ledgers; it never opens a connector / signer.
* The runner refuses to write proposals that touch
  ``forbidden_targets`` (e.g. ``accounts/*``, ``limits.yml``,
  ``secrets/*``, ``live_trading_enabled``) — those are owned by the
  operator and the global ``patch_proposal`` protected-scopes list.
* Each tuning run produces:
  * one ``strategy_tuning_proposal`` (when a recommendation lands), and
  * one ``workspace/strategies/<id>/reviews/tuning_<run_id>.md``
    review document so the dashboard's Self-Evolution panel can
    show the full audit trail.

Inputs
------
:class:`StrategyEvolutionRunner` reads the strategy package, builds a
:class:`StrategyPerformanceSnapshot`, runs the per-strategy
``strategy_tuner`` subagent through :class:`SubAgentDispatcher`,
and validates the resulting proposal payload.

The tuning subagent's expected output schema::

    {
      "summary": "...",
      "evidence": [{"source": "...", "finding": "..."}],
      "proposed_changes": [
        {"file": "main.py", "kind": "code_patch", "rationale": "..."},
        ...
      ],
      "expected_effect": {"return": "...", "drawdown": "..."},
      "validation_plan": ["unit", "fixture_replay", "backtest", "shadow_run"],
      "risk_flags": []
    }

Anything outside the allowed targets is dropped with a warning before
we even create the proposal.
"""

from __future__ import annotations

import fnmatch
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..core import jsonl
from ..core.atomic_write import atomic_write_text
from ..core.config import Config
from ..core.ids import new_id
from ..core.paths import WorkspacePaths
from ..core.redaction import redact_display_dict
from ..core.time import now_iso
from ..evolution.patch_proposal import Proposal, create_proposal
from ..evolution.event_store import record_event
from ..evolution.events import EvolutionSignal
from ..evolution.event_store import append_signal
from ..evolution.validation_plan import build_validation_plan, write_validation_plan
from ..skills.kernel import SkillKernel
from .package import StrategyPackage, StrategyTuningConfig, load_package
from .performance import StrategyPerformanceSnapshot, build_snapshot


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result envelopes
# ---------------------------------------------------------------------------


@dataclass
class TuningRunResult:
    """Return value of :meth:`StrategyEvolutionRunner.run_once`."""

    run_id: str
    strategy_id: str
    started_at: str
    finished_at: str
    duration_ms: int
    status: str  # "ok" | "skipped" | "hold" | "error"
    reason: str = ""
    snapshot: Optional[dict[str, Any]] = None
    subagent_output: dict[str, Any] = field(default_factory=dict)
    proposal_id: Optional[str] = None
    review_path: Optional[str] = None
    audit_path: Optional[str] = None
    source_event_id: Optional[str] = None
    validation_plan_id: Optional[str] = None
    dropped_changes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: Optional[dict[str, Any]] = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class StrategyEvolutionRunner:
    """Run one tuning cycle for one strategy package.

    The runner is intentionally per-call construction: each cron beat
    builds a fresh runner, runs once, and exits. The dispatcher /
    skill kernel come from the workspace's shared :class:`Config`,
    so we pick up any newly-installed prompts / providers.
    """

    config: Config
    skills: SkillKernel

    @property
    def paths(self) -> WorkspacePaths:
        return self.config.paths

    def run_once(
        self,
        strategy_id: str,
        *,
        operator: Optional[str] = None,
        note: str = "",
        dry_run: bool = False,
        trigger_event_id: Optional[str] = None,
    ) -> TuningRunResult:
        """Run one self-evolution cycle.

        ``dry_run=True`` skips proposal creation; the result still
        includes the snapshot + subagent output so the operator can
        inspect what *would* have been proposed.
        """

        run_id = new_id("tune")
        started = now_iso()
        t0 = time.monotonic()
        try:
            pkg = load_package(self.paths, strategy_id)
        except Exception as exc:
            return self._error(
                run_id, strategy_id, started, t0,
                kind="load", message=f"{type(exc).__name__}: {exc}",
            )

        cfg = pkg.manifest.tuning
        if not cfg.enabled:
            return TuningRunResult(
                run_id=run_id, strategy_id=strategy_id,
                started_at=started, finished_at=now_iso(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                status="skipped",
                reason="tuning is disabled in strategy.yml::tuning.enabled",
            )

        snapshot = build_snapshot(
            self.paths,
            strategy_id,
            lookback_runs=int(cfg.lookback.runs or 200),
            package=pkg,
            config_like=self.config,
        )

        if (
            cfg.lookback.min_closed_trades
            and snapshot.trade_metrics.get("closed", 0)
            < cfg.lookback.min_closed_trades
        ):
            res = TuningRunResult(
                run_id=run_id,
                strategy_id=strategy_id,
                started_at=started,
                finished_at=now_iso(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                status="hold",
                reason=(
                    f"only {snapshot.trade_metrics.get('closed', 0)} closed "
                    f"trades; need ≥{cfg.lookback.min_closed_trades}"
                ),
                snapshot=snapshot.asdict(),
            )
            self._journal(res, pkg=pkg, dry_run=dry_run, operator=operator)
            return res

        try:
            envelope = self._dispatch_tuner(
                pkg=pkg,
                snapshot=snapshot,
                trigger_event_id=trigger_event_id,
                run_id=run_id,
            )
        except Exception as exc:
            return self._error(
                run_id, strategy_id, started, t0,
                kind="subagent", message=f"{type(exc).__name__}: {exc}",
                snapshot=snapshot.asdict(),
            )
        if not envelope.get("ok"):
            audit_path = self._write_tuning_audit(
                pkg=pkg,
                run_id=run_id,
                envelope=envelope,
                operator=operator,
                note=note,
                dry_run=dry_run,
            )
            res = TuningRunResult(
                run_id=run_id,
                strategy_id=strategy_id,
                started_at=started,
                finished_at=now_iso(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                status="error",
                reason="tuning subagent failed",
                snapshot=snapshot.asdict(),
                subagent_output=dict(envelope or {}),
                audit_path=str(audit_path) if audit_path else None,
                error={
                    "kind": "subagent",
                    "error_kind": envelope.get("error_kind"),
                    "error": envelope.get("error"),
                },
            )
            self._journal(res, pkg=pkg, dry_run=dry_run, operator=operator)
            return res

        output = envelope.get("output") or {}
        if not isinstance(output, dict):
            output = {}
        audit_path = self._write_tuning_audit(
            pkg=pkg,
            run_id=run_id,
            envelope=envelope,
            operator=operator,
            note=note,
            dry_run=dry_run,
        )

        accepted, dropped, warnings = _filter_changes(output, cfg)
        validation_plan = build_validation_plan(
            _validation_plan_input(output),
            source="strategy_evolution",
            strategy_id=pkg.strategy_id,
            require=bool(accepted),
        )
        validation_plan_id = write_validation_plan(self.paths, validation_plan)
        if validation_plan.blocked_reasons:
            warnings.extend(
                f"validation: {reason}"
                for reason in validation_plan.blocked_reasons
            )
            if accepted:
                for change in accepted:
                    dropped.append({
                        "entry": change,
                        "reason": "validation_plan_blocked",
                        "blocked_reasons": list(validation_plan.blocked_reasons),
                    })
                accepted = []

        signal = EvolutionSignal.create(
            source="strategy",
            kind="strategy_tuning_run",
            severity="info" if accepted else "warn",
            strategy_id=pkg.strategy_id,
            evidence_refs=[f"strategy_tuning:{run_id}"],
            summary=(
                f"Strategy tuning run {run_id}: "
                f"accepted={len(accepted)}, dropped={len(dropped)}"
            ),
            dedupe_key=f"strategy_tuning_run:{run_id}",
            confidence=1.0,
            metadata={"validation_plan_id": validation_plan_id},
        )
        append_signal(self.paths, signal, dedupe=False)
        event = record_event(
            self.paths,
            signals=[signal.id],
            validation_status=(
                "not_run" if not validation_plan.blocked_reasons else "failed"
            ),
            outcome="candidate",
            strategy_id=pkg.strategy_id,
            summary=f"Strategy tuning run {run_id} collected a recommendation.",
            evidence_refs=[f"strategy_tuning:{run_id}"],
            metadata={
                "validation_plan_id": validation_plan_id,
                "accepted_count": len(accepted),
                "dropped_count": len(dropped),
            },
        )

        review_text = _render_review(
            pkg=pkg,
            run_id=run_id,
            snapshot=snapshot,
            output=output,
            accepted=accepted,
            dropped=dropped,
            warnings=warnings,
            operator=operator,
            note=note,
        )
        review_path = pkg.root / "reviews" / f"tuning_{run_id}.md"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(review_path, review_text)

        proposal: Optional[Proposal] = None
        if accepted and not dry_run:
            proposal = self._create_tuning_proposal(
                pkg=pkg,
                run_id=run_id,
                snapshot=snapshot,
                output=output,
                accepted=accepted,
                review_text=review_text,
                audit_text=(
                    audit_path.read_text(encoding="utf-8")
                    if audit_path and audit_path.exists() else "{}"
                ),
                source_event_id=str(event.get("id") or ""),
                validation_plan_id=validation_plan_id,
            )
        elif accepted and dry_run:
            warnings.append("dry_run: proposal not created")

        if not accepted and not dropped:
            status = "hold"
            reason = "tuner returned no proposed_changes"
        elif not accepted:
            status = "hold"
            reason = "all proposed_changes dropped by guardrails"
        else:
            status = "ok"
            reason = "tuning proposal created" if proposal else "tuning recommendation prepared"

        result = TuningRunResult(
            run_id=run_id,
            strategy_id=strategy_id,
            started_at=started,
            finished_at=now_iso(),
            duration_ms=int((time.monotonic() - t0) * 1000),
            status=status,
            reason=reason,
            snapshot=snapshot.asdict(),
            subagent_output=output,
            proposal_id=(proposal.id if proposal else None),
            review_path=str(review_path),
            audit_path=str(audit_path) if audit_path else None,
            source_event_id=str(event.get("id") or ""),
            validation_plan_id=validation_plan_id,
            dropped_changes=list(dropped),
            warnings=list(warnings),
        )
        self._journal(result, pkg=pkg, dry_run=dry_run, operator=operator)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _dispatch_tuner(
        self,
        *,
        pkg: StrategyPackage,
        snapshot: StrategyPerformanceSnapshot,
        trigger_event_id: Optional[str],
        run_id: str,
    ) -> dict[str, Any]:
        cfg = pkg.manifest.tuning
        name = cfg.subagent.name or "strategy_tuner"
        from ..subagents.dispatcher import SubAgentDispatcher
        dispatcher = SubAgentDispatcher(config=self.config, skills=self.skills)
        payload = {
            "strategy_id": pkg.strategy_id,
            "strategy_class_hint": pkg.manifest.extras.get("strategy_class"),
            "manifest": pkg.manifest.asdict(),
            "performance": snapshot.asdict(),
            "objectives": list(cfg.objectives),
            "guardrails": {
                "max_patch_files": cfg.guardrails.max_patch_files,
                "max_position_size_change_pct": cfg.guardrails.max_position_size_change_pct,
                "require_backtest": cfg.guardrails.require_backtest,
                "require_shadow_run": cfg.guardrails.require_shadow_run,
                "require_operator_approval": cfg.guardrails.require_operator_approval,
            },
            "allowed_targets": list(cfg.allowed_targets),
            "forbidden_targets": list(cfg.forbidden_targets),
            "tuning_prompt": cfg.tuning_prompt,
            "run_id": run_id,
        }
        return dispatcher.dispatch(
            f"subagent:{name}",
            payload=payload,
            strategy_id=pkg.strategy_id,
            session_id=run_id,
            trigger_event_id=trigger_event_id,
        )

    def _write_tuning_audit(
        self,
        *,
        pkg: StrategyPackage,
        run_id: str,
        envelope: dict[str, Any],
        operator: Optional[str],
        note: str,
        dry_run: bool,
    ) -> Optional[Any]:
        try:
            audit = dict(envelope.get("audit") or {})
            data = {
                "run_id": run_id,
                "strategy_id": pkg.strategy_id,
                "created_at": now_iso(),
                "operator": operator,
                "note": note,
                "dry_run": dry_run,
                "package_hash": pkg.content_hash,
                "subagent": envelope.get("subagent") or audit.get("subagent"),
                "tier": envelope.get("tier") or audit.get("tier"),
                "ok": bool(envelope.get("ok")),
                "tokens": int(envelope.get("tokens") or 0),
                "usd": float(envelope.get("usd") or 0.0),
                "wall_ms": int(envelope.get("wall_ms") or 0),
                "prompt_path": audit.get("prompt_path"),
                "role_prompt": audit.get("role_prompt") or "",
                "payload": audit.get("payload") or {},
                "prompt_records": audit.get("prompt_records") or [],
                "metrics": redact_display_dict(envelope.get("metrics") or {}),
                "steps": redact_display_dict(envelope.get("steps") or []),
                "subagent_output": redact_display_dict(envelope.get("output") or {}),
                "error": envelope.get("error"),
                "error_kind": envelope.get("error_kind"),
                "redacted": True,
            }
            audit_path = pkg.root / "reviews" / f"tuning_{run_id}_audit.json"
            atomic_write_text(audit_path, _json_dumps(data))
            return audit_path
        except Exception:
            _LOG.exception("tuning audit write failed for run %s", run_id)
            return None

    def _create_tuning_proposal(
        self,
        *,
        pkg: StrategyPackage,
        run_id: str,
        snapshot: StrategyPerformanceSnapshot,
        output: dict[str, Any],
        accepted: list[dict[str, Any]],
        review_text: str,
        audit_text: str,
        source_event_id: str,
        validation_plan_id: str,
    ) -> Optional[Proposal]:
        try:
            extra_files: dict[str, str] = {
                "tuning_run.json": _json_dumps(
                    {
                        "run_id": run_id,
                        "strategy_id": pkg.strategy_id,
                        "package_hash": pkg.content_hash,
                        "snapshot": snapshot.asdict(),
                        "subagent_output": output,
                        "accepted_changes": accepted,
                        "source_event_id": source_event_id,
                        "validation_plan_id": validation_plan_id,
                    }
                ),
                "tuning_review.md": review_text,
                "tuning_audit.json": audit_text,
            }
            return create_proposal(
                self.paths,
                kind="strategy_tuning_proposal",
                summary=str(
                    output.get("summary")
                    or f"Tuning recommendation for {pkg.strategy_id}"
                )[:200],
                rationale=_rationale(pkg, output, accepted, snapshot),
                test_plan=_test_plan(output, pkg.manifest.tuning),
                rollback="Operator can roll back via the strategies workspace.",
                target=f"strategies/{pkg.strategy_id}",
                extra_files=extra_files,
                initial_state="pending_review",
                evidence_refs=[
                    f"strategy_tuning:{run_id}",
                    f"strategy:{pkg.strategy_id}",
                ],
                source_event_id=source_event_id,
                validation_plan_id=validation_plan_id,
                metadata={
                    "strategy_id": pkg.strategy_id,
                    "package_hash": pkg.content_hash,
                },
            )
        except Exception:
            _LOG.exception("create_proposal failed for tuning run %s", run_id)
            return None

    def _journal(
        self,
        result: TuningRunResult,
        *,
        pkg: StrategyPackage,
        dry_run: bool,
        operator: Optional[str],
    ) -> None:
        try:
            jsonl.append(
                self.paths.journal("strategy_evolution"),
                {
                    "kind": "strategy.tuning",
                    "run_id": result.run_id,
                    "strategy_id": result.strategy_id,
                    "package_hash": pkg.content_hash,
                    "status": result.status,
                    "reason": result.reason,
                    "proposal_id": result.proposal_id,
                    "review_path": result.review_path,
                    "audit_path": result.audit_path,
                    "source_event_id": result.source_event_id,
                    "validation_plan_id": result.validation_plan_id,
                    "duration_ms": result.duration_ms,
                    "dry_run": dry_run,
                    "operator": operator,
                    "ts": result.finished_at,
                },
            )
        except Exception:
            _LOG.exception("strategy_evolution journal append failed")

    def _error(
        self,
        run_id: str,
        strategy_id: str,
        started: str,
        t0: float,
        *,
        kind: str,
        message: str,
        snapshot: Optional[dict[str, Any]] = None,
    ) -> TuningRunResult:
        return TuningRunResult(
            run_id=run_id,
            strategy_id=strategy_id,
            started_at=started,
            finished_at=now_iso(),
            duration_ms=int((time.monotonic() - t0) * 1000),
            status="error",
            reason=message,
            snapshot=snapshot,
            error={"kind": kind, "message": message},
        )


# ---------------------------------------------------------------------------
# Filtering / rendering helpers
# ---------------------------------------------------------------------------


def _filter_changes(
    output: dict[str, Any],
    cfg: StrategyTuningConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    raw_changes = (
        output.get("proposed_changes")
        or output.get("proposed_patches")
        or output.get("changes")
        or []
    )
    if not isinstance(raw_changes, list):
        return [], [], ["proposed_changes was not a list"]
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    warnings: list[str] = []
    for entry in raw_changes:
        if not isinstance(entry, dict):
            dropped.append({"entry": entry, "reason": "not a dict"})
            continue
        target = str(entry.get("file") or entry.get("target") or "").strip()
        if not target:
            dropped.append({"entry": entry, "reason": "missing file/target"})
            continue
        if any(fnmatch.fnmatch(target, p) for p in cfg.forbidden_targets):
            dropped.append({"entry": entry, "reason": "forbidden_target"})
            continue
        if cfg.allowed_targets and not any(
            fnmatch.fnmatch(target, p) for p in cfg.allowed_targets
        ):
            dropped.append({"entry": entry, "reason": "not_in_allowed_targets"})
            continue
        accepted.append(dict(entry))
        if len(accepted) >= int(cfg.guardrails.max_patch_files or 5):
            warnings.append(
                f"max_patch_files cap of {cfg.guardrails.max_patch_files} reached"
            )
            break
    return accepted, dropped, warnings


def _validation_plan_input(output: dict[str, Any]) -> Any:
    raw = output.get("validation_plan")
    if raw is not None:
        return raw
    if "backtest_required" not in output and "shadow_run_required" not in output:
        return None
    steps = ["unit"]
    if bool(output.get("backtest_required")):
        steps.append("backtest")
    else:
        steps.append("manual_review")
    if bool(output.get("shadow_run_required")):
        steps.append("shadow_run")
    return steps


def _render_review(
    *,
    pkg: StrategyPackage,
    run_id: str,
    snapshot: StrategyPerformanceSnapshot,
    output: dict[str, Any],
    accepted: list[dict[str, Any]],
    dropped: list[dict[str, Any]],
    warnings: list[str],
    operator: Optional[str],
    note: str,
) -> str:
    lines: list[str] = []
    lines.append(f"# Tuning review — {pkg.strategy_id} ({run_id})")
    lines.append(f"_generated: {now_iso()}_")
    if operator:
        lines.append(f"_triggered by: {operator}_")
    if note:
        lines.append(f"_note: {note}_")
    lines.append("")
    lines.append("## Summary")
    summary = str(output.get("summary") or "—")
    lines.append(summary)
    lines.append("")
    lines.append("## Performance snapshot")
    lines.append("```json")
    lines.append(_json_dumps(snapshot.asdict()))
    lines.append("```")
    lines.append("")
    lines.append("## Accepted changes")
    if not accepted:
        lines.append("_none_")
    else:
        for c in accepted:
            target = c.get("file") or c.get("target") or "?"
            kind = c.get("kind") or "patch"
            rationale = c.get("rationale") or ""
            lines.append(f"- `{target}` ({kind}): {rationale}")
    lines.append("")
    lines.append("## Dropped changes")
    if not dropped:
        lines.append("_none_")
    else:
        for d in dropped:
            entry = d.get("entry") or {}
            target = entry.get("file") or entry.get("target") or "?"
            lines.append(f"- `{target}`: {d.get('reason', 'unspecified')}")
    if warnings:
        lines.append("")
        lines.append("## Warnings")
        for w in warnings:
            lines.append(f"- {w}")
    lines.append("")
    lines.append("## Subagent output")
    lines.append("```json")
    lines.append(_json_dumps(output))
    lines.append("```")
    return "\n".join(lines) + "\n"


def _rationale(
    pkg: StrategyPackage,
    output: dict[str, Any],
    accepted: list[dict[str, Any]],
    snapshot: StrategyPerformanceSnapshot,
) -> str:
    summary = str(output.get("summary") or "")
    return (
        f"# Self-evolution proposal for `{pkg.strategy_id}`\n\n"
        f"{summary}\n\n"
        f"## Snapshot key metrics\n\n"
        f"- runs_considered: {snapshot.runs_considered}\n"
        f"- ok_rate: {snapshot.run_metrics.get('ok_rate')}\n"
        f"- error_rate: {snapshot.run_metrics.get('error_rate')}\n"
        f"- pnl_total_usd: {snapshot.trade_metrics.get('pnl_total_usd')}\n"
        f"- max_drawdown_usd: {snapshot.trade_metrics.get('max_drawdown_usd')}\n"
        f"\n## Accepted file targets\n\n"
        + "\n".join(
            f"- `{c.get('file') or c.get('target')}`" for c in accepted
        )
        + "\n"
    )


def _test_plan(
    output: dict[str, Any],
    cfg: StrategyTuningConfig,
) -> str:
    plan = output.get("validation_plan") or []
    if not isinstance(plan, list) or not plan:
        plan = ["unit", "fixture_replay"]
        if cfg.guardrails.require_backtest:
            plan.append("backtest")
        if cfg.guardrails.require_shadow_run:
            plan.append("shadow_run")
    body = "\n".join(f"- {p}" for p in plan)
    return f"# Tuning validation plan\n\n{body}\n"


def _json_dumps(data: Any) -> str:
    import json as _j

    return _j.dumps(data, indent=2, default=str, sort_keys=True)


def _ensure_runner(config: Config, skills: SkillKernel) -> StrategyEvolutionRunner:
    """Convenience constructor used by SDK / API / CLI / tools."""

    return StrategyEvolutionRunner(config=config, skills=skills)


__all__ = [
    "StrategyEvolutionRunner",
    "TuningRunResult",
]

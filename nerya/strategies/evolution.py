"""Self-evolution loop for one strategy package.

The trading tick (:class:`StrategyRunner`) and the tuning loop
intentionally live in two different runtimes:

* :class:`StrategyRunner` ships market intents on every cron beat.
* :class:`StrategyEvolutionRunner` (this module) runs on a much
  slower cron, snapshots performance, asks a per-strategy
  ``strategy_tuner`` subagent for a recommendation, and writes a
  :class:`Proposal` with materialized ``after/`` files when the tuner
  provides complete replacement content. The proposal **never** applies
  itself; promotion still goes through the operator's
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
        {
          "file": "main.py",
          "kind": "full_file",
          "after_content": "complete replacement content",
          "rationale": "..."
        },
        {"file": "strategy.yml", "kind": "config", "config_after": {...}},
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
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from ..core import jsonl, yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.config import Config
from ..core.ids import new_id
from ..core.paths import WorkspacePaths
from ..core.redaction import redact_display_dict
from ..core.time import now, now_iso
from ..evolution.observation_summary import (
    observation_weight,
    POST_APPLY_HEALTHY_STATUSES,
    POST_APPLY_NEGATIVE_STATUSES,
)
from ..evolution import assets as evolution_assets
from ..evolution.patch_proposal import Proposal, create_proposal, is_protected, list_proposals
from ..evolution.event_store import record_event
from ..evolution.events import EvolutionSignal
from ..evolution.event_store import append_signal
from ..evolution.selector import select_assets_for_signals
from ..evolution.validation_plan import build_validation_plan, write_validation_plan
from ..skills.kernel import SkillKernel
from .package import StrategyPackage, StrategyTuningConfig, load_package
from .performance import StrategyPerformanceSnapshot, build_snapshot
from .validator import validate_proposal_files


_LOG = logging.getLogger(__name__)

_TREND_ADX_THRESHOLD = 25.0
_RANGEBOUND_ADX_THRESHOLD = 18.0
_HIGH_VOL_ATR_PCT_THRESHOLD = 0.03
_HIGH_VOL_RET_THRESHOLD = 0.03
_OPTIMIZER_VERSION = "strategy_tuning_optimizer_v1"
_MAX_TUNING_CANDIDATES = 6
_CANDIDATE_VALIDATION_PREVIEW_TOP_K = 2
_CANDIDATE_BACKTEST_PREVIEW_TOP_K = 1
_MAX_OPTIMIZER_FEEDBACK_RUNS = 40
_MAX_OPTIMIZER_DECISION_FEEDBACK_ROWS = 80
_OPTIMIZER_DECISION_FEEDBACK_HALF_LIFE_DAYS = 45.0
_OPTIMIZER_DECISION_FEATURE_WEIGHT_CAP = 1.2
_OPTIMIZER_FEEDBACK_CALIBRATION_BASE_SCALES = {
    "none": 0.0,
    "low": 0.25,
    "medium": 0.6,
    "high": 1.0,
}
_OPTIMIZER_FEEDBACK_CALIBRATION_WARNING_MULTIPLIERS = {
    "single_run_feedback": 0.85,
    "low_sample_count": 0.8,
    "operator_decision_dominant": 0.75,
    "feature_concentration_high": 0.8,
    "negative_feedback_dominant": 0.85,
    "positive_feedback_unbalanced": 0.8,
    "no_proposal_outcome_samples": 0.7,
}
_TUNING_MATERIALIZATION_CONTRACT = """Strategy tuning materialization contract:
- For any code or prompt mutation you want Nerya to apply, return full replacement file content, not only prose, diff text, or a patch summary.
- Use proposed_changes entries shaped like {"file":"main.py","kind":"full_file","after_content":"<complete file content>","rationale":"..."} for Python/prompt/text targets.
- Use {"file":"strategy.yml","kind":"config","config_after":{...},"rationale":"..."} or {"file":"strategy.yml","kind":"config","yaml_after":"<complete YAML mapping>","rationale":"..."} for YAML targets.
- Return advisory-only changes only when you cannot safely provide complete after content; mark those as {"kind":"advisory",...} and explain the blocker.
- Keep targets inside allowed_targets and never mutate forbidden_targets or live-trading enablement.
"""
_CANDIDATE_BASELINE_COMPARISON_METRICS = (
    "total_return_pct",
    "alpha_vs_benchmark_pct",
    "max_drawdown_pct",
    "sharpe_ratio",
    "profit_factor",
    "win_rate_pct",
    "total_trades",
)
_CANDIDATE_BASELINE_LOWER_IS_BETTER = {
    "max_drawdown_pct",
}
_CANDIDATE_BASELINE_CRITICAL_METRICS = {
    "total_return_pct",
    "alpha_vs_benchmark_pct",
    "max_drawdown_pct",
    "sharpe_ratio",
}
_CANDIDATE_LIST_KEYS = (
    "candidates",
    "candidate_recommendations",
    "candidate_outputs",
    "alternatives",
)
_CANDIDATE_LOCAL_KEYS = {
    *_CANDIDATE_LIST_KEYS,
    "proposed_changes",
    "proposed_patches",
    "changes",
    "summary",
    "validation_plan",
    "expected_effect",
    "risk_flags",
    "risks",
    "backtest_required",
    "shadow_run_required",
}

run_strategy_backtest = None
NoHistoricalDataError = None


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
    optimizer_report: dict[str, Any] = field(default_factory=dict)
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
        raw_output = dict(output)
        selected_assets_for_optimizer = (
            envelope.get("selected_assets")
            if isinstance(envelope.get("selected_assets"), dict)
            else {}
        )
        optimizer_selection = _select_tuning_candidate(
            output,
            cfg,
            pkg,
            self.paths,
            run_id=run_id,
            selected_assets=selected_assets_for_optimizer,
            create_asset_candidates=not dry_run,
        )
        optimizer_report: dict[str, Any] = {}
        if optimizer_selection:
            output = optimizer_selection["selected_output"]
            optimizer_report = optimizer_selection["report"]
            envelope = dict(envelope)
            envelope["raw_output"] = raw_output
            envelope["output"] = output
            envelope["optimizer_report"] = optimizer_report
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
        selected_assets = (
            envelope.get("selected_assets")
            if isinstance(envelope.get("selected_assets"), dict)
            else {}
        )
        selected_genes = [
            str(g.get("id"))
            for g in selected_assets.get("genes", [])
            if isinstance(g, dict) and g.get("id")
        ]
        event = record_event(
            self.paths,
            signals=[signal.id],
            genes_used=selected_genes,
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
                "selected_assets": selected_assets,
                "optimizer": _optimizer_metadata(optimizer_report),
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
                selected_assets=selected_assets,
                optimizer_report=optimizer_report,
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
            optimizer_report=optimizer_report,
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
        selected_assets = _select_tuning_assets(self.paths, pkg, snapshot, run_id)
        selected_genes = selected_assets.get("genes", [])
        similar_capsules = [
            c for c in selected_assets.get("capsules", [])
            if float(c.get("outcome_score") or 0.0) >= 0.0
        ]
        negative_capsules = [
            c for c in selected_assets.get("capsules", [])
            if float(c.get("outcome_score") or 0.0) < 0.0
        ]
        payload = {
            "__team_instructions": _strategy_tuning_team_instructions(pkg),
            "strategy_id": pkg.strategy_id,
            "strategy_class_hint": pkg.manifest.extras.get("strategy_class"),
            "manifest": pkg.manifest.asdict(),
            "performance": snapshot.asdict(),
            "selected_assets": selected_assets,
            "selected_genes": selected_genes,
            "similar_capsules": similar_capsules,
            "negative_capsules": negative_capsules,
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
            "materializable_output_contract": _strategy_tuning_materializable_contract(pkg),
            "tuning_prompt": cfg.tuning_prompt,
            "run_id": run_id,
        }
        envelope = dispatcher.dispatch(
            f"subagent:{name}",
            payload=payload,
            strategy_id=pkg.strategy_id,
            session_id=run_id,
            trigger_event_id=trigger_event_id,
        )
        if isinstance(envelope, dict):
            envelope.setdefault("selected_assets", selected_assets)
        return envelope

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
                "provider": envelope.get("provider") or audit.get("provider"),
                "model": envelope.get("model") or audit.get("model"),
                "model_calls": audit.get("model_calls") or envelope.get("model_calls") or [],
                "ok": bool(envelope.get("ok")),
                "tokens": int(envelope.get("tokens") or 0),
                "usd": float(envelope.get("usd") or 0.0),
                "wall_ms": int(envelope.get("wall_ms") or 0),
                "prompt_path": audit.get("prompt_path"),
                "role_prompt": audit.get("role_prompt") or "",
                "payload": audit.get("payload") or {},
                "selected_assets": redact_display_dict(envelope.get("selected_assets") or {}),
                "raw_subagent_output": redact_display_dict(envelope.get("raw_output") or {}),
                "optimizer_report": redact_display_dict(envelope.get("optimizer_report") or {}),
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
        selected_assets: dict[str, Any] | None = None,
        optimizer_report: dict[str, Any] | None = None,
    ) -> Optional[Proposal]:
        try:
            materialized_files, materialized, unmaterialized = (
                _materialize_strategy_tuning_after_files(pkg, accepted)
            )
            trigger_context = _asset_trigger_context(selected_assets or {})
            extra_files: dict[str, str] = {
                "tuning_run.json": _json_dumps(
                    {
                        "run_id": run_id,
                        "strategy_id": pkg.strategy_id,
                        "package_hash": pkg.content_hash,
                        "snapshot": snapshot.asdict(),
                        "subagent_output": output,
                        "accepted_changes": accepted,
                        "materialized_files": materialized,
                        "unmaterialized_changes": unmaterialized,
                        "selected_assets": selected_assets or {},
                        "optimizer_report": optimizer_report or {},
                        "evolution_trigger_context": trigger_context,
                        "source_event_id": source_event_id,
                        "validation_plan_id": validation_plan_id,
                    }
                ),
                "tuning_review.md": review_text,
                "tuning_audit.json": audit_text,
                "materialization.json": _json_dumps(
                    {
                        "materialized": bool(materialized),
                        "materialized_files": materialized,
                        "unmaterialized_changes": unmaterialized,
                        "advisory_only": not bool(materialized),
                    }
                ),
            }
            extra_files.update(materialized_files)
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
                    "materialized": bool(materialized),
                    "materialized_files": list(materialized),
                    "unmaterialized_changes": list(unmaterialized),
                    "advisory_only": not bool(materialized),
                    "selected_gene_ids": trigger_context.get("selected_gene_ids") or [],
                    "selected_capsule_ids": trigger_context.get("selected_capsule_ids") or [],
                    "evolution_trigger_context": trigger_context,
                    "optimizer": _optimizer_metadata(optimizer_report or {}),
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
                    "optimizer": _optimizer_metadata(result.optimizer_report),
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


def _select_tuning_assets(
    paths: WorkspacePaths,
    pkg: StrategyPackage,
    snapshot: StrategyPerformanceSnapshot,
    run_id: str,
) -> dict[str, list[dict[str, Any]]]:
    signals = _tuning_asset_selection_signals(pkg, snapshot, run_id)
    selected = select_assets_for_signals(
        paths,
        signals,
        strategy_id=pkg.strategy_id,
        limit=8,
    )
    return {**selected, "selection_signals": signals}


def _asset_trigger_context(selected_assets: dict[str, Any]) -> dict[str, Any]:
    signals = [
        row for row in (selected_assets.get("selection_signals") or [])
        if isinstance(row, dict)
    ]
    signal_kinds: list[str] = []
    markets: list[str] = []
    timeframes: list[str] = []
    market_regimes: list[str] = []
    data_quality: list[str] = []
    evidence_refs: list[str] = []
    for signal in signals:
        kind = str(signal.get("kind") or "").strip()
        if kind:
            signal_kinds.append(kind)
        if kind.startswith("market_regime_"):
            market_regimes.append(kind.removeprefix("market_regime_"))
        if kind == "market_news_context":
            market_regimes.append("news_context")
        if kind == "market_data_degraded":
            data_quality.append("degraded")
        metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
        markets.extend(_str_list(metadata.get("markets")))
        timeframe = str(metadata.get("timeframe") or "").strip()
        if timeframe:
            timeframes.append(timeframe)
        evidence_refs.extend(_str_list(signal.get("evidence_refs")))
    return {
        "signal_kinds": _unique_strings(signal_kinds),
        "market_regimes": _unique_strings(market_regimes),
        "markets": _unique_strings(markets),
        "timeframes": _unique_strings(timeframes),
        "data_quality": _unique_strings(data_quality),
        "evidence_refs": _unique_strings(evidence_refs)[-12:],
        "selected_gene_ids": [
            str(row.get("id"))
            for row in (selected_assets.get("genes") or [])
            if isinstance(row, dict) and row.get("id")
        ][:12],
        "selected_capsule_ids": [
            str(row.get("id"))
            for row in (selected_assets.get("capsules") or [])
            if isinstance(row, dict) and row.get("id")
        ][:12],
    }


def _strategy_tuning_team_instructions(pkg: StrategyPackage) -> str:
    cfg = pkg.manifest.tuning
    allowed = ", ".join(cfg.allowed_targets) or "none"
    forbidden = ", ".join(cfg.forbidden_targets) or "none"
    return (
        f"{_TUNING_MATERIALIZATION_CONTRACT}\n"
        f"Allowed targets for this strategy: {allowed}.\n"
        f"Forbidden targets for this strategy: {forbidden}.\n"
        "If you propose multiple alternatives, put them in candidates[] and still use the same materializable proposed_changes shapes."
    )


def _strategy_tuning_materializable_contract(pkg: StrategyPackage) -> dict[str, Any]:
    cfg = pkg.manifest.tuning
    return {
        "version": "strategy_tuning_materializable_output_v1",
        "required_for_applyable_changes": True,
        "allowed_targets": list(cfg.allowed_targets),
        "forbidden_targets": list(cfg.forbidden_targets),
        "accepted_change_shapes": [
            {
                "file": "main.py",
                "kind": "full_file",
                "after_content": "<complete replacement file content>",
                "rationale": "<why this change follows from performance/market/risk evidence>",
            },
            {
                "file": "subagents/strategy_tuner.agent.md",
                "kind": "full_file",
                "after_content": "<complete replacement prompt content>",
                "rationale": "<why this prompt change improves future tuning>",
            },
            {
                "file": "strategy.yml",
                "kind": "config",
                "config_after": {"version": "<complete strategy.yml mapping>"},
                "rationale": "<why this config change is safe>",
            },
            {
                "file": "strategy.yml",
                "kind": "config",
                "yaml_after": "<complete YAML mapping>",
                "rationale": "<why this config change is safe>",
            },
        ],
        "advisory_only_shape": {
            "file": "main.py",
            "kind": "advisory",
            "rationale": "<why complete after_content cannot be provided safely>",
        },
        "notes": [
            "Free-form code_patch/diff-only changes are retained as review evidence but cannot be applied.",
            "Complete after_content/config_after/yaml_after is required for Nerya to write proposal after/ files.",
        ],
    }


def _tuning_asset_selection_signals(
    pkg: StrategyPackage,
    snapshot: StrategyPerformanceSnapshot,
    run_id: str,
) -> list[dict[str, Any]]:
    snap = snapshot.asdict()
    trade = snap.get("trade_metrics") or {}
    refs = [f"strategy_tuning:{run_id}"]
    base = {
        "source": "strategy",
        "strategy_id": pkg.strategy_id,
        "evidence_refs": refs,
        "confidence": 1.0,
    }
    signals: list[dict[str, Any]] = [
        {
            **base,
            "id": f"{run_id}:strategy_tuning_run",
            "kind": "strategy_tuning_run",
            "severity": "info",
            "summary": f"Strategy {pkg.strategy_id} tuning run is selecting reusable evolution assets.",
            "metadata": {
                "runs_considered": snap.get("runs_considered"),
                "package_hash": pkg.content_hash,
            },
        }
    ]
    drawdown = _float_metric(trade.get("max_drawdown_usd"))
    if drawdown < 0:
        signals.append(
            {
                **base,
                "id": f"{run_id}:strategy_drawdown",
                "kind": "strategy_drawdown",
                "severity": "warn",
                "summary": f"Strategy {pkg.strategy_id} has drawdown {drawdown:.2f} USD.",
                "metadata": {"max_drawdown_usd": drawdown},
            }
        )
    avg_slippage = _float_metric(trade.get("avg_slippage"))
    slippage_samples = int(_float_metric(trade.get("slippage_samples")))
    if slippage_samples and abs(avg_slippage) >= 5.0:
        signals.append(
            {
                **base,
                "id": f"{run_id}:high_slippage",
                "kind": "high_slippage",
                "severity": "warn",
                "summary": f"Strategy {pkg.strategy_id} has average slippage {avg_slippage:.2f}.",
                "metadata": {
                    "avg_slippage": avg_slippage,
                    "slippage_samples": slippage_samples,
                },
            }
        )
    signals.extend(_market_regime_selection_signals(pkg, snap, base, run_id))
    evolution = snap.get("evolution_context") if isinstance(snap.get("evolution_context"), dict) else {}
    negative_count = _float_metric(
        evolution.get("weighted_negative_count", evolution.get("negative_count"))
    )
    recent_count = _float_metric(
        evolution.get("weighted_observing_count", evolution.get("recent_count"))
    )
    if negative_count >= 0.5:
        signals.append(
            {
                **base,
                "id": f"{run_id}:post_apply_regression",
                "kind": "post_apply_regression",
                "severity": "warn",
                "summary": (
                    f"Strategy {pkg.strategy_id} has weighted negative "
                    f"post-apply evidence {negative_count:.2f}."
                ),
                "metadata": {
                    "negative_count": negative_count,
                    "raw_negative_count": evolution.get("negative_count"),
                    "weighted_negative_count": evolution.get("weighted_negative_count"),
                    "by_status": evolution.get("by_status") or {},
                    "weighted_by_status": evolution.get("weighted_by_status") or {},
                    "by_source": evolution.get("by_source") or {},
                    "weighted_by_source": evolution.get("weighted_by_source") or {},
                },
            }
        )
    elif recent_count >= 0.5:
        signals.append(
            {
                **base,
                "id": f"{run_id}:post_apply_observation",
                "kind": "post_apply_observation",
                "severity": "info",
                "summary": (
                    f"Strategy {pkg.strategy_id} has weighted post-apply "
                    f"runtime evidence {recent_count:.2f}."
                ),
                "metadata": {
                    "recent_count": recent_count,
                    "raw_recent_count": evolution.get("recent_count"),
                    "weighted_observing_count": evolution.get("weighted_observing_count"),
                    "by_status": evolution.get("by_status") or {},
                    "weighted_by_status": evolution.get("weighted_by_status") or {},
                    "by_source": evolution.get("by_source") or {},
                    "weighted_by_source": evolution.get("weighted_by_source") or {},
                },
            }
        )
    return signals


def _market_regime_selection_signals(
    pkg: StrategyPackage,
    snap: dict[str, Any],
    base: dict[str, Any],
    run_id: str,
) -> list[dict[str, Any]]:
    market_context = snap.get("market_context") if isinstance(snap.get("market_context"), dict) else {}
    news_context = snap.get("news_context") if isinstance(snap.get("news_context"), dict) else {}
    items = [
        row for row in (market_context.get("items") or [])
        if isinstance(row, dict)
    ][:12]
    timeframe = str(market_context.get("timeframe") or "")
    markets = _str_list(market_context.get("markets")) or [
        str(row.get("market") or "")
        for row in items
        if str(row.get("market") or "").strip()
    ]
    trending: list[dict[str, Any]] = []
    rangebound: list[dict[str, Any]] = []
    volatile: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []

    for item in items:
        features = item.get("features") if isinstance(item.get("features"), dict) else {}
        digest = _market_feature_digest(item)
        if int(_float_metric(item.get("candles_count"))) <= 0:
            degraded.append({**digest, "reason": "no_recent_candles"})
        envelope = item.get("_envelope") if isinstance(item.get("_envelope"), dict) else {}
        if _is_degraded_envelope(envelope):
            degraded.append({**digest, "reason": "degraded_market_data", "envelope": _envelope_digest(envelope)})
        if not features:
            continue

        adx = _maybe_float(features.get("adx_14"))
        close = _maybe_float(features.get("close"))
        atr = _maybe_float(features.get("atr_14"))
        ret_1 = _maybe_float(features.get("ret_1"))
        rsi = _maybe_float(features.get("rsi_14"))
        breakout = features.get("breakout") if isinstance(features.get("breakout"), dict) else {}
        breakout_active = bool(breakout.get("breakout"))
        breakout_strength = _maybe_float(breakout.get("strength"))
        atr_pct = (abs(atr) / close) if atr is not None and close and close > 0 else None

        if (adx is not None and adx >= _TREND_ADX_THRESHOLD) or breakout_active:
            trending.append({
                **digest,
                "reason": "adx_or_breakout",
                "adx_14": adx,
                "breakout": breakout,
            })
        if (
            adx is not None
            and adx <= _RANGEBOUND_ADX_THRESHOLD
            and not breakout_active
            and (ret_1 is None or abs(ret_1) <= 0.01)
            and (rsi is None or 40.0 <= rsi <= 60.0)
        ):
            rangebound.append({
                **digest,
                "reason": "low_adx_flat_return",
                "adx_14": adx,
                "ret_1": ret_1,
                "rsi_14": rsi,
            })
        if (
            (atr_pct is not None and atr_pct >= _HIGH_VOL_ATR_PCT_THRESHOLD)
            or (ret_1 is not None and abs(ret_1) >= _HIGH_VOL_RET_THRESHOLD)
            or (breakout_strength is not None and abs(breakout_strength) >= _HIGH_VOL_RET_THRESHOLD)
        ):
            volatile.append({
                **digest,
                "reason": "atr_or_return_spike",
                "atr_pct": round(atr_pct, 6) if atr_pct is not None else None,
                "ret_1": ret_1,
                "breakout_strength": breakout_strength,
            })

    news_items = [
        row for row in (news_context.get("items") or [])
        if isinstance(row, dict)
    ]
    if news_context.get("error"):
        degraded.append({
            "reason": "news_context_error",
            "error": str(news_context.get("error")),
            "symbols": _str_list(news_context.get("symbols")),
        })
    for error in _str_list(news_context.get("errors")):
        degraded.append({"reason": "news_context_error", "error": error})
    for row in news_items[:8]:
        envelope = row.get("_envelope") if isinstance(row.get("_envelope"), dict) else {}
        if _is_degraded_envelope(envelope):
            degraded.append({
                "reason": "degraded_news_data",
                "source": row.get("source"),
                "title": row.get("title"),
                "envelope": _envelope_digest(envelope),
            })

    signals: list[dict[str, Any]] = []
    if trending:
        signals.append(_market_signal(
            base,
            run_id=run_id,
            kind="market_regime_trending",
            severity="info",
            summary=f"Strategy {pkg.strategy_id} is tuning against a trending market regime.",
            markets=markets,
            timeframe=timeframe,
            evidence=trending[:8],
        ))
    if rangebound:
        signals.append(_market_signal(
            base,
            run_id=run_id,
            kind="market_regime_rangebound",
            severity="info",
            summary=f"Strategy {pkg.strategy_id} is tuning against a range-bound market regime.",
            markets=markets,
            timeframe=timeframe,
            evidence=rangebound[:8],
        ))
    if volatile:
        signals.append(_market_signal(
            base,
            run_id=run_id,
            kind="market_regime_high_volatility",
            severity="warn",
            summary=f"Strategy {pkg.strategy_id} is tuning during elevated market volatility.",
            markets=markets,
            timeframe=timeframe,
            evidence=volatile[:8],
        ))
    if news_items:
        signals.append({
            **base,
            "id": f"{run_id}:market_news_context",
            "kind": "market_news_context",
            "severity": "info",
            "summary": f"Strategy {pkg.strategy_id} has matched news context for tuning.",
            "metadata": {
                "markets": markets[:12],
                "symbols": _str_list(news_context.get("symbols"))[:12],
                "news_count": int(_float_metric(news_context.get("count"))) or len(news_items),
                "items": [_news_item_digest(row) for row in news_items[:6]],
            },
        })
    if degraded:
        signals.append({
            **base,
            "id": f"{run_id}:market_data_degraded",
            "kind": "market_data_degraded",
            "severity": "warn",
            "summary": f"Strategy {pkg.strategy_id} has degraded market/news data during tuning.",
            "metadata": {
                "markets": markets[:12],
                "timeframe": timeframe,
                "issues": degraded[:12],
            },
        })
    return signals


def _market_signal(
    base: dict[str, Any],
    *,
    run_id: str,
    kind: str,
    severity: str,
    summary: str,
    markets: list[str],
    timeframe: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **base,
        "id": f"{run_id}:{kind}",
        "kind": kind,
        "severity": severity,
        "summary": summary,
        "metadata": {
            "markets": markets[:12],
            "timeframe": timeframe,
            "thresholds": {
                "trend_adx": _TREND_ADX_THRESHOLD,
                "rangebound_adx": _RANGEBOUND_ADX_THRESHOLD,
                "high_vol_atr_pct": _HIGH_VOL_ATR_PCT_THRESHOLD,
                "high_vol_ret": _HIGH_VOL_RET_THRESHOLD,
            },
            "feature_evidence": evidence,
        },
    }


def _market_feature_digest(item: dict[str, Any]) -> dict[str, Any]:
    features = item.get("features") if isinstance(item.get("features"), dict) else {}
    digest = {
        "market": item.get("market"),
        "timeframe": item.get("timeframe"),
        "candles_count": item.get("candles_count"),
        "close": _maybe_float(features.get("close")),
        "sma_20": _maybe_float(features.get("sma_20")),
        "ema_20": _maybe_float(features.get("ema_20")),
        "ret_1": _maybe_float(features.get("ret_1")),
        "atr_14": _maybe_float(features.get("atr_14")),
        "adx_14": _maybe_float(features.get("adx_14")),
        "rsi_14": _maybe_float(features.get("rsi_14")),
        "breakout": features.get("breakout") if isinstance(features.get("breakout"), dict) else None,
    }
    envelope = item.get("_envelope") if isinstance(item.get("_envelope"), dict) else {}
    if envelope:
        digest["envelope"] = _envelope_digest(envelope)
    return {
        key: value
        for key, value in digest.items()
        if value not in (None, "", [], {})
    }


def _news_item_digest(row: dict[str, Any]) -> dict[str, Any]:
    envelope = row.get("_envelope") if isinstance(row.get("_envelope"), dict) else {}
    return {
        key: value
        for key, value in {
            "source": row.get("source"),
            "title": row.get("title"),
            "published_at": row.get("published_at"),
            "tickers": _str_list(row.get("tickers"))[:12],
            "matched_tickers": _str_list(row.get("matched_tickers"))[:12],
            "envelope": _envelope_digest(envelope) if envelope else None,
        }.items()
        if value not in (None, "", [], {})
    }


def _envelope_digest(envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        key: envelope.get(key)
        for key in ("source", "mode", "degraded", "fallback_used", "error", "provider", "venue")
        if envelope.get(key) not in (None, "", [], {})
    }


def _is_degraded_envelope(envelope: dict[str, Any]) -> bool:
    if not envelope:
        return False
    mode = str(envelope.get("mode") or "").lower()
    return (
        bool(envelope.get("degraded"))
        or bool(envelope.get("error"))
        or mode in {"degraded", "unavailable"}
    )


def _float_metric(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _maybe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _ratio(numerator: int | float, denominator: int | float) -> float:
    denom = float(denominator or 0)
    if denom <= 0:
        return 0.0
    return round(float(numerator or 0) / denom, 4)


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _empty_optimizer_feedback() -> dict[str, Any]:
    return {
        "version": "optimizer_outcome_feedback_v1",
        "run_count": 0,
        "sample_count": 0,
        "positive_samples": 0,
        "negative_samples": 0,
        "neutral_samples": 0,
        "proposal_samples": 0,
        "candidate_decision_samples": 0,
        "candidate_decision_positive_samples": 0,
        "candidate_decision_negative_samples": 0,
        "candidate_decision_neutral_samples": 0,
        "decision_feedback_policy": {
            "version": "optimizer_decision_feedback_policy_v1",
            "half_life_days": _OPTIMIZER_DECISION_FEEDBACK_HALF_LIFE_DAYS,
            "feature_source_cap": _OPTIMIZER_DECISION_FEATURE_WEIGHT_CAP,
            "source": "asset_candidate_decision",
        },
        "features": {},
        "examples": [],
    }


def _optimizer_outcome_feedback(
    paths: WorkspacePaths,
    pkg: StrategyPackage,
) -> dict[str, Any]:
    feedback = _empty_optimizer_feedback()
    proposals = {proposal.id: proposal for proposal in list_proposals(paths)}
    observations = _post_apply_observations_by_proposal(paths)
    rows = [
        row for row in jsonl.read_all(paths.journal("strategy_evolution"))
        if row.get("kind") == "strategy.tuning"
        and str(row.get("strategy_id") or "") == pkg.strategy_id
        and row.get("proposal_id")
    ][-_MAX_OPTIMIZER_FEEDBACK_RUNS:]
    features: dict[str, dict[str, Any]] = {}
    examples: list[dict[str, Any]] = []
    positive = negative = neutral = 0
    run_count = 0
    proposal_samples = 0
    decision_samples = decision_positive = decision_negative = decision_neutral = 0
    for row in rows:
        proposal_id = str(row.get("proposal_id") or "")
        proposal = proposals.get(proposal_id)
        if proposal is None:
            continue
        report = _read_optimizer_report_for_proposal(proposal.path)
        if not report:
            continue
        selected = _selected_optimizer_candidate(report)
        if not selected:
            continue
        run_count += 1
        outcome = _optimizer_sample_outcome(
            proposal_state=proposal.state,
            observations=observations.get(proposal_id, []),
        )
        outcome_score = float(outcome.get("score") or 0.0)
        if outcome_score > 0:
            positive += 1
        elif outcome_score < 0:
            negative += 1
        else:
            neutral += 1
        proposal_samples += 1
        if abs(outcome_score) < 0.1:
            continue
        sample_features = _candidate_feedback_features(selected)
        sample_example = {
            "source": "proposal_outcome",
            "proposal_id": proposal_id,
            "run_id": row.get("run_id"),
            "candidate_id": selected.get("candidate_id"),
            "state": proposal.state,
            "outcome": outcome,
        }
        _record_optimizer_feedback_features(
            features,
            sample_features,
            outcome_score,
            source="proposal_outcome",
            example=sample_example,
        )
        examples.append({
            **sample_example,
            "features": sample_features[:8],
        })
    for row in _optimizer_candidate_decision_rows(paths, pkg):
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        outcome_score, policy, weighting = _optimizer_candidate_decision_feedback_score(
            row,
            payload,
            metadata,
        )
        if outcome_score > 0:
            positive += 1
            decision_positive += 1
        elif outcome_score < 0:
            negative += 1
            decision_negative += 1
        else:
            neutral += 1
            decision_neutral += 1
        decision_samples += 1
        sample_features = _candidate_decision_feedback_features(
            row=row,
            metadata=metadata,
            feedback_score=outcome_score,
        )
        sample_example = {
            "source": "asset_candidate_decision",
            "asset_candidate_id": row.get("id"),
            "candidate_id": metadata.get("optimizer_candidate_id"),
            "run_id": metadata.get("optimizer_run_id"),
            "state": row.get("state"),
            "decision": row.get("decision") or row.get("state"),
            "preview_type": metadata.get("preview_type"),
            "preview_status": metadata.get("preview_status"),
            "outcome_score": payload.get("outcome_score"),
            "feedback_score": round(outcome_score, 4),
            "feedback_policy": policy,
            "feedback_weighting": weighting,
            "evidence_refs": _unique_strings([
                *[str(ref) for ref in row.get("evidence_refs") or []],
                f"strategy_tuning:{metadata.get('optimizer_run_id')}" if metadata.get("optimizer_run_id") else "",
            ])[:8],
        }
        if abs(outcome_score) >= 0.05:
            _record_optimizer_feedback_features(
                features,
                sample_features,
                outcome_score,
                source="asset_candidate_decision",
                example=sample_example,
            )
        examples.append({
            **sample_example,
            "features": sample_features[:8],
        })
    feedback.update({
        "run_count": run_count,
        "sample_count": positive + negative + neutral,
        "positive_samples": positive,
        "negative_samples": negative,
        "neutral_samples": neutral,
        "proposal_samples": proposal_samples,
        "candidate_decision_samples": decision_samples,
        "candidate_decision_positive_samples": decision_positive,
        "candidate_decision_negative_samples": decision_negative,
        "candidate_decision_neutral_samples": decision_neutral,
        "features": features,
        "examples": examples[-8:],
    })
    feedback["calibration"] = _optimizer_outcome_feedback_calibration(feedback)
    return feedback


def _optimizer_outcome_feedback_calibration(feedback: dict[str, Any]) -> dict[str, Any]:
    sample_count = int(feedback.get("sample_count") or 0)
    run_count = int(feedback.get("run_count") or feedback.get("proposal_samples") or 0)
    positive_samples = int(feedback.get("positive_samples") or 0)
    negative_samples = int(feedback.get("negative_samples") or 0)
    neutral_samples = int(feedback.get("neutral_samples") or 0)
    proposal_samples = int(feedback.get("proposal_samples") or 0)
    decision_samples = int(feedback.get("candidate_decision_samples") or 0)
    features = feedback.get("features") if isinstance(feedback.get("features"), dict) else {}
    feature_rows = [
        row for row in features.values()
        if isinstance(row, dict)
    ]
    total_abs_net = sum(abs(_maybe_float(row.get("net")) or 0.0) for row in feature_rows)
    top_abs_net = max([abs(_maybe_float(row.get("net")) or 0.0) for row in feature_rows] or [0.0])
    concentration = round(top_abs_net / total_abs_net, 4) if total_abs_net > 0 else 0.0
    decision_ratio = _ratio(decision_samples, sample_count)
    proposal_ratio = _ratio(proposal_samples, sample_count)
    positive_ratio = _ratio(positive_samples, sample_count)
    negative_ratio = _ratio(negative_samples, sample_count)
    neutral_ratio = _ratio(neutral_samples, sample_count)

    warnings: list[str] = []
    if run_count < 2:
        warnings.append("single_run_feedback")
    if sample_count < 4:
        warnings.append("low_sample_count")
    if decision_samples and decision_ratio >= 0.7:
        warnings.append("operator_decision_dominant")
    if concentration >= 0.75 and len(feature_rows) >= 2:
        warnings.append("feature_concentration_high")
    if negative_ratio >= 0.7 and sample_count >= 4:
        warnings.append("negative_feedback_dominant")
    if positive_ratio >= 0.9 and sample_count >= 4:
        warnings.append("positive_feedback_unbalanced")
    if decision_samples and proposal_samples == 0:
        warnings.append("no_proposal_outcome_samples")

    if sample_count == 0:
        confidence = "none"
        status = "no_feedback"
    elif "low_sample_count" in warnings or "single_run_feedback" in warnings:
        confidence = "low"
        status = "needs_more_evidence"
    elif "operator_decision_dominant" in warnings or "feature_concentration_high" in warnings:
        confidence = "medium"
        status = "watch"
    else:
        confidence = "high"
        status = "calibrated"

    scale, scale_policy = _optimizer_feedback_calibration_scale(
        confidence=confidence,
        warnings=warnings,
        sample_count=sample_count,
    )
    return {
        "version": "optimizer_feedback_score_calibration_v1",
        "status": status,
        "confidence": confidence,
        "warnings": warnings,
        "run_count": run_count,
        "sample_count": sample_count,
        "score_scale": scale,
        "score_scale_policy": scale_policy,
        "source_mix": {
            "proposal_samples": proposal_samples,
            "candidate_decision_samples": decision_samples,
            "proposal_ratio": proposal_ratio,
            "candidate_decision_ratio": decision_ratio,
        },
        "polarity_mix": {
            "positive_samples": positive_samples,
            "negative_samples": negative_samples,
            "neutral_samples": neutral_samples,
            "positive_ratio": positive_ratio,
            "negative_ratio": negative_ratio,
            "neutral_ratio": neutral_ratio,
        },
        "feature_concentration": {
            "top_abs_net": round(top_abs_net, 4),
            "total_abs_net": round(total_abs_net, 4),
            "top_feature_ratio": concentration,
            "feature_count": len(feature_rows),
        },
    }


def _optimizer_feedback_calibration_scale(
    *,
    confidence: str,
    warnings: list[str],
    sample_count: int,
) -> tuple[float, dict[str, Any]]:
    base_scale = (
        0.0 if sample_count <= 0
        else _OPTIMIZER_FEEDBACK_CALIBRATION_BASE_SCALES.get(confidence, 0.25)
    )
    applied_multipliers: dict[str, float] = {}
    scale = base_scale
    for warning in warnings:
        multiplier = _OPTIMIZER_FEEDBACK_CALIBRATION_WARNING_MULTIPLIERS.get(warning)
        if multiplier is None:
            continue
        applied_multipliers[warning] = multiplier
        scale *= multiplier
    scale = round(max(0.0, min(1.0, scale)), 4)
    return scale, {
        "version": "optimizer_feedback_calibration_scale_v1",
        "base_scale": round(base_scale, 4),
        "warning_multipliers": applied_multipliers,
    }


def _record_optimizer_feedback_features(
    features: dict[str, dict[str, Any]],
    sample_features: list[str],
    outcome_score: float,
    *,
    source: str,
    example: dict[str, Any] | None = None,
) -> None:
    for feature in sample_features:
        row_stats = features.setdefault(
            feature,
            {
                "feature": feature,
                "positive": 0.0,
                "negative": 0.0,
                "net": 0.0,
                "samples": 0,
                "sources": {},
                "positive_by_source": {},
                "negative_by_source": {},
                "examples": [],
            },
        )
        positive_by_source = (
            row_stats.get("positive_by_source")
            if isinstance(row_stats.get("positive_by_source"), dict)
            else {}
        )
        negative_by_source = (
            row_stats.get("negative_by_source")
            if isinstance(row_stats.get("negative_by_source"), dict)
            else {}
        )
        if outcome_score > 0:
            positive_by_source[source] = round(float(positive_by_source.get(source) or 0.0) + outcome_score, 4)
        else:
            negative_by_source[source] = round(float(negative_by_source.get(source) or 0.0) + abs(outcome_score), 4)
        row_stats["positive_by_source"] = positive_by_source
        row_stats["negative_by_source"] = negative_by_source
        row_stats["positive"] = _optimizer_feature_source_total(positive_by_source)
        row_stats["negative"] = _optimizer_feature_source_total(negative_by_source)
        row_stats["net"] = round(float(row_stats["positive"]) - float(row_stats["negative"]), 4)
        row_stats["samples"] = int(row_stats["samples"]) + 1
        sources = row_stats.get("sources") if isinstance(row_stats.get("sources"), dict) else {}
        sources[source] = int(sources.get(source) or 0) + 1
        row_stats["sources"] = sources
        row_stats["source_caps"] = {
            "asset_candidate_decision": _OPTIMIZER_DECISION_FEATURE_WEIGHT_CAP,
        }
        if example:
            examples = [
                row for row in (row_stats.get("examples") or [])
                if isinstance(row, dict)
            ]
            key = (
                str(example.get("source") or ""),
                str(example.get("asset_candidate_id") or example.get("proposal_id") or ""),
                str(example.get("candidate_id") or ""),
            )
            existing = {
                (
                    str(row.get("source") or ""),
                    str(row.get("asset_candidate_id") or row.get("proposal_id") or ""),
                    str(row.get("candidate_id") or ""),
                )
                for row in examples
            }
            if key not in existing:
                examples.append(dict(example))
            row_stats["examples"] = examples[-4:]


def _optimizer_feature_source_total(values: dict[str, Any]) -> float:
    total = 0.0
    for source, raw in values.items():
        value = max(0.0, _maybe_float(raw) or 0.0)
        if source == "asset_candidate_decision":
            value = min(_OPTIMIZER_DECISION_FEATURE_WEIGHT_CAP, value)
        total += value
    return round(total, 4)


def _optimizer_candidate_decision_rows(
    paths: WorkspacePaths,
    pkg: StrategyPackage,
) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    rows = jsonl.read_all(paths.evolution_candidates)
    for row in rows[-max(1, _MAX_OPTIMIZER_DECISION_FEEDBACK_ROWS * 4) :]:
        if not isinstance(row, dict):
            continue
        candidate_id = str(row.get("id") or "")
        if candidate_id:
            latest[candidate_id] = row
    out: list[dict[str, Any]] = []
    for row in latest.values():
        state = str(row.get("state") or "candidate").lower()
        if state not in {"promoted", "rejected"}:
            continue
        if str(row.get("strategy_id") or "") != pkg.strategy_id:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if metadata.get("origin") != "strategy_optimizer_preview":
            continue
        if not metadata.get("optimizer_candidate_id"):
            continue
        out.append(row)
    out.sort(key=lambda row: str(row.get("decided_at") or row.get("ts") or ""))
    return out[-_MAX_OPTIMIZER_DECISION_FEEDBACK_ROWS:]


def _optimizer_candidate_decision_feedback_score(
    row: dict[str, Any],
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> tuple[float, str, dict[str, Any]]:
    state = str(row.get("state") or row.get("decision") or "").lower()
    preview_status = str(metadata.get("preview_status") or "").lower()
    raw_outcome = _maybe_float(payload.get("outcome_score"))
    outcome_score = raw_outcome if raw_outcome is not None else 0.0
    magnitude = max(0.15, min(0.8, abs(outcome_score) or 0.25))
    base_score = 0.0
    policy = "candidate_decision_neutral"
    positive_preview = preview_status == "passed" or outcome_score > 0
    negative_preview = preview_status == "failed" or outcome_score < 0
    if state == "promoted" and positive_preview:
        base_score = magnitude * 0.6
        policy = "promoted_positive_preview_reward"
    elif state == "promoted" and negative_preview:
        base_score = -magnitude * 0.55
        policy = "promoted_negative_preview_caution_penalty"
    elif state == "rejected" and positive_preview:
        base_score = -magnitude * 0.45
        policy = "rejected_positive_preview_preference_penalty"
    elif state == "rejected" and negative_preview:
        policy = "rejected_negative_preview_neutral"
    decay_weight = _optimizer_candidate_decision_decay_weight(row)
    score = round(base_score * decay_weight, 4)
    return score, policy, {
        "version": "optimizer_candidate_decision_weighting_v1",
        "base_score": round(base_score, 4),
        "decay_weight": round(decay_weight, 4),
        "half_life_days": _OPTIMIZER_DECISION_FEEDBACK_HALF_LIFE_DAYS,
        "feature_source_cap": _OPTIMIZER_DECISION_FEATURE_WEIGHT_CAP,
        "decided_at": row.get("decided_at") or row.get("ts"),
    }


def _optimizer_candidate_decision_decay_weight(row: dict[str, Any]) -> float:
    weighted_row = {
        "observed_at": row.get("decided_at") or row.get("ts"),
    }
    return observation_weight(
        weighted_row,
        anchor=now(),
        half_life_days=_OPTIMIZER_DECISION_FEEDBACK_HALF_LIFE_DAYS,
    )


def _candidate_decision_feedback_features(
    *,
    row: dict[str, Any],
    metadata: dict[str, Any],
    feedback_score: float,
) -> list[str]:
    state = str(row.get("state") or row.get("decision") or "").lower()
    preview_status = str(metadata.get("preview_status") or "").lower()
    candidate_id = str(metadata.get("optimizer_candidate_id") or "").strip()
    risk_flags = _str_list(metadata.get("risk_flags"))
    blocked = _str_list(metadata.get("blocked_reasons"))
    reasons = _str_list(metadata.get("reasons"))

    features: list[str] = []
    if candidate_id:
        features.append(f"candidate_id:{candidate_id}")
    cautionary_negative = (
        feedback_score < 0
        and state == "promoted"
        and preview_status == "failed"
    )
    if cautionary_negative:
        for flag in risk_flags:
            features.append(f"risk:{_feedback_token(flag)}")
        for reason in [*blocked, *reasons]:
            head = reason.split(":", 1)[0]
            if head:
                features.append(f"reason:{_feedback_token(head)}")
        return _unique_strings([feature for feature in features if feature])

    if feedback_score == 0.0:
        return []
    for target in _str_list(metadata.get("accepted_targets")):
        features.append(f"target:{target}")
    for flag in risk_flags:
        features.append(f"risk:{_feedback_token(flag)}")
    return _unique_strings([feature for feature in features if feature])


def _read_optimizer_report_for_proposal(proposal_path: Any) -> dict[str, Any] | None:
    path = getattr(proposal_path, "joinpath", lambda name: None)("tuning_run.json")
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    report = data.get("optimizer_report") if isinstance(data, dict) else None
    return report if isinstance(report, dict) and report else None


def _selected_optimizer_candidate(report: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        row for row in report.get("candidates", [])
        if isinstance(row, dict)
    ]
    if not candidates:
        return None
    selected_id = str(report.get("selected_candidate_id") or "")
    selected_index = _maybe_int(report.get("selected_index"))
    for idx, candidate in enumerate(candidates):
        if selected_id and str(candidate.get("candidate_id") or "") == selected_id:
            return candidate
        if selected_index is not None and idx == selected_index:
            return candidate
    return candidates[0]


def _post_apply_observations_by_proposal(paths: WorkspacePaths) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for row in jsonl.read_all(paths.journal("evolution")):
        if row.get("kind") != "proposal.post_apply_observation":
            continue
        proposal_id = str(row.get("proposal_id") or "")
        if not proposal_id:
            continue
        rows.setdefault(proposal_id, []).append(row)
    return rows


def _optimizer_sample_outcome(
    *,
    proposal_state: str,
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    healthy = sum(
        1 for row in observations
        if str(row.get("status") or row.get("outcome") or "").lower()
        in POST_APPLY_HEALTHY_STATUSES
    )
    negative = sum(
        1 for row in observations
        if str(row.get("status") or row.get("outcome") or "").lower()
        in POST_APPLY_NEGATIVE_STATUSES
    )
    state = str(proposal_state or "").lower()
    score = healthy * 1.5 - negative * 2.0
    if state == "applied":
        score += 0.4 if not observations else 0.0
    elif state == "approved":
        score += 0.2
    elif state in {"rejected", "rolled_back"}:
        score -= 1.5
    return {
        "score": round(score, 4),
        "state": state,
        "healthy_observations": healthy,
        "negative_observations": negative,
        "observation_count": len(observations),
    }


def _candidate_feedback_features(candidate: dict[str, Any]) -> list[str]:
    features: list[str] = []
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    if candidate_id:
        features.append(f"candidate_id:{candidate_id}")
    status = str(candidate.get("status") or "").strip().lower()
    if status:
        features.append(f"status:{status}")
    for step_type in _str_list(candidate.get("validation_types")):
        features.append(f"validation:{step_type}")
    for path in _str_list(candidate.get("materialized_files")):
        features.append(f"file:{path}")
    for target in _str_list(candidate.get("accepted_targets")):
        features.append(f"target:{target}")
    for flag in _str_list(candidate.get("risk_flags")):
        features.append(f"risk:{_feedback_token(flag)}")
    for reason in _str_list(candidate.get("reasons")):
        head = reason.split(":", 1)[0]
        if head:
            features.append(f"reason:{_feedback_token(head)}")
    return _unique_strings([feature for feature in features if feature])


def _candidate_feedback_features_from_evaluation(
    *,
    output: dict[str, Any],
    accepted: list[dict[str, Any]],
    materialized: list[str],
    validation_types: list[str],
    risk_flags: list[str],
) -> list[str]:
    features: list[str] = []
    candidate_id = str(output.get("candidate_id") or "").strip()
    if candidate_id:
        features.append(f"candidate_id:{candidate_id}")
    for step_type in validation_types:
        features.append(f"validation:{step_type}")
    for path in materialized:
        features.append(f"file:{path}")
    for change in accepted:
        target = _change_target_digest(change)
        if target:
            features.append(f"target:{target}")
        kind = str(change.get("kind") or change.get("type") or "").strip().lower()
        if kind:
            features.append(f"change_kind:{kind}")
    for flag in risk_flags:
        features.append(f"risk:{_feedback_token(flag)}")
    return _unique_strings([feature for feature in features if feature])


def _score_optimizer_outcome_feedback(
    *,
    output: dict[str, Any],
    accepted: list[dict[str, Any]],
    materialized: list[str],
    validation_types: list[str],
    risk_flags: list[str],
    outcome_feedback: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    calibration = (
        outcome_feedback.get("calibration")
        if isinstance(outcome_feedback.get("calibration"), dict)
        else _optimizer_outcome_feedback_calibration(outcome_feedback)
    )
    calibration_scale = _maybe_float(calibration.get("score_scale")) or 0.0
    calibration_status = str(calibration.get("status") or "")
    calibration_confidence = str(calibration.get("confidence") or "")
    calibration_warnings = _str_list(calibration.get("warnings"))
    feature_stats = outcome_feedback.get("features")
    if not isinstance(feature_stats, dict) or not feature_stats:
        return 0.0, {
            "version": "candidate_outcome_feedback_match_v1",
            "score_delta": 0.0,
            "raw_score_delta": 0.0,
            "calibration_scale": calibration_scale,
            "calibration_status": calibration_status,
            "calibration_confidence": calibration_confidence,
            "calibration_warnings": calibration_warnings,
            "matched_features": [],
            "sample_count": int(outcome_feedback.get("sample_count") or 0),
        }
    features = _candidate_feedback_features_from_evaluation(
        output=output,
        accepted=accepted,
        materialized=materialized,
        validation_types=validation_types,
        risk_flags=risk_flags,
    )
    matches: list[dict[str, Any]] = []
    raw_delta = 0.0
    for feature in features:
        stats = feature_stats.get(feature)
        if not isinstance(stats, dict):
            continue
        net = _maybe_float(stats.get("net")) or 0.0
        if not net:
            continue
        raw_delta += net
        matches.append({
            "feature": feature,
            "positive": stats.get("positive", 0.0),
            "negative": stats.get("negative", 0.0),
            "net": round(net, 4),
            "samples": stats.get("samples", 0),
            "sources": stats.get("sources") if isinstance(stats.get("sources"), dict) else {},
            "positive_by_source": (
                stats.get("positive_by_source")
                if isinstance(stats.get("positive_by_source"), dict)
                else {}
            ),
            "negative_by_source": (
                stats.get("negative_by_source")
                if isinstance(stats.get("negative_by_source"), dict)
                else {}
            ),
            "source_caps": stats.get("source_caps") if isinstance(stats.get("source_caps"), dict) else {},
            "examples": [
                row for row in (stats.get("examples") or [])
                if isinstance(row, dict)
            ][-4:],
        })
    if not matches:
        return 0.0, {
            "version": "candidate_outcome_feedback_match_v1",
            "score_delta": 0.0,
            "raw_score_delta": 0.0,
            "calibration_scale": calibration_scale,
            "calibration_status": calibration_status,
            "calibration_confidence": calibration_confidence,
            "calibration_warnings": calibration_warnings,
            "matched_features": [],
            "sample_count": int(outcome_feedback.get("sample_count") or 0),
        }
    raw_score_delta = max(-18.0, min(18.0, raw_delta * 3.0))
    delta = raw_score_delta * calibration_scale
    matches.sort(key=lambda row: abs(float(row.get("net") or 0.0)), reverse=True)
    return round(delta, 3), {
        "version": "candidate_outcome_feedback_match_v1",
        "score_delta": round(delta, 3),
        "raw_score_delta": round(raw_score_delta, 3),
        "calibration_scale": calibration_scale,
        "calibration_status": calibration_status,
        "calibration_confidence": calibration_confidence,
        "calibration_warnings": calibration_warnings,
        "matched_features": matches[:8],
        "sample_count": int(outcome_feedback.get("sample_count") or 0),
    }


def _optimizer_feedback_report(feedback: dict[str, Any]) -> dict[str, Any]:
    features = feedback.get("features")
    feature_rows = [
        row for row in (features or {}).values()
        if isinstance(row, dict)
    ] if isinstance(features, dict) else []
    feature_rows.sort(
        key=lambda row: abs(float(row.get("net") or 0.0)),
        reverse=True,
    )
    return {
        "version": feedback.get("version") or "optimizer_outcome_feedback_v1",
        "sample_count": int(feedback.get("sample_count") or 0),
        "positive_samples": int(feedback.get("positive_samples") or 0),
        "negative_samples": int(feedback.get("negative_samples") or 0),
        "neutral_samples": int(feedback.get("neutral_samples") or 0),
        "proposal_samples": int(feedback.get("proposal_samples") or 0),
        "candidate_decision_samples": int(feedback.get("candidate_decision_samples") or 0),
        "candidate_decision_positive_samples": int(feedback.get("candidate_decision_positive_samples") or 0),
        "candidate_decision_negative_samples": int(feedback.get("candidate_decision_negative_samples") or 0),
        "candidate_decision_neutral_samples": int(feedback.get("candidate_decision_neutral_samples") or 0),
        "decision_feedback_policy": feedback.get("decision_feedback_policy") or {},
        "calibration": (
            feedback.get("calibration")
            if isinstance(feedback.get("calibration"), dict)
            else _optimizer_outcome_feedback_calibration(feedback)
        ),
        "top_features": feature_rows[:10],
        "examples": list(feedback.get("examples") or [])[-5:],
    }


def _feedback_token(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )[:96]


def _maybe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _select_tuning_candidate(
    output: dict[str, Any],
    cfg: StrategyTuningConfig,
    pkg: StrategyPackage,
    paths: WorkspacePaths | None = None,
    run_id: str | None = None,
    selected_assets: dict[str, Any] | None = None,
    create_asset_candidates: bool = True,
) -> dict[str, Any] | None:
    candidates = _candidate_outputs(output)
    if not candidates:
        return None
    outcome_feedback = (
        _optimizer_outcome_feedback(paths, pkg)
        if paths is not None else _empty_optimizer_feedback()
    )
    evaluations = [
        _evaluate_tuning_candidate(
            candidate,
            cfg,
            pkg,
            index=idx,
            outcome_feedback=outcome_feedback,
        )
        for idx, candidate in enumerate(candidates[:_MAX_TUNING_CANDIDATES])
    ]
    if not evaluations:
        return None
    if paths is not None and run_id:
        evaluations = _apply_candidate_validation_previews(
            paths=paths,
            pkg=pkg,
            run_id=run_id,
            evaluations=evaluations,
        )
        evaluations = _apply_candidate_backtest_previews(
            paths=paths,
            pkg=pkg,
            run_id=run_id,
            evaluations=evaluations,
        )
    selected = max(
        evaluations,
        key=lambda row: (float(row.get("score") or 0.0), -int(row.get("index") or 0)),
    )
    if paths is not None and run_id and create_asset_candidates:
        _attach_candidate_preview_asset_candidates(
            paths=paths,
            pkg=pkg,
            run_id=run_id,
            evaluations=evaluations,
            selected_candidate_id=str(selected.get("candidate_id") or ""),
            selected_assets=selected_assets or {},
        )
    report = {
        "version": _OPTIMIZER_VERSION,
        "candidate_count": len(candidates),
        "evaluated_count": len(evaluations),
        "truncated": len(candidates) > _MAX_TUNING_CANDIDATES,
        "selected_candidate_id": selected.get("candidate_id"),
        "selected_index": selected.get("index"),
        "selected_score": selected.get("score"),
        "outcome_feedback": _optimizer_feedback_report(outcome_feedback),
        "validation_preview": _optimizer_validation_preview_summary(evaluations),
        "backtest_preview": _optimizer_backtest_preview_summary(evaluations),
        "selection_reason": (
            "selected highest deterministic local score from materialization, "
            "validation strength, bounded candidate static/backtest previews, "
            "risk, evidence, expected-effect, and historical outcome-feedback signals"
        ),
        "candidates": [_candidate_report(row) for row in evaluations],
    }
    return {
        "selected_output": dict(selected.get("output") or {}),
        "report": report,
    }


def _candidate_outputs(output: dict[str, Any]) -> list[dict[str, Any]]:
    raw_candidates = None
    for key in _CANDIDATE_LIST_KEYS:
        value = output.get(key)
        if isinstance(value, list) and value:
            raw_candidates = value
            break
    if raw_candidates is None:
        return []
    base = {
        key: value
        for key, value in output.items()
        if key not in _CANDIDATE_LOCAL_KEYS
    }
    candidates: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_candidates):
        candidate_id = _candidate_id(raw, idx)
        if not isinstance(raw, dict):
            candidates.append({
                **base,
                "candidate_id": candidate_id,
                "summary": str(raw)[:240],
                "_candidate_invalid": True,
            })
            continue
        candidate = {**base, **raw}
        candidate["candidate_id"] = candidate_id
        candidate["_candidate_index"] = idx
        candidates.append(candidate)
    return candidates


def _candidate_id(raw: Any, idx: int) -> str:
    if isinstance(raw, dict):
        for key in ("candidate_id", "id", "name", "label"):
            value = str(raw.get(key) or "").strip()
            if value:
                return value[:96]
    return f"candidate_{idx + 1}"


def _evaluate_tuning_candidate(
    output: dict[str, Any],
    cfg: StrategyTuningConfig,
    pkg: StrategyPackage,
    *,
    index: int,
    outcome_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    accepted, dropped, warnings = _filter_changes(output, cfg)
    plan = build_validation_plan(
        _validation_plan_input(output),
        source="strategy_evolution_candidate",
        strategy_id=pkg.strategy_id,
        require=bool(accepted),
    )
    effective_accepted = list(accepted)
    effective_dropped = list(dropped)
    effective_warnings = list(warnings)
    if plan.blocked_reasons:
        effective_warnings.extend(
            f"validation: {reason}"
            for reason in plan.blocked_reasons
        )
        if effective_accepted:
            for change in effective_accepted:
                effective_dropped.append({
                    "entry": change,
                    "reason": "validation_plan_blocked",
                    "blocked_reasons": list(plan.blocked_reasons),
                })
            effective_accepted = []
    after_files, materialized, unmaterialized = _materialize_strategy_tuning_after_files(
        pkg,
        effective_accepted,
    )
    risk_flags = _str_list(output.get("risk_flags") or output.get("risks"))
    validation_types = [
        str(getattr(step, "type", "") or "")
        for step in getattr(plan, "steps", [])
    ]
    score, reasons, feedback_match = _score_tuning_candidate(
        output=output,
        accepted=effective_accepted,
        dropped=effective_dropped,
        warnings=effective_warnings,
        materialized=materialized,
        unmaterialized=unmaterialized,
        blocked_reasons=list(plan.blocked_reasons),
        validation_types=validation_types,
        risk_flags=risk_flags,
        outcome_feedback=outcome_feedback or _empty_optimizer_feedback(),
    )
    return {
        "candidate_id": str(output.get("candidate_id") or f"candidate_{index + 1}"),
        "index": index,
        "output": output,
        "score": round(score, 3),
        "status": _candidate_status(
            accepted=effective_accepted,
            materialized=materialized,
            blocked_reasons=list(plan.blocked_reasons),
            invalid=bool(output.get("_candidate_invalid")),
        ),
        "accepted": effective_accepted,
        "dropped": effective_dropped,
        "warnings": effective_warnings,
        "after_files": after_files,
        "materialized_files": materialized,
        "unmaterialized_changes": unmaterialized,
        "validation_status": plan.status,
        "validation_types": validation_types,
        "blocked_reasons": list(plan.blocked_reasons),
        "risk_flags": risk_flags,
        "reasons": reasons,
        "outcome_feedback": feedback_match,
    }


def _apply_candidate_validation_previews(
    *,
    paths: WorkspacePaths,
    pkg: StrategyPackage,
    run_id: str,
    evaluations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run bounded, artifact-backed static previews for the strongest candidates."""

    ranked = sorted(
        [
            row for row in evaluations
            if row.get("status") == "materialized"
            and row.get("materialized_files")
            and isinstance(row.get("after_files"), dict)
        ],
        key=lambda row: (float(row.get("score") or 0.0), -int(row.get("index") or 0)),
        reverse=True,
    )
    previewed_ids: set[int] = set()
    for row in ranked[:_CANDIDATE_VALIDATION_PREVIEW_TOP_K]:
        preview = _candidate_validation_preview(
            paths=paths,
            pkg=pkg,
            run_id=run_id,
            row=row,
        )
        row["validation_preview"] = preview
        previewed_ids.add(id(row))
        score_delta = float(preview.get("score_delta") or 0.0)
        row["score"] = round(float(row.get("score") or 0.0) + score_delta, 3)
        reasons = list(row.get("reasons") or [])
        blocked = list(row.get("blocked_reasons") or [])
        if preview.get("status") == "passed":
            reasons.append("candidate_validation_preview_passed")
        elif preview.get("status") == "failed":
            reasons.append("candidate_validation_preview_failed")
            blocked.extend(
                str(reason)
                for reason in preview.get("blocked_reasons") or []
                if str(reason).strip()
            )
            row["status"] = "failed_preview"
        row["reasons"] = _unique_strings(reasons)
        row["blocked_reasons"] = _unique_strings(blocked)
    for row in evaluations:
        if id(row) in previewed_ids:
            continue
        row["validation_preview"] = _candidate_validation_preview_skipped(row)
    return evaluations


def _candidate_validation_preview(
    *,
    paths: WorkspacePaths,
    pkg: StrategyPackage,
    run_id: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    candidate_id = str(row.get("candidate_id") or f"candidate_{row.get('index', 0)}")
    candidate_slug = _candidate_artifact_slug(candidate_id, row.get("index"))
    root = paths.evolution / "optimizer_runs" / run_id / "candidates" / candidate_slug
    after_files = row.get("after_files") if isinstance(row.get("after_files"), dict) else {}
    package_files = _candidate_preview_package_files(pkg, after_files)
    root.mkdir(parents=True, exist_ok=True)
    for after_path, content in sorted(after_files.items()):
        out = root / str(after_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(out, str(content))
    package_dir = _write_candidate_preview_package(root, pkg, after_files)
    validation = validate_proposal_files(
        strategy_id=pkg.strategy_id,
        files=package_files,
    )
    validation_dict = validation.asdict()
    blocked_reasons = [
        f"static:{issue.get('code')}"
        for issue in validation_dict.get("blockers") or []
        if isinstance(issue, dict) and issue.get("code")
    ]
    status = "passed" if validation.ok else "failed"
    preview_path = root / "validation_preview.json"
    evidence_ref = _file_evidence_ref(paths, preview_path)
    requested_types = list(row.get("validation_types") or [])
    preview = {
        "version": "candidate_validation_preview_v1",
        "status": status,
        "candidate_id": candidate_id,
        "candidate_index": row.get("index"),
        "artifact_dir": str(root),
        "package_dir": str(package_dir),
        "evidence_refs": [evidence_ref],
        "requested_step_types": requested_types,
        "executed_step_types": ["static_check"],
        "deferred_step_types": [
            step_type for step_type in requested_types
            if step_type != "static_check"
        ],
        "score_delta": 12.0 if validation.ok else -80.0,
        "blocked_reasons": blocked_reasons,
        "warning_count": len(validation_dict.get("warnings") or []),
        "blocker_count": len(validation_dict.get("blockers") or []),
        "validation": validation_dict,
        "materialized_files": list(row.get("materialized_files") or []),
        "preview_policy": {
            "top_k": _CANDIDATE_VALIDATION_PREVIEW_TOP_K,
            "executes": "built_in_strategy_static_and_smoke_validator",
            "does_not_execute": ["candidate_shell_commands", "backtest", "live_or_shadow_runs"],
        },
    }
    atomic_write_text(preview_path, json.dumps(preview, indent=2, ensure_ascii=False, default=str))
    return preview


def _apply_candidate_backtest_previews(
    *,
    paths: WorkspacePaths,
    pkg: StrategyPackage,
    run_id: str,
    evaluations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ranked = sorted(
        [
            row for row in evaluations
            if row.get("validation_preview", {}).get("status") == "passed"
            and "backtest" in set(row.get("validation_types") or [])
        ],
        key=lambda row: (float(row.get("score") or 0.0), -int(row.get("index") or 0)),
        reverse=True,
    )
    previewed_ids: set[int] = set()
    for row in ranked[:_CANDIDATE_BACKTEST_PREVIEW_TOP_K]:
        preview = _candidate_backtest_preview(
            paths=paths,
            pkg=pkg,
            run_id=run_id,
            row=row,
        )
        row["backtest_preview"] = preview
        previewed_ids.add(id(row))
        score_delta = float(preview.get("score_delta") or 0.0)
        row["score"] = round(float(row.get("score") or 0.0) + score_delta, 3)
        reasons = list(row.get("reasons") or [])
        blocked = list(row.get("blocked_reasons") or [])
        status = str(preview.get("status") or "")
        if status == "passed":
            reasons.append("candidate_backtest_preview_passed")
        elif status == "no_data":
            reasons.append("candidate_backtest_preview_no_data")
        elif status == "failed":
            reasons.append("candidate_backtest_preview_failed")
            blocked.extend(
                str(reason)
                for reason in preview.get("blocked_reasons") or []
                if str(reason).strip()
            )
            row["status"] = "failed_backtest_preview"
        comparison = (
            preview.get("baseline_comparison")
            if isinstance(preview.get("baseline_comparison"), dict)
            else {}
        )
        comparison_direction = str(comparison.get("overall_direction") or "")
        if comparison_direction == "improved":
            reasons.append("candidate_backtest_baseline_improved")
        elif comparison_direction == "regressed":
            reasons.append("candidate_backtest_baseline_regressed")
        row["reasons"] = _unique_strings(reasons)
        row["blocked_reasons"] = _unique_strings(blocked)
    for row in evaluations:
        if id(row) in previewed_ids:
            continue
        if "backtest" in set(row.get("validation_types") or []):
            row["backtest_preview"] = _candidate_backtest_preview_skipped(row)
    return evaluations


def _attach_candidate_preview_asset_candidates(
    *,
    paths: WorkspacePaths,
    pkg: StrategyPackage,
    run_id: str,
    evaluations: list[dict[str, Any]],
    selected_candidate_id: str,
    selected_assets: dict[str, Any],
) -> None:
    """Turn optimizer preview outcomes into reviewable asset candidates.

    These are deliberately candidates, not promoted Capsules. Optimizer
    alternatives are pre-proposal evidence; promotion stays governed through
    the existing asset-candidate review path.
    """

    trigger_context = _asset_trigger_context(selected_assets or {})
    for row in evaluations:
        preview_type, preview = _candidate_preview_for_learning(row)
        if not preview_type or not preview:
            continue
        try:
            candidate = evolution_assets.create_candidate(
                paths,
                kind="capsule",
                summary=_candidate_preview_asset_summary(pkg, row, preview_type, preview),
                payload=_candidate_preview_asset_payload(
                    pkg=pkg,
                    run_id=run_id,
                    row=row,
                    preview_type=preview_type,
                    preview=preview,
                    selected_candidate_id=selected_candidate_id,
                    trigger_context=trigger_context,
                ),
                evidence_refs=_candidate_preview_evidence_refs(run_id, preview),
                source_event_id=None,
                strategy_id=pkg.strategy_id,
            )
        except Exception:
            _LOG.exception(
                "candidate preview asset-candidate creation failed for %s/%s",
                run_id,
                row.get("candidate_id"),
            )
            continue
        row["asset_candidate"] = _candidate_preview_asset_candidate_digest(candidate)
        reasons = list(row.get("reasons") or [])
        reasons.append("preview_outcome_asset_candidate_created")
        row["reasons"] = _unique_strings(reasons)


def _candidate_preview_for_learning(row: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    backtest_preview = (
        row.get("backtest_preview")
        if isinstance(row.get("backtest_preview"), dict)
        else {}
    )
    if backtest_preview.get("status") in {"passed", "failed"}:
        return "backtest", backtest_preview
    validation_preview = (
        row.get("validation_preview")
        if isinstance(row.get("validation_preview"), dict)
        else {}
    )
    if validation_preview.get("status") in {"passed", "failed"}:
        return "static", validation_preview
    return "", None


def _candidate_preview_asset_summary(
    pkg: StrategyPackage,
    row: dict[str, Any],
    preview_type: str,
    preview: dict[str, Any],
) -> str:
    status = str(preview.get("status") or "unknown")
    candidate_id = str(row.get("candidate_id") or f"candidate_{row.get('index', 0)}")
    return (
        f"{preview_type.title()} preview {status} for strategy "
        f"{pkg.strategy_id} optimizer candidate {candidate_id}."
    )


def _candidate_preview_asset_payload(
    *,
    pkg: StrategyPackage,
    run_id: str,
    row: dict[str, Any],
    preview_type: str,
    preview: dict[str, Any],
    selected_candidate_id: str,
    trigger_context: dict[str, Any],
) -> dict[str, Any]:
    candidate_id = str(row.get("candidate_id") or f"candidate_{row.get('index', 0)}")
    status = str(preview.get("status") or "unknown")
    outcome_score = _candidate_preview_outcome_score(preview_type, status)
    evidence_refs = _candidate_preview_evidence_refs(run_id, preview)
    metadata = {
        "origin": "strategy_optimizer_preview",
        "optimizer_run_id": run_id,
        "optimizer_candidate_id": candidate_id,
        "optimizer_candidate_index": row.get("index"),
        "optimizer_candidate_score": row.get("score"),
        "optimizer_selected_candidate_id": selected_candidate_id,
        "selected_by_optimizer": bool(candidate_id == selected_candidate_id),
        "preview_type": preview_type,
        "preview_status": status,
        "preview_score_delta": preview.get("score_delta"),
        "baseline_comparison": _candidate_preview_baseline_digest(
            preview.get("baseline_comparison")
            if isinstance(preview.get("baseline_comparison"), dict)
            else {}
        ),
        "validation_types": list(row.get("validation_types") or [])[:8],
        "materialized_files": list(row.get("materialized_files") or [])[:8],
        "accepted_targets": [
            _change_target_digest(change)
            for change in row.get("accepted", [])
            if isinstance(change, dict)
        ][:8],
        "risk_flags": list(row.get("risk_flags") or [])[:8],
        "reasons": list(row.get("reasons") or [])[:12],
        "blocked_reasons": list(row.get("blocked_reasons") or [])[:8],
        "trigger_context": trigger_context,
        **{
            key: value
            for key, value in trigger_context.items()
            if key.startswith("trigger_") or key in {
                "signal_kinds",
                "market_regimes",
                "markets",
                "timeframes",
                "data_quality",
                "selected_gene_ids",
                "selected_capsule_ids",
            }
        },
    }
    validation_result: dict[str, Any] = {
        "type": f"candidate_{preview_type}_preview",
        "status": status,
        "candidate_id": candidate_id,
        "score_delta": preview.get("score_delta"),
        "blocked_reasons": list(preview.get("blocked_reasons") or [])[:8],
        "evidence_refs": evidence_refs,
    }
    if preview_type == "backtest":
        result = preview.get("backtest_result") if isinstance(preview.get("backtest_result"), dict) else {}
        validation_result["backtest_result"] = {
            key: result.get(key)
            for key in (
                "ok",
                "reason",
                "verdict",
                "coverage_ok",
                "coverage_message",
                "total_return_pct",
                "max_drawdown_pct",
                "sharpe_ratio",
                "profit_factor",
                "win_rate_pct",
                "total_trades",
            )
            if key in result
        }
        baseline = (
            preview.get("baseline_comparison")
            if isinstance(preview.get("baseline_comparison"), dict)
            else {}
        )
        if baseline:
            validation_result["baseline_comparison"] = _candidate_preview_baseline_digest(baseline)
    else:
        validation = preview.get("validation") if isinstance(preview.get("validation"), dict) else {}
        validation_result["validation"] = {
            "ok": validation.get("ok"),
            "warning_count": preview.get("warning_count"),
            "blocker_count": preview.get("blocker_count"),
            "blockers": list(validation.get("blockers") or [])[:6],
            "warnings": list(validation.get("warnings") or [])[:6],
        }
    return {
        "gene_id": (
            (trigger_context.get("selected_gene_ids") or [None])[0]
            or "gene_nerya_strategy_drawdown_review"
        ),
        "source_event_id": None,
        "summary": _candidate_preview_asset_summary(pkg, row, preview_type, preview),
        "evidence_refs": evidence_refs,
        "validation_results": [validation_result],
        "outcome_score": outcome_score,
        "promotion_ref": f"strategy_tuning:{run_id}:candidate:{candidate_id}:{preview_type}",
        "strategy_id": pkg.strategy_id,
        "metadata": metadata,
    }


def _candidate_preview_baseline_digest(comparison: dict[str, Any]) -> dict[str, Any]:
    if not comparison:
        return {}
    return {
        "version": comparison.get("version"),
        "status": comparison.get("status"),
        "overall_direction": comparison.get("overall_direction"),
        "summary": comparison.get("summary"),
        "score_delta": comparison.get("score_delta"),
        "critical_regressed": list(comparison.get("critical_regressed") or [])[:8],
        "metrics_delta": [
            row for row in (comparison.get("metrics_delta") or [])
            if isinstance(row, dict)
        ][:8],
        "evidence_refs": list(comparison.get("evidence_refs") or [])[:8],
    }


def _candidate_preview_outcome_score(preview_type: str, status: str) -> float:
    if status == "passed":
        return 0.7 if preview_type == "backtest" else 0.4
    if status == "failed":
        return -0.7 if preview_type == "backtest" else -0.45
    return 0.0


def _candidate_preview_evidence_refs(run_id: str, preview: dict[str, Any]) -> list[str]:
    return _unique_strings([
        f"strategy_tuning:{run_id}",
        *[
            str(ref)
            for ref in (preview.get("evidence_refs") or [])
            if str(ref).strip()
        ],
    ])


def _candidate_preview_asset_candidate_digest(candidate: dict[str, Any]) -> dict[str, Any]:
    payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        "id": candidate.get("id"),
        "kind": candidate.get("kind"),
        "safe_to_promote": candidate.get("safe_to_promote"),
        "blocked_reasons": list(candidate.get("blocked_reasons") or [])[:8],
        "evidence_refs": list(candidate.get("evidence_refs") or [])[:8],
        "promotion_gates": candidate.get("promotion_gates") or {},
        "preview_type": metadata.get("preview_type"),
        "preview_status": metadata.get("preview_status"),
        "selected_by_optimizer": metadata.get("selected_by_optimizer"),
        "outcome_score": payload.get("outcome_score"),
    }


def _candidate_backtest_preview(
    *,
    paths: WorkspacePaths,
    pkg: StrategyPackage,
    run_id: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    candidate_id = str(row.get("candidate_id") or f"candidate_{row.get('index', 0)}")
    candidate_slug = _candidate_artifact_slug(candidate_id, row.get("index"))
    root = paths.evolution / "optimizer_runs" / run_id / "candidates" / candidate_slug
    package_dir = Path(str((row.get("validation_preview") or {}).get("package_dir") or "")) if isinstance(row.get("validation_preview"), dict) else root / "package"
    if not str(package_dir):
        package_dir = root / "package"
    if not package_dir.is_absolute():
        package_dir = paths.root / package_dir
    preview_path = root / "backtest_preview.json"
    base: dict[str, Any] = {
        "version": "candidate_backtest_preview_v1",
        "candidate_id": candidate_id,
        "candidate_index": row.get("index"),
        "artifact_dir": str(root),
        "package_dir": str(package_dir),
        "preset": "default",
        "allow_mock": False,
        "preview_policy": {
            "top_k": _CANDIDATE_BACKTEST_PREVIEW_TOP_K,
            "executes": "built_in_backtest_runner",
            "does_not_create_proposal": True,
            "does_not_enter_lineage": True,
        },
    }
    global run_strategy_backtest, NoHistoricalDataError
    if run_strategy_backtest is None:
        from ..skills.builtin.backtest.scripts.backtest_run import (
            run_strategy_backtest as _run_strategy_backtest,
        )

        run_strategy_backtest = _run_strategy_backtest
    if NoHistoricalDataError is None:
        from ..skills.builtin.backtest.scripts.data_cache import (
            NoHistoricalDataError as _NoHistoricalDataError,
        )

        NoHistoricalDataError = _NoHistoricalDataError
    try:
        result = run_strategy_backtest(
            package_dir=package_dir,
            preset="default",
            workspace=paths.root,
            allow_mock=False,
        )
        status, score_delta, blocked = _candidate_backtest_preview_status(result)
        baseline_comparison = _candidate_backtest_baseline_comparison(
            paths=paths,
            pkg=pkg,
            result=result,
        )
        score_delta = round(
            score_delta + float(baseline_comparison.get("score_delta") or 0.0),
            3,
        )
        preview = {
            **base,
            "status": status,
            "score_delta": score_delta,
            "blocked_reasons": blocked,
            "backtest_result": _candidate_backtest_result_digest(result),
            "baseline_comparison": baseline_comparison,
            "artifacts": _candidate_backtest_artifacts(paths, result),
        }
    except NoHistoricalDataError as exc:
        preview = {
            **base,
            "status": "no_data",
            "reason": "no_historical_data",
            "score_delta": -18.0,
            "blocked_reasons": ["no_historical_data"],
            "backtest_result": {
                "ok": False,
                "reason": "no_historical_data",
                "coverage_ok": False,
                "coverage_message": str(exc),
            },
            "artifacts": [],
        }
    except Exception as exc:
        preview = {
            **base,
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "score_delta": -45.0,
            "blocked_reasons": [f"backtest_error:{type(exc).__name__}"],
            "backtest_result": {"ok": False, "reason": str(exc)},
            "artifacts": [],
        }
    evidence_refs = [
        _file_evidence_ref(paths, preview_path),
        *[
            str(ref)
            for ref in (
                (preview.get("baseline_comparison") or {}).get("evidence_refs") or []
            )
            if str(ref).strip()
        ],
        *[
            str(artifact.get("evidence_ref") or "")
            for artifact in preview.get("artifacts") or []
            if isinstance(artifact, dict)
        ],
    ]
    preview["evidence_refs"] = _unique_strings(evidence_refs)
    atomic_write_text(preview_path, json.dumps(preview, indent=2, ensure_ascii=False, default=str))
    return preview


def _candidate_backtest_preview_status(result: dict[str, Any]) -> tuple[str, float, list[str]]:
    if not result.get("ok"):
        return "failed", -45.0, [str(result.get("reason") or "backtest_failed")]
    if str(result.get("verdict") or "").upper() == "FAIL":
        return "failed", -45.0, ["backtest_verdict_fail"]
    if result.get("coverage_ok") is False:
        return "failed", -30.0, ["backtest_coverage_failed"]
    return "passed", 24.0, []


def _candidate_backtest_preview_skipped(row: dict[str, Any]) -> dict[str, Any]:
    reason = "outside_top_k"
    if row.get("validation_preview", {}).get("status") != "passed":
        reason = "static_preview_not_passed"
    return {
        "version": "candidate_backtest_preview_v1",
        "status": "skipped",
        "reason": reason,
        "candidate_id": row.get("candidate_id"),
        "candidate_index": row.get("index"),
        "score_delta": 0.0,
        "preview_policy": {
            "top_k": _CANDIDATE_BACKTEST_PREVIEW_TOP_K,
        },
    }


def _candidate_backtest_baseline_comparison(
    *,
    paths: WorkspacePaths,
    pkg: StrategyPackage,
    result: dict[str, Any],
) -> dict[str, Any]:
    baseline = _latest_strategy_backtest_metrics(paths, pkg.strategy_id)
    candidate_metrics = _candidate_backtest_metrics(result)
    if not baseline:
        return {
            "version": "candidate_backtest_baseline_comparison_v1",
            "status": "missing_baseline",
            "summary": "No workspace baseline backtest artifact was found.",
            "score_delta": 0.0,
            "metrics_delta": [],
            "evidence_refs": [],
        }
    deltas = _candidate_backtest_metric_deltas(
        baseline.get("metrics") if isinstance(baseline.get("metrics"), dict) else {},
        candidate_metrics,
    )
    improved = sum(1 for row in deltas if row.get("direction") == "improved")
    regressed = sum(1 for row in deltas if row.get("direction") == "regressed")
    critical_regressed = [
        str(row.get("key"))
        for row in deltas
        if row.get("direction") == "regressed"
        and str(row.get("key") or "") in _CANDIDATE_BASELINE_CRITICAL_METRICS
    ]
    if not deltas:
        direction = "unknown"
    elif regressed > improved or critical_regressed:
        direction = "regressed"
    elif improved > regressed:
        direction = "improved"
    else:
        direction = "flat"
    score_delta = _candidate_backtest_baseline_score_delta(deltas)
    evidence_refs = [
        _file_evidence_ref(paths, baseline.get("metrics_path"))
        if baseline.get("metrics_path") else "",
    ]
    return {
        "version": "candidate_backtest_baseline_comparison_v1",
        "status": "complete",
        "overall_direction": direction,
        "summary": (
            f"Candidate backtest vs latest workspace baseline: "
            f"{improved} metric(s) improved, {regressed} regressed."
        ),
        "score_delta": score_delta,
        "baseline": baseline,
        "candidate": {
            "metrics": {
                key: candidate_metrics.get(key)
                for key in _CANDIDATE_BASELINE_COMPARISON_METRICS
                if key in candidate_metrics
            },
        },
        "metrics_delta": deltas,
        "critical_regressed": critical_regressed[:8],
        "evidence_refs": _unique_strings(evidence_refs),
    }


def _latest_strategy_backtest_metrics(
    paths: WorkspacePaths,
    strategy_id: str,
) -> dict[str, Any] | None:
    root = paths.strategy(strategy_id) / "backtests"
    if not root.exists() or not root.is_dir():
        return None
    metrics_paths = sorted(
        (path for path in root.glob("*/metrics.json") if path.is_file()),
        key=lambda path: (path.parent.name, str(path)),
    )
    if not metrics_paths:
        return None
    metrics_path = metrics_paths[-1]
    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return {
        "backtest_id": metrics_path.parent.name,
        "metrics_path": str(metrics_path),
        "metrics": {
            key: metrics.get(key)
            for key in _CANDIDATE_BASELINE_COMPARISON_METRICS
            if key in metrics
        },
    }


def _candidate_backtest_metrics(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    out = {
        key: result.get(key)
        for key in _CANDIDATE_BASELINE_COMPARISON_METRICS
        if key in result
    }
    for key in _CANDIDATE_BASELINE_COMPARISON_METRICS:
        if key in metrics and key not in out:
            out[key] = metrics.get(key)
    return out


def _candidate_backtest_metric_deltas(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in _CANDIDATE_BASELINE_COMPARISON_METRICS:
        before = _maybe_float(baseline.get(key))
        after = _maybe_float(candidate.get(key))
        if before is None or after is None:
            continue
        delta = round(after - before, 6)
        rows.append({
            "key": key,
            "before": before,
            "after": after,
            "delta": delta,
            "direction": _candidate_backtest_delta_direction(key, delta),
        })
    return rows


def _candidate_backtest_delta_direction(key: str, delta: float) -> str:
    if abs(delta) < 1e-12:
        return "flat"
    improved = delta < 0 if key in _CANDIDATE_BASELINE_LOWER_IS_BETTER else delta > 0
    return "improved" if improved else "regressed"


def _candidate_backtest_baseline_score_delta(deltas: list[dict[str, Any]]) -> float:
    score = 0.0
    for row in deltas:
        direction = str(row.get("direction") or "")
        if direction == "flat":
            continue
        key = str(row.get("key") or "")
        critical = key in _CANDIDATE_BASELINE_CRITICAL_METRICS
        if direction == "improved":
            score += 6.0 if critical else 3.0
        elif direction == "regressed":
            score -= 14.0 if critical else 5.0
    return round(max(-60.0, min(24.0, score)), 3)


def _candidate_backtest_result_digest(result: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "ok",
        "reason",
        "strategy_id",
        "proposal_id",
        "package_dir",
        "backtest_ts",
        "verdict",
        "coverage_ok",
        "recommended_coverage_ok",
        "coverage_message",
        "total_return_pct",
        "max_drawdown_pct",
        "sharpe_ratio",
        "profit_factor",
        "win_rate_pct",
        "total_trades",
        "total_fees_usd",
        "total_slippage_usd",
        "primary_timeframe",
        "timeframes",
        "operator_summary",
        "operator_summary_text",
        "metrics_display",
    )
    return {key: result.get(key) for key in keys if key in result}


def _candidate_backtest_artifacts(paths: WorkspacePaths, result: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for kind, key in (
        ("metrics", "metrics_path"),
        ("report", "report_path"),
        ("trades", "trades_path"),
        ("config", "config_path"),
    ):
        value = result.get(key)
        if not value:
            continue
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = paths.root / path
        artifacts.append({
            "kind": f"backtest_{kind}",
            "title": path.name,
            "path": str(path),
            "evidence_ref": _file_evidence_ref(paths, path),
        })
    return artifacts


def _write_candidate_preview_package(
    root: Any,
    pkg: StrategyPackage,
    after_files: dict[str, str],
) -> Path:
    package_dir = Path(root) / "package"
    files = _candidate_preview_package_files(pkg, after_files)
    for rel, content in files.items():
        out = package_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(out, str(content))
    return package_dir


def _candidate_validation_preview_skipped(row: dict[str, Any]) -> dict[str, Any]:
    reason = "outside_top_k"
    if row.get("status") != "materialized":
        reason = "candidate_not_materialized"
    elif not row.get("materialized_files"):
        reason = "no_materialized_files"
    return {
        "version": "candidate_validation_preview_v1",
        "status": "skipped",
        "reason": reason,
        "candidate_id": row.get("candidate_id"),
        "candidate_index": row.get("index"),
        "requested_step_types": list(row.get("validation_types") or []),
        "executed_step_types": [],
        "deferred_step_types": list(row.get("validation_types") or []),
        "score_delta": 0.0,
        "preview_policy": {
            "top_k": _CANDIDATE_VALIDATION_PREVIEW_TOP_K,
        },
    }


def _candidate_preview_package_files(
    pkg: StrategyPackage,
    after_files: dict[str, str],
) -> dict[str, str]:
    files: dict[str, str] = {}
    for rel in pkg.files:
        path = pkg.root / rel
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    prefix = f"after/strategies/{pkg.strategy_id}/"
    for path, content in after_files.items():
        rel = str(path)
        if not rel.startswith(prefix):
            continue
        files[rel[len(prefix):]] = str(content)
    return files


def _candidate_artifact_slug(candidate_id: str, index: Any) -> str:
    token = _feedback_token(candidate_id)
    safe = "".join(ch for ch in token if ch.isalnum() or ch in {"_", "-"}).strip("_-")
    if not safe:
        safe = f"candidate_{int(index or 0) + 1}"
    return safe[:96]


def _file_evidence_ref(paths: WorkspacePaths, path: Any) -> str:
    p = path if hasattr(path, "relative_to") else None
    if p is not None:
        try:
            return f"file:{p.relative_to(paths.root).as_posix()}"
        except Exception:
            pass
    return f"file:{path}"


def _optimizer_validation_preview_summary(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    previews = [
        row.get("validation_preview")
        for row in evaluations
        if isinstance(row.get("validation_preview"), dict)
    ]
    previewed = [row for row in previews if row.get("status") in {"passed", "failed"}]
    failed = [row for row in previewed if row.get("status") == "failed"]
    passed = [row for row in previewed if row.get("status") == "passed"]
    return {
        "version": "candidate_validation_preview_summary_v1",
        "top_k": _CANDIDATE_VALIDATION_PREVIEW_TOP_K,
        "previewed_count": len(previewed),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "skipped_count": len(previews) - len(previewed),
        "executed_step_types": ["static_check"] if previewed else [],
        "policy": {
            "executes": "built_in_strategy_static_and_smoke_validator",
            "does_not_execute": ["candidate_shell_commands", "backtest", "live_or_shadow_runs"],
        },
    }


def _optimizer_backtest_preview_summary(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    previews = [
        row.get("backtest_preview")
        for row in evaluations
        if isinstance(row.get("backtest_preview"), dict)
    ]
    previewed = [row for row in previews if row.get("status") in {"passed", "failed", "no_data"}]
    failed = [row for row in previewed if row.get("status") == "failed"]
    passed = [row for row in previewed if row.get("status") == "passed"]
    no_data = [row for row in previewed if row.get("status") == "no_data"]
    return {
        "version": "candidate_backtest_preview_summary_v1",
        "top_k": _CANDIDATE_BACKTEST_PREVIEW_TOP_K,
        "previewed_count": len(previewed),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "no_data_count": len(no_data),
        "skipped_count": len(previews) - len(previewed),
        "policy": {
            "executes": "built_in_backtest_runner",
            "does_not_create_proposal": True,
            "does_not_enter_lineage": True,
        },
    }


def _score_tuning_candidate(
    *,
    output: dict[str, Any],
    accepted: list[dict[str, Any]],
    dropped: list[dict[str, Any]],
    warnings: list[str],
    materialized: list[str],
    unmaterialized: list[dict[str, Any]],
    blocked_reasons: list[str],
    validation_types: list[str],
    risk_flags: list[str],
    outcome_feedback: dict[str, Any],
) -> tuple[float, list[str], dict[str, Any]]:
    score = 0.0
    reasons: list[str] = []
    if output.get("_candidate_invalid"):
        score -= 100.0
        reasons.append("candidate_not_object")
    if accepted:
        score += 40.0 + min(len(accepted), 4) * 4.0
        reasons.append(f"accepted_changes:{len(accepted)}")
    else:
        score -= 30.0
        reasons.append("no_accepted_changes")
    if materialized:
        score += 35.0 + min(len(materialized), 4) * 5.0
        reasons.append(f"materialized_files:{len(materialized)}")
    elif accepted:
        score -= 20.0
        reasons.append("accepted_but_not_materialized")
    if unmaterialized:
        score -= min(len(unmaterialized), 5) * 8.0
        reasons.append(f"unmaterialized_changes:{len(unmaterialized)}")
    if dropped:
        score -= min(len(dropped), 5) * 5.0
        reasons.append(f"dropped_changes:{len(dropped)}")
    if warnings:
        score -= min(len(warnings), 5) * 2.0
    if blocked_reasons:
        score -= 45.0 + min(len(blocked_reasons), 5) * 8.0
        reasons.append("validation_blocked")
    if validation_types:
        score += 8.0
        reasons.append("validation_plan_present")
    validation_weights = {
        "unit_test": 10.0,
        "static_check": 10.0,
        "backtest": 18.0,
        "shadow_run": 6.0,
        "canary": 4.0,
        "manual_review": 2.0,
    }
    for step_type in _unique_strings(validation_types):
        weight = validation_weights.get(step_type, 0.0)
        if weight:
            score += weight
            reasons.append(f"validation_step:{step_type}")
    if risk_flags:
        penalty = min(len(risk_flags), 6) * 10.0
        if any(_risk_flag_is_severe(flag) for flag in risk_flags):
            penalty += 20.0
            reasons.append("severe_risk_flag")
        score -= penalty
        reasons.append(f"risk_flags:{len(risk_flags)}")
    if output.get("expected_effect"):
        score += 4.0
        reasons.append("expected_effect_present")
    if output.get("evidence"):
        score += 4.0
        reasons.append("candidate_evidence_present")
    if output.get("summary") or output.get("rationale"):
        score += 2.0
    if _candidate_reuse_mentions(output):
        score += 3.0
        reasons.append("candidate_reuse_context_present")
    feedback_delta, feedback_match = _score_optimizer_outcome_feedback(
        output=output,
        accepted=accepted,
        materialized=materialized,
        validation_types=validation_types,
        risk_flags=risk_flags,
        outcome_feedback=outcome_feedback,
    )
    if feedback_delta:
        score += feedback_delta
        if feedback_delta > 0:
            reasons.append("historical_outcome_feedback_positive")
        else:
            reasons.append("historical_outcome_feedback_negative")
    return score, reasons, feedback_match


def _risk_flag_is_severe(flag: str) -> bool:
    text = flag.lower()
    return any(
        token in text
        for token in (
            "live",
            "leverage",
            "position_size",
            "secret",
            "account",
            "limit",
            "unbounded",
        )
    )


def _candidate_reuse_mentions(output: dict[str, Any]) -> bool:
    for key in (
        "selected_gene_ids",
        "reused_gene_ids",
        "selected_capsule_ids",
        "reused_capsule_ids",
        "reused_assets",
        "gene_ids",
        "capsule_ids",
    ):
        value = output.get(key)
        if isinstance(value, (list, tuple, set)) and value:
            return True
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def _candidate_status(
    *,
    accepted: list[dict[str, Any]],
    materialized: list[str],
    blocked_reasons: list[str],
    invalid: bool,
) -> str:
    if invalid:
        return "invalid"
    if blocked_reasons:
        return "blocked"
    if materialized:
        return "materialized"
    if accepted:
        return "advisory"
    return "empty"


def _candidate_report(row: dict[str, Any]) -> dict[str, Any]:
    accepted = [
        _change_target_digest(change)
        for change in row.get("accepted", [])
        if isinstance(change, dict)
    ]
    dropped = [
        {
            "target": _change_target_digest(raw.get("entry") or {}),
            "reason": raw.get("reason"),
            "blocked_reasons": raw.get("blocked_reasons") or [],
        }
        for raw in row.get("dropped", [])
        if isinstance(raw, dict)
    ][:8]
    return {
        "candidate_id": row.get("candidate_id"),
        "index": row.get("index"),
        "score": row.get("score"),
        "status": row.get("status"),
        "summary": str((row.get("output") or {}).get("summary") or "")[:240],
        "accepted_count": len(row.get("accepted") or []),
        "dropped_count": len(row.get("dropped") or []),
        "materialized_count": len(row.get("materialized_files") or []),
        "unmaterialized_count": len(row.get("unmaterialized_changes") or []),
        "accepted_targets": accepted[:8],
        "dropped": dropped,
        "materialized_files": list(row.get("materialized_files") or [])[:8],
        "validation_status": row.get("validation_status"),
        "validation_types": list(row.get("validation_types") or [])[:8],
        "blocked_reasons": list(row.get("blocked_reasons") or [])[:8],
        "risk_flags": list(row.get("risk_flags") or [])[:8],
        "reasons": list(row.get("reasons") or [])[:12],
        "warnings": list(row.get("warnings") or [])[:8],
        "outcome_feedback": row.get("outcome_feedback") or {},
        "asset_candidate": row.get("asset_candidate") or {},
        "validation_preview": _candidate_validation_preview_report(
            row.get("validation_preview") if isinstance(row.get("validation_preview"), dict) else {},
        ),
        "backtest_preview": _candidate_backtest_preview_report(
            row.get("backtest_preview") if isinstance(row.get("backtest_preview"), dict) else {},
        ),
    }


def _candidate_validation_preview_report(preview: dict[str, Any]) -> dict[str, Any]:
    if not preview:
        return {}
    validation = preview.get("validation") if isinstance(preview.get("validation"), dict) else {}
    return {
        "version": preview.get("version"),
        "status": preview.get("status"),
        "reason": preview.get("reason"),
        "score_delta": preview.get("score_delta"),
        "executed_step_types": list(preview.get("executed_step_types") or [])[:8],
        "deferred_step_types": list(preview.get("deferred_step_types") or [])[:8],
        "requested_step_types": list(preview.get("requested_step_types") or [])[:8],
        "blocked_reasons": list(preview.get("blocked_reasons") or [])[:8],
        "warning_count": preview.get("warning_count"),
        "blocker_count": preview.get("blocker_count"),
        "evidence_refs": list(preview.get("evidence_refs") or [])[:8],
        "validation": {
            "ok": validation.get("ok"),
            "blockers": list(validation.get("blockers") or [])[:6],
            "warnings": list(validation.get("warnings") or [])[:6],
        } if validation else {},
        "preview_policy": preview.get("preview_policy") or {},
    }


def _candidate_backtest_preview_report(preview: dict[str, Any]) -> dict[str, Any]:
    if not preview:
        return {}
    return {
        "version": preview.get("version"),
        "status": preview.get("status"),
        "reason": preview.get("reason"),
        "score_delta": preview.get("score_delta"),
        "preset": preview.get("preset"),
        "allow_mock": preview.get("allow_mock"),
        "blocked_reasons": list(preview.get("blocked_reasons") or [])[:8],
        "evidence_refs": list(preview.get("evidence_refs") or [])[:8],
        "backtest_result": preview.get("backtest_result") or {},
        "baseline_comparison": preview.get("baseline_comparison") or {},
        "artifacts": list(preview.get("artifacts") or [])[:8],
        "preview_policy": preview.get("preview_policy") or {},
    }


def _change_target_digest(change: Any) -> str:
    if not isinstance(change, dict):
        return str(change or "")[:120]
    return str(change.get("file") or change.get("target") or "")[:120]


def _optimizer_metadata(report: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(report, dict) or not report:
        return {}
    feedback = report.get("outcome_feedback") if isinstance(report.get("outcome_feedback"), dict) else {}
    return {
        "version": report.get("version"),
        "candidate_count": report.get("candidate_count"),
        "evaluated_count": report.get("evaluated_count"),
        "selected_candidate_id": report.get("selected_candidate_id"),
        "selected_index": report.get("selected_index"),
        "selected_score": report.get("selected_score"),
        "truncated": bool(report.get("truncated")),
        "outcome_feedback_samples": feedback.get("sample_count"),
        "outcome_feedback_positive_samples": feedback.get("positive_samples"),
        "outcome_feedback_negative_samples": feedback.get("negative_samples"),
        "outcome_feedback_candidate_decision_samples": feedback.get("candidate_decision_samples"),
    }


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


def _materialize_strategy_tuning_after_files(
    pkg: StrategyPackage,
    accepted: list[dict[str, Any]],
) -> tuple[dict[str, str], list[str], list[dict[str, Any]]]:
    """Convert safe tuning changes into proposal ``after/`` files.

    The first implementation intentionally supports only full-file content and
    structured YAML. Free-form patch text remains advisory until a stricter
    patch parser and conflict model exist.
    """

    files: dict[str, str] = {}
    materialized: list[str] = []
    unmaterialized: list[dict[str, Any]] = []
    for entry in accepted:
        target = str(entry.get("file") or entry.get("target") or "").strip()
        rel, reason = _normalise_strategy_tuning_target(pkg, target)
        if reason:
            unmaterialized.append({"entry": entry, "reason": reason})
            continue
        assert rel is not None
        content, content_reason = _materialized_change_content(entry)
        if content_reason:
            unmaterialized.append({"entry": entry, "target": rel, "reason": content_reason})
            continue
        workspace_path = f"strategies/{pkg.strategy_id}/{rel}"
        files[f"after/{workspace_path}"] = content
        materialized.append(workspace_path)
    return files, materialized, unmaterialized


def _normalise_strategy_tuning_target(
    pkg: StrategyPackage,
    target: str,
) -> tuple[str | None, str | None]:
    raw = (target or "").replace("\\", "/").strip()
    if not raw:
        return None, "missing file/target"
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts:
        return None, "unsafe_target_path"
    rel_posix = rel.as_posix()
    if rel_posix in {"", "."}:
        return None, "unsafe_target_path"
    workspace_path = f"strategies/{pkg.strategy_id}/{rel_posix}"
    if is_protected(workspace_path) or is_protected(rel_posix):
        return None, "protected_target"
    return rel_posix, None


def _materialized_change_content(entry: dict[str, Any]) -> tuple[str, str | None]:
    kind = str(entry.get("kind") or entry.get("type") or "").strip().lower()
    target = str(entry.get("file") or entry.get("target") or "").strip().lower()
    yaml_target = target.endswith((".yml", ".yaml")) or kind in {
        "strategy_yml",
        "strategy_yaml",
        "yaml",
        "config",
    }
    if kind == "advisory":
        return "", "advisory_only"
    if isinstance(entry.get("config_after"), dict):
        if not yaml_target:
            return "", "config_after_requires_yaml_target"
        return yaml_io.dumps(entry["config_after"]), None
    yaml_after = entry.get("yaml_after")
    if isinstance(yaml_after, str) and yaml_after.strip():
        if not yaml_target:
            return "", "yaml_after_requires_yaml_target"
        try:
            parsed = yaml_io.loads(yaml_after, default=None)
        except Exception:
            return "", "invalid_yaml_after"
        if not isinstance(parsed, dict):
            return "", "yaml_after_not_mapping"
        return yaml_io.dumps(parsed), None
    for key in ("after_content", "content"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value, None
    if yaml_target:
        return "", "missing_yaml_after"
    return "", "missing_after_content"


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

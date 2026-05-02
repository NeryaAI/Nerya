"""Strategy SDK wrapper.

this SDK speaks two dialects:

* **Legacy review/history** — :meth:`history`, :meth:`explain_trade`,
  :meth:`review` still exist for backwards compatibility with the
  dashboard's *Strategy History* panel. They no longer go through a
  ``trading_skill`` (the legacy skill was removed); instead they read
  the strategy-history ledger directly via
  :func:`nerya.strategy_history.store.read_ledger`.
* **Runtime** — :meth:`generate_proposal`, :meth:`validate`,
  :meth:`promote`, :meth:`run_tick`, :meth:`schedule`,
  :meth:`pause`, :meth:`resume`, :meth:`status`,
  :meth:`runs`, :meth:`kill_switch` drive the new package lifecycle.
  They wrap :mod:`nerya.evolution.strategy_code_generator`,
  :mod:`nerya.strategies.runner`, :mod:`nerya.strategies.validator`,
  and :mod:`nerya.strategies.scheduler_bridge`.

Caller responsibilities
-----------------------
This SDK runs in-process inside the workspace; it does not enforce
RBAC. Callers (HTTP routes, CLI commands, MCP adapters) must layer
their own auth on top before exposing methods that mutate workspace
state — at minimum, ``promote`` (applies the proposal) and
``kill_switch`` (halts a strategy mid-flight) should require
operator confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..core.config import Config
from ..core.errors import NeryaError, TradingError
from ..evolution.patch_proposal import set_state
from ..evolution.promotion import apply_proposal
from ..evolution.strategy_code_generator import (
    StrategyCodeGenerator,
    StrategyGenerationRequest,
)
from ..evolution.strategy_tuning_generator import (
    StrategyTuningGenerationRequest,
    StrategyTuningGenerator,
)
from ..skills.kernel import SkillKernel
from ..strategies.evolution import StrategyEvolutionRunner
from ..strategies.package import load_package, load_packages
from ..strategies.performance import build_snapshot
from ..strategies.proposal_files import read_proposal_strategy_files
from ..strategies.runner import StrategyRunner
from ..strategies.scheduler_bridge import (
    apply_strategy_schedules,
    remove_strategy_schedules,
    set_strategy_schedule_enabled,
    trading_schedule_id,
    tuning_schedule_id,
)
from ..strategies.state import StrategyKillSwitch, StrategyRunStore
from ..strategies.validator import (
    validate_proposal_files,
    validate_strategy_package,
)
from ..strategy_history import store as history_store
from ..triggers.schedule import load_schedules


@dataclass
class StrategyAPI:
    """In-process strategy SDK used by CLI / HTTP routes / MCP adapters."""

    config: Config
    skills: SkillKernel

    # ------------------------------------------------------------------
    # Legacy convenience — read-only ledger access
    # ------------------------------------------------------------------

    def history(self, strategy_id: str, *, limit: int = 20) -> dict[str, Any]:
        """Return tail rows from each strategy-history ledger.

        Replaces the old ``self.skills.call("trading", "get_strategy_history", ...)``
        path: that legacy skill was removed during the cleanup, so we
        read the ledgers directly.
        """

        out: dict[str, Any] = {"strategy_id": strategy_id, "ledgers": {}}
        for name in (
            "triggers", "intents", "risk", "orders", "fills",
            "messages", "reviews", "decisions", "agent_tasks",
        ):
            try:
                rows = history_store.read_ledger(self.config.paths, strategy_id, name)
            except Exception:
                rows = []
            out["ledgers"][name] = {"count": len(rows), "tail": rows[-limit:]}
        return out

    def agent_tasks(self, strategy_id: str, *, limit: int = 50) -> dict[str, Any]:
        """List strategy Agent task executions from the history ledger."""

        try:
            rows = history_store.read_ledger(self.config.paths, strategy_id, "agent_tasks")
        except Exception:
            rows = []
        entries: list[dict[str, Any]] = []
        for row in rows[-limit:]:
            task = dict(row.get("task") or {})
            entries.append(
                {
                    "session_id": row.get("session_id") or task.get("session_id"),
                    "task_id": task.get("task_id"),
                    "status": task.get("status"),
                    "trigger_event_id": task.get("trigger_event_id"),
                    "turn_id": task.get("turn_id"),
                    "prompt_artifact": task.get("prompt_artifact"),
                    "prompt_chars": task.get("prompt_chars"),
                    "metadata": dict(task.get("metadata") or {}),
                    "task": task,
                }
            )
        return {"strategy_id": strategy_id, "count": len(rows), "tasks": entries}

    def agent_task(
        self,
        strategy_id: str,
        task_id: str,
        *,
        include_prompt: bool = True,
    ) -> dict[str, Any]:
        """Return one Agent task with prompt artifact and session profile."""

        try:
            rows = history_store.read_ledger(self.config.paths, strategy_id, "agent_tasks")
        except Exception:
            rows = []
        match: dict[str, Any] | None = None
        task: dict[str, Any] | None = None
        for row in reversed(rows):
            candidate = dict(row.get("task") or {})
            if str(candidate.get("task_id") or "") == task_id:
                match = row
                task = candidate
                break
        if match is None or task is None:
            return {"ok": False, "error": "agent_task_not_found", "task_id": task_id}

        out: dict[str, Any] = {
            "ok": True,
            "strategy_id": strategy_id,
            "task_id": task_id,
            "entry": match,
            "task": task,
        }
        session_id = match.get("session_id") or task.get("session_id")
        if session_id:
            from ..agent.session import SessionStore

            session = SessionStore(self.config.paths.root).load(str(session_id))
            if session is not None:
                out["session"] = {
                    "session_id": session.session_id,
                    "strategy_id": session.strategy_id,
                    "turn_ids": list(session.turn_ids),
                    "invoked_skills": list(session.invoked_skills),
                    "last_action": session.last_action,
                    "profile": session.meta.get("strategy_agent_profile"),
                }

        rel = str(task.get("prompt_artifact") or "").strip()
        if include_prompt and rel:
            root = self.config.paths.strategy(strategy_id).resolve()
            path = (root / rel).resolve()
            if path == root or root not in path.parents:
                out["prompt_error"] = "prompt_artifact_outside_strategy_root"
            elif path.is_file():
                out["prompt"] = path.read_text(encoding="utf-8")
                out["prompt_path"] = str(path)
            else:
                out["prompt_error"] = "prompt_artifact_missing"
        return out

    def explain_trade(self, strategy_id: str, order_id: str) -> dict[str, Any]:
        """Locate an order row + the surrounding decision/risk rows.

        Older callers expected a ``strategy_review`` skill to assemble
        an explanation; that skill is gone, so we compose the same
        bundle from the strategy-history ledger and return it as-is.
        Front-ends that want narrative text should pipe this through
        an LLM tool themselves.
        """

        ledgers = self.history(strategy_id, limit=200)["ledgers"]
        order_match: Optional[dict[str, Any]] = None
        for row in ledgers.get("orders", {}).get("tail", []) or ():
            payload = row.get("payload") or {}
            if str(payload.get("order_id") or "") == order_id:
                order_match = row
                break
        risk_rows = ledgers.get("risk", {}).get("tail", []) or []
        intent_rows = ledgers.get("intents", {}).get("tail", []) or []
        decision_rows = ledgers.get("decisions", {}).get("tail", []) or []
        return {
            "strategy_id": strategy_id,
            "order_id": order_id,
            "order": order_match,
            "context": {
                "intents": intent_rows[-5:],
                "risk": risk_rows[-5:],
                "decisions": decision_rows[-5:],
            },
        }

    def review(
        self,
        strategy_id: str,
        session_id: str,
        *,
        stage: str = "immediate",
    ) -> dict[str, Any]:
        """Compatibility shim over the old ``strategy_review`` skill.

        We don't try to recreate the LLM-driven review here; instead we
        return the structural bundle the dashboard panel needs and let
        the front-end (or an explicit tool call) compose the narrative.
        """

        ledgers = self.history(strategy_id, limit=200)["ledgers"]
        bundle: dict[str, Any] = {
            "strategy_id": strategy_id,
            "session_id": session_id,
            "stage": stage,
            "events": {},
        }
        for name, contents in ledgers.items():
            rows = [
                row
                for row in (contents.get("tail") or ())
                if (row.get("session_id") == session_id)
            ]
            bundle["events"][name] = rows
        return bundle

    # ------------------------------------------------------------------
    # Runtime — package lifecycle
    # ------------------------------------------------------------------

    def list_packages(self) -> list[dict[str, Any]]:
        """Return manifest summaries for every promoted strategy package."""

        return [
            {
                "strategy_id": pkg.strategy_id,
                "title": pkg.manifest.title,
                "mode": pkg.manifest.mode,
                "package_hash": pkg.content_hash,
                "markets": list(pkg.manifest.markets),
                "accounts": list(pkg.manifest.accounts),
                "subagents": list(pkg.manifest.subagents),
            }
            for pkg in load_packages(self.config.paths)
        ]

    def get_package(self, strategy_id: str) -> dict[str, Any]:
        pkg = load_package(self.config.paths, strategy_id)
        return {
            "strategy_id": pkg.strategy_id,
            "manifest": pkg.manifest.asdict(),
            "package_hash": pkg.content_hash,
            "files": list(pkg.files),
        }

    def generate_proposal(
        self,
        request: StrategyGenerationRequest,
        *,
        validate: bool = True,
    ) -> dict[str, Any]:
        generator = StrategyCodeGenerator(self.config.paths)
        result = generator.generate(
            request, validate=validate, create_proposal_record=True
        )
        return {
            "strategy_id": request.strategy_id,
            "proposal_id": result.proposal.id if result.proposal else None,
            "validation": (
                result.validation.asdict() if result.validation is not None else None
            ),
            "files": list(result.files.keys()),
        }

    def validate(
        self,
        strategy_id: Optional[str] = None,
        *,
        proposal_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if proposal_id:
            sid, files = self._read_proposal_files(proposal_id)
            if not files:
                raise NeryaError(
                    f"proposal {proposal_id!r} has no after/strategies/* tree"
                )
            return validate_proposal_files(
                strategy_id=strategy_id or sid or proposal_id, files=files
            ).asdict()
        if not strategy_id:
            raise NeryaError("strategy_id or proposal_id is required")
        return validate_strategy_package(self.config.paths, strategy_id).asdict()

    def promote(self, proposal_id: str, *, note: str = "") -> dict[str, Any]:
        """Approve + apply a strategy package proposal.

        Refuses to promote when validation reports any blockers.
        """

        sid, files = self._read_proposal_files(proposal_id)
        if not files:
            raise NeryaError(
                f"proposal {proposal_id!r} not found or has no strategy files"
            )
        validation = validate_proposal_files(
            strategy_id=sid or proposal_id, files=files
        )
        if not validation.ok:
            return {
                "ok": False,
                "reason": "validation_blockers",
                "proposal_id": proposal_id,
                "strategy_id": sid,
                "validation": validation.asdict(),
            }
        set_state(
            self.config.paths,
            proposal_id,
            "approved",
            note=note or "approved via SDK",
        )
        outcome = apply_proposal(self.config.paths, proposal_id)
        # Sync schedules so the freshly-promoted package starts firing.
        try:
            pkg = load_package(self.config.paths, sid) if sid else None
            if pkg is not None:
                apply_strategy_schedules(self.config.paths, pkg)
        except Exception:
            outcome.setdefault("warnings", []).append("schedule_sync_failed")
        return {
            "ok": bool(outcome.get("ok")),
            "proposal_id": proposal_id,
            "strategy_id": sid,
            "validation": validation.asdict(),
            "promotion": outcome,
        }

    def run_tick(
        self,
        strategy_id: str,
        *,
        trigger_payload: Optional[dict[str, Any]] = None,
        trigger_event_id: Optional[str] = None,
        operator: Optional[str] = None,
        note: str = "",
        mode_override: Optional[str] = None,
    ) -> dict[str, Any]:
        runner = StrategyRunner(config=self.config, skills=self.skills)
        record = runner.run_tick(
            strategy_id,
            trigger_payload=trigger_payload,
            trigger_event_id=trigger_event_id,
            operator=operator,
            note=note,
            mode_override=mode_override,
        )
        return record.asdict()

    def runs(
        self,
        strategy_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        store = StrategyRunStore(self.config.paths, strategy_id)
        rows = [r.asdict() for r in store.list(limit=limit)]
        return {"strategy_id": strategy_id, "count": len(rows), "runs": rows}

    # ------------------------------------------------------------------
    # Runtime — schedules
    # ------------------------------------------------------------------

    def schedule(self, strategy_id: str) -> dict[str, Any]:
        """Re-install both trading + tuning schedules from the manifest."""

        package = load_package(self.config.paths, strategy_id)
        result = apply_strategy_schedules(self.config.paths, package)
        return {
            "strategy_id": strategy_id,
            "trading_id": result.trading_id,
            "tuning_id": result.tuning_id,
            "added": result.added,
            "updated": result.updated,
            "removed": result.removed,
        }

    def schedule_status(self, strategy_id: str) -> dict[str, Any]:
        existing = list(load_schedules(self.config.paths))
        trading_id = trading_schedule_id(strategy_id)
        tuning_id = tuning_schedule_id(strategy_id)
        out: dict[str, Any] = {
            "strategy_id": strategy_id,
            "trading": None,
            "tuning": None,
        }
        for entry in existing:
            if entry.id == trading_id:
                out["trading"] = _entry_to_dict(entry)
            elif entry.id == tuning_id:
                out["tuning"] = _entry_to_dict(entry)
        return out

    def pause(self, strategy_id: str) -> dict[str, Any]:
        state = set_strategy_schedule_enabled(
            self.config.paths, strategy_id, trading=False, tuning=False
        )
        return {"strategy_id": strategy_id, **state}

    def resume(self, strategy_id: str) -> dict[str, Any]:
        state = set_strategy_schedule_enabled(
            self.config.paths, strategy_id, trading=True, tuning=True
        )
        return {"strategy_id": strategy_id, **state}

    def remove_schedules(self, strategy_id: str) -> dict[str, Any]:
        removed = remove_strategy_schedules(self.config.paths, strategy_id)
        return {"strategy_id": strategy_id, "removed": removed}

    def kill_switch(
        self,
        strategy_id: str,
        *,
        action: str = "get",
        reason: str = "",
        by: str = "operator",
    ) -> dict[str, Any]:
        ks = StrategyKillSwitch(self.config.paths, strategy_id)
        if action == "get":
            state = ks.get()
        elif action == "assert":
            if not reason.strip():
                raise NeryaError("assert requires a non-empty reason")
            state = ks.assert_(reason=reason, by=by)
        elif action == "clear":
            state = ks.clear(by=by)
        else:
            raise NeryaError(f"unknown action {action!r}; use get|assert|clear")
        return {
            "strategy_id": strategy_id,
            "action": action,
            "state": state.asdict(),
        }

    def status(self, strategy_id: str) -> dict[str, Any]:
        """Aggregate status — manifest + schedules + kill switch + last run."""

        try:
            pkg = load_package(self.config.paths, strategy_id)
        except TradingError as exc:
            return {"strategy_id": strategy_id, "ok": False, "error": str(exc)}
        ks_state = StrategyKillSwitch(self.config.paths, strategy_id).get()
        runs = StrategyRunStore(self.config.paths, strategy_id).list(limit=1)
        last_run = runs[0].asdict() if runs else None
        return {
            "ok": True,
            "strategy_id": strategy_id,
            "manifest": pkg.manifest.asdict(),
            "package_hash": pkg.content_hash,
            "schedules": self.schedule_status(strategy_id),
            "kill_switch": ks_state.asdict(),
            "last_run": last_run,
        }

    # ------------------------------------------------------------------
    # Self-evolution / tuning
    # ------------------------------------------------------------------

    @property
    def tuning(self) -> "StrategyTuningAPI":
        """Lazy accessor for the tuning sub-namespace."""

        if getattr(self, "_tuning_api", None) is None:
            object.__setattr__(self, "_tuning_api", StrategyTuningAPI(self))
        return self._tuning_api  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_proposal_files(
        self, proposal_id: str
    ) -> tuple[Optional[str], dict[str, str]]:
        return read_proposal_strategy_files(self.config.paths, proposal_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    """Render a :class:`ScheduleEntry` to a JSON-friendly dict.

    Mirrors :func:`nerya.triggers.schedule.save_schedules` so HTTP /
    CLI consumers see the same shape that lives in
    ``schedules.yml``.
    """

    out: dict[str, Any] = {
        "id": entry.id,
        "kind": entry.kind,
        "target": entry.target,
        "enabled": bool(entry.enabled),
    }
    if entry.every_seconds is not None:
        out["every_seconds"] = int(entry.every_seconds)
    if entry.cron:
        out["cron"] = entry.cron
    if entry.starts_at:
        out["starts_at"] = entry.starts_at
    if entry.ends_at:
        out["ends_at"] = entry.ends_at
    if entry.strategy_id:
        out["strategy_id"] = entry.strategy_id
    if entry.payload:
        out["payload"] = dict(entry.payload)
    return out


# ---------------------------------------------------------------------------
# Tuning sub-namespace
# ---------------------------------------------------------------------------


@dataclass
class StrategyTuningAPI:
    """Per-strategy self-evolution surface.

    Mirrors the trading-side runtime: ``generate`` enables the loop on
    an existing package, ``schedule``/``pause``/``resume`` flip the
    tuning cron, ``run`` executes one cycle (with ``dry_run`` to
    inspect a recommendation without writing a proposal), and
    ``status`` returns a one-stop view for the dashboard.
    """

    parent: StrategyAPI

    @property
    def config(self) -> Config:
        return self.parent.config

    @property
    def skills(self) -> SkillKernel:
        return self.parent.skills

    def generate(
        self,
        strategy_id: str,
        *,
        prompt: str = "",
        cron: str = "0 */6 * * *",
        every_seconds: Optional[int] = None,
        objectives: Optional[list[str]] = None,
        require_backtest: bool = True,
        require_shadow_run: bool = False,
    ) -> dict[str, Any]:
        request = StrategyTuningGenerationRequest(
            strategy_id=strategy_id,
            tuning_prompt=prompt or "",
            cron=cron,
            every_seconds=every_seconds,
            objectives=tuple(objectives or ("risk_adjusted_return",)),
            require_backtest=require_backtest,
            require_shadow_run=require_shadow_run,
        )
        result = StrategyTuningGenerator(self.config.paths).generate(request)
        return {
            "ok": True,
            "strategy_id": strategy_id,
            "proposal_id": result.proposal.id if result.proposal else None,
            "files": list(result.files.keys()),
        }

    def schedule(self, strategy_id: str) -> dict[str, Any]:
        """Re-install only the tuning schedule from the manifest."""

        package = load_package(self.config.paths, strategy_id)
        from ..strategies.scheduler_bridge import apply_strategy_schedules

        result = apply_strategy_schedules(self.config.paths, package)
        return {
            "ok": True,
            "strategy_id": strategy_id,
            "tuning_id": result.tuning_id,
            "tuning_added": result.tuning_id in result.added,
            "tuning_updated": result.tuning_id in result.updated,
        }

    def pause(self, strategy_id: str) -> dict[str, Any]:
        state = set_strategy_schedule_enabled(
            self.config.paths, strategy_id, trading=None, tuning=False
        )
        return {"ok": True, "strategy_id": strategy_id, **state}

    def resume(self, strategy_id: str) -> dict[str, Any]:
        state = set_strategy_schedule_enabled(
            self.config.paths, strategy_id, trading=None, tuning=True
        )
        return {"ok": True, "strategy_id": strategy_id, **state}

    def run(
        self,
        strategy_id: str,
        *,
        dry_run: bool = False,
        operator: Optional[str] = None,
        note: str = "",
        trigger_event_id: Optional[str] = None,
    ) -> dict[str, Any]:
        runner = StrategyEvolutionRunner(config=self.config, skills=self.skills)
        result = runner.run_once(
            strategy_id,
            operator=operator,
            note=note,
            dry_run=dry_run,
            trigger_event_id=trigger_event_id,
        )
        return result.asdict()

    def status(
        self, strategy_id: str, *, lookback_runs: int = 200
    ) -> dict[str, Any]:
        try:
            pkg = load_package(self.config.paths, strategy_id)
        except TradingError as exc:
            return {"strategy_id": strategy_id, "ok": False, "error": str(exc)}
        snapshot = build_snapshot(
            self.config.paths,
            strategy_id,
            lookback_runs=lookback_runs,
            package=pkg,
        )
        return {
            "ok": True,
            "strategy_id": strategy_id,
            "tuning": pkg.manifest.tuning.asdict(),
            "schedule": self.parent.schedule_status(strategy_id).get("tuning"),
            "snapshot": snapshot.asdict(),
            "pending_proposals": [
                {
                    "id": p.id,
                    "summary": p.summary,
                    "state": p.state,
                    "ts": p.ts,
                }
                for p in list_proposals(self.config.paths)
                if p.kind == "strategy_tuning_proposal"
                and (p.target or "").endswith(strategy_id)
                and p.state in ("draft", "pending_review", "approved")
            ],
        }

    def snapshot(
        self, strategy_id: str, *, lookback_runs: int = 200
    ) -> dict[str, Any]:
        snap = build_snapshot(
            self.config.paths, strategy_id, lookback_runs=lookback_runs
        )
        return snap.asdict()


__all__ = ["StrategyAPI", "StrategyTuningAPI"]

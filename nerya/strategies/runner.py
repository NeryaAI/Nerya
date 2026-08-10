"""StrategyRunner — single-tick executor for agent-generated strategies.

The runner is the only place that:

* loads a :class:`~nerya.strategies.package.StrategyPackage` by id
  (via :func:`nerya.strategies.package.load_package`);
* builds a :class:`~nerya.strategies.context.StrategyContext` for
  the run;
* imports the package's ``main.py`` entrypoint *with the package
  root prepended to* :data:`sys.path` (so strategies can split
  helpers across multiple files);
* enforces *runner-side* safety rails (mode gate, kill switch,
  wall-clock timeout);
* writes the canonical run record + the strategy-history rows the
  dashboard reads.

Why a hand-rolled isolation layer
---------------------------------
We cannot simply ``importlib.import_module`` a strategy because:

* Multiple strategies may declare ``main`` modules with the same
  filename — letting them collide in the global module cache makes
  hot-promotion impossible.
* Tests / the agent regenerate the entrypoint repeatedly within
  one process; we need each run to bind the *current* package
  contents, not whatever module was first imported.

So the runner imports ``main.py`` via :func:`importlib.util.spec_from_file_location`
under a uniquely-namespaced module name (``_nerya_strategy.<id>.<hash[:8]>``)
and discards the cached entry afterwards. This keeps reloads
deterministic without breaking ``import other_local_helper`` style
relative imports inside the package.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from ..core import jsonl
from ..core.config import Config
from ..core.errors import NeryaError, TradingError
from ..core.ids import session_id as _new_session_id
from ..core.time import now_iso
from ..strategy_history import store as _history_store
from .context import (
    NewsFetcher,
    StrategyClock,
    StrategyContext,
    StrategyRuntimeError,
    build_strategy_context,
)
from .package import StrategyPackage, load_package
from .result import StrategyResult, StrategyResultStatus
from .state import (
    StrategyKillSwitch,
    StrategyRunRecord,
    StrategyRunStore,
    new_run_id,
)


_LOG = logging.getLogger(__name__)

_VALID_MODES: frozenset[str] = frozenset({"paper", "shadow", "live"})
_RETURN_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in StrategyResultStatus)

# Static-scan verdict cache keyed by package content hash. A package is
# immutable per hash, so one AST scan per (process, version) is enough.
_STATIC_SCAN_CACHE: dict[str, list[dict[str, Any]]] = {}


def _normalise_return_status(raw: dict[str, Any]) -> str:
    value = raw.get("status")
    if value is None:
        value = raw.get("decision")
    if value is None:
        value = raw.get("action")
    return str(value or "").strip().lower()


def _metadata_from_return(raw: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(raw.get("metadata") or {}) if isinstance(raw.get("metadata"), dict) else {}
    summary = {
        k: v
        for k, v in raw.items()
        if k not in {"status", "decision", "action", "reason", "metadata", "execution"}
    }
    if summary:
        metadata["return_summary"] = summary
    if "decision" in raw:
        metadata["decision"] = raw.get("decision")
    if "action" in raw:
        metadata["action"] = raw.get("action")
    return metadata


# ---------------------------------------------------------------------------
# Wall-clock timeout helper
# ---------------------------------------------------------------------------


class StrategyTimeoutError(StrategyRuntimeError):
    """Raised when a strategy's ``run`` exceeds ``policy.max_run_seconds``."""


@contextmanager
def _wall_clock_timeout(seconds: float) -> Iterator[None]:
    """Cooperative wall-clock guard.

    We can't use ``signal.SIGALRM`` portably (Windows) and threads
    can't preempt CPython, so we run the strategy in a worker
    thread and wait on a timeout. The strategy *won't* be hard-
    killed; if it ignores cooperative cancellation we let it run
    in the background and surface a timeout to the caller. This
    matches The runtime' approach and is the same trade-off that the
    rest of the agent loop makes.

    The "context manager" shape is preserved so callers read
    naturally (``with _wall_clock_timeout(60): ...``); on enter we
    just record the deadline and on exit raise if elapsed exceeded
    it. The threaded version is implemented in :func:`_run_with_timeout`
    below; this guard is for inline use during fast checks.
    """

    deadline = time.monotonic() + max(0.0, float(seconds or 0.0))
    try:
        yield
    finally:
        if seconds and time.monotonic() > deadline:
            raise StrategyTimeoutError(
                f"strategy exceeded max_run_seconds={seconds}"
            )


def _run_with_timeout(
    fn: Callable[[], Any],
    *,
    seconds: float,
) -> Any:
    """Run ``fn`` in a worker thread with a wall-clock deadline."""

    if seconds <= 0:
        return fn()

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 — rethrow shape preserved
            box["exc"] = exc

    t = threading.Thread(target=_worker, name="nerya-strategy", daemon=True)
    t.start()
    t.join(timeout=float(seconds))
    if t.is_alive():
        raise StrategyTimeoutError(f"strategy exceeded max_run_seconds={seconds}")
    if "exc" in box:
        raise box["exc"]
    return box.get("result")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class StrategyRunInputs:
    """What the runner records as *inputs* for a tick.

    Decoupled from the trigger payload so the strategy receives a
    flat ``payload`` dict but we still capture metadata (mode at
    runtime, manifest hash, trigger event id) for replay.
    """

    mode: str
    package_hash: str
    trigger_event_id: Optional[str] = None
    trigger_payload: dict[str, Any] = field(default_factory=dict)
    operator: Optional[str] = None
    note: str = ""


@dataclass
class StrategyRunOutputs:
    """What the runner records as *outputs* for a tick.

    Mirrors :class:`StrategyResult`'s shape but explicitly marks
    ``llm_calls`` / ``subagent_calls`` so dashboards can surface
    cost without re-parsing the audit log.
    """

    result: dict[str, Any]
    llm_calls: int = 0
    subagent_calls: int = 0


@dataclass
class StrategyRunner:
    """Single-tick executor.

    Attributes
    ----------
    config:
        Workspace ``Config``. The runner reaches into it only via
        the package loader and the trading kernel inside
        ``ctx.trading``; strategies never see it directly.
    skills:
        Optional ``SkillKernel`` used to back ``ctx.subagents``.
        Falls back to a fresh kernel scoped to the workspace when
        omitted.
    news_fetchers:
        Optional ``source_id -> fetcher`` map injected into
        ``ctx.news``. Operators register these from workspace
        config; tests inject deterministic fixtures here.
    connector_registry:
        Optional shared :class:`~nerya.connectors.registry.ConnectorRegistry`.
        When ``None`` the context lazily builds one. Long-lived
        runners (the trigger schedule loop) should pass a shared
        registry so connectors don't get re-instantiated per tick.
    """

    config: Config
    skills: Any = None
    news_fetchers: dict[str, NewsFetcher] = field(default_factory=dict)
    connector_registry: Any = None
    tool_registry: Any = None
    executor: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_tick(
        self,
        strategy_id: str,
        *,
        trigger_payload: Optional[dict[str, Any]] = None,
        trigger_event_id: Optional[str] = None,
        operator: Optional[str] = None,
        note: str = "",
        mode_override: Optional[str] = None,
        clock: Optional[StrategyClock] = None,
        run_id: Optional[str] = None,
    ) -> StrategyRunRecord:
        """Run one strategy tick and return its persisted record.

        Errors are caught and converted into a :class:`StrategyResult`
        with ``status='error'``; the runner never lets a strategy
        crash the caller. The exception is :class:`TradingError` /
        :class:`NeryaError` raised before the strategy is even
        loaded (bad strategy id, bad mode override) — those bubble
        up to the operator so they can fix the configuration.
        """

        package = load_package(self.config.paths, strategy_id)
        manifest = package.manifest
        mode = self._resolve_mode(manifest.mode, mode_override)

        rid = run_id or new_run_id()
        sid = _new_session_id()
        started_at = now_iso()
        t0 = time.monotonic()

        kill = StrategyKillSwitch(self.config.paths, strategy_id).get()
        if kill.asserted:
            return self._finalize_record(
                package=package,
                run_id=rid,
                session_id=sid,
                started_at=started_at,
                t0=t0,
                inputs=StrategyRunInputs(
                    mode=mode,
                    package_hash=package.content_hash,
                    trigger_event_id=trigger_event_id,
                    trigger_payload=dict(trigger_payload or {}),
                    operator=operator,
                    note=note,
                ),
                result=StrategyResult.hold(
                    reason=f"kill_switch: {kill.reason}",
                    metadata={"kill_switch": kill.asdict()},
                ),
                audit_events=[],
                error=None,
                llm_calls=0,
                subagent_calls=0,
            )

        # Load-time static gate. The promotion pipeline already runs the
        # full validator, but the runner is the last line of defence: a
        # package whose code trips a static *blocker* (forbidden import,
        # dangerous builtin, env access) shares this interpreter with the
        # vault and the connector registry, so live mode refuses to
        # execute it at all. Paper/shadow log a warning so operators can
        # fix the package before promotion.
        static_blockers = self._static_scan_blockers(package)
        if static_blockers:
            enforce = mode == "live" or bool(
                self.config.get("trading.strategy_static_check.enforce_all_modes", False)
            )
            if enforce:
                summary = "; ".join(
                    f"{b.get('code')}@{b.get('where')}" for b in static_blockers[:5]
                )
                return self._finalize_record(
                    package=package,
                    run_id=rid,
                    session_id=sid,
                    started_at=started_at,
                    t0=t0,
                    inputs=StrategyRunInputs(
                        mode=mode,
                        package_hash=package.content_hash,
                        trigger_event_id=trigger_event_id,
                        trigger_payload=dict(trigger_payload or {}),
                        operator=operator,
                        note=note,
                    ),
                    result=StrategyResult.error(
                        message=f"static check blockers: {summary}",
                        kind="strategy_static_check_failed",
                        metadata={"static_blockers": static_blockers},
                    ),
                    audit_events=[],
                    error={
                        "kind": "strategy_static_check_failed",
                        "message": summary,
                        "blockers": static_blockers,
                    },
                    llm_calls=0,
                    subagent_calls=0,
                )
            _LOG.warning(
                "strategy %s has %d static blocker(s) (mode=%s, not enforced): %s",
                strategy_id,
                len(static_blockers),
                mode,
                "; ".join(str(b.get("code")) for b in static_blockers[:5]),
            )

        ctx = build_strategy_context(
            config=self.config,
            package=package,
            skills=self.skills,
            run_id=rid,
            session_id=sid,
            news_fetchers=self.news_fetchers,
            clock=clock,
            connector_registry=self.connector_registry,
            tool_registry=self.tool_registry,
            executor=self.executor,
            trigger_payload=trigger_payload,
            trigger_event_id=trigger_event_id,
        )

        ctx.audit.log(
            "tick.start",
            {
                "trigger_event_id": trigger_event_id,
                "mode": mode,
                "package_hash": package.content_hash,
                "operator": operator,
                "note": note,
            },
        )

        result, error = self._invoke_entrypoint(
            package=package,
            ctx=ctx,
            max_run_seconds=manifest.policy.max_run_seconds,
        )

        # In ``shadow`` mode, demote any submitted intent to a
        # bookkeeping result. The trading kernel itself doesn't know
        # about strategy modes — the runner is the only place we can
        # enforce "shadow runs don't fill orders".
        if mode == "shadow" and result.status == StrategyResultStatus.SUBMITTED:
            ctx.audit.log(
                "shadow.demoted",
                {"original_intent": dict(result.intent or {})},
            )
            result = StrategyResult.ok(
                reason="shadow mode — intent recorded but not executed",
                metadata={
                    "shadow": True,
                    "original_intent": dict(result.intent or {}),
                    "original_status": result.status.value,
                },
            )

        ctx.audit.log(
            "tick.end",
            {
                "status": result.status.value,
                "llm_calls": ctx.llm.calls_made,
            },
            level="error" if error is not None else "info",
        )

        return self._finalize_record(
            package=package,
            run_id=rid,
            session_id=sid,
            started_at=started_at,
            t0=t0,
            inputs=StrategyRunInputs(
                mode=mode,
                package_hash=package.content_hash,
                trigger_event_id=trigger_event_id,
                trigger_payload=dict(trigger_payload or {}),
                operator=operator,
                note=note,
            ),
            result=result,
            audit_events=ctx.audit.events(),
            error=error,
            llm_calls=ctx.llm.calls_made,
            subagent_calls=self._count_subagent_calls(ctx.audit.events()),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _static_scan_blockers(package: StrategyPackage) -> list[dict[str, Any]]:
        """Run (or reuse) the static AST scan for ``package``.

        Fail closed: a scanner crash counts as a blocker — live mode
        must not execute code the scanner could not vet.
        """

        cached = _STATIC_SCAN_CACHE.get(package.content_hash)
        if cached is not None:
            return cached
        try:
            from .validator import static_scan_blockers

            blockers = [issue.asdict() for issue in static_scan_blockers(package)]
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.exception("static scan failed for %s", package.strategy_id)
            blockers = [{
                "severity": "blocker",
                "code": "static_scan_error",
                "message": f"static scanner crashed: {exc}",
                "where": "",
            }]
        _STATIC_SCAN_CACHE[package.content_hash] = blockers
        return blockers

    def _resolve_mode(self, manifest_mode: str, override: Optional[str]) -> str:
        """Pick the run mode, refusing live overrides without runtime flag."""

        chosen = (override or manifest_mode or "paper").strip().lower()
        if chosen not in _VALID_MODES:
            raise NeryaError(f"unknown strategy mode: {chosen!r}")
        if chosen == "live" and not self.config.live_trading_enabled():
            # The trading kernel will reject orders too, but failing here
            # gives a clearer error and avoids journaling a "live tick
            # attempted on paper workspace" record on every cron beat.
            raise NeryaError(
                "live mode requires runtime.live_trading_enabled=true; "
                "set the workspace flag before promoting a live strategy"
            )
        return chosen

    def _invoke_entrypoint(
        self,
        *,
        package: StrategyPackage,
        ctx: StrategyContext,
        max_run_seconds: int,
    ) -> tuple[StrategyResult, Optional[dict[str, Any]]]:
        """Import and run ``main.py`` returning (result, error_payload)."""

        try:
            entry = self._load_entrypoint(package)
        except Exception as exc:
            tb = traceback.format_exc(limit=5)
            ctx.audit.log(
                "entrypoint.import_failed",
                {"error": str(exc), "traceback": tb},
                level="error",
            )
            return (
                StrategyResult.error(
                    message=f"failed to import entrypoint: {exc}",
                    kind="strategy_import_error",
                ),
                {"kind": "strategy_import_error", "message": str(exc), "traceback": tb},
            )

        def _call() -> Any:
            return entry(ctx)

        try:
            raw = _run_with_timeout(_call, seconds=float(max_run_seconds or 0))
        except StrategyTimeoutError as exc:
            ctx.audit.log("entrypoint.timeout", {"error": str(exc)}, level="error")
            return (
                StrategyResult.error(
                    message=str(exc), kind="strategy_timeout",
                    metadata={"max_run_seconds": max_run_seconds},
                ),
                {"kind": "strategy_timeout", "message": str(exc)},
            )
        except StrategyRuntimeError as exc:
            ctx.audit.log("entrypoint.runtime_error", {"error": str(exc)}, level="error")
            return (
                StrategyResult.error(
                    message=str(exc), kind="strategy_runtime_error",
                ),
                {"kind": "strategy_runtime_error", "message": str(exc)},
            )
        except TradingError as exc:
            ctx.audit.log("entrypoint.trading_error", {"error": str(exc)}, level="error")
            return (
                StrategyResult.error(
                    message=str(exc), kind="trading_error",
                ),
                {"kind": "trading_error", "message": str(exc)},
            )
        except Exception as exc:  # generated code is fallible
            tb = traceback.format_exc(limit=10)
            ctx.audit.log(
                "entrypoint.uncaught",
                {"error": str(exc), "traceback": tb},
                level="error",
            )
            return (
                StrategyResult.error(
                    message=f"uncaught {type(exc).__name__}: {exc}",
                    kind="strategy_uncaught",
                    metadata={"traceback": tb},
                ),
                {"kind": "strategy_uncaught", "message": str(exc), "traceback": tb},
            )

        if isinstance(raw, StrategyResult):
            return raw, None

        # Be lenient with older generated strategies: some return
        # {"decision": "HOLD"} / {"action": "hold"} instead of the
        # canonical StrategyResult envelope.
        if isinstance(raw, dict):
            execution = raw.get("execution")
            reason = str(raw.get("reason") or raw.get("summary") or "")
            metadata = _metadata_from_return(raw)
            if isinstance(execution, StrategyResult):
                execution.metadata.update(metadata)
                if reason and not execution.reason:
                    execution.reason = reason
                return execution, None
            if isinstance(execution, dict):
                status = str(execution.get("status") or "").strip().lower()
                if status in _RETURN_STATUS_VALUES:
                    return (
                        StrategyResult.from_trade_envelope(
                            execution,
                            reason=reason,
                            metadata=metadata,
                        ),
                        None,
                    )

            return_status = _normalise_return_status(raw)
            if return_status in _RETURN_STATUS_VALUES:
                try:
                    return (
                        StrategyResult(
                            status=StrategyResultStatus(return_status),
                            reason=reason,
                            intent=dict(raw.get("intent") or {}),
                            order=dict(raw.get("order") or {}),
                            risk_decision=dict(raw.get("risk_decision") or {}),
                            approval_id=raw.get("approval_id"),
                            order_id=raw.get("order_id"),
                            session_id=raw.get("session_id"),
                            metadata=metadata,
                            error_kind=raw.get("error_kind"),
                        ),
                        None,
                    )
                except Exception:
                    pass
            if return_status in {"buy", "sell", "reduce", "exit", "enter"}:
                return (
                    StrategyResult.ok(
                        reason=reason or f"entrypoint returned decision={return_status}",
                        metadata=metadata,
                    ),
                    None,
                )

        # Be lenient: a dict with a known status survives, anything
        # else gets wrapped into an OK result with the original return
        # captured so the operator can debug.
        if isinstance(raw, dict) and raw.get("status") in _RETURN_STATUS_VALUES:
            try:
                return (
                    StrategyResult(
                        status=StrategyResultStatus(raw["status"]),
                        reason=str(raw.get("reason") or ""),
                        intent=dict(raw.get("intent") or {}),
                        order=dict(raw.get("order") or {}),
                        risk_decision=dict(raw.get("risk_decision") or {}),
                        approval_id=raw.get("approval_id"),
                        order_id=raw.get("order_id"),
                        session_id=raw.get("session_id"),
                        metadata=dict(raw.get("metadata") or {}),
                        error_kind=raw.get("error_kind"),
                    ),
                    None,
                )
            except Exception:
                pass

        ctx.audit.log(
            "entrypoint.unexpected_return",
            {"type": type(raw).__name__},
            level="warning",
        )
        return (
            StrategyResult.ok(
                reason="entrypoint returned non-StrategyResult value",
                metadata={"raw_type": type(raw).__name__},
            ),
            None,
        )

    def _load_entrypoint(self, package: StrategyPackage) -> Callable[[StrategyContext], Any]:
        """Import ``main.py`` and return the configured entrypoint callable."""

        manifest = package.manifest
        module_path = package.root / manifest.entrypoint_module
        if not module_path.exists():
            raise FileNotFoundError(str(module_path))

        # Unique module name keeps reloads + multi-strategy isolation correct.
        suffix = uuid.uuid4().hex[:8]
        module_name = (
            f"_nerya_strategy."
            f"{manifest.strategy_id}."
            f"{package.content_hash[:8] or 'noversion'}_{suffix}"
        )
        spec = importlib.util.spec_from_file_location(
            module_name,
            module_path,
            submodule_search_locations=[str(package.root)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build spec for {module_path}")
        module = importlib.util.module_from_spec(spec)

        # Prepend the package root so ``import helpers`` works.
        added_path = str(package.root)
        sys_path_inserted = added_path not in sys.path
        if sys_path_inserted:
            sys.path.insert(0, added_path)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            if sys_path_inserted:
                try:
                    sys.path.remove(added_path)
                except ValueError:
                    pass
            # Drop cache entry — next run rebuilds with the new content.
            sys.modules.pop(module_name, None)

        entry = getattr(module, manifest.entrypoint_func, None)
        if entry is None:
            raise AttributeError(
                f"strategy {manifest.strategy_id!r}: entrypoint "
                f"{manifest.entrypoint!r} resolves to a missing attribute"
            )
        if not callable(entry):
            raise TypeError(
                f"strategy {manifest.strategy_id!r}: entrypoint "
                f"{manifest.entrypoint!r} is not callable"
            )
        return entry

    def _finalize_record(
        self,
        *,
        package: StrategyPackage,
        run_id: str,
        session_id: str,
        started_at: str,
        t0: float,
        inputs: StrategyRunInputs,
        result: StrategyResult,
        audit_events: list[dict[str, Any]],
        error: Optional[dict[str, Any]],
        llm_calls: int,
        subagent_calls: int,
    ) -> StrategyRunRecord:
        finished_at = now_iso()
        duration_ms = int((time.monotonic() - t0) * 1000)
        record = StrategyRunRecord(
            run_id=run_id,
            strategy_id=package.strategy_id,
            package_hash=package.content_hash,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            status=result.status.value,
            mode=inputs.mode,
            reason=result.reason,
            session_id=session_id,
            trigger_event_id=inputs.trigger_event_id,
            inputs={
                "mode": inputs.mode,
                "package_hash": inputs.package_hash,
                "trigger_event_id": inputs.trigger_event_id,
                "trigger_payload": inputs.trigger_payload,
                "operator": inputs.operator,
                "note": inputs.note,
            },
            outputs={
                "result": result.asdict(),
                "llm_calls": llm_calls,
                "subagent_calls": subagent_calls,
            },
            audit=list(audit_events or []),
            error=error,
        )

        store = StrategyRunStore(self.config.paths, package.strategy_id)
        try:
            store.write(record)
        except Exception:
            _LOG.exception("failed to write run record %s", run_id)

        try:
            from ..evolution.strategy_run_observation import (
                record_strategy_run_post_apply_observation,
            )

            record_strategy_run_post_apply_observation(self.config.paths, record)
        except Exception:
            _LOG.exception("strategy run post-apply observation failed")

        # Mirror the legacy strategy-history rows so the existing
        # dashboard panels keep working without a separate migration.
        try:
            self._mirror_history(record)
        except Exception:
            _LOG.exception("strategy history mirror failed")

        return record

    @staticmethod
    def _count_subagent_calls(events: list[dict[str, Any]]) -> int:
        """Count subagent invocations recorded in the audit log."""

        # Strategies invoke subagents through ctx.subagents which the
        # dispatcher writes into the global agent journal. We don't
        # have a direct counter on the facade, so we fall back to
        # parsing the strategy audit log if the strategy code logged
        # explicit ``subagent.run`` events. Otherwise return 0; the
        # global journal is the authoritative count.
        return sum(1 for e in events if e.get("kind") == "strategy.subagent.run")

    def _mirror_history(self, record: StrategyRunRecord) -> None:
        """Re-emit run details into ``strategy_history/`` for the dashboard."""

        paths = self.config.paths
        sid = record.strategy_id
        ses = record.session_id
        result_dict = record.outputs.get("result") or {}

        if record.trigger_event_id:
            _history_store.record_trigger(
                paths,
                strategy_id=sid,
                session_id=ses,
                event={
                    "name": "strategy.tick",
                    "trigger_event_id": record.trigger_event_id,
                    "payload": record.inputs.get("trigger_payload") or {},
                },
            )

        _history_store.record_decision(
            paths,
            strategy_id=sid,
            session_id=ses,
            decision={
                "run_id": record.run_id,
                "status": record.status,
                "reason": record.reason,
                "package_hash": record.package_hash,
                "mode": record.mode,
                "duration_ms": record.duration_ms,
            },
        )

        intent = result_dict.get("intent") or {}
        if intent:
            _history_store.record_intent(
                paths, strategy_id=sid, session_id=ses, intent=intent
            )
        risk = result_dict.get("risk_decision") or {}
        if risk:
            _history_store.record_risk(
                paths, strategy_id=sid, session_id=ses, decision=risk
            )
        order = result_dict.get("order") or {}
        if order:
            _history_store.record_order(
                paths, strategy_id=sid, session_id=ses, payload=order
            )

        # Workspace-level decision journal (one row per tick).
        try:
            jsonl.append(
                paths.journal("strategy_decisions"),
                {
                    "kind": "strategy.decision",
                    "run_id": record.run_id,
                    "strategy_id": sid,
                    "session_id": ses,
                    "status": record.status,
                    "reason": record.reason,
                    "mode": record.mode,
                    "package_hash": record.package_hash,
                    "duration_ms": record.duration_ms,
                    "ts": record.finished_at,
                },
            )
        except Exception:
            _LOG.exception("strategy_decisions journal append failed")


__all__ = [
    "StrategyRunInputs",
    "StrategyRunOutputs",
    "StrategyRunner",
    "StrategyTimeoutError",
]

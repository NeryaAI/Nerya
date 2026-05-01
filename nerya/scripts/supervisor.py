"""ScriptSupervisor — background run + start/stop control for scripts.

Purpose
-------
Until now, every script execution was synchronous: the caller blocked
on ``run_script`` until the script returned. That's fine for short,
one-shot signals but wrong for the strategies that want a long-running
watcher (polling a book, tailing a DEX, reconciling a ledger).

This module adds a lightweight supervisor that:

* launches a script in a background thread (no subprocess — we rely on
  the existing in-process sandbox and the threading primitives already
  loaded by the runtime).
* hands the script a ``cancel`` event it can cooperatively poll.
* tracks every running script in an in-memory registry, keyed by a
  stable ``process_id``.
* journals every lifecycle transition (``start``, ``stop``, ``done``,
  ``error``) to ``workspace/scripts/running.jsonl`` so operator UIs and
  crash-recovery flows can reconstruct state after a restart.

Design constraints
------------------
* **No child processes.** Per the project's always-in-process rule, we
  stay inside the parent interpreter. The sandbox already enforces
  per-run CPU/memory limits.
* **Cooperative cancel.** Python threads cannot be force-killed
  portably. We expose a :class:`threading.Event` to the script via
  ``ctx.stop_event``. Well-behaved scripts poll it; a supervisor
  ``stop(..., timeout=...)`` returns ``status="stopping"`` if the
  script hasn't yielded by the deadline and leaves the thread to exit
  on its next poll.
* **Per-strategy scope.** Every background process records the
  ``strategy_id`` it belongs to so the operator surfaces can filter /
  gate by strategy.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from ..core import jsonl
from ..core.config import Config
from ..core.errors import ScriptError
from ..core.ids import script_run_id
from ..core.time import now_iso
from .runner import SkillInvoker, run_script as _run_script_sync


# ---------------------------------------------------------- data types
@dataclass
class ProcessRecord:
    """One row in the in-memory supervisor registry."""

    process_id: str
    script_id: str
    strategy_id: str | None
    state: str                                   # running | stopping | done | error
    started_at: str
    stopped_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    thread: threading.Thread | None = field(default=None, repr=False)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def asdict(self) -> dict[str, Any]:
        return {
            "process_id": self.process_id,
            "script_id": self.script_id,
            "strategy_id": self.strategy_id,
            "state": self.state,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "error": self.error,
            "result": self.result,
        }


class ScriptSupervisor:
    """Per-workspace supervisor for background script processes.

    A single supervisor instance is created by :class:`SkillKernel` at
    boot time and reused for the lifetime of the process. It is safe
    for concurrent callers on the supervisor API itself (a single lock
    guards the registry); individual scripts are responsible for their
    own in-process concurrency.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._records: dict[str, ProcessRecord] = {}

    # ------------------------------------------------------------ start
    def start(
        self, script_id: str, *,
        args: dict[str, Any] | None = None,
        strategy_id: str | None = None,
        skill_invoker: SkillInvoker | None = None,
    ) -> ProcessRecord:
        """Launch ``script_id`` in a background thread.

        Returns the :class:`ProcessRecord` immediately; the script
        itself runs asynchronously. The returned record's ``state``
        transitions to ``done`` or ``error`` once the thread finishes.
        """
        pid = f"proc_{script_run_id()}"
        record = ProcessRecord(
            process_id=pid,
            script_id=script_id,
            strategy_id=strategy_id,
            state="running",
            started_at=now_iso(),
        )

        def _worker() -> None:
            try:
                # The script body is free to read record.stop_event
                # via the skill_invoker's injected ctx. We also inject
                # it through a dedicated key in args so legacy scripts
                # that take ``**kwargs`` pick it up transparently.
                call_args = dict(args or {})
                call_args.setdefault("stop_event", record.stop_event)
                result = _run_script_sync(
                    self._config, script_id,
                    args=call_args,
                    skill_invoker=skill_invoker,
                )
                with self._lock:
                    record.result = result
                    record.state = "done"
                    record.stopped_at = now_iso()
                self._journal({
                    "kind": "script.bg.done",
                    "process_id": pid, "script_id": script_id,
                    "strategy_id": strategy_id,
                })
            except Exception as exc:
                with self._lock:
                    record.error = f"{type(exc).__name__}: {exc}"
                    record.state = "error"
                    record.stopped_at = now_iso()
                self._journal({
                    "kind": "script.bg.error",
                    "process_id": pid, "script_id": script_id,
                    "strategy_id": strategy_id,
                    "error": record.error,
                    "trace": traceback.format_exc(limit=5),
                })

        thread = threading.Thread(
            target=_worker,
            name=f"nerya-script-{script_id}-{pid[-6:]}",
            daemon=True,
        )
        record.thread = thread

        with self._lock:
            self._records[pid] = record
        self._journal({
            "kind": "script.bg.start",
            "process_id": pid, "script_id": script_id,
            "strategy_id": strategy_id,
        })
        thread.start()
        return record

    # ------------------------------------------------------------- stop
    def stop(self, process_id: str, *, timeout_s: float = 5.0) -> ProcessRecord:
        """Signal ``process_id`` to stop and wait up to ``timeout_s``."""
        with self._lock:
            record = self._records.get(process_id)
        if record is None:
            raise ScriptError(f"unknown process_id: {process_id!r}")
        if record.state in {"done", "error"}:
            return record
        record.stop_event.set()
        with self._lock:
            record.state = "stopping"
        self._journal({
            "kind": "script.bg.stop_requested",
            "process_id": process_id, "script_id": record.script_id,
        })
        thread = record.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout_s))
        # Refresh the record view after the join.
        with self._lock:
            return self._records[process_id]

    # ----------------------------------------------------------- status
    def status(self, process_id: str) -> ProcessRecord:
        with self._lock:
            record = self._records.get(process_id)
        if record is None:
            raise ScriptError(f"unknown process_id: {process_id!r}")
        return record

    # ------------------------------------------------------------- list
    def list(
        self, *, strategy_id: str | None = None,
        include_finished: bool = True,
    ) -> list[ProcessRecord]:
        with self._lock:
            rows = list(self._records.values())
        if strategy_id is not None:
            rows = [r for r in rows if r.strategy_id == strategy_id]
        if not include_finished:
            rows = [r for r in rows if r.state in {"running", "stopping"}]
        rows.sort(key=lambda r: r.started_at)
        return rows

    # ---------------------------------------------------------- journal
    def _journal(self, row: dict[str, Any]) -> None:
        try:
            path = self._config.paths.scripts_dir / "running.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            row.setdefault("ts", now_iso())
            jsonl.append(path, row)
        except Exception:
            # Journaling must never crash the supervisor; operator UIs
            # can always fall back to the in-memory registry.
            pass


# ------------------------------------------------ per-workspace singleton
#
# Skill actions don't receive the top-level SkillKernel, only a
# :class:`SkillCallContext`. To avoid plumbing the supervisor through
# every call site we keep one supervisor per workspace root path. Tests
# that create temp workspaces automatically get their own supervisor.
_SUPERVISORS: dict[str, ScriptSupervisor] = {}
_SUPERVISORS_LOCK = threading.Lock()


def get_supervisor(config: Config) -> ScriptSupervisor:
    """Return the supervisor for ``config``'s workspace, creating it once."""
    key = str(config.paths.root.resolve())
    with _SUPERVISORS_LOCK:
        sup = _SUPERVISORS.get(key)
        if sup is None:
            sup = ScriptSupervisor(config)
            _SUPERVISORS[key] = sup
    return sup


def reset_supervisors_for_tests() -> None:
    """Drop every cached supervisor. Test-only helper."""
    with _SUPERVISORS_LOCK:
        for sup in _SUPERVISORS.values():
            for pid in list(sup._records):
                try:
                    sup.stop(pid, timeout_s=0.1)
                except Exception:
                    pass
        _SUPERVISORS.clear()


__all__ = [
    "ScriptSupervisor", "ProcessRecord",
    "get_supervisor", "reset_supervisors_for_tests",
]

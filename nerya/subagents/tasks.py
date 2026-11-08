"""Async subagent task store — .

Implementation notes:

* The same task store supports either *foreground* (block until result)
  or *background* sub-agents. Background runs return a task handle,
  write their final output to a file the coordinator can list/get/stop,
  and surface progress notifications along the way.

Why a separate task store
-------------------------
The sync ``subagent_run`` tool fits inside the regular ``messages →
tools → tool_result`` loop: the parent waits for the child, gets one
``tool_result`` block, moves on. Background runs don't fit that
shape — the model can't sit and wait. The task store gives every
background run a stable ``task_id`` (rendered into the
``tool_result`` so the model knows what to ask about later) and
persists the run's metadata + output JSON under
``<workspace>/agent_tasks/<task_id>.json``.

Design constraints
------------------

* **Cooperative cancellation only.** Python threads can't be killed
  from the outside; ``stop()`` flips an event the worker checks
  between iterations and the next-iteration cancellation is enough
  for our use case (subagent runs are bounded by the runtime's own
  step budget anyway).
* **Process-local registry.** A daemon would need a real queue; we
  intentionally don't ship that yet — the kernel runs in-process and
  the dashboard reads task state via the JSON files on disk.
* **Append-only progress journal.** Every progress notification is
  appended to ``<workspace>/journals/agent.jsonl`` under
  ``kind="agent.task.progress"`` so the dashboard's live feed picks
  it up via the same path it uses for tool events.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..core import jsonl
from ..core.paths import WorkspacePaths


def _publish_bus(kind: str, **payload: Any) -> None:
    """Best-effort publish to the process-local streaming bus.

    Imports lazily so unit tests / minimal embeddings that don't wire
    up the streaming layer can still use :class:`TaskStore` without
    blowing up. Any error is swallowed — task progress is also
    journalled to ``agent.jsonl``, so the bus is purely additive.
    """

    try:
        from ..agent.streaming import get_default_bus

        get_default_bus().publish(kind, **payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Task model
# ---------------------------------------------------------------------------


_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
_LIVE_LOCK = threading.Lock()
_LIVE_TASKS: dict[str, tuple] = {}


def new_task_id(prefix: str = "task") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class TaskRecord:
    """One async task entry. Persisted on disk under ``agent_tasks/``.

    ``progress`` is intentionally a list (not a counter) so each note
    carries a timestamp and a free-form payload — the model can
    surface "step X of Y" or partial findings without us having to
    define a schema upfront.
    """

    task_id: str
    name: str
    state: str = "queued"  # queued | running | succeeded | failed | cancelled
    payload: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: Optional[str] = None
    output: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_kind: Optional[str] = None
    tokens: int = 0
    usd: float = 0.0
    wall_ms: int = 0
    progress: list[dict[str, Any]] = field(default_factory=list)
    parent_turn_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    strategy_id: Optional[str] = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TaskStore:
    """File-backed registry of background subagent tasks."""

    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths
        self._dir = paths.root / "agent_tasks"
        # NativeToolDeps/API readers can be recreated for the next turn.
        # Reuse process ownership across those instances, scoped by workspace.
        with _LIVE_LOCK:
            self._lock, self._cancel_events, self._processes, self._threads = _LIVE_TASKS.setdefault(
                str(paths.root.resolve()), (threading.RLock(), {}, {}, {}),
            )

    def _path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    def _save(self, record: TaskRecord) -> Path:
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            p = self._path(record.task_id)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(record.asdict(), indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, p)
            return p

    # --------------------------------------------------------------- public
    def create(
        self,
        *,
        name: str,
        payload: dict[str, Any],
        parent_turn_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
    ) -> TaskRecord:
        record = TaskRecord(
            task_id=new_task_id(),
            name=name,
            state="queued",
            payload=dict(payload or {}),
            started_at=_utc_now_iso(),
            parent_turn_id=parent_turn_id,
            parent_session_id=parent_session_id,
            strategy_id=strategy_id,
        )
        self._save(record)
        with self._lock:
            self._cancel_events[record.task_id] = threading.Event()
        try:
            jsonl.append(
                self.paths.journal("agent"),
                {
                    "kind": "agent.task.created",
                    "task_id": record.task_id,
                    "name": record.name,
                    "parent_turn_id": parent_turn_id,
                    "parent_session_id": parent_session_id,
                    "strategy_id": strategy_id,
                },
            )
        except Exception:
            pass
        _publish_bus(
            "agent.task.created",
            task_id=record.task_id,
            name=record.name,
            parent_turn_id=parent_turn_id,
            parent_session_id=parent_session_id,
            strategy_id=strategy_id,
        )
        return record

    def load(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            p = self._path(task_id)
            if not p.is_file():
                return None
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        if not isinstance(raw, dict):
            return None
        record = TaskRecord(**{
            k: raw.get(k) for k in TaskRecord.__dataclass_fields__.keys()
            if k in raw
        })
        process = self._processes.get(task_id)
        if process is not None and not record.is_terminal():
            record.output = self._process_output(process.snapshot())
        return record

    @staticmethod
    def _process_output(result) -> dict[str, Any]:
        return {key: getattr(result, key) for key in (
            "pid", "stdout", "stderr", "returncode", "elapsed_ms",
            "cancelled", "timed_out", "process_exited",
            "process_group_stopped", "truncated",
        )} | {"exit_code": result.returncode}

    def attach_process(self, task_id: str, process) -> None:
        """Adopt an already launched managed child, without replaying it."""
        with self._lock:
            self._processes[task_id] = process
            self.update_state(task_id, "running")

        def complete():
            try:
                result = process.wait()
                error_kind = ("cancelled" if result.cancelled else
                              "timeout" if result.timed_out else
                              "execution_error" if result.returncode else None)
                self.finish(task_id, output=self._process_output(result),
                            error=error_kind, error_kind=error_kind, wall_ms=result.elapsed_ms)
            except Exception as exc:
                self.finish(task_id, error=str(exc), error_kind="execution_error")
            finally:
                with self._lock:
                    self._processes.pop(task_id, None)

        thread = run_in_thread(complete, name=f"shell-task-{task_id}", daemon=False)
        self.attach_thread(task_id, thread)

    def attach_thread(self, task_id: str, thread: threading.Thread) -> None:
        """Share a real process-local worker owner across stores for this workspace."""
        if not isinstance(thread, threading.Thread):
            raise TypeError("task owner must be a Thread")
        with self._lock:
            record = self.load(task_id)
            if record is not None and not record.is_terminal():
                self._threads[task_id] = thread

    def wait_for_completion(
        self, task_id: str, check_cancel: Callable[[], None] | None = None,
    ) -> Optional[TaskRecord]:
        """Wait only while an attached worker is alive; never infer ownership from disk."""
        while True:
            record = self.load(task_id)
            if record is None or record.is_terminal():
                return record
            with self._lock:
                thread = self._threads.get(task_id)
            if thread is None or thread is threading.current_thread() or not thread.is_alive():
                return self.load(task_id)
            if check_cancel is not None:
                check_cancel()
            thread.join(timeout=0.1)

    def list(
        self,
        *,
        state: Optional[str] = None,
        limit: int = 50,
        parent_session_id: Optional[str] = None,
    ) -> list[TaskRecord]:
        if not self._dir.is_dir():
            return []
        out: list[TaskRecord] = []
        files = sorted(
            self._dir.glob("task_*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for p in files:
            rec = self.load(p.stem)
            if rec is None:
                continue
            if state and rec.state != state:
                continue
            if parent_session_id and rec.parent_session_id != parent_session_id:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    def update_state(self, task_id: str, state: str) -> Optional[TaskRecord]:
        rec = self.load(task_id)
        if rec is None:
            return None
        rec.state = state
        if state in _TERMINAL_STATES and not rec.finished_at:
            rec.finished_at = _utc_now_iso()
        self._save(rec)
        return rec

    def append_progress(
        self, task_id: str, *, note: str, payload: Optional[dict[str, Any]] = None,
    ) -> Optional[TaskRecord]:
        rec = self.load(task_id)
        if rec is None:
            return None
        entry = {
            "ts": _utc_now_iso(),
            "note": note,
            "payload": dict(payload or {}),
        }
        rec.progress.append(entry)
        self._save(rec)
        try:
            jsonl.append(
                self.paths.journal("agent"),
                {
                    "kind": "agent.task.progress",
                    "task_id": task_id,
                    "note": note,
                    **(payload or {}),
                },
            )
        except Exception:
            pass
        _publish_bus(
            "agent.task.progress",
            task_id=task_id,
            name=rec.name,
            state=rec.state,
            note=note,
            payload=dict(payload or {}),
        )
        return rec

    def finish(
        self,
        task_id: str,
        *,
        output: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
        error_kind: Optional[str] = None,
        tokens: int = 0,
        usd: float = 0.0,
        wall_ms: int = 0,
    ) -> Optional[TaskRecord]:
        rec = self.load(task_id)
        if rec is None:
            return None
        if error:
            rec.state = "cancelled" if error_kind == "cancelled" else "failed"
        else:
            rec.state = "succeeded"
        rec.output = dict(output or {})
        rec.error = error
        rec.error_kind = error_kind
        rec.tokens = tokens
        rec.usd = usd
        rec.wall_ms = wall_ms
        rec.finished_at = _utc_now_iso()
        self._save(rec)
        try:
            jsonl.append(
                self.paths.journal("agent"),
                {
                    "kind": "agent.task.finished",
                    "task_id": task_id,
                    "state": rec.state,
                    "error": error,
                    "error_kind": error_kind,
                    "tokens": tokens,
                    "usd": usd,
                    "wall_ms": wall_ms,
                },
            )
        except Exception:
            pass
        _publish_bus(
            "agent.task.finished",
            task_id=task_id,
            name=rec.name,
            state=rec.state,
            error=error,
            error_kind=error_kind,
            tokens=tokens,
            usd=usd,
            wall_ms=wall_ms,
        )
        with self._lock:
            self._cancel_events.pop(task_id, None)
            self._threads.pop(task_id, None)
        return rec

    def request_stop(self, task_id: str) -> bool:
        """Flip the cancel event so the worker bails between iterations.

        Returns True if a live worker was found, False if the task is
        unknown or already terminal.
        """

        rec = self.load(task_id)
        if rec is None or rec.is_terminal():
            return False
        with self._lock:
            ev = self._cancel_events.get(task_id)
            process = self._processes.get(task_id)
        if process is not None:
            process.stop()
        if ev is None:
            # A missing owner is not proof the external operation stopped.
            self.append_progress(task_id, note="stop requested; worker unavailable",
                                 payload={"stop_confirmed": False})
            return False
        ev.set()
        return True

    def cancel_event(self, task_id: str) -> Optional[threading.Event]:
        with self._lock:
            return self._cancel_events.get(task_id)


# ---------------------------------------------------------------------------
# Helpers for workers
# ---------------------------------------------------------------------------


def run_in_thread(
    target: Callable[..., None],
    *,
    name: str,
    daemon: bool = True,
    args: tuple[Any, ...] = (),
    kwargs: Optional[dict[str, Any]] = None,
) -> threading.Thread:
    th = threading.Thread(
        target=target,
        name=name,
        args=args,
        kwargs=dict(kwargs or {}),
        daemon=daemon,
    )
    th.start()
    return th


__all__ = [
    "TaskRecord",
    "TaskStore",
    "new_task_id",
    "run_in_thread",
]

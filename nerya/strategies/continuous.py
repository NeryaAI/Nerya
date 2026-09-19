"""Long-lived strategy services. Threads are cooperative, never fake-killed.

The listener and Agent consumer are separate: slow model turns cannot block
receiving a feed. An exclusive file lease covers BOTH workers until exit.
Events are durably claimed before enqueue; uncertain events are never retried.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
from typing import Any
import uuid

from ..core.atomic_write import atomic_write_text
from ..core.config import Config
from ..core.errors import TradingError
from ..harness.cancellation import CancelToken, CancelledError
from .agent_task import StrategyAgentTask
from .agent_task_mode import agent_task_requested
from .continuous_config import ContinuousConfig, is_continuous
from .input_context import safe_data
from .package import load_package
from .runner import StrategyRunner, _strategy_tick_lock


def _state_dir(config: Config, sid: str) -> Path:
    if not isinstance(sid, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", sid):
        raise TradingError("invalid strategy_id")
    root = config.paths.strategy(sid).resolve()
    if not root.is_relative_to(config.paths.strategies.resolve()):
        raise TradingError("strategy path leaves strategies directory")
    state = root / "state"
    if not state.resolve().is_relative_to(root):
        raise TradingError("strategy state path leaves package")
    return state


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TradingError("invalid continuous runtime state")
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n")


def _current_runtime(config: Config) -> dict[str, Any]:
    # Agent execution has a copied Config. Re-read durable safety switches so
    # an operator's stop is visible even while a model request is in flight.
    from ..core import yaml_io
    raw = yaml_io.load(config.paths.root / "nerya.yml", default={})
    if not isinstance(raw, dict) or not isinstance(raw.get("runtime", {}), dict):
        raise TradingError("invalid runtime safety configuration")
    return raw.get("runtime", {})


def _halted(config: Config, sid: str) -> bool:
    return bool(config.kill_switch() or _current_runtime(config).get("kill_switch")
                or _read(_state_dir(config, sid) / "kill_switch.json").get("asserted"))


class EventLedger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, generation TEXT NOT NULL, package_hash TEXT NOT NULL, observed_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}')")

    def claim(self, event_id: str, generation: str, package_hash: str, observed: float, expires: float) -> bool:
        with closing(sqlite3.connect(self.path, timeout=10)) as con, con:
            row = con.execute("INSERT OR IGNORE INTO events (event_id,generation,package_hash,observed_at,expires_at,status) VALUES (?,?,?,?,?,'queued')", (event_id, generation, package_hash, observed, expires))
            return row.rowcount == 1

    def finish(self, event_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        with closing(sqlite3.connect(self.path, timeout=10)) as con, con:
            con.execute("UPDATE events SET status=?, detail=? WHERE event_id=?", (status, json.dumps(detail or {}, allow_nan=False), event_id))

    def abandon_previous(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=10)) as con, con:
            con.execute("UPDATE events SET status='uncertain' WHERE status IN ('running','queued')")

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.path)) as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute("SELECT * FROM events ORDER BY rowid DESC LIMIT ?", (max(1, min(int(limit), 200)),))]


def assert_event_active(config: Config, sid: str, event_id: str) -> None:
    """Runtime-owned order fence, checked again immediately before order tools.

    An accepted exchange order cannot be recalled by cancelling a listener.
    This prevents NEW submissions from stopped, expired or obsolete turns.
    """
    state = _state_dir(config, sid)
    control = _read(state / "continuous-control.json")
    status = _read(state / "continuous-status.json")
    if status.get("state") not in {"starting", "running", "restarting", "finished"} or time.time() - status.get("heartbeat_at", 0) > 10:
        raise TradingError("continuous service owner is not active")
    if control.get("desired") != "running" or _halted(config, sid):
        raise TradingError("continuous service stopped or kill switch asserted")
    with _strategy_tick_lock(state / "continuous.lock") as owner_missing:
        if owner_missing:
            raise TradingError("continuous service owner exited")
    db = state / "continuous-events.sqlite"
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
    if row is None or row["status"] != "running" or row["generation"] != control.get("generation"):
        raise TradingError("continuous event is not owned by the active service")
    if time.time() > row["expires_at"]:
        raise TradingError("continuous event expired")
    package = load_package(config.paths, sid)
    if package.manifest.mode == "live" and not _current_runtime(config).get("live_trading_enabled", config.live_trading_enabled()):
        raise TradingError("live trading was disabled")
    if not is_continuous(package.manifest) or package.content_hash != row["package_hash"]:
        raise TradingError("continuous strategy source changed; restart after review")


@dataclass
class _Service:
    package: Any
    settings: ContinuousConfig
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)
    token: CancelToken = field(default_factory=CancelToken)
    lock: Any = field(default_factory=threading.RLock)
    data: dict[str, Any] = field(default_factory=dict)
    thread: Any = None
    listener: Any = None
    consumer: Any = None
    agent_token: Any = None
    context: Any = None
    lease: Any = None
    ledger: Any = None
    queue: Any = None
    last_dispatch: float = 0


class ContinuousSupervisor:
    def __init__(self, config: Config, *, skills: Any = None, task_executor: Any = None):
        self.config, self.skills, self.task_executor = config, skills, task_executor
        self._lock = threading.RLock()
        self._services: dict[str, _Service] = {}

    def _update(self, service: _Service, **values: Any) -> None:
        with service.lock:
            service.data.update(values)
            # Feed traffic only updates memory; the supervisor flushes once/sec.

    def _flush(self, service: _Service) -> None:
        with service.lock:
            service.data.update(heartbeat_at=time.time(), queue_depth=service.queue.qsize())
            _write(service.package.state_dir / "continuous-status.json", service.data)

    def start(self, sid: str, *, expected_hash: str = "") -> dict[str, Any]:
        state = _state_dir(self.config, sid)
        with self._lock:
            package = load_package(self.config.paths, sid)
            existing = self._services.get(sid)
            if expected_hash and expected_hash != package.content_hash:
                raise TradingError("strategy changed since start was requested")
            if existing and existing.thread.is_alive():
                return self.status(sid)
            if not is_continuous(package.manifest):
                raise TradingError("strategy is not configured for continuous execution")
            if expected_hash and expected_hash != package.content_hash:
                raise TradingError("strategy changed since start was requested")
            if _halted(self.config, sid):
                raise TradingError("kill switch asserted")
            if package.manifest.mode == "live" and not self.config.live_trading_enabled():
                raise TradingError("live trading is disabled")
            from .validator import validate_strategy_package
            validation = validate_strategy_package(self.config.paths, sid)
            if not validation.ok:
                raise TradingError("continuous strategy has validation blockers")
            settings = ContinuousConfig.parse(package.manifest.extras["runtime"])
            if settings.streams:
                import importlib.util
                if importlib.util.find_spec("websockets") is None:
                    raise TradingError("install Nerya with websockets>=15,<16 to use stream.websocket")
            lease = _strategy_tick_lock(state / "continuous.lock")
            if not lease.__enter__():
                lease.__exit__(None, None, None)
                return self.status(sid)
            service = _Service(package=package, settings=settings, lease=lease)
            try:
                service.queue = queue.Queue(maxsize=settings.queue_size)
                service.ledger = EventLedger(state / "continuous-events.sqlite")
                service.ledger.abandon_previous()
                service.data = {"strategy_id": sid, "generation": service.generation,
                    "package_hash": package.content_hash, "pid": os.getpid(),
                    "state": "starting", "connection": "idle", "started_at": time.time(),
                    "restart_count": 0, "accepted_events": 0, "rejected_events": 0,
                    "completed_events": 0, "agent_active": False, "last_error": None}
                _write(state / "continuous-control.json", {"desired": "running", "generation": service.generation, "package_hash": package.content_hash})
                self._flush(service)
                service.thread = threading.Thread(target=self._supervise, args=(service,), name=f"strategy-service-{sid}", daemon=True)
                self._services[sid] = service
                service.thread.start()
            except BaseException:
                lease.__exit__(None, None, None)
                raise
            return self.status(sid)

    def stop(self, sid: str, *, timeout: float = 5) -> dict[str, Any]:
        state = _state_dir(self.config, sid)
        with self._lock:
            control = _read(state / "continuous-control.json")
            if control:
                _write(state / "continuous-control.json", {**control, "desired": "stopped"})
            service = self._services.get(sid)
            if service and service.thread.is_alive():
                self._cancel(service, "operator_stop")
        if service and service.thread is not threading.current_thread():
            service.thread.join(max(0, min(float(timeout), 10)))
        return self.status(sid)

    def _cancel(self, service: _Service, reason: str) -> None:
        if service.data.get("state") == "failed":
            service.data["failed"] = True
        if not service.data.get("stop_reason"):
            service.data["stop_reason"] = reason
        service.token.cancel(reason)
        if service.agent_token:
            service.agent_token.cancel(reason)
        if service.context is not None:
            service.context.run_deadline.trigger(reason)
        self._update(service, state="stopping")
        self._flush(service)

    def status(self, sid: str) -> dict[str, Any]:
        state = _state_dir(self.config, sid)
        with self._lock:
            service = self._services.get(sid)
            if service and service.thread and service.thread.is_alive():
                with service.lock:
                    return {"ok": True, **service.data, "queue_depth": service.queue.qsize(), "listener_alive": bool(service.listener and service.listener.is_alive())}
        saved = _read(state / "continuous-status.json")
        if not saved:
            return {"ok": True, "strategy_id": sid, "state": "stopped", "agent_active": False, "queue_depth": 0}
        if saved.get("state") in {"starting", "running", "restarting", "stopping"}:
            with _strategy_tick_lock(state / "continuous.lock") as free:
                if free:
                    saved.update(state="interrupted", agent_active=False, reason="service owner exited; queued/inflight events are not automatically replayed")
                elif time.time() - saved.get("heartbeat_at", 0) > 10:
                    saved.update(state="unresponsive")
        return {"ok": True, **saved}

    def events(self, sid: str, *, limit: int = 50) -> dict[str, Any]:
        path = _state_dir(self.config, sid) / "continuous-events.sqlite"
        return {"ok": True, "strategy_id": sid, "events": EventLedger(path).list(limit) if path.exists() else []}

    def _enqueue(self, service: _Service, task: Any, key: str, observed: float, inputs: Any) -> dict[str, Any]:
        service.token.raise_if_cancelled()
        if not isinstance(key, str) or not key.strip() or len(key) > 240:
            raise ValueError("event_id must be a stable nonempty upstream id (max 240 chars)")
        if isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isfinite(observed):
            raise ValueError("observed_at must be a finite Unix timestamp in seconds")
        now = time.time()
        event_id = "continuous_" + hashlib.sha256(key.encode()).hexdigest()
        expires = observed + service.settings.max_event_age_seconds
        task = StrategyAgentTask.from_value(task)
        payload = safe_data({"task": task.asdict(), "inputs": inputs})
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 262144:
            raise ValueError("continuous event snapshot exceeds 256 KiB")
        with service.lock:
            service.token.raise_if_cancelled()
            reason = None
            if not agent_task_requested(service.package.manifest):
                reason = "agent_disabled"
            elif task.status != "dispatch" or not task.prompt.strip():
                reason = "not_dispatch"
            elif now > expires or observed > now + 5:
                reason = "expired"
            elif now - service.last_dispatch < service.settings.min_dispatch_interval_seconds:
                reason = "cooldown"
            elif service.queue.full():
                reason = "queue_full"
            elif not service.ledger.claim(event_id, service.generation, service.package.content_hash, observed, expires):
                reason = "duplicate"
            if reason:
                service.data["rejected_events"] += 1
                service.data["last_rejection"] = reason
                return {"accepted": False, "reason": reason, "event_id": event_id}
            service.queue.put_nowait((event_id, expires, payload))
            service.last_dispatch = now
            service.data["accepted_events"] += 1
            return {"accepted": True, "event_id": event_id}

    def _listen(self, service: _Service) -> None:
        from .context import build_strategy_context
        from .streaming import StrategyStream
        package = service.package
        for attempt in range(service.settings.max_restarts + 1):
            if service.token.is_set:
                return
            try:
                ctx = build_strategy_context(config=self.config, package=package, run_id=service.generation, skills=self.skills)
                # This worker only gathers evidence. Model/order work uses the
                # separate event executor with a fresh per-event policy budget.
                ctx.trading = ctx.llm = ctx.subagents = _ListenerOnly()
                ctx.stream = StrategyStream(config=self.config, settings=service.settings, token=service.token,
                    enqueue=lambda *args: self._enqueue(service, *args), update=lambda **values: self._update(service, **values), inputs=ctx.inputs)
                service.context = ctx
                fn = StrategyRunner._load_entrypoint(package)
                service.token.raise_if_cancelled()
                self._update(service, state="running")
                fn(ctx)
                if not service.token.is_set:
                    self._update(service, state="finished")
                return
            except CancelledError:
                return
            except Exception as exc:
                self._update(service, last_error=type(exc).__name__)
                if isinstance(exc, (PermissionError, ImportError)) or attempt >= service.settings.max_restarts:
                    self._update(service, state="failed", failed=True)
                    return
                self._update(service, state="restarting", restart_count=attempt + 1)
                if service.token.wait(min(60, service.settings.restart_backoff_seconds * 2 ** attempt)):
                    return

    def _consume(self, service: _Service) -> None:
        from ..triggers.event import TriggerEvent
        from ..triggers.router import RouterResult
        from ..triggers.strategy_agent_task_executor import StrategyAgentTaskExecutor, TARGET
        executor = self.task_executor or StrategyAgentTaskExecutor(config=self.config, skills=self.skills)
        while not service.token.is_set:
            try:
                event_id, expires, payload = service.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if time.time() > expires:
                    service.ledger.finish(event_id, "expired")
                    continue
                service.ledger.finish(event_id, "running")
                assert_event_active(self.config, service.package.strategy_id, event_id)
                service.agent_token = CancelToken(deadline_s=expires)
                if service.token.is_set:
                    service.agent_token.cancel("service_stopped")
                event = TriggerEvent(event_id=event_id, source="script", kind="strategy.continuous", target=TARGET,
                    strategy_id=service.package.strategy_id, payload={"service_generation": service.generation})
                route = RouterResult(event_id=event_id, status="routed", target=TARGET, route_id=None, strategy_id=service.package.strategy_id)
                self._update(service, agent_active=True, active_event_id=event_id)
                result = executor.execute(event, route, prepared_task=payload["task"], prepared_inputs=payload["inputs"],
                    expected_hash=service.package.content_hash, cancel_token=service.agent_token)
                detail = {"task_id": result.task_id, "session_id": result.session_id, "turn_id": result.turn_id,
                    "agent_status": result.status, "stopped_reason": (result.result or {}).get("stopped_reason")}
                status = "cancelled" if service.agent_token.is_set else result.status
                service.ledger.finish(event_id, status, detail)
                with service.lock:
                    service.data["completed_events"] += 1
                self._update(service, last_event={"event_id": event_id, "status": status, **detail})
            except Exception as exc:
                service.ledger.finish(event_id, "failed", {"error": type(exc).__name__})
                self._update(service, last_error=type(exc).__name__)
            finally:
                service.queue.task_done()
                service.agent_token = None
                self._update(service, agent_active=False, active_event_id=None)

    def _supervise(self, service: _Service) -> None:
        sid = service.package.strategy_id
        try:
            service.listener = threading.Thread(target=self._listen, args=(service,), name=f"listener-{sid}", daemon=True)
            service.consumer = threading.Thread(target=self._consume, args=(service,), name=f"events-{sid}", daemon=True)
            service.consumer.start()
            service.listener.start()
            while service.listener.is_alive() or service.consumer.is_alive():
                try:
                    control = _read(service.package.state_dir / "continuous-control.json")
                    if control.get("desired") != "running" or control.get("generation") != service.generation:
                        self._cancel(service, "operator_stop")
                    elif _halted(self.config, sid):
                        self._cancel(service, "kill_switch")
                    elif load_package(self.config.paths, sid).content_hash != service.package.content_hash:
                        self._cancel(service, "source_changed")
                    elif not service.consumer.is_alive() and not service.token.is_set:
                        self._cancel(service, "event_worker_exited")
                        self._update(service, state="failed", failed=True)
                    elif not service.listener.is_alive() and service.queue.unfinished_tasks == 0:
                        service.token.cancel("listener_exited")
                    self._flush(service)
                except Exception as exc:
                    self._cancel(service, "supervisor_error")
                    self._update(service, last_error=type(exc).__name__)
                # Do not busy spin once the cancellation flag is set.
                time.sleep(0.5)
            while not service.queue.empty():
                event_id, _, _ = service.queue.get_nowait()
                service.ledger.finish(event_id, "cancelled")
                service.queue.task_done()
            final = "failed" if service.data.get("failed") else "stopped"
            self._update(service, state=final, connection="disconnected", stopped_at=time.time())
            self._flush(service)
        finally:
            # Never release exclusivity while a worker can still execute.
            service.token.cancel("service_owner_exit")
            if service.agent_token:
                service.agent_token.cancel("service_owner_exit")
            for worker in (service.listener, service.consumer):
                if worker is not None and worker.ident is not None:
                    worker.join()
            service.lease.__exit__(None, None, None)

    def restore(self) -> None:
        from .package import load_packages
        for package in load_packages(self.config.paths):
            if not is_continuous(package.manifest):
                continue
            try:
                settings = ContinuousConfig.parse(package.manifest.extras["runtime"])
                control = _read(package.state_dir / "continuous-control.json")
                if settings.resume_on_start and control.get("desired") == "running" and control.get("package_hash") == package.content_hash:
                    self.start(package.strategy_id, expected_hash=package.content_hash)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("could not restore continuous strategy %s", package.strategy_id)

    def shutdown(self) -> None:
        for sid in list(self._services):
            service = self._services[sid]
            if service.thread and service.thread.is_alive():
                self._cancel(service, "host_shutdown")
        for service in list(self._services.values()):
            if service.thread:
                service.thread.join(5)


class _ListenerOnly:
    def __getattr__(self, name: str):
        raise TradingError("listener is evidence-only; dispatch to the strategy Agent for model/order work")


_SUPERVISORS: dict[str, ContinuousSupervisor] = {}
_SUPERVISORS_LOCK = threading.Lock()


def get_continuous_supervisor(config: Config) -> ContinuousSupervisor:
    key = str(config.paths.root.resolve())
    with _SUPERVISORS_LOCK:
        if key not in _SUPERVISORS:
            _SUPERVISORS[key] = ContinuousSupervisor(config)
        return _SUPERVISORS[key]

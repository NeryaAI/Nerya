"""Bounded, resumable LP data acquisition; no policy execution or replay admission."""
from contextlib import closing, ExitStack
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time

from nerya.core.redaction import redact_text
from nerya.data_api.types import DataApiError
from nerya.harness.cancellation import CancelToken, CancelledError
from nerya.subagents.tasks import TaskStore, run_in_thread
from nerya.trading.locks import trading_lock
from .lp_state_requirements import inspect_lp_state_requirements, download_lp_states
from .uniswap_event_data import _require, event_history_operation

_HANDLES, _GUARD = {}, threading.RLock()
_FINAL = {"states_acquired", "intervals_acquired", "budget_exhausted", "deadline_exceeded", "source_changed"}
_FIELDS = ("source_revision", "required_state_count", "matched_state_count", "missing_state_count",
           "matched_decision_count", "missing_decision_count", "boundaries", "next_download_args")
_INTERVAL_FIELDS = _FIELDS + ("interval_count", "verified_interval_count", "missing_interval_count",
    "pending_state_interval_count", "interval_preview", "next_interval_download_args",
    "range_continuity_verified", "range_continuity_status")


def _read(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM job WHERE id=1").fetchone())
    row["spec"], row["receipt"] = json.loads(row["spec"]), json.loads(row["receipt"])
    return row


def _update(path, **values):
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db, db:
        db.execute("UPDATE job SET " + ",".join(key + "=?" for key in values) + " WHERE id=1", tuple(values.values()))


def _status(path):
    row = _read(path)
    with _GUARD:
        thread = _HANDLES.get(str(path))
        active = bool(thread and thread.is_alive())
    status = row["state"]
    if status in {"queued", "running"} and not active:
        status = "interrupted"
    if row["deadline"] and (status not in _FINAL or status == "deadline_exceeded") and time.time() >= row["deadline"]:
        status = "deadline_pending" if active else "deadline_exceeded"
    spec = row["spec"]
    phase = spec.get("phase", "states")
    budget_key = "max_requests" if phase == "intervals" else "max_states_total"
    return {"ok": True, "result_type": "historical_data", "dataset_kind": "lp_state_job",
        "phase": phase,
        "job_id": path.stem, "task_id": row["task_id"], "status": status,
        "next_status_call": {"tool": "task_output", "arguments": {"task_id": row["task_id"], "wait_for_completion": True}} if row["task_id"] else None,
        "worker_status": "local_worker" if active else "terminal" if row["state"] in _FINAL | {"stopped"} else "owner_unconfirmed",
        "request_id": spec["request_id"], "source_revision": spec["expected_source_revision"],
        budget_key: spec[budget_key], "attempts_reserved": row["attempts"],
        "remaining_attempts": spec[budget_key] - row["attempts"] if spec[budget_key] is not None else None,
        **({"completed_state_count": spec.get("own_completed_state_count"),
            "completed_state_count_known": spec.get("own_completed_state_count") is not None}
           if phase == "states" else {}),
        "deadline": row["deadline"], "stop_requested": bool(row["stop_requested"]),
        "receipt": row["receipt"], "error_kind": row["error_kind"], "failure": row["receipt"].get("failure"),
        "coverage_complete": False, "requested_window_complete": False, "replay_input": False,
        "range_continuity_verified": phase == "intervals" and row["receipt"].get("range_continuity_verified") is True,
        "tick_fee_reconstruction_verified": False,
        "performance_evidence": False, "financial_execution_allowed": False}


class _JobToken(CancelToken):
    def __init__(self, path, deadline, event):
        super().__init__(deadline_s=deadline or None, _flag=event)
        self.path = path
        self._next_stop_poll = float("-inf")

    @property
    def is_set(self):
        if super().is_set:
            return True
        if time.monotonic() >= self._next_stop_poll:
            stopped = bool(_read(self.path)["stop_requested"])
            self._next_stop_poll = time.monotonic() + 0.25
            if stopped:
                self.cancel("stopped")
        return super().is_set


def _worker(config, path, task, lock):
    from nerya.agent.error_recovery import RecoveryAction, classify_for_recovery, policy_for_kind

    store = TaskStore(config.paths)
    row = _read(path)
    spec, task_id = row["spec"], task.task_id
    intervals = spec.get("phase", "states") == "intervals"
    budget = spec["max_requests" if intervals else "max_states_total"]
    fields = _INTERVAL_FIELDS if intervals else _FIELDS
    token = _JobToken(path, row["deadline"], store.cancel_event(task_id))
    inspect_args = {k: spec[k] for k in ("proposal_id", "strategy_id") if k in spec}
    state, error, failure = "interrupted", None, None
    retries = 0
    previous_cursor = None
    try:
        store.update_state(task_id, "running")
        _update(path, state="running")
        while True:
            token.raise_if_cancelled()
            receipt = inspect_lp_state_requirements(config, inspect_args, token.raise_if_cancelled)
            if receipt["source_revision"] != spec["expected_source_revision"]:
                state = "source_changed"
                break
            compact = {k: receipt[k] for k in fields if k in receipt}
            _update(path, receipt=json.dumps(compact))
            if intervals:
                store.append_progress(task_id, note="LP interval acquisition", payload=_status(path))
            cursor = receipt["next_interval_download_args" if intervals else "next_download_args"]
            if cursor is None:
                if intervals and receipt["range_continuity_verified"] is not True:
                    raise DataApiError("lp_interval_continuity_unverified", kind="historical_data_incomplete",
                        retryable=False)
                state = "intervals_acquired" if intervals else "states_acquired"
                break
            if intervals and cursor == previous_cursor:
                raise DataApiError("lp_interval_cursor_not_advanced", kind="historical_data_incomplete", retryable=False)
            token.raise_if_cancelled()
            # Commit the charge before entering the downloader; failures/crashes never refund it.
            with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db, db:
                reserved = db.execute("UPDATE job SET attempts=attempts+1 WHERE id=1 AND stop_requested=0 "
                    "AND (? IS NULL OR attempts<?) AND (deadline=0 OR deadline>?)", (budget, budget, time.time())).rowcount
            if not reserved:
                token.raise_if_cancelled()
                state = "budget_exhausted"
                break
            try:
                if intervals:
                    result = event_history_operation(config, dict(cursor),
                        check_cancel=token.raise_if_cancelled, cancel_token=token)
                else:
                    result = download_lp_states(config, {**cursor, "max_states": 1},
                        check_cancel=token.raise_if_cancelled, cancel_token=token)
            except DataApiError as exc:
                verdict = classify_for_recovery(error_kind="retryable" if exc.retryable is True else None)
                policy = policy_for_kind(verdict.category)
                if (exc.message in {"state_download_source_changed", "source_changed_during_inspection"}
                        or policy.action != RecoveryAction.AUTO_RETRY or retries >= policy.max_retries
                        or budget is not None and _read(path)["attempts"] >= budget):
                    raise
                retries += 1
                until = time.monotonic() + policy.backoff_for_attempt(retries)
                while time.monotonic() < until:
                    token.raise_if_cancelled()
                    token.wait(min(0.25, until - time.monotonic()))
                token.raise_if_cancelled()
                continue  # Reinspect the same source; the next read reserves another attempt.
            retries = 0
            if intervals:
                previous_cursor = dict(cursor)
                continue  # Reinspect committed event/state chains, never trust an event-range completion flag.
            if result["source_revision"] != spec["expected_source_revision"]:
                state = "source_changed"
                break
            completed = result.get("downloaded_state_count")
            _require(type(completed) is int and 0 <= completed <= 1, "invalid_downloaded_state_count")
            # Internal bookkeeping only: immutable request fields and deadline never change.
            # Legacy jobs lack attribution; never reconstruct it from shared archive progress.
            if spec.get("own_completed_state_count") is not None:
                spec["own_completed_state_count"] += completed
            _update(path, spec=json.dumps(spec), receipt=json.dumps({k: result[k] for k in _FIELDS if k in result}))
            store.append_progress(task_id, note="LP checkpoint acquisition", payload=_status(path))
    except CancelledError:
        state = "deadline_exceeded" if row["deadline"] and time.time() >= row["deadline"] else "stopped"
    except DataApiError as exc:
        state = "source_changed" if exc.message in {"state_download_source_changed", "source_changed_during_inspection"} else "interrupted"
        error = redact_text(exc.kind)[:100]
        detail = {key: redact_text(value)
            for key, value in exc.detail.items()
            if key in {"provider", "action", "reason", "rpc_method", "failure_stage"}
            and isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,159}", value)}
        for key, lower, upper in (("http_status", 100, 599), ("rpc_error_code", -(2**31), 2**31 - 1)):
            value = exc.detail.get(key)
            if type(value) is int and lower <= value <= upper:
                detail[key] = value
        if isinstance(exc.detail.get("rpc_error_message"), str):
            detail["rpc_error_message"] = redact_text(exc.detail["rpc_error_message"])[:500]
        failure = {"kind": error, "message": redact_text(exc.message)[:500],
                   "retryable": exc.retryable, "detail": detail}
    except Exception as exc:
        state = "source_changed" if str(exc) in {"state_download_source_changed", "source_changed_during_inspection"} else "interrupted"
        error = type(exc).__name__
    finally:
        try:
            if failure is not None:
                receipt = _read(path)["receipt"]
                receipt["failure"] = failure
                _update(path, receipt=json.dumps(receipt))
            _update(path, state=state, error_kind=error)
            output = _status(path)
            succeeded = state in {"states_acquired", "intervals_acquired"} or (not intervals
                and state == "budget_exhausted" and error is None
                and output["completed_state_count"] == spec["max_states_total"])
            diagnostic = f"{error}: {failure['message']}" if failure else error or state
            store.finish(task_id, output=output, error=None if succeeded else diagnostic,
                error_kind=None if succeeded else "cancelled" if state in {"stopped", "deadline_exceeded"} else error or state)
        finally:
            lock.close()


def _launch(config, path, turn_id):
    with ExitStack() as lock:
        if not lock.enter_context(trading_lock(config.paths, "lp-state-job:" + path.stem)):
            return _status(path)
        row = _read(path)
        if row["state"] in _FINAL:
            return _status(path)
        if row["deadline"] and time.time() >= row["deadline"]:
            _update(path, state="deadline_exceeded")
            return _status(path)
        spec = row["spec"]
        store = TaskStore(config.paths)
        task = store.create(name="lp_state_job", payload={"job_id": path.stem, "phase": spec.get("phase", "states"),
            "session_id": spec["session_id"], "actor_id": spec["actor_id"]},
            parent_session_id=spec["session_id"], parent_turn_id=turn_id, strategy_id=spec.get("strategy_id"))
        _update(path, task_id=task.task_id, state="queued", stop_requested=0, error_kind=None)
        held = lock.pop_all()
        try:
            with _GUARD:
                thread = run_in_thread(_worker, name="lp-state-job-" + path.stem,
                    args=(config, path, task, held))
                _HANDLES[str(path)] = thread
                store.attach_thread(task.task_id, thread)
        except BaseException:
            held.close()
            _update(path, state="interrupted")
            raise
    return _status(path)


def _start(config, path, request, session_id, actor_id, turn_id):
    ids = set(request) & {"strategy_id", "proposal_id"}
    target = {**{k: request[k] for k in ids}, "expected_source_revision": request["expected_source_revision"],
              "session_id": session_id, "actor_id": actor_id}
    phase = request.get("phase", "states")
    if phase == "intervals":
        target["phase"] = phase  # Preserve the legacy states lock identity.
    with trading_lock(config.paths, "lp-state-target:" + json.dumps(target, sort_keys=True)) as acquired:
        _require(acquired, "lp_job_start_busy")
        original = path.exists()
        if original:
            row = _read(path)
            _require(row["spec"]["session_id"] == session_id and row["spec"]["actor_id"] == actor_id, "lp_job_owner_mismatch")
            _require({k: v for k, v in row["spec"].items()
                      if k not in {"session_id", "actor_id", "initial_matched", "own_completed_state_count"}}
                     == request, "lp_job_spec_mismatch")
            if row["state"] in _FINAL:
                return _status(path)  # Original request aliases retain their terminal receipt.
        # Scan existing producer records; legacy attribution stays unknown, never inferred from shared progress.
        matches = []
        for existing in sorted(path.parent.glob("lpjob_*.sqlite3")):
            if not re.fullmatch(r"lpjob_[0-9a-f]{32}", existing.stem):
                continue
            _require(not existing.is_symlink(), "unsafe_lp_job_path")
            row = _read(existing)
            if (row["state"] in _FINAL or row["spec"].get("phase", "states") != phase
                    or not all(row["spec"].get(k) == v for k, v in target.items())):
                continue
            with trading_lock(config.paths, "lp-state-job:" + existing.stem) as available:
                active = not available
            with _GUARD:
                thread = _HANDLES.get(str(existing))
                active = active or bool(thread and thread.is_alive())
            if not active and row["deadline"] and time.time() >= row["deadline"]:
                continue
            completed = row["attempts"] if phase == "intervals" else row["spec"].get("own_completed_state_count")
            matches.append(((active, completed if completed is not None else -1), existing))
        if matches:
            selected = max(matches, key=lambda match: match[0])[1]
            return _status(selected) if selected == path else _launch(config, selected, turn_id)
        if original:
            return _status(path)
        inspection = inspect_lp_state_requirements(config, {k: request[k] for k in ids})
        _require(inspection["source_revision"] == request["expected_source_revision"], "lp_job_source_changed")
        spec = {**request, "session_id": session_id, "actor_id": actor_id,
                **({"initial_matched": inspection["matched_state_count"], "own_completed_state_count": 0}
                   if phase == "states" else {})}
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS job (id INTEGER PRIMARY KEY CHECK(id=1), spec TEXT NOT NULL, "
                "deadline REAL NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                "stop_requested INTEGER NOT NULL DEFAULT 0, task_id TEXT, receipt TEXT NOT NULL, error_kind TEXT)")
            created = bool(db.execute("INSERT OR IGNORE INTO job(id,spec,deadline,state,receipt) VALUES(1,?,?,?,?)",
                (json.dumps(spec), time.time() + request["timeout_seconds"] if request["timeout_seconds"] else 0, "queued",
                 json.dumps({k: inspection[k] for k in (_INTERVAL_FIELDS if phase == "intervals" else _FIELDS)
                             if k in inspection}))).rowcount)
        row = _read(path)
        _require(row["spec"]["session_id"] == session_id and row["spec"]["actor_id"] == actor_id, "lp_job_owner_mismatch")
        _require({k: v for k, v in row["spec"].items()
                  if k not in {"session_id", "actor_id", "initial_matched", "own_completed_state_count"}}
                 == request, "lp_job_spec_mismatch")
        return _launch(config, path, turn_id) if created else _status(path)


def operation(config, args, session_id, actor_id, turn_id=None):
    _require(isinstance(args, dict) and args.get("action") in {"start", "status", "resume", "stop"}
             and args.get("dataset_kind", "lp_state_job") == "lp_state_job", "invalid_lp_job_action")
    _require(all(isinstance(v, str) and 0 < len(v) <= 200 for v in (session_id, actor_id)), "lp_job_owner_required")
    action = args["action"]
    request = {k: v for k, v in args.items() if k not in {"action", "dataset_kind"}}
    if action == "start":
        phase = request.get("phase", "states")
        _require(phase in {"states", "intervals"}, "invalid_lp_job_phase")
        if phase == "states":
            request.pop("phase", None)  # Explicit states is identical to the original default request.
        else:
            _require("max_requests" not in request or type(request["max_requests"]) is int
                     and 1 <= request["max_requests"] <= 10_000, "invalid_lp_job_budget")
            request.setdefault("max_requests", None)  # Internal only; external null is not a budget.
        budget_key = "max_requests" if phase == "intervals" else "max_states_total"
        ids = set(request) & {"strategy_id", "proposal_id"}
        _require(len(ids) == 1 and set(request) == ids | {"request_id", "expected_source_revision",
                 budget_key, "timeout_seconds"} | ({"phase"} if phase == "intervals" else set()), "exact_lp_job_spec_required")
        _require(isinstance(request["request_id"], str) and re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request["request_id"]),
                 "invalid_lp_job_request_id")
        _require(isinstance(request["expected_source_revision"], str)
                 and re.fullmatch(r"[0-9a-f]{64}", request["expected_source_revision"]), "invalid_lp_job_source_revision")
        _require((phase == "intervals" and request[budget_key] is None
                  or type(request[budget_key]) is int and 1 <= request[budget_key] <= 10_000)
                 and type(request["timeout_seconds"]) is int and 0 <= request["timeout_seconds"] <= 3600, "invalid_lp_job_budget")
        identity = request["request_id"] if phase == "states" else json.dumps({
            **{k: request[k] for k in ids | {"phase", "request_id", "expected_source_revision"}},
            "session_id": session_id, "actor_id": actor_id}, sort_keys=True)
        job_id = "lpjob_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    else:
        _require(set(request) == {"job_id"} and isinstance(request["job_id"], str)
                 and re.fullmatch(r"lpjob_[0-9a-f]{32}", request["job_id"]), "exact_lp_job_id_required")
        job_id = request["job_id"]
    root = Path(config.paths.artifacts) / "backtest_cache" / "lp-jobs"
    path = root / (job_id + ".sqlite3")
    _require(path.resolve().is_relative_to(config.paths.root.resolve())
             and not any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(config.paths.root)), "unsafe_lp_job_path")
    path = path.resolve()
    if action == "start":
        return _start(config, path, request, session_id, actor_id, turn_id)
    _require(path.exists(), "lp_job_not_found")
    row = _read(path)
    _require(row["spec"]["session_id"] == session_id and row["spec"]["actor_id"] == actor_id, "lp_job_owner_mismatch")
    if action == "resume":
        return _launch(config, path, turn_id)
    if action == "stop" and row["state"] not in _FINAL:
        _update(path, stop_requested=1)
        if row["task_id"]:
            TaskStore(config.paths).request_stop(row["task_id"])
    return _status(path)

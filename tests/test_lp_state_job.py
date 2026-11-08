"""Offline worker ownership, durable budget, cancellation and explicit resume."""
from threading import Event
from types import SimpleNamespace
from contextlib import closing
import hashlib
import json
import sqlite3
import time

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.data_api.types import DataApiError
from nerya.harness import cancellation
from nerya.subagents.tasks import TaskStore
from nerya.skills.builtin.backtest.scripts import lp_state_job as jobs
from nerya.tools.native.tasks import task_output_handler
from nerya.tools.types import ToolCall


@pytest.mark.parametrize("terminal", ["budget_exhausted", "deadline_exceeded", "foreign_progress", "failed_attempt", "no_deadline"])
def test_state_job_stop_resume_retains_budget_and_committed_checkpoint(tmp_path, monkeypatch, terminal):
    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    clock = [time.time()]
    monotonic = [100.0]
    fake_time = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: monotonic[0])
    monkeypatch.setattr(jobs, "time", fake_time)
    monkeypatch.setattr(cancellation, "time", fake_time)
    config = SimpleNamespace(paths=WorkspacePaths(tmp_path))
    store = TaskStore(config.paths)
    committed, fetched = set(), []
    paused, release = Event(), Event()
    revision = "a" * 64
    no_deadline = terminal == "no_deadline"
    required = 2 if no_deadline else 4
    request = dict(action="start", request_id="offline-job-001", strategy_id="lp_fixture",
                   expected_source_revision=revision, max_states_total=2, timeout_seconds=0 if no_deadline else 30)
    changed_request = {**request, "request_id": "offline-new-nonce", "max_states_total": 2850,
                       "timeout_seconds": 3600}

    def inspect(config, args, check_cancel=None):
        if check_cancel and len(committed) == 1 and not release.is_set():
            paused.set()
            assert release.wait(5), "test failed to release paused worker"
        if check_cancel:
            check_cancel()
        missing = next((i for i in range(required) if i not in committed), None)
        return dict(source_revision=revision, required_state_count=required, matched_state_count=len(committed),
            missing_state_count=required-len(committed), next_download_args=None if missing is None else dict(action="download",
                dataset_kind="lp_state_requirements", strategy_id="lp_fixture", expected_source_revision=revision,
                expected_next_timestamp=str(missing), max_states=1))

    def download(config, args, *, check_cancel, cancel_token):
        assert isinstance(cancel_token, cancellation.CancelToken) and args["max_states"] == 1
        path = next((tmp_path / "artifacts/backtest_cache/lp-jobs").glob("*.sqlite3"))
        assert jobs._read(path)["attempts"] == len(fetched) + 1  # Charge committed before IO.
        assert cancel_token.deadline_s == (None if no_deadline else clock[0] + 30)
        check_cancel()
        checkpoint = int(args["expected_next_timestamp"])
        assert checkpoint not in committed
        fetched.append(checkpoint)
        if terminal == "failed_attempt" and checkpoint == 1:
            raise DataApiError("archive unavailable sk-abcdefghijklmnopqrstuvwx", kind="historical_rpc_error",
                retryable=True, detail={"provider": "fixture_rpc", "action": "pool_state", "reason": "rpc_rejected",
                    "http_status": 429, "rpc_error_code": -32005, "rpc_method": "eth_call", "failure_stage": "rpc_error",
                    "raw_body": "private-provider-body", "api_key": "private-secret", "nested": {"secret": "private"}})
        committed.add(checkpoint)
        # Return committed state without entering the inspector's next-iteration pause.
        return {**inspect(config, args),
                "downloaded_state_count": 0 if terminal == "foreign_progress" and checkpoint == 1 else 1}

    monkeypatch.setattr(jobs, "inspect_lp_state_requirements", inspect)
    monkeypatch.setattr(jobs, "download_lp_states", download)
    first = jobs.operation(config, request, "session", "actor", "turn")
    path = (tmp_path / "artifacts/backtest_cache/lp-jobs" / (first["job_id"] + ".sqlite3")).resolve()
    controls = dict(job_id=first["job_id"])
    assert paused.wait(5)
    assert first["next_status_call"] == {"tool": "task_output", "arguments": {
        "task_id": first["task_id"], "wait_for_completion": True}}
    if no_deadline:
        assert first["deadline"] == 0
        clock[0] += 7200
        assert jobs.operation(config, {**controls, "action": "status"}, "session", "actor")["status"] == "running"
    assert jobs.operation(config, request, "session", "actor")["task_id"] == first["task_id"]
    original = jobs._read(path)
    reused = jobs.operation(config, changed_request, "session", "actor")
    assert reused["job_id"] == first["job_id"] and reused["task_id"] == first["task_id"]
    assert reused["request_id"] == request["request_id"] and reused["max_states_total"] == 2
    assert reused["attempts_reserved"] == reused["completed_state_count"] == 1
    assert reused["deadline"] == first["deadline"] and jobs._read(path)["spec"] == original["spec"]
    assert len(list(path.parent.glob("*.sqlite3"))) == len(store.list(limit=10)) == 1
    assert jobs.operation(config, {**controls, "action": "resume"}, "session", "actor")["task_id"] == first["task_id"]
    with pytest.raises(ValueError, match="owner_mismatch"):
        jobs.operation(config, {**controls, "action": "stop"}, "session", "other")
    with pytest.raises(ValueError, match="spec_mismatch"):
        jobs.operation(config, {**request, "timeout_seconds": 1 if no_deadline else 0}, "session", "actor")
    stopped = jobs.operation(config, {**controls, "action": "stop"}, "session", "actor")
    assert stopped["stop_requested"] is True
    release.set()
    jobs._HANDLES[str(path)].join(5)
    assert not jobs._HANDLES[str(path)].is_alive()
    assert store.load(first["task_id"]).state == "cancelled"
    stopped = jobs.operation(config, {**controls, "action": "status"}, "session", "actor")
    assert stopped["status"] == "stopped" and stopped["attempts_reserved"] == 1
    assert stopped["completed_state_count"] == 1 and fetched == [0]
    assert jobs.operation(config, request, "session", "actor")["status"] == "stopped"
    # Persisted TaskStore state alone cannot prove an active worker, nor start one on read.
    jobs._update(path, state="running")
    store.update_state(first["task_id"], "running")
    before = path.read_bytes()
    stale = jobs.operation(config, {**controls, "action": "status"}, "session", "actor")
    assert stale["status"] == "interrupted" and stale["worker_status"] == "owner_unconfirmed"
    assert path.read_bytes() == before and fetched == [0]
    if terminal in {"budget_exhausted", "foreign_progress", "no_deadline"}:
        jobs._update(path, state="stopped")  # Exercise both stopped and interrupted target reuse.
    if terminal == "deadline_exceeded":
        clock[0] = first["deadline"] + 1
        resumed = jobs.operation(config, {**controls, "action": "resume"}, "session", "actor")
    else:
        resumed = jobs.operation(config, changed_request, "session", "actor")
        assert resumed["job_id"] == first["job_id"] and resumed["request_id"] == request["request_id"]
        assert resumed["max_states_total"] == 2 and resumed["deadline"] == first["deadline"]
        assert len(list(path.parent.glob("*.sqlite3"))) == 1
    jobs._HANDLES[str(path)].join(5)
    if terminal == "failed_attempt":
        failed = jobs.operation(config, {**controls, "action": "status"}, "session", "actor")
        assert failed["status"] == "interrupted"
        failure = failed["failure"]
        assert failure == {"kind": "historical_rpc_error", "message": "archive unavailable ***REDACTED***",
            "retryable": True, "detail": {"provider": "fixture_rpc", "action": "pool_state", "reason": "rpc_rejected",
                "http_status": 429, "rpc_error_code": -32005, "rpc_method": "eth_call", "failure_stage": "rpc_error"}}
        assert jobs._read(path)["receipt"]["failure"] == failure
        output = task_output_handler(ToolCall(name="task_output", arguments={
            "task_id": resumed["task_id"], "wait_for_completion": True}), store=TaskStore(config.paths)).content[0].data
        assert output["state"] == "failed" and output["error_kind"] == failure["kind"]
        assert output["output"]["failure"] == output["output"]["receipt"]["failure"] == failure
        assert output["error"] == "historical_rpc_error: archive unavailable ***REDACTED***"
        assert not any(secret in str(output) for secret in ("sk-abcdefghijklmnopqrstuvwx", "private-provider-body", "private-secret"))
        jobs.operation(config, {**controls, "action": "resume"}, "session", "actor")
        jobs._HANDLES[str(path)].join(5)
    final = jobs.operation(config, {**controls, "action": "status"}, "session", "actor")
    assert final["status"] == ("states_acquired" if no_deadline else "deadline_exceeded" if terminal == "deadline_exceeded" else "budget_exhausted")
    assert final["deadline"] == first["deadline"]
    assert fetched == ([0] if terminal == "deadline_exceeded" else [0, 1])
    assert final["attempts_reserved"] == len(fetched)
    assert final["completed_state_count"] == (2 if terminal == "budget_exhausted" or no_deadline else 1)
    assert final["completed_state_count_known"] is True
    if terminal == "foreign_progress":
        assert final["receipt"]["matched_state_count"] == 2  # Shared progress is not this job's success.
    assert final["remaining_attempts"] == 2-len(fetched)
    assert final["receipt"]["missing_state_count"] == required-len(committed)
    assert all(final[k] is False for k in ("coverage_complete", "requested_window_complete", "replay_input",
        "range_continuity_verified", "tick_fee_reconstruction_verified", "performance_evidence", "financial_execution_allowed"))
    if terminal != "deadline_exceeded":
        assert resumed["task_id"] != first["task_id"]
        record = store.load(final["task_id"])
        assert record.state == ("succeeded" if terminal == "budget_exhausted" or no_deadline else "failed")
        assert record.output["status"] == ("states_acquired" if no_deadline else "budget_exhausted")
        if record.state == "succeeded":
            assert record.error is record.error_kind is None
    assert len(store.list(limit=10)) == (1 if terminal == "deadline_exceeded" else 3 if terminal == "failed_attempt" else 2)
    # Missing legacy attribution remains unknown, without a status-read migration.
    import json
    saved = jobs._read(path)["spec"]
    saved.pop("own_completed_state_count")
    jobs._update(path, spec=json.dumps(saved))
    before = path.read_bytes()
    legacy = jobs.operation(config, {**controls, "action": "status"}, "session", "actor")
    assert legacy["completed_state_count"] is None and legacy["completed_state_count_known"] is False
    assert path.read_bytes() == before
    # Final batches admit a new nonce, while the original alias keeps its old receipt even on a new source.
    with monkeypatch.context() as patch:
        patch.setattr(jobs, "_launch", lambda config, path, turn_id: jobs._status(path))
        next_batch = jobs.operation(config, changed_request, "session", "actor")
        assert next_batch["job_id"] != first["job_id"] and next_batch["attempts_reserved"] == 0
        assert next_batch["max_states_total"] == 2850 and next_batch["completed_state_count"] == 0
        next_revision = "b" * 64
        patch.setattr(jobs, "inspect_lp_state_requirements", lambda *args: {
            **inspect(config, {}), "source_revision": next_revision})
        old = jobs.operation(config, request, "session", "actor")
        assert old["job_id"] == first["job_id"] and old["source_revision"] == revision
        new_source = jobs.operation(config, {**changed_request, "request_id": "offline-new-source",
            "expected_source_revision": next_revision}, "session", "actor")
        assert new_source["job_id"] not in {first["job_id"], next_batch["job_id"]}
        assert new_source["source_revision"] == next_revision and new_source["attempts_reserved"] == 0
    # Only durable stop polling is throttled; local cancellation/deadline are immediate.
    polls, durable_stop = [], [False]
    with monkeypatch.context() as patch:
        def stop_row(path):
            polls.append(monotonic[0])
            return {"stop_requested": durable_stop[0]}
        patch.setattr(jobs, "_read", stop_row)
        token = jobs._JobToken(path, clock[0] + 30, Event())
        assert token.is_set is False and len(polls) == 1  # First poll is immediate.
        for i in range(1000):
            monotonic[0] = 100 + i / 1000
            assert token.is_set is False
        assert polls == [100.0, 100.25, 100.5, 100.75]
        durable_stop[0] = True
        assert token.is_set is False and len(polls) == 4
        monotonic[0] = 101.0
        assert token.is_set is True and token.reason == "stopped" and len(polls) == 5
        durable_stop[0] = False
        local_event = Event()
        local = jobs._JobToken(path, clock[0] + 30, local_event)
        assert local.is_set is False
        count = len(polls)
        local_event.set()
        assert local.is_set is True and len(polls) == count
        deadline = jobs._JobToken(path, clock[0] + 0.01, Event())
        assert deadline.is_set is False
        count = len(polls)
        clock[0] += 0.02
        assert deadline.is_set is True and deadline.reason == "deadline_exceeded" and len(polls) == count


@pytest.mark.parametrize("owner", ["stopped", "local", "remote", "unconfirmed", "legacy_unknown"])
def test_start_selects_active_owner_then_own_progress_across_legacy_duplicates(tmp_path, monkeypatch, owner):
    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    config = SimpleNamespace(paths=WorkspacePaths(tmp_path))
    request = dict(action="start", request_id="original-request", proposal_id="lp_fixture",
        expected_source_revision="a" * 64, max_states_total=100, timeout_seconds=0)
    monkeypatch.setattr(jobs, "inspect_lp_state_requirements", lambda *args: {
        "source_revision": request["expected_source_revision"], "matched_state_count": 500})
    launch = jobs._launch
    monkeypatch.setattr(jobs, "_launch", lambda config, path, turn_id: jobs._status(path))
    first = jobs.operation(config, request, "session", "actor")
    root = tmp_path / "artifacts/backtest_cache/lp-jobs"
    original = root / (first["job_id"] + ".sqlite3")
    spec = jobs._read(original)["spec"]
    spec["own_completed_state_count"] = 77
    if owner == "legacy_unknown":
        spec.pop("own_completed_state_count")
    jobs._update(original, state="stopped", attempts=82, spec=json.dumps(spec))
    duplicate_request = {**request, "request_id": "legacy-duplicate"}
    duplicate = root / ("lpjob_" + hashlib.sha256(duplicate_request["request_id"].encode()).hexdigest()[:32] + ".sqlite3")
    with closing(sqlite3.connect(original)) as source, closing(sqlite3.connect(duplicate)) as target:
        source.backup(target)
    jobs._update(duplicate, state="running" if owner in {"remote", "unconfirmed"} else "stopped", attempts=4,
        spec=json.dumps({**spec, "request_id": duplicate_request["request_id"], "own_completed_state_count": 3}))
    if owner == "local":
        monkeypatch.setitem(jobs._HANDLES, str(duplicate), SimpleNamespace(is_alive=lambda: True))
    changed = {**request, "request_id": "brand-new-request", "max_states_total": 2850, "timeout_seconds": 3600}
    before = {path: path.read_bytes() for path in (original, duplicate)}
    if owner == "remote":
        # Only the per-job lock proves another process owns this worker; persisted running is insufficient.
        monkeypatch.setattr(jobs, "_launch", launch)
        with jobs.trading_lock(config.paths, "lp-state-job:" + duplicate.stem) as acquired:
            assert acquired
            result = jobs.operation(config, changed, "session", "actor")
        assert result["worker_status"] == "owner_unconfirmed" and result["status"] == "interrupted"
    else:
        result = jobs.operation(config, changed, "session", "actor")
    selected = duplicate if owner in {"local", "remote", "legacy_unknown"} else original
    assert result["job_id"] == selected.stem and result["request_id"] == jobs._read(selected)["spec"]["request_id"]
    assert result["attempts_reserved"] == (82 if selected == original else 4)
    assert result["completed_state_count"] == (77 if selected == original else 3)
    assert result["max_states_total"] == 100 and result["deadline"] == 0
    assert len(list(root.glob("*.sqlite3"))) == 2
    assert all(path.read_bytes() == data for path, data in before.items())
    assert jobs._status(original)["completed_state_count_known"] == (owner != "legacy_unknown")
    with pytest.raises(ValueError, match="spec_mismatch"):
        jobs.operation(config, {**request, "max_states_total": 2850}, "session", "actor")
    if owner == "stopped":
        target = {k: request[k] for k in ("proposal_id", "expected_source_revision")}
        target.update(session_id="session", actor_id="actor")
        with jobs.trading_lock(config.paths, "lp-state-target:" + json.dumps(target, sort_keys=True)) as acquired:
            assert acquired
            with pytest.raises(ValueError, match="lp_job_start_busy"):
                jobs.operation(config, changed, "session", "actor")
        assert len(list(root.glob("*.sqlite3"))) == 2


@pytest.mark.parametrize("mode", ["recover", "exhaust", "budget", "permanent", "unknown", "stop", "deadline", "source_changed"])
def test_worker_recovers_known_reads_in_same_task_with_original_limits(tmp_path, monkeypatch, mode):
    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    clock, monotonic, waits, fetched, committed = [time.time()], [100.0], [], [], set()
    fake_time = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: monotonic[0])
    monkeypatch.setattr(jobs, "time", fake_time)
    monkeypatch.setattr(cancellation, "time", fake_time)
    config = SimpleNamespace(paths=WorkspacePaths(tmp_path))
    revision = "a" * 64
    request = dict(action="start", request_id="offline-retry-001", strategy_id="lp_fixture",
        expected_source_revision=revision, max_states_total=1 if mode == "budget" else 5, timeout_seconds=30)
    failure = DataApiError("Event slice RPC failure", kind="historical_rpc_error",
        retryable=mode not in {"permanent", "unknown"}, detail={"provider": "uniswap", "action": "pool_event_slice",
            "rpc_method": "eth_call", "failure_stage": "rpc_error", "rpc_error_code": 30,
            "rpc_error_message": "access denied" if mode == "permanent" else "unclassified failure" if mode == "unknown"
                else "backend request failed ***REDACTED***", "raw_body": "private-body"})

    def inspect(config, args, check_cancel=None):
        if check_cancel:
            check_cancel()
        missing = next((i for i in range(2) if i not in committed), None)
        return dict(source_revision="b" * 64 if mode == "source_changed" and fetched else revision,
            required_state_count=2, matched_state_count=len(committed), missing_state_count=2-len(committed),
            next_download_args=None if missing is None else dict(action="download", strategy_id="lp_fixture",
                expected_source_revision=revision, expected_next_timestamp=str(missing), max_states=1))

    def download(config, args, *, check_cancel, cancel_token):
        check_cancel()
        assert args["max_states"] == 1 and args["expected_source_revision"] == revision
        path = next((tmp_path / "artifacts/backtest_cache/lp-jobs").glob("*.sqlite3"))
        row = jobs._read(path)
        assert row["attempts"] == len(fetched) + 1  # Every retry is charged before the read.
        assert row["deadline"] == clock_start + 30 and row["spec"]["max_states_total"] == request["max_states_total"]
        checkpoint = int(args["expected_next_timestamp"])
        fetched.append(checkpoint)
        if mode != "recover" or len(fetched) == 1:
            raise failure
        committed.add(checkpoint)
        return {**inspect(config, args), "downloaded_state_count": 1}

    def wait(token, delay):
        waits.append(delay)
        monotonic[0] += delay
        clock[0] += delay
        if mode == "stop":
            token.cancel("stopped")
        if mode == "deadline":
            clock[0] = token.deadline_s + 1
        return token.is_set

    monkeypatch.setattr(jobs, "inspect_lp_state_requirements", inspect)
    monkeypatch.setattr(jobs, "download_lp_states", download)
    monkeypatch.setattr(jobs._JobToken, "wait", wait)
    clock_start = clock[0]
    first = jobs.operation(config, request, "session", "actor", "turn")
    path = (tmp_path / "artifacts/backtest_cache/lp-jobs" / (first["job_id"] + ".sqlite3")).resolve()
    jobs._HANDLES[str(path)].join(5)
    assert not jobs._HANDLES[str(path)].is_alive()
    final = jobs.operation(config, {"action": "status", "job_id": first["job_id"]}, "session", "actor")
    records = TaskStore(config.paths).list(limit=10)
    assert len(records) == 1 and final["task_id"] == first["task_id"]  # No polling/resume/model continuation.
    assert final["attempts_reserved"] == len(fetched) == (3 if mode in {"recover", "exhaust"} else 1)
    assert final["deadline"] == clock_start + 30 and final["source_revision"] == revision
    assert final["remaining_attempts"] == request["max_states_total"] - len(fetched)
    assert final["completed_state_count"] == len(committed)
    assert final["status"] == {"recover": "states_acquired", "stop": "stopped", "deadline": "deadline_exceeded",
                               "source_changed": "source_changed"}.get(mode, "interrupted")
    if mode in {"permanent", "unknown", "budget"}:
        assert waits == []
    elif mode == "exhaust":
        assert sum(waits) == 1.5  # Existing policy: two retries, 0.5s then 1s.
    elif mode in {"recover", "source_changed"}:
        assert sum(waits) == 0.5
    if mode in {"exhaust", "budget", "permanent", "unknown"}:
        assert records[0].state == "failed" and records[0].error_kind == failure.kind
        assert final["failure"]["kind"] == failure.kind and final["failure"]["retryable"] == failure.retryable
        assert final["failure"]["detail"]["rpc_error_message"] == failure.detail["rpc_error_message"]
        assert "private-body" not in str(records[0].output)
    if mode == "recover":
        assert fetched == [0, 0, 1] and records[0].state == "succeeded" and final["failure"] is None
    assert all(final[key] is False for key in ("coverage_complete", "requested_window_complete", "replay_input",
        "range_continuity_verified", "tick_fee_reconstruction_verified", "performance_evidence", "financial_execution_allowed"))


def test_interval_phase_thread_resume_isolation_and_real_event_cursor(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from threading import Thread
    from nerya.core import yaml_io
    from nerya.data_api.uniswap_events import UniswapEventProvider
    from nerya.skills.builtin.backtest.scripts.lp_state_requirements import inspect_lp_state_requirements
    from nerya.tools.native.historical_data import historical_data_handler, HISTORICAL_DATA_SCHEMA
    from test_uniswap_history_data import Fake, POOL, TOKEN0, TOKEN1

    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    config = SimpleNamespace(paths=WorkspacePaths(tmp_path), get=lambda key: "actor")
    root = tmp_path / "strategies/lp_fixture"
    root.mkdir(parents=True)
    yaml_io.dump(root / "strategy.yml", dict(id="lp_fixture", mode="paper", entrypoint="main.py:run",
        timeframe="1h", schedule=dict(type="cron", cron="*/5 * * * *", enabled=False), params=dict(
            lp_source=dict(chain="Ethereum", chain_id=1, protocol="uniswap_v3", pool_id=POOL,
                token0=TOKEN0, token1=TOKEN1, decimals=[6, 18], fee=3000, tick_spacing=60,
                tick_lower=-120, tick_upper=120, source_id="lp_fixture", token_id=None),
            lp_replay=dict(version=1, dataset_id="lp_history", mode="cadence",
                initial_token0_units="1000", initial_token1_units="1", action_cost_token1="0.0001"))))
    (root / "main.py").write_text("def run(ctx): return ctx.result.hold()\n")
    (root / "replay_inputs.json").write_text(json.dumps({"version": 1, "datasets": [{
        "id": "lp_history", "provider": "uniswap", "action": "pool_history", "args": dict(
            pool_address=POOL, tick_lower=-120, tick_upper=120, start="2026-10-01T00:00:00Z",
            end="2026-10-01T00:10:00Z", step_blocks=1, max_samples=64)}]}))
    epoch = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())

    def mutate(req, doc, transport):
        if req["method"] == "eth_getBlockByNumber":
            block = int(req["params"][0], 16)
            doc["result"].update(timestamp=hex(epoch + block - 99), parentHash="0x" + format(block, "064x"))
        return 200, doc

    class Rpc(Fake):
        def request(self, method, url, **kwargs):
            req = kwargs["body"]
            if isinstance(req, dict) and req["method"] == "eth_getLogs":
                return 200, {"jsonrpc": "2.0", "id": req["id"], "result": []}
            return super().request(method, url, **kwargs)

    producer = UniswapEventProvider(Rpc(mutate=mutate))
    event_operation = jobs.event_history_operation
    for block in (100, 400, 700):
        event_operation(config, dict(action="download", pool_address=POOL, rpc_provider="publicnode",
            tick_lower=-120, tick_upper=120, start_block=block, end_block=block), provider=producer)
    inspect_args = dict(strategy_id="lp_fixture")
    initial = inspect_lp_state_requirements(config, inspect_args)
    assert initial["missing_state_count"] == 0 and initial["missing_interval_count"] == 2
    request = dict(action="start", request_id="shared-phase-request", strategy_id="lp_fixture",
        expected_source_revision=initial["source_revision"], max_states_total=7, timeout_seconds=30)
    with monkeypatch.context() as patch:
        patch.setattr(jobs, "_launch", lambda config, path, turn_id: jobs._status(path))
        states = jobs.operation(config, request, "session", "actor")
        explicit = jobs.operation(config, {**request, "phase": "states"}, "session", "actor")
        assert explicit["job_id"] == states["job_id"] == "lpjob_" + hashlib.sha256(
            request["request_id"].encode()).hexdigest()[:32]
    job_root = config.paths.artifacts / "backtest_cache/lp-jobs"
    states_path = job_root / (states["job_id"] + ".sqlite3")
    jobs._update(states_path, state="running", attempts=3)
    states_before = states_path.read_bytes()
    paused, release, waiting = Event(), Event(), Event()
    calls, errors = [], [DataApiError("unclassified", retryable=None)]

    def inspect(config, args, check_cancel=None):
        receipt = inspect_lp_state_requirements(config, args, check_cancel)
        if check_cancel and len(calls) == 1 and not release.is_set():
            paused.set()
            assert release.wait(5)
            check_cancel()
        return receipt

    def download(config, args, *, check_cancel, cancel_token):
        expected = inspect_lp_state_requirements(config, inspect_args)["next_interval_download_args"]
        assert args == expected  # Full identity, unchanged bounds and producer cursor.
        calls.append(dict(args))
        if len(calls) == 2 and errors:
            raise errors.pop()
        return event_operation(config, args, provider=producer, check_cancel=check_cancel, cancel_token=cancel_token)

    monkeypatch.setattr(jobs, "inspect_lp_state_requirements", inspect)
    monkeypatch.setattr(jobs, "event_history_operation", download)
    interval_request = {k: v for k, v in request.items() if k != "max_states_total"}
    interval_request.update(phase="intervals", max_requests=3)
    assert HISTORICAL_DATA_SCHEMA["properties"]["phase"]["enum"] == ["states", "intervals"]
    def native(args):
        return historical_data_handler(ToolCall(name="historical_data", arguments={
            "dataset_kind": "lp_state_job", **args}, metadata={"session_id": "session"}), config=config).content[0].data
    first = native(interval_request)
    path = job_root / (first["job_id"] + ".sqlite3")
    assert first["job_id"] != states["job_id"] and paused.wait(5)
    worker = jobs._HANDLES[str(path)]
    assert isinstance(worker, Thread) and worker.is_alive()
    outputs = []
    def wait():
        waiting.set()
        outputs.append(task_output_handler(ToolCall(name="task_output",
            arguments=first["next_status_call"]["arguments"]), store=TaskStore(config.paths)).content[0].data)
    waiter = Thread(target=wait)
    waiter.start()
    try:
        assert waiting.wait(5) and waiter.is_alive() and not outputs
        reused = native({**interval_request, "request_id": "different-interval-nonce", "max_requests": 999,
                         "timeout_seconds": 3600})
        assert reused["task_id"] == first["task_id"] and reused["max_requests"] == 3
        assert reused["deadline"] == first["deadline"]
        with pytest.raises(ValueError, match="spec_mismatch"):
            jobs.operation(config, {**interval_request, "max_requests": 9}, "session", "actor")
        with pytest.raises(ValueError, match="owner_mismatch"):
            jobs.operation(config, dict(action="stop", job_id=first["job_id"]), "session", "other")
    finally:
        release.set()
        worker.join(5)
        waiter.join(5)
    assert not worker.is_alive() and not waiter.is_alive()
    failed = outputs[0]["output"]
    assert outputs[0]["state"] == "failed" and failed["status"] == "interrupted"
    assert failed["attempts_reserved"] == 2 and len(calls) == 2  # Unknown failure never retried.
    assert failed["receipt"]["next_interval_download_args"]["expected_next_block"] == 200
    jobs._HANDLES.pop(str(path))  # Recover solely from persisted records, as after restart.
    saved = path.read_bytes()
    assert native(dict(action="status", job_id=first["job_id"]))["status"] == "interrupted"
    assert path.read_bytes() == saved
    resumed = native(dict(action="resume", job_id=first["job_id"]))
    assert resumed["job_id"] == first["job_id"] and resumed["task_id"] != first["task_id"]
    completed = task_output_handler(ToolCall(name="task_output", arguments=resumed["next_status_call"]["arguments"]),
        store=TaskStore(config.paths)).content[0].data
    jobs._HANDLES[str(path)].join(5)
    capped = completed["output"]
    assert completed["state"] == "failed" and capped["status"] == "budget_exhausted"
    assert capped["max_requests"] == capped["attempts_reserved"] == 3 and capped["remaining_attempts"] == 0
    assert capped["deadline"] == first["deadline"] and len(calls) == 3
    assert native(interval_request)["job_id"] == first["job_id"]  # Original receipt cannot reset its cap.
    full_request = {k: v for k, v in interval_request.items() if k != "max_requests"}
    full_request.update(request_id="complete-saved-window", timeout_seconds=0)
    for invalid in (None, 0, True):
        with pytest.raises(ValueError, match="invalid_lp_job_budget"):
            jobs.operation(config, {**full_request, "max_requests": invalid}, "session", "actor")
    full = native(full_request)
    full_path = job_root / (full["job_id"] + ".sqlite3")
    completed = task_output_handler(ToolCall(name="task_output", arguments=full["next_status_call"]["arguments"]),
        store=TaskStore(config.paths)).content[0].data
    jobs._HANDLES[str(full_path)].join(5)
    final = completed["output"]
    assert completed["state"] == "succeeded" and final["status"] == "intervals_acquired"
    assert final["phase"] == "intervals" and final["max_requests"] is None and "max_states_total" not in final
    assert final["attempts_reserved"] == 4 and final["remaining_attempts"] is None and final["deadline"] == 0
    assert [a["expected_next_block"] for a in calls] == [100, 200, 200, 300, 400, 500, 600]
    assert final["receipt"]["verified_interval_count"] == 2 and final["receipt"]["range_continuity_verified"] is True
    assert final["range_continuity_verified"] is True
    assert all(final[key] is False for key in ("coverage_complete", "requested_window_complete", "replay_input",
        "tick_fee_reconstruction_verified", "performance_evidence", "financial_execution_allowed"))
    assert states_path.read_bytes() == states_before and len(list(job_root.glob("*.sqlite3"))) == 3
    old = native(dict(action="status", job_id=states["job_id"]))
    assert old["phase"] == "states" and old["max_states_total"] == 7 and old["attempts_reserved"] == 3
    assert old["remaining_attempts"] == 4 and "phase" not in jobs._read(states_path)["spec"]
    # Even an uncapped job cannot spin on a successful no-op or certify an absent bridge.
    monkeypatch.setattr(jobs, "inspect_lp_state_requirements", lambda *args: initial)
    monkeypatch.setattr(jobs, "event_history_operation", lambda *args, **kwargs: {"event_range_complete": True})
    stalled = native({**full_request, "request_id": "stalled-event-cursor"})
    stalled_path = job_root / (stalled["job_id"] + ".sqlite3")
    jobs._HANDLES[str(stalled_path)].join(5)
    stopped = native(dict(action="status", job_id=stalled["job_id"]))
    assert stopped["attempts_reserved"] == 1 and stopped["failure"]["message"] == "lp_interval_cursor_not_advanced"
    monkeypatch.setattr(jobs, "inspect_lp_state_requirements", lambda *args: {
        **initial, "next_interval_download_args": None})
    native(dict(action="resume", job_id=stalled["job_id"]))
    jobs._HANDLES[str(stalled_path)].join(5)
    blocked = native(dict(action="status", job_id=stalled["job_id"]))
    assert blocked["attempts_reserved"] == 1 and blocked["failure"]["message"] == "lp_interval_continuity_unverified"

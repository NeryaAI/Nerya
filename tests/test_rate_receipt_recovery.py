"""Recover bounded host rate operations without certifying requested delivery."""
import hashlib
import json

import pytest

from nerya.core.runtime_identity import SDK_BUILD_ID
from nerya.strategies.verification import (
    completed_replay_receipt, replay_status, source_revision,
)
from nerya.strategies.workflow_service import _read_files

pytestmark = pytest.mark.smoke


@pytest.fixture
def producer(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "main.py").write_text("def run(ctx): return None\n")
    (root / "strategy.yml").write_text("id: rates\nentrypoint: main.py:run\nmode: paper\n")
    revision = source_revision(_read_files(root)[0])
    run = root / "backtests" / "rates_20261028_000000"
    inputs = run / "prepared_inputs"
    inputs.mkdir(parents=True)
    sources = [{"sha256": "a" * 64}]
    payload = {
        "rows": [{"timestamp": "2026-01-01T00:00:00Z", "deposit_apy_percent": 3},
                 {"timestamp": "2026-01-02T00:00:00Z", "deposit_apy_percent": 4}],
        "_envelope": {"mock": False, "observation_kind": "historical_rate_observations",
                      "sources": sources},
    }
    blob = json.dumps(payload).encode()
    (inputs / "rates.json").write_bytes(blob)
    metrics = {
        "backtest_kind": "rate_policy_replay", "evaluation_mode": "simulation",
        "performance_evidence": False, "verdict": "PASS", "coverage_ok": False,
        "requested_window_complete": False, "requested_window_days": 365,
        "start_utc": "2026-01-01T00:00:00Z", "end_utc": "2026-01-02T00:00:00Z",
        "replay": {"scope": "historical_rate_policy_simulation",
                   "strategy_execution_verified": True, "financial_execution_allowed": False,
                   "errors": 0, "sdk_errors": 0, "order_accounting_ok": True},
        "provenance": {
            "version": 1, "strategy_id": "rates", "proposal_id": "prp_rates",
            "source_revision": revision, "source_changed_during_run": False,
            "sdk_build_id": SDK_BUILD_ID, "data_kind": "historical_observations",
            "scope": "historical_rate_policy_simulation",
            "isolation": {"source_revision": revision, "network": "denied",
                          "process_spawn": "denied", "process_exited": True,
                          "process_group_stopped": True},
            "datasets": [{"id": "rates", "snapshot_file": "prepared_inputs/rates.json",
                          "data_kind": "historical_observations", "rows": 2,
                          "sha256": hashlib.sha256(blob).hexdigest(), "sources": sources}],
        },
    }
    state = {"version": 1, "status": "completed", "strategy_id": "rates",
             "proposal_id": "prp_rates", "backtest_ts": run.name,
             "source_revision": revision, "performance_evidence": False}
    (run / "report.md").write_text("# Historical rate-policy simulation\n")
    save(run, metrics, state)
    return root, run, metrics, state, revision


def save(run, metrics, state):
    (run / "metrics.json").write_text(json.dumps(metrics))
    (run / "run.json").write_text(json.dumps(state))


def test_completed_partial_operation_preserves_delivery_limits(producer):
    root, run, metrics, _, revision = producer
    receipt = completed_replay_receipt(root, "rates", "prp_rates")
    assert receipt is not None
    assert receipt["status"] == "completed" and receipt["operation_completed"] is True
    assert receipt["requested_window_complete"] is False
    assert receipt["performance_evidence"] is False
    assert receipt["scope"] == "historical_rate_policy_simulation"
    assert receipt["source_revision"] == revision
    assert receipt["metrics_path"] == str(run / "metrics.json")


@pytest.mark.parametrize("complete", [False, True])
def test_workflow_recovers_operation_but_never_verifies_trading(producer, complete):
    root, run, metrics, state, revision = producer
    metrics.update(requested_window_complete=complete, coverage_ok=complete)
    save(run, metrics, state)
    status = replay_status({"id": run.name, "metrics": metrics}, revision,
                           "rates", "prp_rates", root=root)
    assert status["status"] == "limited"
    assert status["operation_completed"] is True
    assert status["requested_window_complete"] is complete
    assert status["scope"] == "historical_rate_policy_simulation"
    assert status["performance_evidence"] is False
    assert status["metrics"]["coverage_ok"] is complete


@pytest.mark.parametrize("section,key,value", [
    ("provenance", "sdk_build_id", "old-sdk"),
    ("provenance", "strategy_id", "other"),
    ("provenance", "proposal_id", "other"),
    ("provenance", "source_revision", "old-source"),
    ("provenance", "source_changed_during_run", True),
    ("provenance", "version", 0),
    ("provenance", "data_kind", "sample"),
    ("provenance", "isolation", {}),
    ("replay", "strategy_execution_verified", False),
    ("replay", "scope", "historical_code_replay"),
    ("replay", "financial_execution_allowed", True),
    ("replay", "errors", 1),
    ("replay", "sdk_errors", 1),
    ("replay", "order_accounting_ok", False),
    (None, "performance_evidence", True),
    (None, "evaluation_mode", "trading"),
    (None, "backtest_kind", "freeform"),
    ("state", "status", "failed"),
    ("state", "status", "running"),
    ("state", "preflight_only", True),
    ("state", "strategy_id", "other"),
    ("state", "proposal_id", "other"),
    ("state", "source_revision", "old-source"),
    ("state", "backtest_ts", "other-run"),
    ("state", "performance_evidence", True),
])
def test_unbound_or_failed_outputs_do_not_recover(producer, section, key, value):
    root, run, metrics, state, _ = producer
    target = state if section == "state" else metrics[section] if section else metrics
    target[key] = value
    save(run, metrics, state)
    assert completed_replay_receipt(root, "rates", "prp_rates") is None


@pytest.mark.parametrize("artifact", ["main.py", "strategy.yml", "snapshot", "source_hash",
                                     "snapshot_symlink", "inputs_symlink", "run_symlink",
                                     "oversized_snapshot", "missing_run"])
def test_source_and_persisted_snapshot_integrity(producer, tmp_path, artifact):
    root, run, metrics, state, _ = producer
    snapshot = run / "prepared_inputs/rates.json"
    if artifact in {"main.py", "strategy.yml"}:
        path = root / artifact
        path.write_text(path.read_text() + "\n# changed\n")
    elif artifact == "snapshot":
        snapshot.write_text(snapshot.read_text() + " ")
    elif artifact == "source_hash":
        metrics["provenance"]["datasets"][0]["sources"][0]["sha256"] = "invalid"
        save(run, metrics, state)
    elif artifact in {"snapshot_symlink", "run_symlink"}:
        path = snapshot if artifact == "snapshot_symlink" else run / "run.json"
        outside = tmp_path / "outside.json"
        path.rename(outside)
        path.symlink_to(outside)
    elif artifact == "inputs_symlink":
        snapshot.parent.rename(tmp_path / "outside-inputs")
        (run / "prepared_inputs").symlink_to(tmp_path / "outside-inputs", target_is_directory=True)
    elif artifact == "oversized_snapshot":
        snapshot.write_bytes(b" " * 4_000_001)
    else:
        (run / "run.json").unlink()
    assert completed_replay_receipt(root, "rates", "prp_rates") is None


def test_metrics_alone_never_attest_completed_operation(producer):
    _, run, metrics, _, revision = producer
    status = replay_status({"id": run.name, "metrics": metrics}, revision, "rates", "prp_rates")
    assert status["status"] == "limited"
    assert status["operation_completed"] is False


def test_new_source_marks_rate_operation_stale(producer):
    root, run, metrics, _, _ = producer
    (root / "main.py").write_text("def run(ctx): return 'changed'\n")
    status = replay_status({"id": run.name, "metrics": metrics},
                           source_revision(_read_files(root)[0]), "rates", "prp_rates", root=root)
    assert status["status"] == "stale"
    assert status["operation_completed"] is False


def test_finalize_rechecks_saved_index_observations_without_replay(producer):
    from test_task_delivery import _runtime, _command
    from nerya.strategies.tool_verification import (
        attach_strategy_checks, current_command_delivery_receipts,
    )
    from nerya.tools.types import ToolResult

    root, run, metrics, state, revision = producer
    config, runtime = _runtime(root.parent)
    command = _command(runtime)
    command = runtime.store.claim(command["session_id"], "receipt-test")
    metrics["replay"]["accrual_basis"] = "aave_normalized_income"
    dataset = metrics["provenance"]["datasets"][0]
    dataset.update(provider="aave", action="reserve_index_history")
    snapshot = run / dataset["snapshot_file"]
    payload = json.loads(snapshot.read_text())
    payload["_envelope"].update(provider="aave", action="reserve_index_history",
        observation_kind="historical_reserve_index_observations")
    snapshot.write_text(json.dumps(payload))
    dataset["sha256"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    save(run, metrics, state)
    binding = {"artifact_id": "strategy:rates:proposal:prp_rates", "revision": revision}
    result = attach_strategy_checks(ToolResult.from_json(tool_use_id="saved", name="strategy_backtest",
        data={**metrics, "ok": True, "run_receipt": str(run / "run.json"),
              "report_path": str(run / "report.md")}),
        before=binding, after=binding, kind="backtest", config=config)
    receipt = result.metadata["verification"]
    checks = {row["check_id"]: row for row in receipt["checks"]}
    assert checks["historical_data"]["status"] == "passed"
    checks["historical_data"]["status"] = "missing"  # Receipt from the older verifier.
    runtime.store.record_event(command["command_id"], {"event_id": "saved-replay",
        "kind": "tool.complete", "session_id": command["session_id"],
        "turn_id": command["turn_id"], "action": "strategy_backtest", "verification": receipt})

    def statuses():
        receipts, _ = current_command_delivery_receipts(config, command["command_id"], command["session_id"])
        return {row["check_id"]: row["status"] for row in receipts[0]["checks"]}

    assert statuses()["historical_data"] == "passed"
    assert statuses()["historical_performance"] == "missing"
    snapshot.write_text(snapshot.read_text() + " ")
    assert statuses()["historical_data"] == "missing"

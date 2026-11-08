from __future__ import annotations

import pytest

from nerya.agent.verifier import compute_verifier_outcome, is_validation_command
from nerya.agent.tool_projection import project_tool_results
from nerya.strategies.tool_verification import attach_strategy_checks
from nerya.tools.types import ToolResult
from nerya.tools.verification import verification_receipt, attach_verification, read_verification_receipt
from nerya.tools.verification import public_verification_receipt

pytestmark = pytest.mark.smoke


def test_scope_identity_hashes_survive_redaction_without_exposing_arbitrary_paths():
    scope = "request_scope:command-123:" + "a" * 64
    for ref in [scope, scope + ":" + "b" * 64 + ":backtests/run-1/metrics.json"]:
        value = verification_receipt(artifact_id="strategy:one", revision="c" * 64, scope="test",
            checks=[{"check_id":"request_scope_match","status":"passed","evidence_ref":ref}])
        assert public_verification_receipt(value)["checks"][0]["evidence_ref"] == ref
    for suffix in ["../../secret", "/Users/person/private", "https://private.example/token"]:
        ref=scope+":"+"b"*64+":"+suffix
        value=verification_receipt(artifact_id="strategy:one",revision="c"*64,scope="test",
            checks=[{"check_id":"request_scope_match","status":"passed","evidence_ref":ref}])
        assert public_verification_receipt(value)["checks"][0]["evidence_ref"] != ref


def outcome(*blocks):
    return compute_verifier_outcome(blocks=iter(blocks))


def use(action, call_id="call", **payload):
    return {"kind": "tool_use", "action": action, "call_id": call_id, "payload": payload}


def result(action, call_id="call", ok=True, **values):
    return {"kind": "tool_result", "action": action, "call_id": call_id, "ok": ok, **values}


def receipt(check_id="contract", status="passed", revision="a" * 64, artifact_id="strategy:one"):
    return verification_receipt(artifact_id=artifact_id, revision=revision, scope="Recorded source check only",
                                checks=[{"check_id": check_id, "status": status}], unverified=["live_execution"])


@pytest.mark.parametrize("action", ["llm_classify", "llm_extract_json"])
def test_model_review_is_never_hard_evidence(action):
    check = outcome(use(action, task="verify correctness"), result(action, result={"ok": True}))
    assert check.hard_status == "missing" and not check.trusted


@pytest.mark.parametrize("command", ["pytest --help", "pytest --collect-only", "tsc --showConfig", "ruff check --help", "pytest --version"])
def test_discovery_flags_do_not_execute_checks(command):
    assert not is_validation_command(command)


def test_receipt_messages_are_redacted_without_destroying_source_hash():
    data = receipt()
    data["checks"][0]["message"] = "token=sk-" + "sensitive" * 8
    raw = ToolResult.from_json(tool_use_id="one", name="strategy_validate", data={"ok": True})
    attach_verification(raw, data)
    assert "sensitive" not in raw.metadata["verification"]["checks"][0]["message"]
    assert raw.metadata["verification"]["revision"] == "a" * 64


def test_any_unresolved_required_failure_blocks_unrelated_success():
    check = outcome(result("strategy_validate", verification=receipt()),
                    result("strategy_backtest", verification=receipt("coverage", "failed")))
    assert check.hard_status == "failed" and not check.trusted
    assert check.details["hard"]["successful_validation_actions"] == ["strategy_validate"]


def test_repair_supersedes_same_check_not_another_required_check():
    check = outcome(result("validate", verification=receipt(status="failed")),
                    result("validate", verification=receipt()),
                    result("replay", verification=receipt("coverage", "missing")))
    assert check.hard_status == "missing" and not check.trusted
    assert check.details["hard"]["checks"][0]["status"] == "passed"


def test_new_source_invalidates_old_checks_until_each_is_rechecked():
    blocks = [result("validate", verification=receipt()), result("replay", verification=receipt("replay")),
              result("validate", verification=receipt(revision="b" * 64))]
    assert outcome(*blocks).hard_status == "stale"
    assert outcome(*blocks, result("replay", verification=receipt("replay", revision="b" * 64))).trusted


def test_distinct_artifacts_do_not_supersede_each_other():
    assert not outcome(result("validate", verification=receipt(status="failed")),
                       result("validate", verification=receipt(artifact_id="strategy:other"))).trusted


def test_source_edit_after_check_marks_it_stale():
    assert outcome(result("validate", verification=receipt()), result("edit_file")).hard_status == "stale"


@pytest.mark.parametrize("command", ["echo pytest", "printf 'pytest'", "pytest || true", "pytest | tee log",
    "pytest; echo done", "python -c 'print(\"pytest\")'", "cat test.py", "pytest && echo done", "pytest &",
    "pytest $(echo --help)", "cd repo && echo pytest"])
def test_nonexecuted_or_masked_commands_are_not_validation(command):
    assert not is_validation_command(command)


@pytest.mark.parametrize("command", ["python -m pytest tests/ -q", ".venv/bin/pytest tests/",
    "cd dashboard && ../node_modules/.bin/tsc --noEmit", "CI=1 npm test", "go test ./...", "ruff check ."])
def test_real_checker_commands_remain_supported(command):
    assert is_validation_command(command)


@pytest.mark.parametrize("data", [{"ok": False}, {"success": False}, {"exit_code": 1}, {"error": "failed"}])
def test_transport_success_cannot_hide_checker_failure(data):
    check = outcome(use("run_shell", command="pytest tests/"), result("run_shell", result=data))
    assert check.hard_status == "failed" and not check.trusted


def test_pending_is_not_a_failed_or_passing_check():
    check = outcome(use("run_shell", command="pytest tests/"))
    assert check.hard_status == "pending" and not check.trusted
    assert check.details["hard"]["failed_validation_actions"] == []


def test_tool_name_and_user_content_cannot_forge_a_host_receipt():
    assert not outcome(result("verify_something", result={"verification": receipt(), "ok": True})).trusted
    assert not outcome(result("read_file", result={"verification": receipt(), "ok": True})).trusted


def test_compaction_preserves_only_host_receipt_metadata():
    raw = ToolResult.from_json(tool_use_id="one", name="strategy_validate", data={"ok": True})
    attach_verification(raw, receipt())
    projection = project_tool_results([raw], render_tool_result=lambda value: {"content": [], "compaction": {"rule_id": "test"}},
                                      rendered_tool_result_text=lambda block: "[compacted]")
    assert projection.event_blocks[0]["verification"] == receipt()
    assert outcome(*projection.event_blocks).trusted
    raw.metadata.clear()
    projection = project_tool_results([raw], render_tool_result=lambda value: {}, rendered_tool_result_text=lambda block: None)
    assert projection.event_blocks[0]["verification"] is None


@pytest.mark.parametrize("change", [{"checks": []}, {"checks": [{"check_id": "a", "status": "success"}]},
    {"revision": None}, {"checks": [{"check_id": "a", "status": "passed", "required": "false"}]}])
def test_malformed_receipts_fail_closed(change):
    assert read_verification_receipt({**receipt(), **change}) is None


def test_strategy_validation_receipt_preserves_failures_and_scopes():
    binding = {"artifact_id": "strategy:one", "revision": "a" * 64}
    raw = ToolResult.from_json(tool_use_id="call", name="strategy_validate", data={"ok": False, "blockers": [{"message": "Missing main.py"}]})
    wrapped = attach_strategy_checks(raw, before=binding, after=binding, kind="validation")
    assert wrapped.semantic_success is False
    assert wrapped.metadata["verification"]["checks"][1]["status"] == "failed"
    assert "historical_replay" in wrapped.metadata["verification"]["unverified"]


def test_economic_loss_is_not_replay_execution_failure(tmp_path):
    from test_delivery_producer_outputs import fixture
    config, runtime, command, deps, proposal, event, args, run, data, binding = fixture(tmp_path)
    assert data["verdict"] == "FAIL" and data["total_return_pct"] < 0
    raw = ToolResult.from_json(tool_use_id="call", name="strategy_backtest", data=data)
    wrapped = attach_strategy_checks(raw, before=binding, after=binding, kind="backtest", config=config)
    assert all(check["status"] == "passed" for check in wrapped.metadata["verification"]["checks"])
    assert wrapped.semantic_success is True
    assert "live_execution" in wrapped.metadata["verification"]["unverified"]

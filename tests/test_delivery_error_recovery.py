import pytest

from nerya.tools.native import delivery
from nerya.tools.types import ToolCall
from nerya.tools.result_contracts import tool_json_data
from nerya.task_delivery import DeliveryError, read_delivery, record_delivery_verification, start_delivery, update_delivery
from test_task_delivery import _runtime, _command, _deps, _stages

pytestmark=pytest.mark.smoke


def test_delivery_schema_failure_returns_saved_revision_and_stage_ids(tmp_path):
    config,runtime=_runtime(tmp_path)
    command=_command(runtime)
    deps=_deps(config,command)
    initial=delivery.delivery_start_handler(ToolCall(name="delivery_start",arguments={
        "skill_id":"strategy_author","workflow_id":"strategy_authoring",
        "requested_scope":{"summary":"test"},"stages":[{"id":"scope","state":"in_progress"}]}),deps=deps)
    assert not initial.is_error
    revision=initial.content[0].data["delivery_revision"]
    bad=delivery.delivery_update_handler(ToolCall(name="delivery_update",arguments={
        "expected_revision":revision,"stages":[{"id":"invented","state":"completed"}]}),deps=deps)
    assert bad.is_error
    assert bad.error.retryable is not False
    hint=bad.error.recovery_hint
    assert hint["delivery_revision"]==revision
    assert hint["stage_ids"]==["scope"]
    assert hint["producer_checks_read_only"] is True
    assert hint["side_effects"]=="none"


def test_finalize_without_current_delivery_reports_history_as_read_only(tmp_path):
    config, runtime = _runtime(tmp_path)
    original = _command(runtime)
    start_delivery(config.paths, command_id=original["command_id"], session_id=original["session_id"],
                   skill_id="research", workflow_id="research_brief",
                   requested_scope={"summary": "recorded evidence"}, stages=_stages())
    followup = runtime.submit({
        "command_id": "delivery-followup", "session_id": original["session_id"],
        "_auth_actor_id": "operator", "request": {"payload": {"text": "Read the recorded result"}},
    }, start=False)["command"]
    config.data["runtime"]["command_id"] = followup["command_id"]
    result = delivery.delivery_finalize_handler(
        ToolCall(name="delivery_finalize", arguments={"expected_revision": 3}),
        deps=_deps(config, followup))
    assert result.is_error
    error = result.error
    assert error.detail["code"] == "delivery_not_started"
    assert error.recovery_hint["code"] == "delivery_not_started"
    assert error.recovery_hint["current_command_has_delivery"] is False
    assert error.recovery_hint["historical_snapshots_read_only"] is True
    assert error.recovery_hint["read_only"]["action"] == "report_recorded_completion_and_reasons"
    assert error.recovery_hint["read_only"]["do_not_call"] == ["delivery_finalize", "delivery_start"]
    assert error.recovery_hint["authorized_execution"]["first_action"] == "load_named_skill"
    assert error.recovery_hint["authorized_execution"]["delivery_command"] == followup["command_id"]
    assert "delivery_not_started" in error.message
    assert "historical" in error.message.lower()
    assert error.retryable is False


def test_persisted_delivery_read_does_not_revalidate_ancestor_binding(tmp_path, monkeypatch):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    deps = _deps(config, command)
    args = {"skill_id": "strategy_author", "workflow_id": "strategy_authoring",
            "requested_scope": {"summary": "test"}, "stages": [{"id": "deliver"}]}
    initial = tool_json_data(delivery.delivery_start_handler(
        ToolCall(name="delivery_start", arguments=args), deps=deps))

    def unavailable(*_, **__):
        raise ValueError("continuation_binding_unverified")

    # Local readback does not import evidence from any predecessor.
    monkeypatch.setattr("nerya.agent.continuation_binding.trusted_continuation_source", unavailable)
    assert read_delivery(config.paths, command_id=command["command_id"],
                         session_id=command["session_id"]) == initial
    from nerya.strategies.tool_verification import current_command_delivery_receipts
    with pytest.raises(DeliveryError, match="delivery_continuation_invalid"):
        current_command_delivery_receipts(config, command["command_id"], command["session_id"])
    with pytest.raises(DeliveryError, match="command_not_found"):
        read_delivery(config.paths, command_id=command["command_id"], session_id="another-session")
    result = delivery.delivery_update_handler(ToolCall(name="delivery_update", arguments={
        "expected_revision": initial["delivery_revision"], "limitations": ["No live trading"]}), deps=deps)
    assert not result.is_error, result.text()
    latest = tool_json_data(result)
    conflict = delivery.delivery_update_handler(ToolCall(name="delivery_update", arguments={
        "expected_revision": initial["delivery_revision"], "limitations": []}), deps=deps)
    assert conflict.is_error
    assert conflict.error.recovery_hint["expected_revision"] == latest["delivery_revision"]
    assert conflict.error.recovery_hint["completion"] == latest["completion"]


def test_duplicate_producer_receipt_preserves_delivery_revision(tmp_path):
    from test_task_delivery import _receipt

    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    deps = _deps(config, command)
    args = {"skill_id": "strategy_author", "workflow_id": "strategy_authoring",
            "requested_scope": {"summary": "test"}, "stages": [{"id": "deliver"}]}
    tool_json_data(delivery.delivery_start_handler(ToolCall(name="delivery_start", arguments=args), deps=deps))
    receipt = _receipt("behavioral_tests", artifact_id="strategy:btc:proposal:installed")
    first = record_delivery_verification(config.paths, command_id=command["command_id"], receipt=receipt)
    again = record_delivery_verification(config.paths, command_id=command["command_id"], receipt=receipt)
    assert again == first
    restarted = delivery.delivery_start_handler(ToolCall(name="delivery_start", arguments=args), deps=deps)
    assert tool_json_data(restarted) == first


def test_completed_replay_cas_recovery_finalizes_without_repeating_producers(tmp_path, monkeypatch):
    from test_task_delivery import _strategy_command, _completed_replay_before_delivery
    from nerya.tools.native import strategy_runtime

    config, runtime = _runtime(tmp_path)
    command, contract = _strategy_command(config, runtime, base="ETH")
    deps, _, _, args = _completed_replay_before_delivery(config, runtime, command, contract=contract)
    args["stages"][-1]["state"] = "completed"

    def no_producer(*_, **__):
        pytest.fail("Delivery recovery must not rerun completed producers")

    monkeypatch.setattr(strategy_runtime, "strategy_validate_handler", no_producer)
    monkeypatch.setattr(strategy_runtime, "strategy_backtest_handler", no_producer)
    initial = tool_json_data(delivery.delivery_start_handler(ToolCall(
        name="delivery_start", arguments=args), deps=deps))
    latest = update_delivery(config.paths, command_id=command["command_id"],
        session_id=command["session_id"], expected_revision=initial["delivery_revision"],
        limitations=["Historical replay only; no live trading"])
    assert runtime.store.finish(command["command_id"], command["session_id"],
                                "delivery-fixture-worker", "succeeded", {"final_text": "done"})
    # Command terminal state does not revoke access to its durable delivery.
    restarted = delivery.delivery_start_handler(ToolCall(name="delivery_start", arguments=args), deps=deps)
    assert tool_json_data(restarted) == latest
    with pytest.raises(DeliveryError) as caught:
        update_delivery(config.paths, command_id=command["command_id"], session_id=command["session_id"],
                        expected_revision=initial["delivery_revision"], limitations=[])
    assert caught.value.code == "delivery_revision_conflict"
    assert caught.value.delivery == latest
    for handler in (delivery.delivery_update_handler, delivery.delivery_finalize_handler):
        conflict = handler(ToolCall(name=handler.__name__.removesuffix("_handler"),
            arguments={"expected_revision": initial["delivery_revision"]}), deps=deps)
        assert conflict.is_error
        hint = conflict.error.recovery_hint
        assert hint["expected_revision"] == latest["delivery_revision"]
        assert hint["completion"] == latest["completion"]
        assert read_delivery(config.paths, command_id=command["command_id"],
                             session_id=command["session_id"]) == latest
    final = tool_json_data(delivery.delivery_finalize_handler(ToolCall(name="delivery_finalize",
        arguments={"expected_revision": hint["expected_revision"]}), deps=deps))
    assert final["completion"]["allowed"] is True
    assert final["completion"]["status"] == "partial"
    assert all(check["producer_owned"] and check["status"] == "passed" for check in final["checks"])
    again = tool_json_data(delivery.delivery_finalize_handler(ToolCall(name="delivery_finalize",
        arguments={"expected_revision": final["delivery_revision"]}), deps=deps))
    assert again == final

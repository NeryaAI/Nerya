from copy import deepcopy

import pytest

from nerya.agent.command_runtime import CommandRuntime
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.strategy_runtime import _retrospective_authorization_interaction
from nerya.tools.types import ToolCall


def test_missing_retrospective_authorization_creates_clickable_interaction(tmp_path):
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    runtime = CommandRuntime(config, lambda *_: {}, epoch="retrospective-interaction")
    receipt = runtime.submit({
        "command_id": "replay-command",
        "session_id": "replay-session",
        "_auth_actor_id": "operator",
        "_auth_scopes": ["execute:strategy"],
        "request": {"payload": {"text": "script replay"}},
    }, start=False)
    row = runtime.store.claim("replay-session", "worker")
    assert row and receipt["command"]["command_id"] == "replay-command"
    config.data["runtime"].update(
        command_id="replay-command",
        command_session_id="replay-session",
        command_turn_id=row["turn_id"],
    )

    result = _retrospective_authorization_interaction(
        ToolCall(id="replay-call", name="strategy_backtest"),
        config,
        reason="retrospective_replay_requires_operator_authorization",
    )

    assert result is not None
    assert result.result_protocol == "user_interaction_pending"
    assert result.content[0].data["reason"] == "retrospective_replay_authorization_pending"
    interaction = result.content[0].data["interaction"]
    assert interaction["state"] == "pending"
    question = interaction["payload"]["questions"][0]
    assert question["id"] == "allow_current_retrospective_replay"
    assert question["options"] == ["允许本次模型历史回放", "暂不继续"]


@pytest.mark.parametrize("case", ["valid", "expired", "invalid_budget", "declined", "unanswered", "foreign_session", "wrong_id"])
def test_policy_check_rechecks_once_after_answer_and_checkpoint(tmp_path, monkeypatch, case):
    import json
    from types import SimpleNamespace

    from nerya.agent.loop_state import LoopRunState
    from nerya.agent.tool_phase import ToolBatchPhase, ToolBatchPolicy
    from nerya.skills.builtin.backtest.scripts import authorized_replay
    from nerya.tools.native.strategy_runtime import (
        _retrospective_consent, strategy_backtest_handler,
    )
    from nerya.tools.orchestrator import BatchResult
    from nerya.tools.registry import ToolRegistry
    from nerya.tools.result_contracts import tool_json_data
    from nerya.tools.types import PermissionScope, RiskLevel, ToolDescriptor, ToolErrorKind

    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    runtime = CommandRuntime(config, lambda *_: {}, epoch="policy-recheck")
    runtime.submit({"command_id": "policy-command", "session_id": "policy-session",
                    "_auth_actor_id": "operator", "_auth_scopes": ["execute:strategy"],
                    "request": {"payload": {"text": "script replay"}}}, start=False)
    row = runtime.store.claim("policy-session", "worker")
    config.data["runtime"].update(command_id=row["command_id"],
                                 command_session_id=row["session_id"],
                                 command_turn_id=row["turn_id"])
    clock = 1800000000
    monkeypatch.setattr(authorized_replay.time, "time", lambda: clock)
    executed = []

    def dispatch(calls):
        executed.extend(calls)
        return BatchResult(results=[strategy_backtest_handler(c, config=config) for c in calls])

    registry = ToolRegistry()
    registry.register(ToolDescriptor(name="strategy_backtest", description="replay",
        handler=lambda c: strategy_backtest_handler(c, config=config), input_schema={"type": "object"},
        risk=RiskLevel.EXEC, permission_scope=PermissionScope.WORKSPACE, read_only=False))
    phase = ToolBatchPhase(orchestrator=SimpleNamespace(run_batch=dispatch), registry=registry)
    policy = ToolBatchPolicy(frozenset({"strategy_backtest"}), frozenset({"strategy_backtest"}))
    state = LoopRunState(turn_id=row["turn_id"], message_id="message")

    def check(call_id, state, interaction_id=None):
        args = {"policy_check_only": True}
        if interaction_id is not None:
            args["policy_recheck_interaction_id"] = interaction_id
        return phase.run([ToolCall(id=call_id, name="strategy_backtest",
                                  arguments=args)],
                         state=state, policy=policy).batch.results[0]

    first = check("initial", state)
    assert first.result_protocol == "user_interaction_pending"
    iid = tool_json_data(first)["interaction"]["interaction_id"]
    assert tool_json_data(first)["next_required_action"]["arguments"] == {
        "policy_check_only": True, "policy_recheck_interaction_id": iid}
    with runtime.store.transaction() as con:
        response = {"action": "answer", "selected": [], "answers": {
            "allow_current_retrospective_replay": {"selected": ["暂不继续" if case == "declined" else "允许本次模型历史回放"], "text": ""}}}
        if case == "unanswered":
            response["answers"] = {}
        con.execute("UPDATE agent_interactions SET state='resolved',response_json=? WHERE interaction_id=?",
                    (json.dumps(response), iid))
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id=?",
                    (json.dumps({"interaction_id": iid}), row["command_id"]))
        if case == "foreign_session":
            con.execute("UPDATE agent_interactions SET session_id='other-session' WHERE interaction_id=?", (iid,))
    config.data["backtest"]["retrospective_replay"] = dict(
        enabled=True, authorization_id="fixture-authorization", provider="fixture", model="fixture",
        key_ref="vault://fixture", max_calls=20, max_tokens=100000, deadline=clock + 120)
    if case == "expired":
        config.data["backtest"]["retrospective_replay"]["deadline"] = clock - 1
    if case == "invalid_budget":
        config.data["backtest"]["retrospective_replay"]["max_calls"] = 21
    resumed = LoopRunState.from_checkpoint(state.to_checkpoint(resumable=True, resume_block_reason=""))
    recheck_id = "invented-id" if case == "wrong_id" else iid
    second = check("recheck", resumed, recheck_id)
    if case == "valid":
        assert not second.is_error, second.text()
        body = tool_json_data(second)
        assert body["authorization_valid"] is True
        assert body["budget_remaining_verified"] is False
        assert body["execution_readiness_verified"] is False
        assert body["replay_started"] is False
        assert _retrospective_consent(config) is True
    else:
        assert second.is_error and second.error.kind != ToolErrorKind.DEDUPED
        if case in {"expired", "invalid_budget"}:
            assert tool_json_data(second)["reason"] == "invalid_or_expired_retrospective_authorization"
    third = check("repeated", resumed, recheck_id)
    assert third.error.kind == ToolErrorKind.DEDUPED
    assert [call.id for call in executed] == ["initial", "recheck"]
    assert not (config.paths.vault / "retrospective-decisions.sqlite3").exists()

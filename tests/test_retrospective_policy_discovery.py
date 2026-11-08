"""Read-only policy discovery through the public tool, before a candidate exists."""
from copy import deepcopy
import json

import pytest
from jsonschema import Draft7Validator

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.native import strategy_runtime
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize("case", ["valid", "missing", "expired", "invalid", "mixed"])
def test_policy_discovery_has_no_candidate_secret_ledger_or_replay_effects(
    tmp_path, monkeypatch, case
):
    from nerya.skills.builtin.backtest.scripts import authorized_replay
    from test_dex_exact_scope_routing import read_skill

    def forbidden(*args, **kwargs):
        pytest.fail("policy discovery must not access source, secrets, ledger, model or network")

    monkeypatch.setattr(authorized_replay.time, "time", lambda: 1800000000)
    monkeypatch.setattr(authorized_replay, "SecretVault", forbidden)
    monkeypatch.setattr(authorized_replay, "DecisionProducer", forbidden)
    monkeypatch.setattr(authorized_replay, "LLMGateway", forbidden)
    monkeypatch.setattr(strategy_runtime, "_strategy_backtest_handler", forbidden)
    monkeypatch.setattr("nerya.strategies.tool_verification.source_binding", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    policy = dict(enabled=True, authorization_id="fixture-authorization",
                  provider="fixture", model="fixture-model", key_ref="vault://PRIVATE_SIGNER",
                  max_calls=20, max_tokens=100000, deadline=1800000120, private="PRIVATE_VALUE")
    if case == "missing":
        policy = {"enabled": False}
    elif case == "expired":
        policy["deadline"] = 1799999999
    elif case == "invalid":
        policy["max_calls"] = 21
    data = deepcopy(DEFAULT_CONFIG)
    data["backtest"]["retrospective_replay"] = policy
    config = Config(WorkspacePaths(tmp_path), data)
    saved = deepcopy(data)
    args = {"policy_check_only": True}
    if case == "mixed":
        args["proposal_id"] = "prp_fixture"
    Draft7Validator(strategy_runtime.STRATEGY_BACKTEST_SCHEMA).validate(args)
    assert "policy_check_only" in strategy_runtime.STRATEGY_BACKTEST_SCHEMA["properties"]
    result = strategy_runtime.strategy_backtest_handler(
        ToolCall(name="strategy_backtest", arguments=args), config=config)
    body = tool_json_data(result) or {}
    if case == "valid":
        assert not result.is_error and body["authorization_valid"] is True
        assert body["policy"] == {**{k: policy[k] for k in (
            "authorization_id", "provider", "model", "max_calls", "max_tokens", "deadline")},
            "max_output_tokens": 4096}
        assert body["replay_started"] is False
        assert body["budget_remaining_verified"] is False
        assert body["execution_readiness_verified"] is False
    elif case == "mixed":
        assert result.is_error and "alone" in result.error.message
    else:
        assert result.is_error and result.error.retryable is False
        assert result.error.message == "模型回放授权不可用或已变化，已停止重试；请先检查授权状态。"
        assert body["reason"] == ("retrospective_replay_requires_operator_authorization"
                                  if case == "missing" else "invalid_or_expired_retrospective_authorization")
        assert body["backtest_status"] == "blocked"
        assert body["next_required_action"]["requires_operator_authorization"] is True
        assert body["next_required_action"]["automatic"] is False
    assert "PRIVATE" not in json.dumps(body)
    assert "verification" not in body
    assert not body.get("run_receipt") and not body.get("report_path")
    assert data == saved and not list(tmp_path.iterdir())
    reference = read_skill(skill="backtest", file="references/replay-capabilities.md")
    assert 'strategy_backtest(policy_check_only=true)' in reference
    assert "remaining budget" in reference and "operator" in reference


def test_operator_policy_change_reaches_native_replay_without_argument_changes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from nerya.agent.loop_state import LoopRunState
    from nerya.agent.tool_phase import ToolBatchPhase, ToolBatchPolicy
    from nerya.skills.builtin.backtest.scripts import authorized_replay
    from nerya.skills.builtin.backtest.scripts.decision_producer import DecisionProducerError
    from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
    from nerya.tools.orchestrator import BatchResult
    from nerya.tools.registry import ToolRegistry
    from nerya.tools.types import ToolErrorKind, ToolResult

    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    operator = dict(enabled=True, authorization_id="same-authorization", provider="fixture",
                    model="fixture", key_ref="vault://fixture", max_calls=20,
                    max_tokens=100000, deadline=1800000120, max_output_tokens=1024)
    config.data["backtest"]["retrospective_replay"] = operator
    monkeypatch.setattr(authorized_replay.time, "time", lambda: 1800000000)
    monkeypatch.setattr("nerya.strategies.tool_verification.source_binding", lambda *a: {"revision": "same-source"})
    monkeypatch.setattr("nerya.strategies.tool_verification.attach_strategy_checks", lambda result, **k: result)
    outcome = ["decision_response_truncated"]
    produced = []
    def replay(call, **kwargs):
        produced.append(operator["max_output_tokens"])
        if outcome[0]:
            return strategy_runtime._backtest_error(call, DecisionProducerError(outcome[0]),
                reason="backtest_failed", strategy_id=None, proposal_id="prp_same")
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data={"ok": True})
    monkeypatch.setattr(strategy_runtime, "_strategy_backtest_handler", replay)
    registry = ToolRegistry()
    register_native_tools(registry, build_native_tool_deps(workspace_root=tmp_path,
        skill_roots=[], paths=config.paths, config=config))
    descriptor = registry.get("strategy_backtest")
    phase = ToolBatchPhase(registry=registry, orchestrator=SimpleNamespace(run_batch=lambda calls:
        BatchResult(results=[descriptor.handler(c) for c in calls])))
    policy = ToolBatchPolicy(frozenset({descriptor.name}), frozenset({descriptor.name}))
    state = LoopRunState(turn_id="same-turn", message_id="message")
    args = {"proposal_id": "prp_same", "settings": {"agent_replay": True}, "data_mode": "local"}
    def run(arguments=args):
        return phase.run([ToolCall(name=descriptor.name, arguments=deepcopy(arguments))],
                         state=state, policy=policy).batch.results[0]

    assert run().is_error
    assert run().error.kind == ToolErrorKind.DEDUPED
    config.data["unrelated_setting"] = "changed"
    assert run().error.kind == ToolErrorKind.DEDUPED
    observed = run({"policy_check_only": True})
    assert not observed.is_error
    state = LoopRunState.from_checkpoint(state.to_checkpoint(resumable=True, resume_block_reason=""))
    operator["max_output_tokens"] = 4096
    changed = run()
    assert changed.is_error and changed.error.kind != ToolErrorKind.DEDUPED
    assert produced == [1024, 4096]
    assert run().error.kind == ToolErrorKind.DEDUPED
    assert tool_json_data(run({"policy_check_only": True}))["policy"]["max_output_tokens"] == 4096
    outcome[0] = ""
    operator["max_output_tokens"] = 16384
    assert not run().is_error
    operator["max_output_tokens"] = 8192
    assert run().error.kind == ToolErrorKind.DEDUPED
    assert produced == [1024, 4096, 16384]
    state = LoopRunState(turn_id="unknown-turn", message_id="message")
    outcome[0] = "decision_request_unconfirmed; reconcile before retry"
    assert run().is_error
    state = LoopRunState.from_checkpoint(state.to_checkpoint(resumable=True, resume_block_reason=""))
    operator["max_output_tokens"] = 16384
    assert run().error.kind == ToolErrorKind.DEDUPED
    assert produced == [1024, 4096, 16384, 8192]
    assert not config.paths.db.exists()

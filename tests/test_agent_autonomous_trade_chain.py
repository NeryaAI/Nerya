"""Scripted model decisions, real isolated paper execution and durable receipts."""
from __future__ import annotations

from contextlib import closing

import pytest

from nerya.agent.loop import WorkspaceNativeAgentLoop
from nerya.agent.loop_contracts import LoopConfig
from nerya.agent.runtime import GateDecision
from nerya.tools import NativeToolExecutor, PermissionContext, PermissionEngine
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import ToolResult
from nerya.trading.order_tracker import OrderTracker
from nerya.trading.position_book import PositionBook
from nerya.trading.submit import submit_trade_intent
from test_live_lifecycle import _paper_config, _strategy, _snapshot
from test_subagent_native_runtime import Gateway, call, final, descriptor, runtime, spec, run

pytestmark = pytest.mark.smoke


def test_research_submit_and_verify_share_one_loop_and_real_paper_ledger(tmp_path):
    cfg = _paper_config(tmp_path)
    _strategy(cfg, "paper_agent", "paper_main")
    receipts, steps = [], []

    def research(c):
        steps.append("research")
        return ToolResult.from_json(tool_use_id=c.id, name=c.name, data={
            "team_run_id": "fixture-research", "status": "completed", "ok": True,
            "results": [{"subagent": "analyst", "output": {"summary": "test evidence only"}}],
        })

    def trade(c):
        steps.append("submit")
        # This fixture is an authorized paper strategy; production live flags stay untouched.
        receipt = submit_trade_intent(cfg, spec={
            "strategy_id": "paper_agent", "account_id": "paper_main", "market": "mock:BTC/USDT",
            "side": "buy", "size": 150.0, "size_unit": "usd", "confidence": 1.0,
            "source": "strategy_runtime", "order_type": "market",
        }, market_snapshot=_snapshot())
        receipts.append(receipt)
        return ToolResult.from_json(tool_use_id=c.id, name=c.name, data=receipt)

    def verify(c):
        steps.append("verify")
        with closing(OrderTracker(cfg.paths)) as tracker:
            actual = tracker.get(receipts[0]["order_id"])
        assert actual is not None and actual.state == "filled"
        assert actual.filled_size > 0 and actual.avg_price > 0
        assert receipts[0]["order"] == actual.asdict()
        assert receipts[0]["orders"] == [actual.asdict()]
        position = PositionBook(cfg.paths).get_open(
            account_id="paper_main", strategy_id="paper_agent", market="mock:BTC/USDT")
        assert position is not None
        return ToolResult.from_json(tool_use_id=c.id, name=c.name, data={
            "verified": True, "execution_mode": "paper", "order": actual.asdict(),
        })

    registry = ToolRegistry()
    registry.register_all([descriptor("team_run", handler=research),
        descriptor("submit_paper_intent", handler=trade), descriptor("verify_order", handler=verify)])
    executor = NativeToolExecutor(registry=registry, permission_engine=PermissionEngine(),
                                  permission_context=PermissionContext())
    gateway = Gateway(call("team_run"), call("submit_paper_intent", "submit"),
                      call("verify_order", "verify"), final("Paper fill verified; no live order placed."))
    outcome = WorkspaceNativeAgentLoop(gateway=gateway, registry=registry,
        orchestrator=ToolOrchestrator(registry=registry, executor=executor),
        config=LoopConfig(max_iterations=5),
    ).run(system="Finish only after checking the actual paper ledger.", user_message="research, execute and verify")
    assert steps == ["research", "submit", "verify"]
    assert outcome.completion_status == "complete"
    assert outcome.tool_calls == 3 and outcome.error_count == 0
    assert len(gateway.calls) == 4
    assert receipts[0]["executor"]["state"] == "done"
    assert receipts[0]["reservation_id"]
    assert "Paper fill verified" in outcome.final_text
    assert "verified" in str(gateway.calls[-1]["messages"])


def test_completed_claim_cannot_override_missing_required_action(tmp_path):
    gateway = Gateway(final("claimed complete"))
    rt = runtime(tmp_path, gateway, [descriptor()])
    result = run(rt, spec(tmp_path, max_iterations=1, required_native_tools=["probe"]),
                 completion_gate=lambda snapshot: GateDecision.complete(reason="model_claim"))
    assert result["completion_status"] == "blocked"
    assert result["output"]["done"] is False
    assert result["completion"]["reason"] == "required_artifact_missing"


def test_order_intent_returns_exact_plan_envelope_without_legacy_rewrapping(tmp_path, monkeypatch):
    from nerya.trading import submit
    response = {"status": "open", "plan_id": "plan", "executor": {
        "state": "running", "order_ids": ["observed-order"], "result": {}},
        "orders": [{"order_id": "observed-order", "state": "open", "avg_price": None, "filled_size": 0}],
    }
    monkeypatch.setattr(submit, "submit_trade_plan", lambda *a, **k: response)
    result = submit.submit_trade_intent(_paper_config(tmp_path), spec={
        "strategy_id": "paper_agent", "account_id": "paper_main", "market": "mock:BTC/USDT",
        "side": "buy", "size": 100, "size_unit": "usd", "source": "strategy_runtime", "order_type": "market",
    })
    assert result is response
    assert "order" not in result
    assert result["orders"][0]["avg_price"] is None


def test_root_configuration_does_not_use_removed_harness_alias(tmp_path):
    from nerya.agent.kernel import _loop_config_from_config
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths
    config = Config(paths=WorkspacePaths(tmp_path), data={"agent": {
        "harness": {"max_tool_calls": 1, "max_iterations": 1, "max_wall_seconds": 1},
        "native": {"max_iterations": 7, "max_total_tool_calls": 0},
    }})
    policy = _loop_config_from_config(config, turn_id="real-turn", reasoning_effort="high")
    assert policy.max_iterations == 7 and policy.tool_call_limit == 0
    assert policy.max_wall_seconds is None
    assert policy.turn_id == "real-turn" and policy.reasoning_effort == "high"

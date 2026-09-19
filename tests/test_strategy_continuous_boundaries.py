"""Fail-closed lifecycle and preflight regressions from real-model dogfooding."""
from contextlib import closing
import hashlib
import threading
import time
from types import SimpleNamespace

import pytest
from nerya.core import yaml_io
from nerya.core.errors import TradingError
from nerya.strategies.continuous import assert_event_active, ContinuousSupervisor, _read
from nerya.strategies.package import load_package
from nerya.strategies.streaming import _connect
from nerya.strategies.agent_task import StrategyAgentTask
from nerya.strategies.runner import StrategyRunner
from nerya.trading.order_tracker import OrderTracker
from nerya.tools.native.bootstrap import build_native_tool_deps, _wrap_trade_intent_submit
from nerya.tools.native.trading import risk_check_handler
from nerya.tools.types import ToolCall
from test_strategy_continuous import _config, package, supervisor, until, Kernel

pytestmark = pytest.mark.smoke


def start_event(cfg, *, ttl=60, **runtime):
    pkg = package(cfg, max_event_age_seconds=ttl, **runtime)
    manager = supervisor(cfg, Kernel(cfg))
    manager.start(pkg.strategy_id)
    until(lambda: manager.status(pkg.strategy_id)["state"] == "running")
    s = manager._services[pkg.strategy_id]
    eid = "continuous_" + hashlib.sha256(b"boundary-test").hexdigest()
    s.ledger.claim(eid, s.generation, pkg.content_hash, time.time(), time.time()+ttl)
    s.ledger.finish(eid, "running")
    deps = build_native_tool_deps(workspace_root=cfg.paths.root, skill_roots=[], paths=cfg.paths, config=cfg)
    deps.active_strategy_id, deps.active_trigger_event_id = pkg.strategy_id, eid
    deps.active_session_id, deps.strategy_order_auto_approve = "boundary_session", True
    return pkg, manager, deps, eid


def order(**updates):
    return {"account_id": "paper_main", "market": "mock:BTC/USDT", "side": "buy", "size": 10,
        "size_unit": "usd", "order_type": "market", "confidence": 0.9,
        "market_snapshot": {"price": 50000, "age_s": 0}, **updates}


def submit(deps, **updates):
    return _wrap_trade_intent_submit(deps)(ToolCall(name="trade_intent_submit", id="boundary", arguments=order(**updates)))


def test_preflight_then_submit_fills_and_second_submit_still_dedupes(tmp_path):
    cfg = _config(tmp_path)
    pkg, manager, deps, eid = start_event(cfg)
    try:
        for _ in range(2):
            result = risk_check_handler(ToolCall(name="risk_check", id="preview", arguments={
                "intent": {**{key: value for key, value in order().items() if key != "market_snapshot"}, "strategy_id": pkg.strategy_id, "source": "strategy_agent"},
                "market_snapshot": order()["market_snapshot"]}), config=cfg)
            assert not result.is_error, result.asdict()
            assert "duplicate_intent" not in result.content[0].data["risk_decision"]["reasons"]
        first = submit(deps)
        assert not first.is_error and first.content[0].data["status"] == "filled", first
        assert load_package(cfg.paths, pkg.strategy_id).content_hash == pkg.content_hash
        assert_event_active(cfg, pkg.strategy_id, eid)
        again = submit(deps)
        assert again.content[0].data["status"] == "rejected"
        assert "duplicate_intent" in again.content[0].data["risk_decision"]["reasons"]
        with closing(OrderTracker(cfg.paths)) as tracker:
            assert len(tracker.cached_orders(account_id="paper_main")) == 1
    finally:
        manager.stop(pkg.strategy_id)
        manager.shutdown()


@pytest.mark.parametrize("bad", [{"account_id": "other"}, {"market": "mock:ETH/USDT"}, {"size": 1000}, {"confidence": 0.1}])
def test_agent_policy_cannot_be_widened_by_event(tmp_path, bad):
    cfg = _config(tmp_path)
    pkg, manager, deps, _ = start_event(cfg)
    try:
        reply = submit(deps, **bad)
        assert reply.is_error or reply.content[0].data.get("status") == "rejected", reply
        with closing(OrderTracker(cfg.paths)) as tracker:
            assert tracker.cached_orders(account_id="paper_main") == []
    finally:
        manager.stop(pkg.strategy_id)
        manager.shutdown()


@pytest.mark.parametrize("cause", ["stop", "expire", "source", "kill", "durable_global_kill", "invalid_safety_config"])
def test_late_order_fenced(tmp_path, cause):
    cfg = _config(tmp_path)
    pkg, manager, deps, eid = start_event(cfg, ttl=1 if cause == "expire" else 60)
    try:
        if cause == "stop":
            manager.stop(pkg.strategy_id)
        elif cause == "expire":
            time.sleep(1.1)
        elif cause == "source":
            (pkg.root / "main.py").write_text("def run(ctx):\n    return None\n")
        elif cause == "durable_global_kill":
            yaml_io.dump(cfg.paths.root / "nerya.yml", {"runtime": {"kill_switch": True}})
            assert not cfg.kill_switch()  # stale Agent Config is intentionally unchanged
        elif cause == "invalid_safety_config":
            yaml_io.dump(cfg.paths.root / "nerya.yml", {"runtime": "invalid"})
        else:
            (pkg.state_dir / "kill_switch.json").write_text('{"asserted": true}')
        assert submit(deps).is_error
        with closing(OrderTracker(cfg.paths)) as tracker:
            assert tracker.cached_orders(account_id="paper_main") == []
    finally:
        manager.stop(pkg.strategy_id)
        manager.shutdown()


def test_stop_cancels_inflight_agent_not_only_listener(tmp_path):
    cfg = _config(tmp_path)
    pkg = package(cfg)
    kernel = Kernel(cfg, block=True)
    manager = supervisor(cfg, kernel)
    try:
        manager.start(pkg.strategy_id)
        until(lambda: manager.status(pkg.strategy_id)["state"] == "running")
        manager._enqueue(manager._services[pkg.strategy_id], StrategyAgentTask.dispatch(prompt="bounded test"), "one", time.time(), {})
        assert kernel.entered.wait(3)
        assert manager.stop(pkg.strategy_id)["state"] == "stopped"
        assert kernel.calls[0]["cancel_token"].is_set
        assert manager.events(pkg.strategy_id)["events"][0]["status"] in {"cancelled", "failed"}
    finally:
        manager.shutdown()


def test_noncooperative_listener_is_not_falsely_killed(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    pkg = package(cfg)
    release = threading.Event()
    monkeypatch.setattr(StrategyRunner, "_load_entrypoint", staticmethod(lambda _: lambda ctx: release.wait(6)))
    manager = supervisor(cfg, Kernel(cfg))
    try:
        first = manager.start(pkg.strategy_id)
        until(lambda: manager.status(pkg.strategy_id)["state"] == "running")
        assert manager.stop(pkg.strategy_id, timeout=0.05)["state"] == "stopping"
        assert ContinuousSupervisor(cfg).start(pkg.strategy_id)["generation"] == first["generation"]
    finally:
        release.set()
        manager.stop(pkg.strategy_id)
        manager.shutdown()


def test_restart_budget_failure_and_shutdown_does_not_erase_status(tmp_path):
    cfg = _config(tmp_path)
    pkg = package(cfg, "def run(ctx):\n    raise ValueError('controlled failure')\n", max_restarts=1, restart_backoff_seconds=0.1)
    manager = supervisor(cfg, Kernel(cfg))
    manager.start(pkg.strategy_id)
    until(lambda: manager.status(pkg.strategy_id)["state"] == "failed")
    until(lambda: not manager._services[pkg.strategy_id].thread.is_alive())
    manager.shutdown()
    assert manager.status(pkg.strategy_id)["state"] == "failed"
    assert manager.status(pkg.strategy_id)["restart_count"] == 1


def test_no_private_websocket_without_operator_policy(tmp_path):
    with pytest.raises(PermissionError):
        _connect(_config(tmp_path), {"url": "ws://127.0.0.1:9"})


def test_only_explicit_unchanged_resume_and_explicit_stop_persists(tmp_path):
    cfg = _config(tmp_path)
    pkg = package(cfg, resume_on_start=True)
    first = supervisor(cfg, Kernel(cfg))
    first.start(pkg.strategy_id)
    until(lambda: first.status(pkg.strategy_id)["state"] == "running")
    first.shutdown()
    assert _read(pkg.state_dir / "continuous-control.json")["desired"] == "running"
    second = supervisor(cfg, Kernel(cfg))
    try:
        second.restore()
        until(lambda: second.status(pkg.strategy_id)["state"] == "running")
        second.stop(pkg.strategy_id)
        third = supervisor(cfg, Kernel(cfg))
        third.restore()
        assert not third._services
    finally:
        second.shutdown()


def test_no_runtime_backtest_or_timer_for_continuous(tmp_path):
    cfg = _config(tmp_path)
    pkg = package(cfg)
    from nerya.strategies.scheduler_bridge import compile_trading_schedule
    from nerya.skills.builtin.backtest.scripts.backtest_run import run_strategy_backtest
    with pytest.raises(TradingError, match="continuous"):
        compile_trading_schedule(pkg)
    yaml_io.dump(cfg.paths.root / "nerya.yml", cfg.data)
    with pytest.raises(TradingError, match="continuous"):
        run_strategy_backtest(workspace=cfg.paths.root, strategy_id=pkg.strategy_id)

"""Real local sockets + production service/Agent adapter + isolated paper ledger.

Scripted kernels below are labelled test doubles; the opt-in real-model review
is separate. No test may use real funds or change the user's workspace.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest
from websockets.sync.server import serve

from nerya.core import jsonl, yaml_io
from nerya.core.errors import TradingError
from nerya.strategies.agent_task import StrategyAgentTask
from nerya.strategies.continuous import ContinuousSupervisor, EventLedger, _Service, _write, assert_event_active
from nerya.strategies.continuous_config import ContinuousConfig
from nerya.strategies.package import load_package
from nerya.strategies.runner import StrategyRunner
from nerya.strategies.scheduler_bridge import apply_strategy_schedules
from nerya.strategies.state import StrategyKillSwitch
from nerya.strategies.validator import validate_strategy_package
from nerya.tools.native.bootstrap import build_native_tool_deps, _wrap_trade_intent_submit
from nerya.tools.types import ToolCall
from nerya.triggers.strategy_agent_task_executor import StrategyAgentTaskExecutor
from test_strategy_agent_task_chain import _config

pytestmark = pytest.mark.smoke

IDLE = "def run(ctx):\n    while not ctx.stream.wait(0.05):\n        pass\n"
WS = '''from nerya.strategies import StrategyAgentTask

def run(ctx):
    for message in ctx.stream.websocket("prices"):
        data = message["data"]
        if data["move"] < 2:
            continue
        ctx.inputs.publish("signal", data)
        task = StrategyAgentTask.dispatch(prompt="Evaluate this price event within the approved strategy policy.",
            context={"market": "mock:BTC/USDT", "account_id": "paper_main", "price": data["price"]},
            sources=[], outputs=["signal"], roles=[])
        receipt = ctx.stream.dispatch(task, event_id=data["id"], observed_at=data["observed_at"])
        ctx.audit.log("stream.receipt", receipt)
'''


def package(cfg, main=IDLE, **runtime):
    root = cfg.paths.strategy("ws_observer")
    root.mkdir(parents=True, exist_ok=True)
    yaml_io.dump(root / "strategy.yml", {
        "version": 1, "strategy_id": "ws_observer", "title": "Continuous paper test",
        "mode": "paper", "entrypoint": "main.py:run", "execution_mode": "agent",
        "markets": ["mock:BTC/USDT"], "accounts": ["paper_main"],
        "runtime": {"mode": "continuous", "min_dispatch_interval_seconds": 0, **runtime},
        "agent_task": {"enabled": True}, "evaluation": {"mode": "trading"},
        "policy": {"allow_direct_order": False, "max_single_order_usd": 120,
                   "max_daily_notional_usd": 500, "min_confidence": 0.7,
                   "max_open_positions": 1, "max_run_seconds": 1},
        "agent_profile": {"role": "Handle approved paper signals and use trade_intent_submit when justified.",
            "allowed_tools": ["trade_intent_submit", "risk_check", "portfolio_summary"],
            "attached_skills": ["trading"]},
    })
    (root / "main.py").write_text(main, encoding="utf-8")
    return load_package(cfg.paths, "ws_observer")


def until(predicate, timeout=7):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for continuous-service condition")


class Kernel:
    def __init__(self, config, *, trade=False, block=False):
        self.config, self.trade, self.block = config, trade, block
        self.calls, self.receipts = [], []
        self.entered = threading.Event()

    def run_turn(self, **kw):
        self.calls.append(kw)
        self.entered.set()
        if self.block:
            kw["cancel_token"].wait(10)
            kw["cancel_token"].raise_if_cancelled()
        if self.trade:
            deps = build_native_tool_deps(workspace_root=self.config.paths.root,
                skill_roots=[], paths=self.config.paths, config=self.config)
            deps.active_strategy_id = kw["strategy_id"]
            deps.active_session_id = kw["session_id"]
            deps.active_trigger_event_id = kw["trigger"]["event_id"]
            deps.strategy_order_auto_approve = True
            result = _wrap_trade_intent_submit(deps)(ToolCall(name="trade_intent_submit", id="paper_order", arguments={
                "account_id": "paper_main", "market": "mock:BTC/USDT", "side": "buy", "size": 10,
                "size_unit": "usd", "order_type": "market", "confidence": 0.9,
                "market_snapshot": {"price": 50000, "age_s": 0, "source": "controlled_websocket_test"}}))
            self.receipts.append(result)
        return SimpleNamespace(turn_id=kw["turn_id"], actions=[], tool_trace=[], final_text="Scripted test kernel returned", stopped_reason="end_turn", budget={})


def supervisor(cfg, kernel):
    return ContinuousSupervisor(cfg, task_executor=StrategyAgentTaskExecutor(config=cfg, kernel_factory=lambda _: kernel))


@pytest.mark.parametrize("bad", [{"queue_size": 0}, {"max_restarts": True}, {"max_event_age_seconds": float("nan")}, {"typo": 1}, {"streams": {"x": {"url": "file:///etc/passwd"}}}])
def test_config_rejects_unsafe_values(bad):
    with pytest.raises(TradingError):
        ContinuousConfig.parse({"mode": "continuous", **bad})


def test_continuous_not_a_timer_and_validation_never_runs_loop(tmp_path):
    cfg = _config(tmp_path)
    pkg = package(cfg)
    assert pkg.manifest.schedule.type == "none"
    assert not pkg.manifest.schedule.enabled
    assert validate_strategy_package(cfg.paths, pkg.strategy_id).ok
    assert apply_strategy_schedules(cfg.paths, pkg).trading_id == ""
    with pytest.raises(Exception, match="continuous"):
        StrategyRunner(cfg).run_tick(pkg.strategy_id)


def test_stays_running_beyond_tick_budget_and_stops(tmp_path):
    cfg = _config(tmp_path)
    pkg = package(cfg)
    manager = supervisor(cfg, Kernel(cfg))
    try:
        first = manager.start(pkg.strategy_id)
        until(lambda: manager.status(pkg.strategy_id)["state"] == "running")
        time.sleep(1.3)
        assert manager.status(pkg.strategy_id)["listener_alive"]
        assert manager.start(pkg.strategy_id)["generation"] == first["generation"]
        other = ContinuousSupervisor(cfg)
        assert other.start(pkg.strategy_id)["generation"] == first["generation"]
        assert not other._services
        stopped = manager.stop(pkg.strategy_id)
        assert stopped["state"] == "stopped"
        assert stopped["stop_reason"] == "operator_stop"
    finally:
        manager.shutdown()


def test_event_tombstone_survives_restart_and_uncertain_is_not_replayed(tmp_path):
    ledger = EventLedger(tmp_path / "events.sqlite")
    assert ledger.claim("id", "old", "hash", 1, 5)
    ledger.finish("id", "running")
    recovered = EventLedger(tmp_path / "events.sqlite")
    recovered.abandon_previous()
    assert recovered.list()[0]["status"] == "uncertain"
    assert not recovered.claim("id", "new", "hash", 2, 6)


def test_queue_is_bounded_and_snapshot_is_frozen(tmp_path):
    cfg = _config(tmp_path)
    pkg = package(cfg)
    manager = ContinuousSupervisor(cfg)
    s = _Service(package=pkg, settings=ContinuousConfig(queue_size=1, min_dispatch_interval_seconds=0))
    s.queue = queue.Queue(maxsize=1)
    s.ledger = EventLedger(tmp_path / "events.sqlite")
    s.data = {"accepted_events": 0, "rejected_events": 0}
    task = StrategyAgentTask.dispatch(prompt="inspect", context={"price": 42})
    assert manager._enqueue(s, task, "first", time.time(), {})["accepted"]
    task.context["price"] = 99
    assert manager._enqueue(s, task, "second", time.time(), {})["reason"] == "queue_full"
    item = s.queue.get_nowait()
    assert item[2]["task"]["context"]["price"] == 42
    assert manager._enqueue(s, task, "first", time.time(), {})["reason"] == "duplicate"
    assert manager._enqueue(s, task, "old", time.time()-120, {})["reason"] == "expired"


def test_real_websocket_reconnect_dedupe_and_paper_order(tmp_path):
    cfg = _config(tmp_path)
    yaml_io.dump(cfg.paths.root / "security/web_policy.yml", {"allow_hosts": ["127.0.0.1"]})
    connections = []
    release = threading.Event()
    stamp = time.time()
    def handler(ws):
        connections.append(True)
        assert json.loads(ws.recv(timeout=2)) == {"subscribe": "prices"}
        ws.send(json.dumps({"id": "small", "move": 0.1, "price": 50000, "observed_at": stamp}))
        ws.send(json.dumps({"id": "shock-1", "move": 2.5, "price": 50000, "observed_at": stamp}))
        if len(connections) > 1:
            release.wait(8)
    with serve(handler, "127.0.0.1", 0) as server:
        host = threading.Thread(target=server.serve_forever, daemon=True)
        host.start()
        port = server.socket.getsockname()[1]
        pkg = package(cfg, WS, streams={"prices": {"url": f"ws://127.0.0.1:{port}", "subscribe": [{"subscribe": "prices"}], "reconnect_seconds": 0.1}})
        kernel = Kernel(cfg, trade=True)
        manager = supervisor(cfg, kernel)
        try:
            manager.start(pkg.strategy_id)
            until(lambda: len(connections) >= 2)
            until(lambda: manager.status(pkg.strategy_id).get("completed_events") == 1)
            assert len(kernel.calls) == 1
            assert kernel.receipts and not kernel.receipts[0].is_error, kernel.receipts
            receipt = kernel.receipts[0].content[0].data
            assert receipt["status"] == "filled", receipt
            events = manager.events(pkg.strategy_id)["events"]
            assert len(events) == 1 and events[0]["status"] == "executed", events
            assert "shock-1" in kernel.calls[0]["trigger"]["payload"]["text"]
            assert manager.status(pkg.strategy_id)["state"] == "running"
            from contextlib import closing
            from nerya.trading.order_tracker import OrderTracker
            with closing(OrderTracker(cfg.paths)) as tracker:
                order = tracker.get(receipt["order_id"])
                assert order and order.state == "filled" and order.strategy_id == pkg.strategy_id
                fills = tracker.fills_for_order(order.order_id)
                assert len(fills) == 1 and fills[0].source == "paper"
                assert len(tracker.cached_orders(account_id="paper_main")) == 1
            assert jsonl.read_all(cfg.paths.approvals_pending) == []
            assert manager.stop(pkg.strategy_id)["state"] == "stopped"
            with pytest.raises(TradingError):
                assert_event_active(cfg, pkg.strategy_id, events[0]["event_id"])
        finally:
            manager.shutdown()
            release.set()
            server.shutdown()
            host.join(3)


def test_kill_and_source_change_stop_listener(tmp_path):
    cfg = _config(tmp_path)
    pkg = package(cfg)
    manager = supervisor(cfg, Kernel(cfg))
    try:
        manager.start(pkg.strategy_id)
        until(lambda: manager.status(pkg.strategy_id)["state"] == "running")
        StrategyKillSwitch(cfg.paths, pkg.strategy_id).assert_(reason="test", by="test")
        until(lambda: manager.status(pkg.strategy_id)["state"] == "stopped")
        with pytest.raises(TradingError, match="kill switch"):
            manager.start(pkg.strategy_id)
        StrategyKillSwitch(cfg.paths, pkg.strategy_id).clear(by="test")
        manager.start(pkg.strategy_id)
        until(lambda: manager.status(pkg.strategy_id)["state"] == "running")
        (pkg.root / "main.py").write_text(IDLE + "\n# revised\n")
        until(lambda: manager.status(pkg.strategy_id)["state"] == "stopped")
        assert manager.status(pkg.strategy_id)["stop_reason"] == "source_changed"
        with pytest.raises(TradingError, match="changed"):
            manager.start(pkg.strategy_id, expected_hash=pkg.content_hash)
    finally:
        manager.shutdown()

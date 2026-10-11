import time

import pytest

from nerya.trading.guard import TradingGuard, active
from nerya.db.sqlite import connect
from test_trading_kernel_safety import cfg

pytestmark = pytest.mark.smoke


def test_guard_owner_is_unique_and_expired_owner_can_be_recovered(cfg):
    first, second = TradingGuard(cfg), TradingGuard(cfg)
    assert first.claim() and active(cfg)
    assert not second.claim()
    second.release()
    assert active(cfg)
    con = connect(cfg.paths.db)
    con.execute("UPDATE trading_guard_lease SET lease_until=?", (time.time()-1,))
    con.close()
    assert second.claim()
    first.release()
    assert active(cfg)
    second.release()
    assert not active(cfg)


def test_guard_receipt_recovery_and_executor_are_independent_of_agent_turns(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr("nerya.trading.order_polling.poll_active_live_orders", lambda *_: calls.append("orders"))
    monkeypatch.setattr("nerya.trading.executors.orchestrator.ExecutorOrchestrator.run_once", lambda *_: calls.append("protection") or 2)
    monkeypatch.setattr("nerya.wallet.swap_approval.reconcile_pending", lambda *_: calls.append("receipts"))
    guard = TradingGuard(cfg)
    result = guard.run_once()
    assert result["executors"] == 2 and calls == ["orders", "protection", "receipts"]
    assert TradingGuard(cfg).run_once()["state"] == "standby"
    guard.release()


def test_guard_cli_is_registered_without_starting_services():
    from nerya.cli.app import build_parser
    args = build_parser().parse_args(["trading", "guard", "--once", "--workspace", "/tmp/example"])
    assert args.once and args.interval == 5

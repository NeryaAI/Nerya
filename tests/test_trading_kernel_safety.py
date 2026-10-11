"""Admission, account truth and preview contracts for all strategy producers."""
from dataclasses import replace
import socket
import time
from types import SimpleNamespace

import pytest

from nerya.connectors.base import Balance, ContractPosition
from nerya.connectors.ccxt_adapter import CcxtConnector
from nerya.core.errors import TradingError
from nerya.sdk.trading_api import TradingAPI
from nerya.trading.account_snapshots import AccountSnapshot, _live_snapshot, _persist, latest_snapshot
from nerya.trading.accounts import get_account_profile
from nerya.trading.capital import BudgetChecker, CapitalReservationStore
from nerya.trading.intents import TradeIntent
from nerya.trading.order_intents import SizingPolicy
from nerya.trading.risk import RiskGate
from test_risk_gate import _build_workspace

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("NETWORK_DISABLED")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def cfg(tmp_path):
    return _build_workspace(tmp_path, venue="mock", market="MOCK:SOLUSDT", accounts=["acct_1", "acct_2"])


def snapshot(account_id="acct_1", **kwargs):
    return AccountSnapshot("audit_" + account_id, account_id, time.time(), "paper", 1000,
                           free_by_asset={"USDT": 1000}, **kwargs)


def test_all_account_snapshot_reads_and_indexes_each_snapshot_once(cfg, monkeypatch):
    calls = []
    def latest(paths):
        calls.append(paths)
        return [snapshot(), snapshot("acct_2")]
    monkeypatch.setattr("nerya.trading.account_snapshots.latest_snapshots", latest)
    result = TradingAPI(cfg, None).portfolio_snapshot()
    assert [row["snapshot"]["account_id"] for row in result["accounts"]] == ["acct_1", "acct_2"]
    assert len(calls) == 1


def test_preview_does_not_persist_risk_or_consume_submission_dedupe(cfg, monkeypatch):
    intent = TradeIntent.new(strategy_id="alpha", account_id="acct_1", market="MOCK:SOLUSDT",
                             side="buy", size=100, size_unit="usd", order_type="market", confidence=1,
                             source="strategy_runtime")
    mark = {"price": 100, "age_s": 0}
    records = []
    with monkeypatch.context() as patch:
        patch.setattr(RiskGate, "_persist", lambda *a, **kw: records.append(a))
        result = TradingAPI(cfg, None).risk_preview(intent=intent.asdict(), market_snapshot=mark)
        assert result["risk_decision"]["decision"] == "allow"
        assert records == []
    assert RiskGate(cfg).evaluate(intent, market_snapshot=mark).decision == "allow"


def test_rejected_preview_does_not_persist_risk(cfg, monkeypatch):
    records = []
    monkeypatch.setattr(RiskGate, "_persist", lambda *a, **kw: records.append(a))
    intent = TradeIntent.new(strategy_id="missing", account_id="acct_1", market="MOCK:SOLUSDT",
                             side="buy", size=100, size_unit="usd", order_type="market", confidence=1)
    assert RiskGate(cfg).evaluate(intent, preview=True).decision == "reject"
    assert not records


@pytest.mark.parametrize("failed", ["positions", "open_orders"])
def test_failed_private_collection_cannot_be_healthy_zero_exposure(cfg, monkeypatch, failed):
    class Venue:
        kind = "cex"
        def get_balances(self):
            return [Balance("USDT", 1000, total=1000)]
        def fetch_positions(self):
            if failed == "positions":
                raise TimeoutError("positions unavailable")
            return [ContractPosition("BYBIT:SOL/USDT:USDT", "long", contracts=2,
                                     mark_price=100, notional_usd=200, initial_margin_usd=40)]
        def fetch_open_orders(self):
            if failed == "open_orders":
                raise TimeoutError("orders unavailable")
            return []
    monkeypatch.setattr("nerya.connectors.ConnectorRegistry", lambda **kw: SimpleNamespace(get=lambda *a: Venue()))
    profile = replace(get_account_profile(cfg.paths, "acct_1"), mode="live", venue="bybit_perpetual")
    snap = _live_snapshot(profile, cfg)
    assert snap.health == "degraded"
    assert snap.meta["collections"][failed]["status"] == "error"
    assert snap.meta["collections"][failed]["error"]


def test_position_valuation_does_not_alias_resting_order_notional():
    snap = snapshot(open_order_notional_usd=777, meta={"positions_value_usd": 250})
    assert snap.positions_value_usd == 250
    assert snapshot(open_order_notional_usd=777).positions_value_usd is None


def test_collection_evidence_survives_snapshot_persistence(cfg):
    snap = snapshot(meta={"positions_value_usd": 250, "collections": {"positions": {"status": "ok", "as_of": 123}}})
    _persist(cfg.paths, snap)
    restored = latest_snapshot(cfg.paths, "acct_1")
    assert restored.meta == snap.meta
    assert restored.positions_value_usd == 250


def budget(cfg, **kwargs):
    return BudgetChecker(profile=get_account_profile(cfg.paths, "acct_1"), snapshot=snapshot(**kwargs),
                         store=CapitalReservationStore(cfg.paths))


def order_args(**kwargs):
    return {"plan_strategy_id": "alpha", "market": "MOCK:SOLUSDT", "side": "buy",
            "sizing": SizingPolicy(method="fixed_usd", fixed_usd=800), "mark_price": 100, **kwargs}


def test_known_reduction_does_not_require_healthy_cash_snapshot(cfg):
    decision = budget(cfg, health="degraded").evaluate(**order_args(side="sell", reduce_only=True))
    assert decision.verdict == "allow"
    assert "snapshot_health_degraded_exempt" in decision.reasons


def test_resize_cannot_overwrite_bad_snapshot_rejection(cfg):
    checker = budget(cfg, health="degraded")
    checker.profile.limits.max_order_notional_usd = 100
    assert checker.evaluate(**order_args()).verdict == "reject"


def test_atomic_reservation_rechecks_stale_concurrent_budget(cfg):
    checker = budget(cfg)
    first = checker.evaluate(**order_args())
    second = checker.evaluate(**order_args())
    assert first.verdict == second.verdict == "allow"
    checker.store.reserve_checked(candidate=first.candidate, profile=checker.profile, snapshot=checker.snapshot)
    with pytest.raises(TradingError, match="budget_changed_before_reservation"):
        checker.store.reserve_checked(candidate=second.candidate, profile=checker.profile, snapshot=checker.snapshot)
    assert 800 <= checker.store.total_blocked_usd("acct_1") < 801


def test_margin_and_notional_are_not_added_as_two_collateral_requirements(cfg):
    store = CapitalReservationStore(cfg.paths)
    row = store.reserve(account_id="acct_1", strategy_id="alpha", market="MOCK:SOLUSDT", side="buy",
                        notional_usd=1000, estimated_margin_usd=100, estimated_fee_usd=1)
    assert row.total_blocked_usd == 101


def test_real_ccxt_client_object_is_used_for_funds_calls():
    calls = []
    client = SimpleNamespace(has={"transfer": True, "fetchTransfers": True, "withdraw": True, "fetchWithdrawals": True},
                             transfer=lambda *a: calls.append(a) or {"id": "transfer"})
    conn = CcxtConnector(exchange_id="bybit", _client=client)
    assert conn.funds_capabilities()["transfer"] is True
    conn._check_live_and_keys = lambda: None
    assert conn.transfer_assets("USDT", "2", "spot", "funding") == {"id": "transfer"}
    assert calls == [("USDT", "2", "spot", "funding", {})]


@pytest.mark.parametrize("state", ["submitted", "partially_filled", "lost"])
def test_local_reservation_ttl_cannot_release_an_uncertain_venue_order(cfg, state):
    from nerya.trading.order_tracker import OrderTracker
    store = CapitalReservationStore(cfg.paths)
    row = store.reserve(account_id="acct_1", strategy_id="alpha", market="MOCK:SOLUSDT", side="buy",
                        notional_usd=100, ttl_seconds=1)
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(client_order_id="ttl_" + state, account_id="acct_1", strategy_id="alpha",
                             market="MOCK:SOLUSDT", side="buy", order_type="limit", size_base=1,
                             notional_usd=100, price=100, reservation_id=row.reservation_id)
    tracker.update_state(order.order_id, state)
    assert store.expire_due(now=time.time() + 10) == 0
    assert store.get(row.reservation_id).state == "reserved"
    tracker.update_state(order.order_id, "canceled")
    assert store.expire_due(now=time.time() + 10) == 1
    tracker.close()


def test_strategy_drawdown_limit_uses_observed_account_high_water(cfg, monkeypatch):
    from nerya.core import yaml_io
    limits = cfg.paths.strategy("alpha") / "limits.yml"
    data = yaml_io.load(limits)
    data["max_drawdown_pct"] = 10
    yaml_io.dump(limits, data)
    _persist(cfg.paths, snapshot())
    monkeypatch.setattr("nerya.trading.risk.fresh_snapshot", lambda *a, **kw: replace(snapshot(), nav_usd=800))
    intent = TradeIntent.new(strategy_id="alpha", account_id="acct_1", market="MOCK:SOLUSDT",
                             side="buy", size=100, size_unit="usd", order_type="market", confidence=1,
                             source="strategy_runtime")
    result = RiskGate(cfg).evaluate(intent, market_snapshot={"price": 100, "age_s": 0}, preview=True)
    assert result.decision == "reject"
    assert any(reason.startswith("max_drawdown_exceeded") for reason in result.reasons)


def test_concurrent_reductions_cannot_spend_another_strategy_position(cfg):
    from nerya.trading.position_book import PositionBook
    book = PositionBook(cfg.paths)
    for strategy in ("alpha", "beta"):
        book.apply_fill(account_id="acct_1", strategy_id=strategy, market="MOCK:SOLUSDT", side="buy", price=100, size_base=10)
    checker = budget(cfg)
    first = checker.evaluate(**order_args(side="sell", reduce_only=True, sizing=SizingPolicy(method="fixed_base", fixed_base=6)))
    second = checker.evaluate(**order_args(side="sell", reduce_only=True, sizing=SizingPolicy(method="fixed_base", fixed_base=7)))
    checker.store.reserve_checked(candidate=first.candidate, profile=checker.profile, snapshot=checker.snapshot)
    with pytest.raises(TradingError, match="close_quantity_already_reserved"):
        checker.store.reserve_checked(candidate=second.candidate, profile=checker.profile, snapshot=checker.snapshot)
    book.close()


def test_atomic_reservation_preserves_the_account_cash_floor(cfg):
    checker = budget(cfg)
    checker.profile.limits.min_free_balance_pct = .2
    first = checker.evaluate(**order_args(sizing=SizingPolicy(method="fixed_usd", fixed_usd=400)))
    second = checker.evaluate(**order_args(sizing=SizingPolicy(method="fixed_usd", fixed_usd=450)))
    assert first.verdict == second.verdict == "allow"
    checker.store.reserve_checked(candidate=first.candidate, profile=checker.profile, snapshot=checker.snapshot)
    with pytest.raises(TradingError, match="free_balance_floor"):
        checker.store.reserve_checked(candidate=second.candidate, profile=checker.profile, snapshot=checker.snapshot)


@pytest.mark.parametrize("mode", ["halt_new_risk", "reduce_only"])
def test_opposite_hedge_open_is_new_risk_not_a_net_position_exit(cfg, mode):
    from nerya.trading.position_book import PositionBook
    book = PositionBook(cfg.paths)
    book.apply_fill(account_id="acct_1", strategy_id="alpha", market="MOCK:SOLUSDT", side="buy", price=100,
                    size_base=10, position_side="long")
    cfg.data["trading"]["risk_mode"] = mode
    intent = TradeIntent.new(strategy_id="alpha", account_id="acct_1", market="MOCK:SOLUSDT", side="sell",
                             size=100, size_unit="usd", order_type="market", confidence=1, source="strategy_runtime",
                             meta={"position_side": "short", "plan_action": "open_position"})
    assert "trading_risk_mode_blocks_action" in RiskGate(cfg).evaluate(intent, market_snapshot={"price": 100}, preview=True).reasons
    book.close()


def test_preview_refresh_does_not_persist_account_collection(cfg, monkeypatch):
    from nerya.trading.account_snapshots import fresh_snapshot
    calls = []
    monkeypatch.setattr("nerya.trading.account_snapshots.capture_snapshot", lambda *a, **kw: calls.append(kw) or snapshot())
    fresh_snapshot(cfg, "acct_1", persist=False)
    assert calls == [{"profile": None, "persist": False}]


def test_spot_unsupported_position_query_is_not_an_empty_derivatives_account():
    conn = CcxtConnector(exchange_id="bitstamp", _client=SimpleNamespace(has={"fetchPositions": False}))
    conn._check_live_and_keys = lambda: None
    with pytest.raises(NotImplementedError):
        conn.fetch_positions()


def test_fresh_workspace_preview_creates_no_ledger_or_database_files(cfg):
    def files():
        return {str(path.relative_to(cfg.paths.root)): path.read_bytes() for path in cfg.paths.root.rglob("*") if path.is_file()}
    before = files()
    intent = TradeIntent.new(strategy_id="alpha", account_id="acct_1", market="MOCK:SOLUSDT", side="buy",
                             size=100, size_unit="usd", order_type="market", confidence=1, source="strategy_runtime")
    assert RiskGate(cfg).evaluate(intent, market_snapshot={"price": 100}, preview=True).decision == "allow"
    assert files() == before

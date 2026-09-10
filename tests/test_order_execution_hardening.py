"""Hardening fixes for the order-execution layer.

Covers audit findings:

* B1 — ambiguous ``place_order`` failures keep the order tracked
  (``submitted`` + ``place_unknown`` note) instead of orphaning a live
  venue order behind a local ``rejected`` row; the resume/poller path
  adopts the venue order by client id.
* F1 — ``make_client_order_id`` stays within venue charset/length
  limits (``[A-Za-z0-9_-]{1,36}``).
* B2 — cumulative venue fees are converted to per-fill increments.
* B4 — ``record_fill`` cumulative watermark + deterministic fill ids +
  ``PositionBook.apply_fill`` idempotency kill the double-fill race.
* F5 — ``lost`` orders finalize the executor (release reservations).
* B7 — the background poller skips paper/shadow accounts and only
  counts definitive venue not-founds toward ``lost``.
* B9 — reconciliation spot fallback builds book-compatible market ids.
* B8 — partial-exit protection re-arms; ``pnl_usd``/``r_multiple``
  specs are rejected; stale marks never trigger soft stops.
* B12 — paper fills respect a raced cancel.
* D6 — fills mirror into the strategy history ledger.
"""

from __future__ import annotations

import re
import time
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import IntentValidationError
from nerya.core.paths import WorkspacePaths
from nerya.trading.executors.market_order import MarketOrderExecutor
from nerya.trading.executors.orchestrator import ExecutorOrchestrator
from nerya.trading.executors.position_protection import PositionProtectionExecutor
from nerya.trading.order_intents import (
    OrderCandidate,
    PartialExitSpec,
    ProtectionRule,
    StopLossSpec,
)
from nerya.trading.order_polling import (
    is_definitive_not_found_error,
    poll_active_live_orders,
)
from nerya.trading.order_tracker import (
    TERMINAL_STATES,
    OrderTracker,
    make_client_order_id,
)
from nerya.trading.position_book import PositionBook
from nerya.trading.protection_store import ProtectionStore
from nerya.strategy_history.store import read_ledger


pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _config(tmp_path, *, accounts: list[dict[str, Any]]) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["runtime"]["live_trading_enabled"] = True
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(tmp_path / "nerya.yml", data)
    yaml_io.dump(cfg.paths.accounts_file, {"accounts": accounts})
    return cfg


def _account(account_id: str, mode: str) -> dict[str, Any]:
    return {
        "id": account_id,
        "exchange": "mock",
        "venue": "mock",
        "mode": mode,
        "status": "active",
        "initial_balance_usd": 100_000,
        "live_trading_enabled": True,
        "permissions": {
            "read_balances": True, "place_order": True, "cancel_order": True,
        },
    }


def _candidate(**overrides: Any) -> OrderCandidate:
    payload: dict[str, Any] = {
        "account_id": "live_main",
        "strategy_id": "s1",
        "market": "mock:BTC/USDT",
        "side": "buy",
        "order_type": "market",
        "size_base": 0.2,
        "notional_usd": 10_000.0,
    }
    payload.update(overrides)
    return OrderCandidate(**payload)


class _FakeRegistry:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def get(self, _account_id: str, _connector_cfg: dict) -> Any:
        return self._conn


class _VenueStub:
    """Fake live connector: place_order 'accepts' then the transport dies."""

    def __init__(self, *, place_error: Exception | None = None) -> None:
        self.open_orders: list[Any] = []
        self.place_calls = 0
        self.acked: dict[str, Any] = {}
        self.place_error = place_error
        self.get_order_error: Exception | None = None

    def place_order(self, **kwargs: Any) -> Any:
        self.place_calls += 1
        # The venue *accepts* the order first — then the transport dies
        # before the response reaches us (the B1 orphan scenario).
        ack = SimpleNamespace(
            order_id=f"venue-{self.place_calls}",
            client_order_id=str(kwargs.get("client_order_id") or ""),
            status="new",
            market=kwargs.get("market"),
            side=kwargs.get("side"),
            filled=0.0,
            avg_price=0.0,
            fee_usd=0.0,
        )
        self.open_orders.append(ack)
        self.acked[ack.client_order_id] = ack
        if self.place_error is not None:
            exc = self.place_error
            self.place_error = None
            raise exc
        return ack

    def fetch_open_orders(self) -> list[Any]:
        return list(self.open_orders)

    def get_order(self, *, market: str, order_id: str) -> Any:
        if self.get_order_error is not None:
            exc = self.get_order_error
            self.get_order_error = None
            raise exc
        for ack in self.open_orders:
            if ack.order_id == order_id:
                return ack
        raise RuntimeError(f"OrderNotFound: order not found: {order_id}")


# ---------------------------------------------------------------------------
# F1 — client order id charset / length
# ---------------------------------------------------------------------------


def test_make_client_order_id_venue_safe_and_unique():
    seen = set()
    pattern = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
    for i in range(200):
        cid = make_client_order_id(
            strategy_id="s" * 40,
            executor_id="e" * 40,
            leg="long_leg_entry",
            seq=i,
        )
        assert pattern.match(cid), cid
        assert len(cid) <= 36
        assert ":" not in cid
        seen.add(cid)
    assert len(seen) == 200, "ids must stay unique across seq"


def test_make_client_order_id_deterministic():
    kwargs = dict(strategy_id="strat", executor_id="exec", leg="0", seq=7)
    assert make_client_order_id(**kwargs) == make_client_order_id(**kwargs)


def test_lost_is_terminal_state():
    assert "lost" in TERMINAL_STATES


# ---------------------------------------------------------------------------
# B4 — record_fill watermark + apply_fill idempotency
# ---------------------------------------------------------------------------


def test_record_fill_cumulative_watermark_dedupes(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(
        client_order_id="cid-b4",
        account_id="live_main",
        strategy_id="s1",
        market="mock:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=1.0,
    )
    first = tracker.record_fill(
        order_id=order.order_id, price=100.0, size_base=0.5,
        fee_usd=1.0, source="live", cumulative_filled=0.5,
    )
    assert first is not None
    assert first.fill_id == f"{order.order_id}:0.5"
    # Replay of the same cumulative — the poller/executor race — is a no-op.
    replay = tracker.record_fill(
        order_id=order.order_id, price=100.0, size_base=0.5,
        fee_usd=1.0, source="live", cumulative_filled=0.5,
    )
    assert replay is None
    assert len(tracker.fills_for_order(order.order_id)) == 1
    # A genuine further fill still records (deterministic id per level).
    second = tracker.record_fill(
        order_id=order.order_id, price=101.0, size_base=0.2,
        fee_usd=1.5, source="live", cumulative_filled=0.7,
    )
    assert second is not None
    rows = tracker.fills_for_order(order.order_id)
    assert [r.size_base for r in rows] == pytest.approx([0.5, 0.2])
    refreshed = tracker.get(order.order_id)
    assert refreshed.filled_size == pytest.approx(0.7)
    assert refreshed.fee_usd == pytest.approx(2.5)


def test_apply_fill_idempotent_on_fill_id(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("paper_main", "paper")])
    book = PositionBook(cfg.paths)
    kwargs = dict(
        account_id="paper_main",
        strategy_id="s1",
        market="mock:BTC/USDT",
        side="buy",
        price=100.0,
        size_base=1.0,
        fee_usd=0.5,
    )
    pos = book.apply_fill(fill_id="fill-1", **kwargs)
    assert pos.size_base == pytest.approx(1.0)
    # Same fill_id again (double-apply race) — no second effect.
    pos2 = book.apply_fill(fill_id="fill-1", **kwargs)
    assert pos2.size_base == pytest.approx(1.0)
    assert pos2.fees_usd == pytest.approx(0.5)
    merged = book.get_open_merged(account_id="paper_main", market="mock:BTC/USDT")
    assert merged.size_base == pytest.approx(1.0)
    share = book.get_share(
        strategy_id="s1", account_id="paper_main", market="mock:BTC/USDT",
    )
    assert share.size_share_base == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# B1 — ambiguous place_order keeps the order tracked and recoverable
# ---------------------------------------------------------------------------


def test_place_timeout_after_venue_accept_keeps_order_recoverable(tmp_path, monkeypatch):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    stub = _VenueStub(
        place_error=TimeoutError("ReadTimeout: connection timed out after send"),
    )
    monkeypatch.setattr("nerya.connectors.ConnectorRegistry", lambda **kw: _FakeRegistry(stub))
    monkeypatch.setenv("NERYA_VAULT_PASSPHRASE", "unit-test-passphrase")

    orch = ExecutorOrchestrator(cfg)
    executor = orch.create_market_order(candidate=_candidate())
    try:
        # Tick 1: place_order times out *after* the stub "accepted" the order.
        terminal = orch.step_executor(executor)
        assert terminal is False

        tracker = OrderTracker(cfg.paths)
        order = tracker.get_by_client_order_id(executor.run.config_json["candidate"]["client_order_id"])
        assert order is not None
        assert order.state == "submitted", "must not be rejected — the venue may hold it"
        assert order.exchange_order_id is None
        assert any(
            str(n).startswith("place_unknown:") for n in (order.meta.get("notes") or [])
        )
        assert executor.run.state == "submitted"
        assert executor.run.order_ids == [order.order_id]
        assert tracker.active_orders(account_id="live_main"), "still pollable"

        # Tick 2: resume adopts the venue order by client id.
        terminal = orch.step_executor(executor)
        assert terminal is False
        order = tracker.get(order.order_id)
        assert order.exchange_order_id == "venue-1"
        assert order.state == "submitted"

        # Tick 3: the venue reports a full fill — the run finalizes done.
        adopted = stub.open_orders[0]
        adopted.filled = 0.2
        adopted.avg_price = 50_000.0
        adopted.status = "filled"
        terminal = orch.step_executor(executor)
        assert terminal is True
        assert executor.run.state == "done"
        order = tracker.get(order.order_id)
        assert order.state == "filled"
        assert order.filled_size == pytest.approx(0.2)
        book = PositionBook(cfg.paths)
        pos = book.get_open_merged(account_id="live_main", market="mock:BTC/USDT")
        assert pos is not None and pos.size_base == pytest.approx(0.2)
    finally:
        orch.close()


def test_place_definitive_reject_still_marks_rejected(tmp_path, monkeypatch):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    stub = _VenueStub(place_error=RuntimeError("InsufficientFunds: not enough balance"))
    monkeypatch.setattr("nerya.connectors.ConnectorRegistry", lambda **kw: _FakeRegistry(stub))
    monkeypatch.setenv("NERYA_VAULT_PASSPHRASE", "unit-test-passphrase")

    orch = ExecutorOrchestrator(cfg)
    executor = orch.create_market_order(candidate=_candidate())
    try:
        # Tick 1: place_order rejected definitively — order marked rejected.
        orch.step_executor(executor)
        assert executor.run.state == "failed"
        tracker = OrderTracker(cfg.paths)
        active = tracker.active_orders(account_id="live_main")
        assert active == []
        cached = tracker.cached_orders(account_id="live_main")
        assert cached and cached[0].state == "rejected"
        # Tick 2: the executor finalizes on the rejected order row.
        terminal = orch.step_executor(executor)
        assert terminal is True
    finally:
        orch.close()


def test_is_definitive_not_found_classification():
    assert is_definitive_not_found_error(RuntimeError("OrderNotFound: no such order"))
    assert is_definitive_not_found_error(
        RuntimeError("mock fetch_order failed: Order not found")
    )
    assert not is_definitive_not_found_error(RuntimeError("rate_limited"))
    assert not is_definitive_not_found_error(TimeoutError("timed out"))


# ---------------------------------------------------------------------------
# F5 — lost orders finalize the executor
# ---------------------------------------------------------------------------


def test_lost_order_finalizes_executor_and_releases(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("paper_main", "paper")])
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(
        client_order_id="cid-f5",
        account_id="paper_main",
        strategy_id="s1",
        market="mock:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=0.2,
        initial_state="submitted",
    )
    lost = False
    for _ in range(tracker.lost_threshold):
        lost = tracker.mark_not_found(order.order_id)
    assert lost and tracker.get(order.order_id).state == "lost"

    from nerya.trading.executors.base import ExecutorRun

    run = ExecutorRun(
        executor_id="ex_f5",
        kind="market_order",
        account_id="paper_main",
        strategy_id="s1",
        market="mock:BTC/USDT",
        state="submitted",
        order_ids=[order.order_id],
    )
    executor = MarketOrderExecutor(run, cfg.paths)
    terminal = executor.step()
    assert terminal is True
    assert run.state == "failed"
    assert run.result_json.get("reason") == "order_lost"


# ---------------------------------------------------------------------------
# B7 — poller skips paper/shadow accounts; only real not-founds strike
# ---------------------------------------------------------------------------


def _register_active_order(cfg: Config, account_id: str, cid: str, *, place_unknown: bool = True):
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(
        client_order_id=cid,
        account_id=account_id,
        strategy_id="s1",
        market="mock:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=0.2,
        initial_state="submitted",
        meta={"place_unknown": place_unknown},
    )
    tracker.mark_submitted(order.order_id)
    tracker.close()
    return order


def test_poller_skips_paper_accounts_and_transport_errors(tmp_path):
    cfg = _config(tmp_path, accounts=[
        _account("paper_main", "paper"), _account("live_main", "live"),
    ])
    paper_order = _register_active_order(cfg, "paper_main", "cid-paper", place_unknown=False)
    live_order = _register_active_order(cfg, "live_main", "cid-live", place_unknown=False)

    class _ErrConn(_VenueStub):
        def get_order(self, *, market, order_id):
            raise RuntimeError("rate_limited")

    out = poll_active_live_orders(
        cfg, connector_factory=lambda _aid, _cfg: _ErrConn(),
    )
    # Paper order never reached the connector; live order errored softly.
    assert out.scanned == 1
    assert out.errors == 1
    assert out.not_found == 0
    paper_per = next(p for p in out.per_order if p["order_id"] == paper_order.order_id)
    assert paper_per["state"] == "mode_skipped:paper"
    tracker = OrderTracker(cfg.paths)
    assert tracker.get(paper_order.order_id).not_found_streak == 0
    assert tracker.get(live_order.order_id).not_found_streak == 0
    tracker.close()


def test_poller_counts_only_definitive_not_found_toward_lost(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    live_order = _register_active_order(cfg, "live_main", "cid-nf")
    # place_unknown order with no venue order to adopt — adoption fails
    # each tick but that must NOT strike toward lost.
    for _ in range(6):
        out = poll_active_live_orders(
            cfg, connector_factory=lambda _aid, _cfg: _VenueStub(),
        )
        assert out.not_found == 0
    tracker = OrderTracker(cfg.paths)
    assert tracker.get(live_order.order_id).state == "submitted"
    tracker.close()

    # Now the order has a venue id and the venue provably reports not-found.
    tracker = OrderTracker(cfg.paths)
    tracker.mark_submitted(live_order.order_id, exchange_order_id="venue-gone")
    tracker.close()
    for _ in range(4):
        out = poll_active_live_orders(
            cfg, connector_factory=lambda _aid, _cfg: _VenueStub(),
        )
    tracker = OrderTracker(cfg.paths)
    assert tracker.get(live_order.order_id).state == "lost"
    tracker.close()


# ---------------------------------------------------------------------------
# B2 — cumulative fee becomes incremental at the poller
# ---------------------------------------------------------------------------


def test_poller_fee_incremental_on_partial_fills(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    order = _register_active_order(cfg, "live_main", "cid-fee")
    tracker = OrderTracker(cfg.paths)
    tracker.mark_submitted(order.order_id, exchange_order_id="venue-fee")
    tracker.close()

    class _FillConn(_VenueStub):
        def __init__(self) -> None:
            super().__init__()
            self.filled = 0.12
            self.fee = 1.5

        def get_order(self, *, market, order_id):
            return SimpleNamespace(
                order_id=order_id, client_order_id="", status="open",
                market=market, side="buy",
                filled=self.filled, avg_price=50_010.0, fee_usd=self.fee,
            )

    conn = _FillConn()
    out = poll_active_live_orders(cfg, connector_factory=lambda _aid, _cfg: conn)
    assert out.fills_applied == 1
    tracker = OrderTracker(cfg.paths)
    row = tracker.get(order.order_id)
    assert row.fee_usd == pytest.approx(1.5)

    # Next tick: cumulative fee 2.5 over 0.2 filled → only +1.0 fee, +0.08 size.
    conn.filled = 0.2
    conn.fee = 2.5
    poll_active_live_orders(cfg, connector_factory=lambda _aid, _cfg: conn)
    row = tracker.get(order.order_id)
    assert row.fee_usd == pytest.approx(2.5)
    assert row.filled_size == pytest.approx(0.2)
    tracker.close()


# ---------------------------------------------------------------------------
# B9 — reconciliation spot-fallback market ids match the book
# ---------------------------------------------------------------------------


def test_reconciliation_spot_fallback_market_casing(tmp_path, monkeypatch):
    from nerya.trading import reconciliation

    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    profile = SimpleNamespace(
        id="live_main",
        venue="Bybit",
        base_currency="USDT",
        reads_real_balances=True,
        to_connector_account=lambda live=True: SimpleNamespace(connector_cfg=lambda: {}),
    )

    class _BalConn:
        def get_balances(self):
            return [SimpleNamespace(asset="ETH", total=2.5)]

    monkeypatch.setattr(
        "nerya.connectors.ConnectorRegistry",
        lambda **kw: _FakeRegistry(_BalConn()),
    )
    positions, covered = reconciliation._fetch_exchange_positions(cfg, profile)
    # Book keys are lowercase-venue market ids — the diff must compare
    # like with like, not "BYBIT:ETHUSDT".
    assert positions == [{"market": "bybit:ETHUSDT", "size_base": 2.5}]
    assert covered == {"bybit:ETHUSDT"}

    # A venue whose balance query fails covers nothing — no
    # external_closed conclusions may be drawn from it.
    class _DeadConn:
        def get_balances(self):
            raise RuntimeError("network down")

    monkeypatch.setattr(
        "nerya.connectors.ConnectorRegistry",
        lambda **kw: _FakeRegistry(_DeadConn()),
    )
    positions, covered = reconciliation._fetch_exchange_positions(cfg, profile)
    assert positions == [] and covered == set()


# ---------------------------------------------------------------------------
# B8 — protection: re-arm after partial, reject dead spec types, stale marks
# ---------------------------------------------------------------------------


def _protection_setup(cfg: Config, rule: ProtectionRule):
    book = PositionBook(cfg.paths)
    position = book.apply_fill(
        account_id="paper_main",
        strategy_id="protect_s1",
        market="mock:BTC/USDT",
        side="buy",
        price=100.0,
        size_base=1.0,
        source="paper",
    )
    rule.position_id = position.position_id
    ProtectionStore(cfg.paths).upsert(rule)
    book.attach_protection(position.position_id, rule.protection_id)
    orch = ExecutorOrchestrator(cfg)
    executor = orch.create_position_protection(
        rule=rule, position_id=position.position_id,
    )
    return orch, executor, position


def _fill_child(self):
    """Stub flattener: applies its reduce fill to the book, then finishes."""
    candidate = self._candidate()
    if candidate.reduce_only and candidate.size_base:
        PositionBook(self.paths).apply_fill(
            account_id=candidate.account_id,
            strategy_id=candidate.strategy_id,
            market=candidate.market,
            side=candidate.side,
            price=110.0,
            size_base=float(candidate.size_base),
            fee_usd=0.0,
            source="paper",
            fill_id=f"stub-{self.run.executor_id}",
        )
    self.transition("done", close_type="filled")
    return True


def test_partial_exit_rearms_rule_for_remaining_position(tmp_path, monkeypatch):
    cfg = _config(tmp_path, accounts=[_account("paper_main", "paper")])
    rule = ProtectionRule(
        position_id="", strategy_id="protect_s1", account_id="paper_main",
        market="mock:BTC/USDT", side="long",
        stop_loss=StopLossSpec(type="price", value=95.0),
        partial_exits=[PartialExitSpec(trigger_pct=0.05, close_pct=0.5)],
        status="armed",
    )
    orch, executor, position = _protection_setup(cfg, rule)
    monkeypatch.setattr(PositionProtectionExecutor, "_live_mark_price", lambda self, rule: 110.0)
    monkeypatch.setattr(MarketOrderExecutor, "step", _fill_child)
    try:
        # Tick 1: +10% mark fires the 5% partial; the stub flattener
        # fills, so the monitor immediately re-arms for the rest.
        assert orch.step_executor(executor) is False
        assert executor.run.state == "working"
        assert executor.run.result_json.get("pending_partial") is None
        executed = executor.run.result_json.get("partial_exits_executed")
        assert executed and executed[0]["trigger_pct"] == pytest.approx(0.05)

        stored = ProtectionStore(cfg.paths).get(rule.protection_id)
        assert stored.status == "armed", "rule must stay live for the remaining size"
        assert stored.partial_exits == [], "executed level must not refire"
        assert stored.stop_loss is not None

        pos = PositionBook(cfg.paths).get_by_id(position.position_id)
        assert pos.size_base == pytest.approx(0.5)

        # Tick 2: same mark — the executed level must not refire.
        assert orch.step_executor(executor) is False
        assert executor.run.state == "working"

        # Tick 3: price collapses — the SL must still protect the rest.
        monkeypatch.setattr(
            PositionProtectionExecutor, "_live_mark_price", lambda self, rule: 90.0,
        )
        terminal = orch.step_executor(executor)
        assert terminal is True
        assert executor.run.state == "done"
        assert executor.run.close_type == "stop_loss"
        stored = ProtectionStore(cfg.paths).get(rule.protection_id)
        assert stored.status == "triggered"
        assert stored.triggered_kind == "stop_loss"
        pos = PositionBook(cfg.paths).get_by_id(position.position_id)
        assert pos.size_base == pytest.approx(0.0)
    finally:
        orch.close()


def test_protection_rejects_pnl_usd_and_r_multiple_specs(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("paper_main", "paper")])
    store = ProtectionStore(cfg.paths)
    rule = ProtectionRule(
        position_id="pos_x", strategy_id="s1", account_id="paper_main",
        market="mock:BTC/USDT", side="long",
        stop_loss=StopLossSpec(type="pnl_usd", value=250.0),
        status="armed",
    )
    with pytest.raises(IntentValidationError, match="pnl_usd"):
        store.upsert(rule)

    tp_rule = ProtectionRule(
        position_id="pos_y", strategy_id="s1", account_id="paper_main",
        market="mock:BTC/USDT", side="long",
        take_profit=StopLossSpec(type="r_multiple", value=2.0),
        status="armed",
    )
    with pytest.raises(IntentValidationError, match="r_multiple"):
        store.upsert(tp_rule)


def test_protection_executor_rejects_unsupported_spec_at_prepare(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("paper_main", "paper")])
    rule = ProtectionRule(
        position_id="pos_z", strategy_id="s1", account_id="paper_main",
        market="mock:BTC/USDT", side="long",
        stop_loss=StopLossSpec(type="pnl_usd", value=100.0),
        status="armed",
    )
    orch = ExecutorOrchestrator(cfg)
    executor = orch.create_position_protection(rule=rule, position_id="pos_z")
    try:
        terminal = orch.step_executor(executor)
        assert terminal is True
        assert executor.run.state == "rejected"
        assert "unsupported_protection_spec" in str(executor.run.result_json.get("reason"))
    finally:
        orch.close()


def test_stale_mark_skips_trigger_and_journals(tmp_path, monkeypatch):
    cfg = _config(tmp_path, accounts=[_account("paper_main", "paper")])
    rule = ProtectionRule(
        position_id="", strategy_id="protect_s1", account_id="paper_main",
        market="mock:BTC/USDT", side="long",
        stop_loss=StopLossSpec(type="price", value=95.0),
        status="armed",
    )
    orch, executor, position = _protection_setup(cfg, rule)
    monkeypatch.setattr(PositionProtectionExecutor, "_live_mark_price", lambda self, rule: 0.0)
    try:
        # Freeze the position row's mark timestamps far in the past.
        from nerya.db.sqlite import connect

        stale_ts = time.time() - 10_000
        con = connect(cfg.paths.db)
        con.execute(
            "UPDATE positions SET updated_at = ?, mark_price = 50.0 WHERE position_id = ?",
            (stale_ts, position.position_id),
        )
        con.commit()
        con.close()

        terminal = orch.step_executor(executor)
        assert terminal is False
        assert executor.run.state == "working"
        assert executor.run.result_json.get("reason") == "stale_mark_skipped"
        assert executor.run.result_json.get("flatten_executor_id") is None
        stored = ProtectionStore(cfg.paths).get(rule.protection_id)
        assert stored.status == "armed", "no trigger may fire on a stale mark"
    finally:
        orch.close()


# ---------------------------------------------------------------------------
# B12 — paper fills respect a raced cancel
# ---------------------------------------------------------------------------


def test_paper_resolve_respects_cancel_race(tmp_path, monkeypatch):
    cfg = _config(tmp_path, accounts=[_account("paper_main", "paper")])
    monkeypatch.setattr(
        MarketOrderExecutor,
        "_paper_mark_price",
        lambda self, candidate, prefer_frozen=False: 100.0,
    )
    orch = ExecutorOrchestrator(cfg)
    executor = orch.create_market_order(
        candidate=_candidate(account_id="paper_main"),
    )
    try:
        executor.prepare()
        tracker = OrderTracker(cfg.paths)
        order_id = executor.run.order_ids[0]
        # Cancel wins the race before the fill resolves.
        tracker.request_cancel(order_id)
        tracker.close()

        terminal = orch.step_executor(executor)
        assert terminal is False, "cancel_requested is not terminal"
        tracker = OrderTracker(cfg.paths)
        row = tracker.get(order_id)
        assert row.state == "cancel_requested"
        assert row.filled_size == 0.0, "must not fill after cancel_requested"
        assert tracker.fills_for_order(order_id) == []
        assert PositionBook(cfg.paths).get_open_merged(
            account_id="paper_main", market="mock:BTC/USDT",
        ) is None

        # Once the cancel confirms, the executor finalizes as canceled.
        tracker.confirm_cancel(order_id)
        tracker.close()
        terminal = orch.step_executor(executor)
        assert terminal is True
        assert executor.run.state == "canceled"
    finally:
        orch.close()


# ---------------------------------------------------------------------------
# D6 — fills mirror into the strategy history ledger
# ---------------------------------------------------------------------------


def test_record_fill_mirrors_to_strategy_history(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(
        client_order_id="cid-d6",
        account_id="live_main",
        strategy_id="hist_s1",
        market="mock:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=1.0,
    )
    tracker.record_fill(
        order_id=order.order_id, price=100.0, size_base=0.5,
        fee_usd=0.5, source="live", cumulative_filled=0.5,
    )
    fills = read_ledger(cfg.paths, "hist_s1", "fills")
    assert len(fills) == 1
    assert fills[0]["fill"]["fill_id"] == f"{order.order_id}:0.5"
    assert fills[0]["fill"]["size_base"] == pytest.approx(0.5)
    # First fill is an entry — no realized pnl line yet.
    assert read_ledger(cfg.paths, "hist_s1", "pnl") == []

    tracker.record_fill(
        order_id=order.order_id, price=110.0, size_base=0.3,
        fee_usd=0.3, source="live", cumulative_filled=0.8,
    )
    fills = read_ledger(cfg.paths, "hist_s1", "fills")
    assert len(fills) == 2
    pnl_rows = read_ledger(cfg.paths, "hist_s1", "pnl")
    assert len(pnl_rows) == 1
    assert pnl_rows[0]["pnl"]["fill_id"] == fills[1]["fill"]["fill_id"]

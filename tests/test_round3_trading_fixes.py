"""Round-3 final-audit fixes (R3T1..R3T6) for the order path.

Regressions, one section per finding:

* R3T1 — a cancel raced by a *partial* fill must NOT release the
  reservation: the run is terminal but the venue keeps filling the
  remainder, and the background poller's completion consumes the SAME
  reservation (via the tracker row's ``reservation_id``).
* R3T2 — a non-definitive cancel error (cancel + re-fetch both failed,
  or the order is still live) stays non-terminal ``cancel_requested``
  so the poller keeps driving it; only definitive venue outcomes take
  terminal states.
* R3T3 — the explicit ``TradingError(not_found=True)`` flag (set by the
  ccxt adapter on ``ccxt.OrderNotFound``) counts toward the 4-strike
  ``lost`` machinery, and Bybit's "order not exists" retMsg matches the
  substring fallback.
* R3T4 — ``CcxtConnector.fetch_closed_orders`` exists so closed-order
  adoption works on real venues, and a failed adoption lookup does NOT
  age a ``place_unknown`` order toward ``lost``.
* R3T5 — an ``attach_protection`` plan attaches the rule to the open
  position instead of placing a real BUY market order.
* R3T6 — market orders fall back to the reference mark for the
  min-notional cost check instead of skipping it.
"""

from __future__ import annotations

import time
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from nerya.connectors.base import OrderAck
from nerya.core import yaml_io
from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.errors import IntentValidationError, TradingError
from nerya.core.paths import WorkspacePaths
from nerya.trading.capital import CapitalReservationStore
from nerya.trading.order_intents import (
    OrderCandidate,
    ProtectionRule,
    SizingPolicy,
    StopLossSpec,
    TakeProfitSpec,
    TradeEntry,
    TradePlan,
)
from nerya.trading.order_polling import (
    PLACE_UNKNOWN_MAX_AGE_S,
    is_definitive_not_found_error,
    poll_active_live_orders,
)
from nerya.trading.order_tracker import OrderTracker
from nerya.trading.position_book import PositionBook
from nerya.trading.submit import submit_trade_plan


pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _vault_passphrase(monkeypatch):
    monkeypatch.setenv("NERYA_VAULT_PASSPHRASE", "round3-fixes-passphrase")


def _config(tmp_path, *, accounts: list[dict[str, Any]]) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["runtime"]["live_trading_enabled"] = True
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(tmp_path / "nerya.yml", data)
    yaml_io.dump(cfg.paths.accounts_file, {"accounts": accounts})
    return cfg


def _account(account_id: str, mode: str = "live") -> dict[str, Any]:
    return {
        "id": account_id,
        "exchange": "fake",
        "venue": "fake",
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
        "market": "fake:BTC/USDT",
        "side": "buy",
        "order_type": "market",
        "size_base": 0.2,
        "notional_usd": 10_000.0,
        "meta": {"mark_price": 50_000.0},
    }
    payload.update(overrides)
    return OrderCandidate(**payload)


class _FakeRegistry:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def get(self, _account_id: str, _connector_cfg: dict) -> Any:
        return self._conn


def _ack(cid: str = "", **kw: Any) -> Any:
    base = dict(
        order_id="venue-1", client_order_id=cid, status="new",
        market="fake:BTC/USDT", side="buy", size=0.2, filled=0.0,
        avg_price=50_000.0, fee_usd=0.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _CancelRaceVenue:
    """cancel_order always fails (race); get_order serves a queue."""

    def __init__(self, get_order_queue: list[Any] | None = None) -> None:
        self.cancel_calls = 0
        self.place_calls: list[dict[str, Any]] = []
        self.get_order_queue = list(get_order_queue or [])

    def place_order(self, **kwargs: Any) -> Any:
        self.place_calls.append(dict(kwargs))
        return _ack(cid=str(kwargs.get("client_order_id") or ""))

    def cancel_order(self, *, market: str, order_id: str) -> Any:
        self.cancel_calls += 1
        raise TimeoutError("cancel request timed out")

    def get_order(self, *, market: str, order_id: str) -> Any:
        if self.get_order_queue:
            return self.get_order_queue.pop(0)
        raise ConnectionError("venue unreachable")

    def fetch_open_orders(self) -> list[Any]:
        return []


def _make_live_order(tmp_path, cfg: Config, reservation_id: str, cid: str):
    """Place a live order through the executor; returns (orch, executor)."""
    from nerya.trading.executors.orchestrator import ExecutorOrchestrator

    orch = ExecutorOrchestrator(cfg)
    executor = orch.create_market_order(
        candidate=_candidate(reservation_id=reservation_id),
    )
    assert orch.step_executor(executor) is False
    tracker = OrderTracker(cfg.paths)
    order = tracker.get_by_client_order_id(
        executor.run.config_json["candidate"]["client_order_id"],
    )
    tracker.close()
    assert order is not None and order.exchange_order_id == "venue-1"
    return orch, executor, order


# ---------------------------------------------------------------------------
# R3T1 — cancel raced by partial fill: reservation stays until the
# poller's completion consumes it
# ---------------------------------------------------------------------------


def test_r3t1_partial_fill_cancel_keeps_reservation_and_poller_consumes(
    tmp_path, monkeypatch,
):
    cfg = _config(tmp_path, accounts=[_account("live_main")])
    store = CapitalReservationStore(cfg.paths)
    reservation = store.reserve(
        account_id="live_main", strategy_id="s1", market="fake:BTC/USDT",
        side="buy", notional_usd=10_000.0,
    )
    stub = _CancelRaceVenue(
        # on_cancel re-fetch: venue reports filled status but only HALF
        # the order — the R2B1 partial branch. The poller then sees the
        # venue complete the remainder (the auditor's repro).
        get_order_queue=[
            _ack(status="filled", filled=0.1, avg_price=50_000.0, fee_usd=1.0),
            _ack(status="filled", filled=0.2, avg_price=50_000.0, fee_usd=2.0),
        ],
    )
    monkeypatch.setattr(
        "nerya.connectors.ConnectorRegistry", lambda **kw: _FakeRegistry(stub),
    )

    orch, executor, order = _make_live_order(
        tmp_path, cfg, reservation.reservation_id, "cli-r3t1",
    )
    try:
        run = orch.cancel(executor.run.executor_id, reason="operator_cancel")
        assert run is not None and run.state == "canceled"

        tracker = OrderTracker(cfg.paths)
        row = tracker.get(order.order_id)
        assert row.state == "partially_filled", (
            "cancel raced a partial fill: the remainder must stay pollable"
        )
        # R3T1: the reservation of a still-filling order must NOT be
        # released by the dead run.
        assert store.get(reservation.reservation_id).state == "reserved", (
            "a partial fill raced by cancel must keep the reservation active"
        )

        # The poller (last writer — the run is terminal) drives the
        # remainder to filled and consumes the SAME reservation.
        result = poll_active_live_orders(
            cfg, connector_factory=lambda _aid, _cfg: stub,
        )
        assert result.terminal == 1
        tracker = OrderTracker(cfg.paths)
        assert tracker.get(order.order_id).state == "filled"
        assert store.get(reservation.reservation_id).state == "consumed"

        # R3T6 plumbing: the executor passed the frozen mark from the
        # candidate meta as the adapter's reference price.
        assert stub.place_calls[0]["reference_price"] == pytest.approx(50_000.0)
    finally:
        orch.close()


# ---------------------------------------------------------------------------
# R3T2 — non-definitive cancel errors stay non-terminal
# ---------------------------------------------------------------------------


def test_r3t2_both_cancel_and_refetch_failed_stays_polled(tmp_path, monkeypatch):
    cfg = _config(tmp_path, accounts=[_account("live_main")])
    store = CapitalReservationStore(cfg.paths)
    reservation = store.reserve(
        account_id="live_main", strategy_id="s1", market="fake:BTC/USDT",
        side="buy", notional_usd=10_000.0,
    )
    stub = _CancelRaceVenue()  # get_order queue empty -> refetch raises too
    monkeypatch.setattr(
        "nerya.connectors.ConnectorRegistry", lambda **kw: _FakeRegistry(stub),
    )

    orch, executor, order = _make_live_order(
        tmp_path, cfg, reservation.reservation_id, "cli-r3t2",
    )
    try:
        run = orch.cancel(executor.run.executor_id)
        assert run is not None and run.state == "canceled"

        tracker = OrderTracker(cfg.paths)
        row = tracker.get(order.order_id)
        assert row.state == "cancel_requested", (
            "a transient cancel failure must stay non-terminal"
        )
        assert any(
            n.startswith("cancel_failed_still_polling")
            for n in row.meta.get("notes", [])
        )
        assert store.get(reservation.reservation_id).state == "reserved"

        # The venue later reports the order canceled: the poller drives
        # a REAL terminal state and frees the reservation.
        stub.get_order_queue.append(
            _ack(status="canceled", filled=0.0, avg_price=0.0, fee_usd=0.0),
        )
        result = poll_active_live_orders(
            cfg, connector_factory=lambda _aid, _cfg: stub,
        )
        assert result.terminal == 1
        tracker = OrderTracker(cfg.paths)
        assert tracker.get(order.order_id).state == "canceled"
        assert store.get(reservation.reservation_id).state == "released"
    finally:
        orch.close()


def test_r3t2_definitive_venue_outcomes_still_take_terminal_states(
    tmp_path, monkeypatch,
):
    cfg = _config(tmp_path, accounts=[_account("live_main")])
    store = CapitalReservationStore(cfg.paths)
    reservation = store.reserve(
        account_id="live_main", strategy_id="s1", market="fake:BTC/USDT",
        side="buy", notional_usd=10_000.0,
    )
    stub = _CancelRaceVenue(
        get_order_queue=[_ack(status="expired", filled=0.0, fee_usd=0.0)],
    )
    monkeypatch.setattr(
        "nerya.connectors.ConnectorRegistry", lambda **kw: _FakeRegistry(stub),
    )

    orch, executor, order = _make_live_order(
        tmp_path, cfg, reservation.reservation_id, "cli-r3t2b",
    )
    try:
        orch.cancel(executor.run.executor_id)
        tracker = OrderTracker(cfg.paths)
        row = tracker.get(order.order_id)
        assert row.state == "expired", "definitive venue outcome is recorded"
        assert store.get(reservation.reservation_id).state == "released"
    finally:
        orch.close()


# ---------------------------------------------------------------------------
# R3T3 — explicit not_found flag drives the lost machinery (Bybit)
# ---------------------------------------------------------------------------


def test_r3t3_not_found_flag_and_bybit_retmsg_count_strikes(tmp_path):
    assert is_definitive_not_found_error(
        TradingError("fetch failed", not_found=True),
    ) is True
    # Bybit retMsg — matched by the substring fallback even unflagged.
    assert is_definitive_not_found_error(
        TradingError(
            "BYBIT fetch_order failed: order not exists or too late to be cancelled",
        ),
    ) is True
    assert is_definitive_not_found_error(
        RuntimeError("ConnectionError: reset"),
    ) is False

    # Poller integration: a connector raising the flagged error counts
    # toward the 4-strike lost threshold.
    cfg = _config(tmp_path, accounts=[_account("live_main")])
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(
        client_order_id="cli-r3t3",
        account_id="live_main",
        strategy_id="s1",
        market="bybit:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=0.2,
        initial_state="submitted",
    )
    tracker.mark_submitted(order.order_id, exchange_order_id="venue-x")
    tracker.close()

    class _GhostVenue:
        def get_order(self, *, market: str, order_id: str) -> Any:
            raise TradingError(
                "BYBIT fetch_order failed", not_found=True,
            )

    for _ in range(4):
        result = poll_active_live_orders(
            cfg, connector_factory=lambda _aid, _cfg: _GhostVenue(),
        )
        assert result.not_found == 1  # one strike per pass (4 passes)

    tracker = OrderTracker(cfg.paths)
    row = tracker.get(order.order_id)
    assert row.not_found_streak == 4
    assert row.state == "lost"


def test_r3t3_ccxt_adapter_preserves_order_not_found():
    ccxt = pytest.importorskip("ccxt")
    from nerya.connectors.ccxt_adapter import CcxtConnector
    from nerya.connectors.cex_base import CEXCredentials

    class _GhostClient:
        def fetch_order(self, order_id, sym):
            raise ccxt.OrderNotFound(
                "bybit order not exists or too late to be cancelled",
            )

        def cancel_order(self, order_id, sym):
            raise ccxt.OrderNotFound("bybit order not exists")

    conn = CcxtConnector(
        exchange_id="bybit", live=True,
        credentials=CEXCredentials(api_key="k", api_secret="s"),
    )
    conn._client = _GhostClient()

    with pytest.raises(TradingError) as ei:
        conn.get_order(market="BTC/USDT", order_id="gone-1")
    assert ei.value.not_found is True
    assert ei.value.ambiguous is False

    with pytest.raises(TradingError) as ei:
        conn.cancel_order(market="BTC/USDT", order_id="gone-1")
    assert ei.value.not_found is True


# ---------------------------------------------------------------------------
# R3T4 — closed-order adoption works on the ccxt adapter, and fetch
# errors do not age place_unknown orders to lost
# ---------------------------------------------------------------------------


class _ClosedOrderClient:
    def __init__(self) -> None:
        self.closed: list[dict[str, Any]] = []

    def fetch_open_orders(self, symbol=None):
        return []

    def fetch_closed_orders(self, symbol=None, since=None, limit=None):
        return list(self.closed)

    def fetch_my_trades(self, *args, **kwargs):
        return []


def test_r3t4_ccxt_fetch_closed_orders_adopts_instant_fill(tmp_path):
    from nerya.connectors.ccxt_adapter import CcxtConnector
    from nerya.connectors.cex_base import CEXCredentials

    cfg = _config(tmp_path, accounts=[_account("live_main")])
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(
        client_order_id="cli-r3t4",
        account_id="live_main",
        strategy_id="s1",
        market="fake:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=0.2,
        initial_state="submitted",
        meta={"place_unknown": True},
    )
    tracker.close()

    client = _ClosedOrderClient()
    client.closed.append({
        "id": "venue-closed-9", "clientOrderId": "cli-r3t4",
        "status": "closed", "symbol": "BTC/USDT", "side": "buy",
        "amount": 0.2, "filled": 0.2, "average": 50_000.0,
    })
    conn = CcxtConnector(
        exchange_id="binance", live=True,
        credentials=CEXCredentials(api_key="k", api_secret="s"),
    )
    conn._client = client

    result = poll_active_live_orders(
        cfg, connector_factory=lambda _aid, _cfg: conn,
    )
    assert result.fills_applied == 1
    assert result.terminal == 1
    tracker = OrderTracker(cfg.paths)
    row = tracker.get(order.order_id)
    assert row.state == "filled"
    assert row.exchange_order_id == "venue-closed-9"


class _FlakyAdoptionVenue:
    """fetch_open_orders raises until ``healthy`` is set."""

    def __init__(self) -> None:
        self.healthy = False

    def fetch_open_orders(self) -> list[Any]:
        if not self.healthy:
            raise TradingError("BINANCE fetch_open_orders failed: timeout")
        return []

    def fetch_closed_orders(self) -> list[Any]:  # optional connector hook
        return []

    def fetch_my_trades(self, **kwargs: Any) -> list[Any]:
        return []


def test_r3t4_fetch_error_does_not_age_place_unknown_order(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("live_main")])
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(
        client_order_id="cli-r3t4b",
        account_id="live_main",
        strategy_id="s1",
        market="fake:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=0.2,
        initial_state="submitted",
        meta={"place_unknown": True},
    )
    tracker.close()

    venue = _FlakyAdoptionVenue()
    now = time.time()
    # While the lookup errors, the order must NEVER age to lost — even
    # well past PLACE_UNKNOWN_MAX_AGE_S (old behavior: silently lost,
    # then R2B9 refused the late fill forever).
    for _ in range(5):
        result = poll_active_live_orders(
            cfg,
            connector_factory=lambda _aid, _cfg: venue,
            now=now + PLACE_UNKNOWN_MAX_AGE_S + 60,
        )
        assert result.errors >= 1
        tracker = OrderTracker(cfg.paths)
        assert tracker.get(order.order_id).state == "submitted"

    # Once queries succeed again, two consecutive clean-absent cycles
    # re-arm the age bound: pending, then lost.
    venue.healthy = True
    first = poll_active_live_orders(
        cfg, connector_factory=lambda _aid, _cfg: venue,
        now=now + PLACE_UNKNOWN_MAX_AGE_S + 61,
    )
    assert first.terminal == 0
    second = poll_active_live_orders(
        cfg, connector_factory=lambda _aid, _cfg: venue,
        now=now + PLACE_UNKNOWN_MAX_AGE_S + 62,
    )
    assert second.terminal == 1
    tracker = OrderTracker(cfg.paths)
    assert tracker.get(order.order_id).state == "lost"


# ---------------------------------------------------------------------------
# R3T5 — attach_protection plans must not place a BUY market order
# ---------------------------------------------------------------------------


def _attach_plan(account_id: str, market: str = "fake:BTC/USDT") -> TradePlan:
    return TradePlan(
        action="attach_protection",
        strategy_id="s1",
        account_id=account_id,
        market=market,
        side="long",
        sizing=SizingPolicy(method="fixed_usd", fixed_usd=100.0),
        entry=TradeEntry(order_type="market"),
        protection=ProtectionRule(
            strategy_id="s1",
            account_id=account_id,
            market=market,
            side="long",
            stop_loss=StopLossSpec(type="pct", value=0.05),
            take_profit=TakeProfitSpec(type="pct", value=0.10),
        ),
        confidence=1.0,
        source="strategy_runtime",
    )


class _RecordingVenue:
    def __init__(self) -> None:
        self.place_calls: list[dict[str, Any]] = []

    def place_order(self, **kwargs: Any) -> Any:
        self.place_calls.append(dict(kwargs))
        return _ack(cid=str(kwargs.get("client_order_id") or ""))


def test_r3t5_attach_protection_plan_attaches_instead_of_buying(
    tmp_path, monkeypatch,
):
    cfg = _config(tmp_path, accounts=[_account("acct1")])
    book = PositionBook(cfg.paths)
    position = book.apply_fill(
        account_id="acct1", strategy_id="s1", market="fake:BTC/USDT",
        side="buy", price=100.0, size_base=1.0, venue="fake",
        source="paper",
    )
    venue = _RecordingVenue()
    monkeypatch.setattr(
        "nerya.connectors.ConnectorRegistry", lambda **kw: _FakeRegistry(venue),
    )

    out = submit_trade_plan(cfg, _attach_plan("acct1"))
    assert out["status"] == "protection_attached", out
    assert out["protection_id"]
    assert out["position_id"] == position.position_id
    # R3T5: no order may reach a connector.
    assert venue.place_calls == []

    from nerya.trading.executors.orchestrator import ExecutorOrchestrator
    from nerya.trading.protection_store import ProtectionStore

    rule = ProtectionStore(cfg.paths).get_for_position(position.position_id)
    assert rule is not None and rule.status == "armed"
    assert rule.stop_loss is not None and rule.take_profit is not None
    fresh = PositionBook(cfg.paths).get_by_id(position.position_id)
    assert fresh.protection_id == rule.protection_id
    # A restart-recoverable protection executor backs the rule; no
    # market_order executor was created.
    orch = ExecutorOrchestrator(cfg)
    try:
        kinds = {r.kind for r in orch.list_recent(limit=50)}
    finally:
        orch.close()
    assert "position_protection" in kinds
    assert "market_order" not in kinds


def test_r3t5_attach_protection_without_position_rejects(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("acct1")])
    with pytest.raises(IntentValidationError, match="open position"):
        submit_trade_plan(cfg, _attach_plan("acct1"))


# ---------------------------------------------------------------------------
# R3T6 — market orders use the reference mark for the min-notional check
# ---------------------------------------------------------------------------


def test_r3t6_market_order_min_notional_uses_reference_price():
    from nerya.connectors.ccxt_adapter import CcxtConnector
    from nerya.connectors.cex_base import CEXCredentials

    class _MinCostClient:
        def __init__(self) -> None:
            self.create_calls: list[dict[str, Any]] = []

        def load_markets(self):
            return {
                "BTC/USDT": {
                    "limits": {
                        "amount": {"min": 0.0001},
                        "cost": {"min": 10.0},
                    },
                },
            }

        def amount_to_precision(self, sym, amount):
            return str(amount)

        def price_to_precision(self, sym, price):
            return str(price)

        def create_order(self, sym, type_, side, amount, price, params):
            self.create_calls.append({"price": price, "amount": amount})
            return {
                "id": "1", "status": "open", "amount": amount,
                "filled": 0, "average": 0, "price": price,
            }

    client = _MinCostClient()
    conn = CcxtConnector(
        exchange_id="binance", live=True,
        credentials=CEXCredentials(api_key="k", api_secret="s"),
    )
    conn._client = client

    # Market order below min notional with a reference mark: refused
    # locally with a clear error instead of a cryptic venue rejection.
    with pytest.raises(TradingError, match="min_notional"):
        conn.place_order(
            market="BTC/USDT", side="buy", order_type="market",
            size=0.0001, price=None, reference_price=100.0,
        )
    assert client.create_calls == []

    # Without a reference mark the guard cannot estimate cost — the
    # documented R3T6 gap — and the order proceeds to the venue.
    conn.place_order(
        market="BTC/USDT", side="buy", order_type="market",
        size=0.0001, price=None,
    )
    assert len(client.create_calls) == 1
    # The reference price is only a local cost check — never sent.
    assert client.create_calls[0]["price"] is None

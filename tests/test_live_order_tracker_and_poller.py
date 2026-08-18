"""P0-5: live order tracking + background polling.

Covers the bug fixed by wiring :class:`ExecutionEngine._execute_live`
to :class:`OrderTracker` and adding :func:`poll_active_live_orders`:

* Every live ``place_order`` ack now lands in the tracker with
  ``state='submitted'`` (or ``filled`` if the ack reports an immediate
  fill).
* A failed ``place_order`` lands as ``state='rejected'`` so operators
  see it in /incidents.
* The background poller picks up non-terminal orders, calls
  ``get_order`` on the connector, applies any new fills to the
  :class:`PositionBook`, and transitions the tracker through terminal
  states.
* Restart safety: orders that were active when the previous process
  died are still in ``active_orders`` on next boot, so the poller
  picks them up — we test this by simulating a "second tick" against
  an order that was registered without the poller having seen it yet.
"""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

import pytest

from nerya.connectors.base import Connector, OrderAck, Ticker
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import TradingError
from nerya.core.paths import WorkspacePaths
from nerya.trading.accounts import get_account
from nerya.trading.execution import ExecutionEngine
from nerya.trading.intents import TradeIntent
from nerya.trading.orders import OrderRequest
from nerya.trading.order_polling import poll_active_live_orders
from nerya.trading.order_tracker import OrderTracker
from nerya.trading.position_book import PositionBook


pytestmark = pytest.mark.smoke


class _StubConnector(Connector):
    """In-memory connector that records ``place_order`` calls and lets
    a test step through ``get_order`` responses one tick at a time.
    """

    venue = "mock"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_ack: OrderAck | None = None
        self._get_order_queue: list[OrderAck] = []
        self._raise_on_place: Exception | None = None

    def queue_ack(self, ack: OrderAck) -> None:
        self._next_ack = ack

    def queue_get_order(self, ack: OrderAck) -> None:
        self._get_order_queue.append(ack)

    def raise_on_place(self, exc: Exception) -> None:
        self._raise_on_place = exc

    def get_ticker(self, market: str) -> Ticker:
        return Ticker(
            market=market, bid=49_999.0, ask=50_001.0, mid=50_000.0,
            last=50_000.0, spread_bps=4.0, ts_ms=int(time.time() * 1000),
            venue=self.venue,
        )

    def place_order(
        self, *, market, side, order_type, size, price=None,
        client_order_id=None, time_in_force="GTC",
    ):
        if self._raise_on_place is not None:
            exc = self._raise_on_place
            self._raise_on_place = None
            raise exc
        self.calls.append({
            "market": market, "side": side, "order_type": order_type,
            "size": size, "price": price, "client_order_id": client_order_id,
        })
        if self._next_ack is None:
            return OrderAck(
                order_id="venue-1", client_order_id=client_order_id or "",
                status="new", market=market, side=side,
                size=size, filled=0.0,
            )
        ack = self._next_ack
        self._next_ack = None
        return ack

    def get_order(self, *, market: str, order_id: str) -> OrderAck:
        if not self._get_order_queue:
            return OrderAck(
                order_id=order_id, client_order_id=order_id,
                status="new", market=market, side="buy",
                filled=0.0,
            )
        return self._get_order_queue.pop(0)


def _live_config(tmp_path) -> tuple[Config, _StubConnector]:
    """Workspace with a single live account + strategy bound to it.

    Returns (config, stub_connector) so tests can drive the broker
    responses directly.
    """
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["runtime"]["live_trading_enabled"] = True
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [{
                "id": "live_main",
                "exchange": "mock", "venue": "mock", "mode": "live",
                "status": "active", "initial_balance_usd": 100_000,
                "live_trading_enabled": True,
                "permissions": {
                    "read_balances": True, "place_order": True, "cancel_order": True,
                },
            }],
        },
    )
    yaml_io.dump(
        cfg.paths.strategy("s1") / "strategy.yml",
        {
            "id": "s1", "status": "live", "account_id": "live_main",
            "markets": ["mock:BTC/USDT"],
            "paper_trading_enabled": False, "live_trading_enabled": True,
        },
    )
    yaml_io.dump(
        cfg.paths.strategy("s1") / "limits.yml",
        {
            "allowed_markets": ["mock:BTC/USDT"],
            "min_confidence": 0, "max_stale_seconds": 60,
            "approval_threshold_usd": 0,
            "max_single_order_usd": 1_000_000,
        },
    )
    stub = _StubConnector()
    return cfg, stub


def _build_engine(cfg: Config, stub: _StubConnector) -> ExecutionEngine:
    """An ExecutionEngine whose registry's ``get`` always returns the stub."""
    engine = ExecutionEngine(config=cfg)

    class _StubRegistry:
        def get(self, _account_id, _connector_cfg):
            return stub
    engine.registry = _StubRegistry()
    return engine


def _intent(*, side="buy", size_usd=10_000.0) -> TradeIntent:
    return TradeIntent.new(
        strategy_id="s1", account_id="live_main",
        market="mock:BTC/USDT", side=side,
        size=size_usd, size_unit="usd",
        order_type="market", confidence=1.0,
        source="test",
    )


def _execute_unchecked_legacy_live_for_tracker_test(
    engine: ExecutionEngine,
    intent: TradeIntent,
    *,
    mark_price: float = 50_000.0,
):
    """Exercise the historical tracker adapter without exposing it publicly."""

    account = get_account(engine.config.paths, intent.account_id)
    request = OrderRequest(
        intent_id=intent.intent_id,
        strategy_id=intent.strategy_id,
        account_id=intent.account_id,
        market=intent.market,
        side=intent.side,
        size=intent.size,
        size_unit=intent.size_unit,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        stop_price=intent.stop_price,
        time_in_force=intent.time_in_force,
    )
    return engine._execute_live(account, intent, request, mark_price)


def test_public_legacy_execution_refuses_real_money_accounts(tmp_path):
    cfg, stub = _live_config(tmp_path)
    engine = _build_engine(cfg, stub)

    with pytest.raises(TradingError, match="cannot execute canary/live"):
        engine.execute(_intent(), market_snapshot={"price": 50_000})

    assert stub.calls == []


def test_legacy_execution_engine_is_not_exported_from_trading_package():
    import nerya.trading as trading

    assert not hasattr(trading, "ExecutionEngine")


def test_execute_live_registers_order_with_tracker_even_on_zero_fill_ack(tmp_path):
    """Live ``place_order`` ack with ``filled=0`` must still produce a
    tracker row in ``submitted`` state so the poller can pick it up.
    """
    cfg, stub = _live_config(tmp_path)
    engine = _build_engine(cfg, stub)
    stub.queue_ack(OrderAck(
        order_id="venue-zerofill", client_order_id="cli-1",
        status="new", market="mock:BTC/USDT", side="buy",
        size=0.2, filled=0.0,
    ))

    result = _execute_unchecked_legacy_live_for_tracker_test(engine, _intent())
    assert result.filled_size == 0.0
    assert result.order_id == "venue-zerofill"
    assert result.fills == []

    tracker = OrderTracker(cfg.paths)
    active = tracker.active_orders(account_id="live_main")
    assert len(active) == 1
    row = active[0]
    assert row.state == "submitted"
    assert row.exchange_order_id == "venue-zerofill"
    assert row.filled_size == 0.0
    # Tracker row carries enough context for reconciliation/dashboards.
    assert row.strategy_id == "s1"
    assert row.market == "mock:BTC/USDT"
    assert row.side == "buy"


def test_execute_live_records_immediate_fill_on_tracker_and_position_book(tmp_path):
    """When the venue returns ``filled>0`` in the place ack, the
    tracker rolls up the fill and the OrderResult carries it forward
    to ``_sync_position_book_after_execution``.
    """
    cfg, stub = _live_config(tmp_path)
    engine = _build_engine(cfg, stub)
    stub.queue_ack(OrderAck(
        order_id="venue-fast", client_order_id="cli-fast",
        status="filled", market="mock:BTC/USDT", side="buy",
        size=0.2, filled=0.2, avg_price=50_000.0, fee_usd=2.5,
    ))

    result = _execute_unchecked_legacy_live_for_tracker_test(engine, _intent())
    assert result.filled_size == pytest.approx(0.2)
    assert result.fee_usd == pytest.approx(2.5)
    assert len(result.fills) == 1
    fill = result.fills[0]
    assert fill.size == pytest.approx(0.2)
    assert fill.fee_usd == pytest.approx(2.5)

    tracker = OrderTracker(cfg.paths)
    rows = tracker.active_orders(account_id="live_main")
    assert rows == [], "filled order should be terminal, not active"
    # Cached (terminal-but-retained) order carries the fill rollup.
    cached = tracker.cached_orders(account_id="live_main")
    assert len(cached) == 1
    row = cached[0]
    assert row.state == "filled"
    assert row.filled_size == pytest.approx(0.2)
    assert row.avg_price == pytest.approx(50_000.0)


def test_execute_live_marks_rejected_when_place_order_raises(tmp_path):
    cfg, stub = _live_config(tmp_path)
    engine = _build_engine(cfg, stub)
    stub.raise_on_place(RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        _execute_unchecked_legacy_live_for_tracker_test(engine, _intent())

    tracker = OrderTracker(cfg.paths)
    # Rejected is terminal so it's in cached, not active.
    active = tracker.active_orders(account_id="live_main")
    assert active == []
    cached = tracker.cached_orders(account_id="live_main")
    assert len(cached) == 1
    assert cached[0].state == "rejected"


def test_poller_applies_late_fills_to_position_book(tmp_path):
    """A live order placed with ``filled=0`` gets a late fill on the
    *next* poll tick — the poller must apply it to the PositionBook
    so the merged position matches the broker.
    """
    cfg, stub = _live_config(tmp_path)
    engine = _build_engine(cfg, stub)
    stub.queue_ack(OrderAck(
        order_id="venue-late", client_order_id="cli-late",
        status="new", market="mock:BTC/USDT", side="buy",
        size=0.2, filled=0.0,
    ))
    _execute_unchecked_legacy_live_for_tracker_test(engine, _intent())

    # The order is open, PositionBook still empty.
    book = PositionBook(cfg.paths)
    assert book.open_positions(account_id="live_main") == []

    # Broker reports a partial fill on next poll.
    stub.queue_get_order(OrderAck(
        order_id="venue-late", client_order_id="cli-late",
        status="partially_filled", market="mock:BTC/USDT", side="buy",
        size=0.2, filled=0.12, avg_price=50_010.0, fee_usd=1.5,
    ))
    result = poll_active_live_orders(
        cfg,
        connector_factory=lambda _aid, _cfg: stub,
    )
    assert result.scanned == 1
    assert result.fills_applied == 1
    assert result.terminal == 0

    book = PositionBook(cfg.paths)
    open_rows = book.open_positions(account_id="live_main")
    assert len(open_rows) == 1
    pos = open_rows[0]
    assert pos.size_base == pytest.approx(0.12)
    assert pos.avg_entry_price == pytest.approx(50_010.0)

    # And the strategy share matches.
    share = book.get_share(
        strategy_id="s1",
        account_id="live_main",
        market="mock:BTC/USDT",
    )
    assert share is not None
    assert share.size_share_base == pytest.approx(0.12)

    # Now broker reports the rest of the fill and a terminal status.
    stub.queue_get_order(OrderAck(
        order_id="venue-late", client_order_id="cli-late",
        status="filled", market="mock:BTC/USDT", side="buy",
        size=0.2, filled=0.2, avg_price=50_010.0, fee_usd=2.5,
    ))
    result2 = poll_active_live_orders(
        cfg,
        connector_factory=lambda _aid, _cfg: stub,
    )
    assert result2.fills_applied == 1
    assert result2.terminal == 1

    book = PositionBook(cfg.paths)
    pos = book.open_positions(account_id="live_main")[0]
    assert pos.size_base == pytest.approx(0.2)

    tracker = OrderTracker(cfg.paths)
    assert tracker.active_orders(account_id="live_main") == []
    cached = tracker.cached_orders(account_id="live_main")
    assert len(cached) == 1
    assert cached[0].state == "filled"


def test_poller_handles_get_order_errors_via_not_found_streak(tmp_path):
    """Connector errors should not crash the loop — instead the
    poller increments ``not_found_streak`` so the order eventually
    flips to ``lost``.
    """
    cfg, stub = _live_config(tmp_path)
    engine = _build_engine(cfg, stub)
    stub.queue_ack(OrderAck(
        order_id="venue-flaky", client_order_id="cli-flaky",
        status="new", market="mock:BTC/USDT", side="buy",
        size=0.2, filled=0.0,
    ))
    _execute_unchecked_legacy_live_for_tracker_test(engine, _intent())

    # Sanity check: order was registered as active.
    tracker_before = OrderTracker(cfg.paths)
    assert len(tracker_before.active_orders(account_id="live_main")) == 1, \
        tracker_before.active_orders(account_id="live_main")

    class _ErrConn(_StubConnector):
        def get_order(self, *, market, order_id):
            raise RuntimeError("rate_limited")

    err_stub = _ErrConn()
    for _ in range(5):  # exceed LOST_ORDER_NOT_FOUND_THRESHOLD (4)
        out = poll_active_live_orders(
            cfg,
            connector_factory=lambda _aid, _cfg: err_stub,
        )
        if out.scanned == 0:
            # The order already flipped to ``lost`` — the loop is done.
            break
        assert out.errors == 1

    tracker = OrderTracker(cfg.paths)
    lost = tracker.lost_orders(account_id="live_main")
    assert len(lost) == 1
    assert lost[0].order_id  # any non-empty id


def test_poller_is_restart_safe(tmp_path):
    """Simulate a process restart: the order was registered by an
    earlier ``_execute_live`` call (we just keep the row in the DB),
    a fresh PositionBook is empty, and the first poll tick in the new
    process applies the broker's full fill onto the PositionBook.
    """
    cfg, stub = _live_config(tmp_path)
    engine = _build_engine(cfg, stub)
    stub.queue_ack(OrderAck(
        order_id="venue-restart", client_order_id="cli-restart",
        status="new", market="mock:BTC/USDT", side="buy",
        size=0.2, filled=0.0,
    ))
    _execute_unchecked_legacy_live_for_tracker_test(engine, _intent())

    # The "new process" starts here. PositionBook + OrderTracker are
    # rebuilt from disk; the active order survives.
    fresh_tracker = OrderTracker(cfg.paths)
    fresh_book = PositionBook(cfg.paths)
    assert len(fresh_tracker.active_orders(account_id="live_main")) == 1
    assert fresh_book.open_positions(account_id="live_main") == []

    stub.queue_get_order(OrderAck(
        order_id="venue-restart", client_order_id="cli-restart",
        status="filled", market="mock:BTC/USDT", side="buy",
        size=0.2, filled=0.2, avg_price=50_020.0, fee_usd=2.5,
    ))
    result = poll_active_live_orders(
        cfg,
        tracker=fresh_tracker,
        book=fresh_book,
        connector_factory=lambda _aid, _cfg: stub,
    )
    assert result.fills_applied == 1
    assert result.terminal == 1
    pos = fresh_book.open_positions(account_id="live_main")[0]
    assert pos.size_base == pytest.approx(0.2)

"""Round-2 audit fixes (R2B1..R2B10) for the order/trading core.

Regressions, one section per finding:

* R2B1 — a live ack-time *partial* fill must stay ``partially_filled``
  (pollable) instead of being forced terminal ``filled``.
* R2B2 — approval ping-pong: (a) approved reasons accumulate across the
  card chain, (b) parameterized threshold reasons match on key +
  threshold so NAV/budget drift between approve and resume does not
  re-page; a genuinely new reason still re-escalates.
* R2B3 — ``place_unknown`` orders adopt from closed orders / trades and
  age to ``lost`` after 10 minutes instead of wedging the poller.
* R2B4 — an explicit ``ambiguous=False`` on the exception takes the
  clean ``rejected`` path.
* R2B5 — a deleted account must not advance the not-found strike.
* R2B6 — reservation consume/release are guarded (no double-release).
* R2B7 — a generic executor crash releases the reservation; submit
  sweeps TTL-expired reservations.
* R2B8 — a fills-PK IntegrityError inside the poll tick is treated as a
  replay instead of aborting the whole pass.
* R2B9 — late fills refuse ``lost``/``rejected``/``failed``/``expired``
  orders.
* R2B10 — expired approvals show state ``expired``, not ``rejected``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from nerya.connectors.base import Balance, Connector, OrderAck, Ticker
from nerya.core import jsonl, yaml_io
from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.errors import TradingError
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import ApprovalRepository
from nerya.db.sqlite import connect
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
    expire_place_unknown_order,
    poll_active_live_orders,
)
from nerya.trading.order_tracker import OrderTracker
from nerya.trading.position_book import PositionBook
from nerya.trading.submit import (
    _reason_approval_signature,
    _resume_reasons_satisfied,
    submit_trade_plan,
)


pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _vault_passphrase(monkeypatch):
    monkeypatch.setenv("NERYA_VAULT_PASSPHRASE", "round2-fixes-passphrase")


def _config(tmp_path, *, accounts: list[dict[str, Any]]) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["runtime"]["live_trading_enabled"] = True
    # Keep the canary per-trade cap out of the way; the R2B2 tests
    # exercise the *approval threshold* escalation, not the cap.
    data.setdefault("trading", {})["canary"] = {
        "max_single_order_usd": 1_000_000,
    }
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(tmp_path / "nerya.yml", data)
    yaml_io.dump(cfg.paths.accounts_file, {"accounts": accounts})
    return cfg


def _account(
    account_id: str,
    mode: str,
    *,
    initial_balance_usd: int = 100_000,
) -> dict[str, Any]:
    return {
        "id": account_id,
        "exchange": "fake",
        "venue": "fake",
        "mode": mode,
        "status": "active",
        "initial_balance_usd": initial_balance_usd,
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
    }
    payload.update(overrides)
    return OrderCandidate(**payload)


class _FakeRegistry:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def get(self, _account_id: str, _connector_cfg: dict) -> Any:
        return self._conn


class _VenueStub:
    """Fake live connector for direct executor tests."""

    def __init__(
        self,
        *,
        place_ack: Any = None,
        place_error: Exception | None = None,
    ) -> None:
        self.place_calls = 0
        self.place_ack = place_ack
        self.place_error = place_error
        self.get_order_queue: list[Any] = []

    def place_order(self, **kwargs: Any) -> Any:
        self.place_calls += 1
        if self.place_error is not None:
            exc = self.place_error
            self.place_error = None
            raise exc
        if self.place_ack is not None:
            return self.place_ack
        return SimpleNamespace(
            order_id=f"venue-{self.place_calls}",
            client_order_id=str(kwargs.get("client_order_id") or ""),
            status="new",
            market=kwargs.get("market"),
            side=kwargs.get("side"),
            filled=0.0,
            avg_price=0.0,
            fee_usd=0.0,
        )

    def get_order(self, *, market: str, order_id: str) -> Any:
        if self.get_order_queue:
            return self.get_order_queue.pop(0)
        return SimpleNamespace(
            order_id=order_id, client_order_id="", status="new",
            market=market, side="buy", filled=0.0, avg_price=0.0, fee_usd=0.0,
        )

    def fetch_open_orders(self) -> list[Any]:
        return []


def _make_partial_ack(order_id: str, client_order_id: str, filled: float) -> Any:
    return SimpleNamespace(
        order_id=order_id,
        client_order_id=client_order_id,
        status="new",
        market="fake:BTC/USDT",
        side="buy",
        filled=filled,
        avg_price=50_000.0,
        fee_usd=1.0,
    )


def _register_active_order(
    cfg: Config,
    account_id: str,
    cid: str,
    *,
    place_unknown: bool = True,
    size_base: float = 0.2,
) -> Any:
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(
        client_order_id=cid,
        account_id=account_id,
        strategy_id="s1",
        market="fake:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=size_base,
        initial_state="submitted",
        meta={"place_unknown": place_unknown},
    )
    tracker.close()
    return order


# ---------------------------------------------------------------------------
# R2B1 — partial ack fill must stay pollable and converge to filled
# ---------------------------------------------------------------------------


def test_r2b1_partial_ack_fill_stays_pollable_then_completes(tmp_path, monkeypatch):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    store = CapitalReservationStore(cfg.paths)
    reservation = store.reserve(
        account_id="live_main", strategy_id="s1", market="fake:BTC/USDT",
        side="buy", notional_usd=10_000.0,
    )
    candidate = _candidate(reservation_id=reservation.reservation_id)
    stub = _VenueStub(place_ack=_make_partial_ack("venue-part", "cli-r2b1", 0.1))
    monkeypatch.setattr(
        "nerya.connectors.ConnectorRegistry", lambda **kw: _FakeRegistry(stub),
    )

    from nerya.trading.executors.market_order import MarketOrderExecutor
    from nerya.trading.executors.orchestrator import ExecutorOrchestrator

    orch = ExecutorOrchestrator(cfg)
    executor = orch.create_market_order(candidate=candidate)
    try:
        # Tick 1: place acks a HALF fill. The order must NOT go terminal.
        terminal = orch.step_executor(executor)
        assert terminal is False

        tracker = OrderTracker(cfg.paths)
        order = tracker.get_by_client_order_id(
            executor.run.config_json["candidate"]["client_order_id"],
        )
        assert order is not None
        assert order.state == "partially_filled", (
            "a partial ack fill must not force the order filled (R2B1)"
        )
        assert order.filled_size == pytest.approx(0.1)
        active = tracker.active_orders(account_id="live_main")
        assert [o.order_id for o in active] == [order.order_id], (
            "partial order must stay pollable"
        )
        # Reservation untouched while the remainder is still open.
        assert store.get(reservation.reservation_id).state == "reserved"

        # Tick 2: the venue reports the rest — the run finalizes done and
        # the reservation is consumed exactly once.
        stub.get_order_queue.append(SimpleNamespace(
            order_id="venue-part", client_order_id="cli-r2b1",
            status="filled", market="fake:BTC/USDT", side="buy",
            filled=0.2, avg_price=50_000.0, fee_usd=2.0,
        ))
        terminal = orch.step_executor(executor)
        assert terminal is True
        assert executor.run.state == "done"
        order = tracker.get(order.order_id)
        assert order.state == "filled"
        assert order.filled_size == pytest.approx(0.2)
        assert len(tracker.fills_for_order(order.order_id)) == 2
        assert store.get(reservation.reservation_id).state == "consumed"
        book = PositionBook(cfg.paths)
        pos = book.get_open_merged(account_id="live_main", market="fake:BTC/USDT")
        assert pos is not None and pos.size_base == pytest.approx(0.2)
    finally:
        orch.close()


# ---------------------------------------------------------------------------
# R2B2 — approval ping-pong + parameterized-string mismatch
# ---------------------------------------------------------------------------


def test_r2b2_b_threshold_reason_matches_ignoring_drifted_notional():
    # (ii) NAV/budget drift between card creation and resume must NOT
    # re-page: same key + same threshold, different observed notional.
    approved = {
        "risk_reasons": [
            "canary_per_trade_approval_required",
            "approval_required_threshold:5041.23>=5000.00",
        ],
    }
    assert _reason_approval_signature(
        "approval_required_threshold:5041.23>=5000.00",
    ) == "approval_required_threshold:>=5000.00"
    assert _resume_reasons_satisfied(
        ["approval_required_threshold:5012.10>=5000.00"], approved,
    )
    # A raised/lowered threshold is a different policy — re-escalate.
    assert not _resume_reasons_satisfied(
        ["approval_required_threshold:5012.10>=6000.00"], approved,
    )
    # (iii) A genuinely new reason never inherits the old approval.
    assert not _resume_reasons_satisfied(
        ["reconciliation_action_required:report-9"], approved,
    )
    assert not _resume_reasons_satisfied(
        ["canary_per_trade_approval_required"], {"risk_reasons": []},
    )
    assert not _resume_reasons_satisfied(
        ["canary_per_trade_approval_required"], None,
    )


def _canary_plan(
    account_id: str,
    market: str = "fake:SOL/USDT",
    sizing: SizingPolicy | None = None,
) -> TradePlan:
    protection = ProtectionRule(
        strategy_id="s1",
        account_id=account_id,
        market=market,
        side="long",
        stop_loss=StopLossSpec(type="pct", value=0.05),
        take_profit=TakeProfitSpec(type="pct", value=0.10),
    )
    return TradePlan(
        action="open_position",
        strategy_id="s1",
        account_id=account_id,
        market=market,
        side="long",
        sizing=sizing or SizingPolicy(method="fixed_usd", fixed_usd=10_000.0),
        entry=TradeEntry(order_type="market"),
        protection=protection,
        confidence=1.0,
        source="strategy_runtime",
    )


class _FillingVenue(Connector):
    """Connector whose balances + fills drive the resume BudgetChecker."""

    venue = "fake"
    kind = "cex"

    def __init__(self, free_usd: float) -> None:
        self.free_usd = free_usd
        self.place_calls: list[dict[str, Any]] = []

    def get_ticker(self, market: str) -> Ticker:
        return Ticker(
            market=market, bid=149.0, ask=151.0, mid=150.0, last=150.0,
            spread_bps=13.0, ts_ms=0, venue=self.venue,
        )

    def get_mark_price(self, market: str) -> float:
        return 150.0

    def get_balances(self) -> list[Balance]:
        return [
            Balance(asset="USDT", free=self.free_usd, locked=0.0, total=self.free_usd),
        ]

    def place_order(self, **kwargs) -> OrderAck:
        self.place_calls.append(dict(kwargs))
        size = float(kwargs.get("size") or 0.0)
        return OrderAck(
            order_id=f"fake-{len(self.place_calls)}",
            client_order_id=str(kwargs.get("client_order_id") or ""),
            status="filled", market=str(kwargs.get("market") or ""),
            side=str(kwargs.get("side") or ""), price=150.0, size=size,
            filled=size, avg_price=150.0, fee_usd=size * 150.0 * 0.0005,
        )

    def cancel_order(self, *, market: str, order_id: str) -> OrderAck:
        return OrderAck(order_id=order_id, client_order_id="", status="canceled",
                        market=market, side="")

    def get_order(self, *, market: str, order_id: str) -> OrderAck:
        return OrderAck(order_id=order_id, client_order_id="", status="filled",
                        market=market, side="buy", filled=1.0, avg_price=150.0)


def _canary_workspace(tmp_path, free_usd: float):
    cfg = _config(
        tmp_path,
        accounts=[
            _account("canary_acct", "live", initial_balance_usd=int(free_usd)),
        ],
    )
    yaml_io.dump(
        cfg.paths.strategy("s1") / "strategy.yml",
        {
            "id": "s1", "status": "canary", "account_id": "canary_acct",
            "markets": ["fake:SOL/USDT"],
            "paper_trading_enabled": False, "live_trading_enabled": True,
        },
    )
    yaml_io.dump(
        cfg.paths.strategy("s1") / "limits.yml",
        {
            "allowed_markets": ["fake:SOL/USDT"],
            "min_confidence": 0, "max_stale_seconds": 60,
            "approval_threshold_usd": 5_000,
            "max_single_order_usd": 100_000,
        },
    )
    venue = _FillingVenue(free_usd=free_usd)
    return cfg, venue


def _approve_card(cfg: Config, approval_id: str) -> None:
    """Approve a pending card the way the API callback does (DB + JSONL)."""
    from nerya.trading.approval import ApprovalGate

    ApprovalGate(cfg).approve(approval_id)
    src = cfg.paths.approvals_pending
    moved = None
    kept = []
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("approval_id") == approval_id and moved is None:
            rec["state"] = "approved"
            moved = rec
            continue
        kept.append(line)
    src.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    cfg.paths.approvals_approved.parent.mkdir(parents=True, exist_ok=True)
    with cfg.paths.approvals_approved.open("a", encoding="utf-8") as f:
        f.write(json.dumps(moved) + "\n")


def test_r2b2_a_canary_threshold_chain_converges_after_one_approval(
    tmp_path, monkeypatch,
):
    """(i) A canary order whose card carries {canary, threshold} completes
    after ONE operator approval even though the budget checker RESIZES
    the notional between escalation and resume (10000 estimated vs
    ~9995 resolved — the taker-fee buffer pushes required margin above
    free) — the threshold reason matches by key + threshold, so no
    second card is created."""
    from nerya.connectors.registry import ConnectorRegistry
    from nerya.trading.approval_resume import resume_approved

    cfg, venue = _canary_workspace(tmp_path, free_usd=10_000)
    monkeypatch.setattr(
        ConnectorRegistry, "get", lambda self, aid, acfg: venue,
    )

    snapshot = {
        "price": 150.0, "age_s": 0, "source": "test",
        "_envelope": {"mode": "live", "source": "test_feed", "venue": "fake"},
    }
    out = submit_trade_plan(cfg, _canary_plan("canary_acct"), market_snapshot=snapshot)
    assert out["status"] == "pending_approval", out
    approval_id = out["approval_id"]

    # Card 1 escalates with BOTH the canary reason and the threshold,
    # estimated at the full 10_000 notional.
    pending = jsonl.read_all(cfg.paths.approvals_pending)
    cards = [r for r in pending if r.get("approval_id")]
    assert len(cards) == 1
    reasons = cards[0]["risk_reasons"]
    assert "canary_per_trade_approval_required" in reasons
    assert any(
        r.startswith("approval_required_threshold:") and "10000.00" in r
        for r in reasons
    ), reasons

    _approve_card(cfg, approval_id)
    result = resume_approved(cfg, approval_id)
    assert result["ok"], result
    assert result["resume_response"]["status"] == "filled", result

    # The order really was resized down by the budget check (the drift
    # that used to re-page): ~9995 USD at mark 150, not 10_000.
    placed_size = venue.place_calls[0]["size"]
    assert placed_size == pytest.approx(9995.0 / 150.0, rel=0.02)
    assert placed_size != pytest.approx(10_000.0 / 150.0)

    # ONE card, ONE order — the post-budget re-escalation (with the
    # resized notional in the string) was auto-satisfied.
    pending_after = [
        r for r in jsonl.read_all(cfg.paths.approvals_pending) if r.get("approval_id")
    ]
    assert pending_after == [], "no second card may be created"
    assert len(venue.place_calls) == 1


def test_r2b2_c_two_card_chain_converges_without_ping_pong(tmp_path, monkeypatch):
    """(iii) The audit's actual ping-pong: pct_nav sizing has a ≈0
    pre-budget notional, so card 1 carries ONLY the canary reason. At
    resume the budget checker resolves 6000 USD → the post-budget
    threshold escalation opens card 2. Without reason accumulation,
    approving card 2 re-escalates the canary reason (card 3) and
    operators approve alternately-typed cards forever. With it, card 2
    seeds the prior approved reasons and one approval of card 2
    converges the chain."""
    from nerya.connectors.registry import ConnectorRegistry
    from nerya.trading.approval_resume import resume_approved

    cfg, venue = _canary_workspace(tmp_path, free_usd=10_000)
    monkeypatch.setattr(
        ConnectorRegistry, "get", lambda self, aid, acfg: venue,
    )

    snapshot = {
        "price": 150.0, "age_s": 0, "source": "test",
        "_envelope": {"mode": "live", "source": "test_feed", "venue": "fake"},
    }
    plan = _canary_plan(
        "canary_acct", sizing=SizingPolicy(method="pct_nav", pct_nav=0.6),
    )
    out = submit_trade_plan(cfg, plan, market_snapshot=snapshot)
    assert out["status"] == "pending_approval"
    card_1 = out["approval_id"]

    # Card 1: canary only (the pct_nav notional is a placeholder here).
    pending = {
        r["approval_id"]: r
        for r in jsonl.read_all(cfg.paths.approvals_pending)
        if r.get("approval_id")
    }
    assert "canary_per_trade_approval_required" in pending[card_1]["risk_reasons"]
    assert not any(
        r.startswith("approval_required_threshold:")
        for r in pending[card_1]["risk_reasons"]
    )

    _approve_card(cfg, card_1)
    first = resume_approved(cfg, card_1)
    assert first["ok"], first
    assert first["resume_response"]["status"] == "pending_approval", first
    card_2 = first["resume_response"]["approval_id"]
    assert card_2 and card_2 != card_1
    assert venue.place_calls == []

    # Card 2 carries its own threshold reason AND the seeded canary.
    pending = {
        r["approval_id"]: r
        for r in jsonl.read_all(cfg.paths.approvals_pending)
        if r.get("approval_id")
    }
    card_2_reasons = pending[card_2]["risk_reasons"]
    assert any(
        r.startswith("approval_required_threshold:") and "6000.00" in r
        for r in card_2_reasons
    ), card_2_reasons
    assert "canary_per_trade_approval_required" in card_2_reasons

    # ONE approval of card 2 → the whole chain converges (no card 3).
    _approve_card(cfg, card_2)
    final = resume_approved(cfg, card_2)
    assert final["ok"], final
    assert final["resume_response"]["status"] == "filled", final
    assert len(venue.place_calls) == 1

    pending_after = [
        r for r in jsonl.read_all(cfg.paths.approvals_pending) if r.get("approval_id")
    ]
    assert pending_after == [], "no card 3 may be created"


# ---------------------------------------------------------------------------
# R2B3 — place_unknown adoption via closed orders/trades + aging to lost
# ---------------------------------------------------------------------------


class _NeverReceivedVenue(_VenueStub):
    """Venue that never got the order: open orders is always empty."""

    def __init__(self) -> None:
        super().__init__()
        self.closed_orders: list[Any] = []
        self.trades: list[Any] = []

    def fetch_closed_orders(self) -> list[Any]:  # optional connector hook
        return list(self.closed_orders)

    def fetch_my_trades(self, **kwargs: Any) -> list[Any]:
        return list(self.trades)


def _ack(cid: str, **kw: Any) -> Any:
    base = dict(
        order_id="venue-closed-1", client_order_id=cid, status="filled",
        market="fake:BTC/USDT", side="buy", size=0.2, filled=0.2,
        avg_price=50_000.0, fee_usd=1.5,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_r2b3_instant_filled_market_order_adopts_via_closed_orders(tmp_path):
    """The dominant ambiguous case: a MARKET order that filled instantly
    never rests in open orders — adoption must find it in closed orders."""
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    order = _register_active_order(cfg, "live_main", "cli-closed")
    venue = _NeverReceivedVenue()
    venue.closed_orders.append(_ack("cli-closed"))

    result = poll_active_live_orders(
        cfg, connector_factory=lambda _aid, _cfg: venue,
    )
    assert result.fills_applied == 1
    assert result.terminal == 1

    tracker = OrderTracker(cfg.paths)
    row = tracker.get(order.order_id)
    assert row.state == "filled"
    assert row.exchange_order_id == "venue-closed-1"
    assert any(n.startswith("adopted_place_unknown") for n in row.meta.get("notes", []))
    book = PositionBook(cfg.paths)
    pos = book.get_open_merged(account_id="live_main", market="fake:BTC/USDT")
    assert pos is not None and pos.size_base == pytest.approx(0.2)


def test_r2b3_adoption_falls_back_to_trades_by_client_id(tmp_path):
    """When neither open nor closed order endpoints know the order, the
    fills (``fetch_my_trades``) still prove the venue received it — the
    poller adopts and completes the order from the folded trades."""
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    order = _register_active_order(cfg, "live_main", "cli-trades")
    venue = _NeverReceivedVenue()
    venue.trades.append(
        _ack("cli-trades", order_id="venue-trade-1", filled=0.12, fee_usd=0.9),
    )
    venue.trades.append(
        _ack("cli-trades", order_id="venue-trade-1", filled=0.08, fee_usd=0.6),
    )

    tracker = OrderTracker(cfg.paths)
    result = poll_active_live_orders(
        cfg, tracker=tracker, connector_factory=lambda _aid, _cfg: venue,
    )
    assert result.fills_applied == 1
    assert result.terminal == 1

    row = tracker.get(order.order_id)
    assert row.state == "filled"
    assert row.exchange_order_id == "venue-trade-1"
    assert any(
        n.startswith("adopted_place_unknown_via_trades")
        for n in row.meta.get("notes", [])
    )
    assert row.filled_size == pytest.approx(0.2)
    assert row.fee_usd == pytest.approx(1.5)


def test_r2b3_unreceived_place_unknown_order_ages_to_lost(tmp_path):
    """An order the venue never received is never found anywhere. After
    PLACE_UNKNOWN_MAX_AGE_S it goes terminal ``lost`` (releasing the
    reservation and failing the run) instead of polling forever."""
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    order = _register_active_order(cfg, "live_main", "cli-ghost")
    store = CapitalReservationStore(cfg.paths)
    reservation = store.reserve(
        account_id="live_main", strategy_id="s1", market="fake:BTC/USDT",
        side="buy", notional_usd=10_000.0,
    )

    venue = _NeverReceivedVenue()
    now = time.time()
    # Young flag: the poller keeps waiting.
    young = poll_active_live_orders(
        cfg, connector_factory=lambda _aid, _cfg: venue, now=now + 5,
    )
    assert young.terminal == 0
    tracker = OrderTracker(cfg.paths)
    assert tracker.get(order.order_id).state == "submitted"

    # Past the bound: expired to lost.
    aged = poll_active_live_orders(
        cfg,
        connector_factory=lambda _aid, _cfg: venue,
        now=now + PLACE_UNKNOWN_MAX_AGE_S + 30,
    )
    assert aged.terminal == 1
    tracker = OrderTracker(cfg.paths)
    row = tracker.get(order.order_id)
    assert row.state == "lost"
    assert any(
        n.startswith("place_unknown_expired") for n in row.meta.get("notes", [])
    )
    assert tracker.active_orders(account_id="live_main") == []

    # The executor's lost-finalization path releases the reservation and
    # labels the run for operator review.
    from nerya.trading.executors.base import ExecutorRun
    from nerya.trading.executors.market_order import MarketOrderExecutor

    run = ExecutorRun(
        executor_id="ex_r2b3",
        kind="market_order",
        account_id="live_main",
        strategy_id="s1",
        market="fake:BTC/USDT",
        state="submitted",
        order_ids=[order.order_id],
        reservation_ids=[reservation.reservation_id],
    )
    executor = MarketOrderExecutor(run, cfg.paths)
    assert executor.step() is True
    assert run.state == "failed"
    assert run.result_json.get("reason") == "place_unknown_expired"
    assert store.get(reservation.reservation_id).state == "released"


def test_r2b3_expire_helper_respects_young_flags(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    order = _register_active_order(cfg, "live_main", "cli-young")
    tracker = OrderTracker(cfg.paths)
    now = time.time()
    assert expire_place_unknown_order(
        tracker, tracker.get(order.order_id), now=now + 10,
    ) is False
    assert expire_place_unknown_order(
        tracker, tracker.get(order.order_id), now=now + PLACE_UNKNOWN_MAX_AGE_S + 1,
    ) is True
    assert tracker.get(order.order_id).state == "lost"


# ---------------------------------------------------------------------------
# R2B4 — explicit ambiguous flag is trusted exactly
# ---------------------------------------------------------------------------


def test_r2b4_explicit_ambiguous_flag_is_trusted_exactly():
    from nerya.trading.executors.market_order import _is_ambiguous_place_error

    # ccxt adapter raises TradingError with an explicit classification.
    assert _is_ambiguous_place_error(
        TradingError("fetchOpenOrders failed: timeout", ambiguous=True),
    ) is True
    # Adapter-local definitive failures (min notional, markets
    # unavailable, leverage set) carry ambiguous=False — clean reject.
    assert _is_ambiguous_place_error(
        TradingError("min notional violated", ambiguous=False),
    ) is False
    assert _is_ambiguous_place_error(
        TradingError("markets unavailable", ambiguous=False),
    ) is False
    # Plain exceptions (no flag) still fall back to the heuristics.
    assert _is_ambiguous_place_error(TimeoutError("read timed out")) is True
    assert _is_ambiguous_place_error(RuntimeError("mystery failure")) is True
    assert _is_ambiguous_place_error(
        RuntimeError("InvalidOrder: bad size"),
    ) is False


# ---------------------------------------------------------------------------
# R2B5 — deleted account must not strike the not-found counter
# ---------------------------------------------------------------------------


def test_r2b5_account_missing_skips_without_not_found_strike(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    # Register the order for an account that does not exist anymore.
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(
        client_order_id="cli-ghost-acct",
        account_id="deleted_account",
        strategy_id="s1",
        market="fake:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=0.2,
        initial_state="submitted",
    )
    tracker.close()

    venue = _NeverReceivedVenue()
    for _ in range(6):  # well past the lost threshold
        result = poll_active_live_orders(
            cfg, connector_factory=lambda _aid, _cfg: venue,
        )
        assert result.not_found == 0

    tracker = OrderTracker(cfg.paths)
    row = tracker.get(order.order_id)
    assert row.state == "submitted", "order must stay untouched for review"
    assert row.not_found_streak == 0
    assert "account_missing" in (row.meta.get("notes") or [])


# ---------------------------------------------------------------------------
# R2B6 — reservation consume/release are guarded transitions
# ---------------------------------------------------------------------------


def test_r2b6_double_release_and_consume_after_release_are_no_ops(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    store = CapitalReservationStore(cfg.paths)
    reservation = store.reserve(
        account_id="live_main", strategy_id="s1", market="fake:BTC/USDT",
        side="buy", notional_usd=5_000.0,
    )
    rid = reservation.reservation_id
    assert reservation.state == "reserved"

    # Fill path: consume works once.
    assert store.consume(rid) is True
    assert store.get(rid).state == "consumed"
    # Cancel-raced-with-fill / late release: no-op, capital not freed.
    assert store.release(rid) is False
    assert store.get(rid).state == "consumed"
    assert store.consume(rid) is False

    # Release path: works once, everything after is a no-op.
    rid2 = store.reserve(
        account_id="live_main", strategy_id="s1", market="fake:BTC/USDT",
        side="buy", notional_usd=5_000.0,
    ).reservation_id
    assert store.release(rid2) is True
    assert store.release(rid2) is False
    assert store.consume(rid2) is False
    assert store.get(rid2).state == "released"
    # Unknown ids are no-ops too.
    assert store.release("does-not-exist") is False
    assert store.consume("does-not-exist") is False


# ---------------------------------------------------------------------------
# R2B7 — generic executor crash releases the reservation
# ---------------------------------------------------------------------------


def test_r2b7_step_crash_releases_reservation_and_submit_sweeps_expiry(
    tmp_path, monkeypatch,
):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    store = CapitalReservationStore(cfg.paths)
    reservation = store.reserve(
        account_id="live_main", strategy_id="s1", market="fake:BTC/USDT",
        side="buy", notional_usd=1_000.0,
    )
    stub = _VenueStub()
    monkeypatch.setattr(
        "nerya.connectors.ConnectorRegistry", lambda **kw: _FakeRegistry(stub),
    )

    from nerya.trading.executors.market_order import MarketOrderExecutor
    from nerya.trading.executors.orchestrator import ExecutorOrchestrator

    orch = ExecutorOrchestrator(cfg)
    executor = orch.create_market_order(
        candidate=_candidate(reservation_id=reservation.reservation_id),
    )
    # Simulate a generic crash inside the executor step.
    monkeypatch.setattr(
        MarketOrderExecutor, "step",
        lambda self: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    try:
        terminal = orch.step_executor(executor)
        assert terminal is True
        assert executor.run.state == "failed"
        # The reservation did NOT leak: released in the crash path.
        assert store.get(reservation.reservation_id).state == "released"

        # expire_due sweep: a TTL-expired reservation frees without any
        # active_for_account read.
        expired = store.reserve(
            account_id="live_main", strategy_id="s1", market="fake:BTC/USDT",
            side="buy", notional_usd=1_000.0, ttl_seconds=-1,
        )
        store.expire_due()
        assert store.get(expired.reservation_id).state == "expired"
        assert store.total_blocked_usd("live_main") == pytest.approx(0.0)
    finally:
        orch.close()


# ---------------------------------------------------------------------------
# R2B8 — fills-PK IntegrityError is a replay, not a poll-tick abort
# ---------------------------------------------------------------------------


def test_r2b8_fill_integrity_error_does_not_abort_poll_tick(tmp_path, monkeypatch):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    tracker = OrderTracker(cfg.paths)
    crasher = tracker.register(
        client_order_id="cli-crash",
        account_id="live_main",
        strategy_id="s1",
        market="fake:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=0.2,
        initial_state="submitted",
    )
    healthy = tracker.register(
        client_order_id="cli-healthy",
        account_id="live_main",
        strategy_id="s1",
        market="fake:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=0.2,
        initial_state="submitted",
    )
    # Give both rows exchange ids so the poller takes the get_order path.
    tracker.mark_submitted(crasher.order_id, exchange_order_id="venue-a")
    tracker.mark_submitted(healthy.order_id, exchange_order_id="venue-b")
    tracker.close()

    original_record_fill = OrderTracker.record_fill

    def _crash_for_first(self, *, order_id, **kwargs):
        if order_id == crasher.order_id:
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: fills.fill_id",
            )
        return original_record_fill(self, order_id=order_id, **kwargs)

    monkeypatch.setattr(OrderTracker, "record_fill", _crash_for_first)

    class _TwoOrderVenue(Connector):
        venue = "fake"

        def get_ticker(self, market: str) -> Ticker:
            return Ticker(market=market, bid=1, ask=1, mid=1, last=1,
                          spread_bps=0, ts_ms=0, venue="fake")

        def get_order(self, *, market: str, order_id: str) -> OrderAck:
            filled = 0.2
            return OrderAck(
                order_id=order_id, client_order_id="", status="filled",
                market=market, side="buy", filled=filled, avg_price=100.0,
                fee_usd=0.5,
            )

    # Without the fix the IntegrityError propagated and the whole tick
    # died — the second order would never be polled.
    result = poll_active_live_orders(
        cfg,
        tracker=tracker,
        connector_factory=lambda _aid, _cfg: _TwoOrderVenue(),
    )
    assert result.scanned == 2
    assert result.errors == 0
    per = {p["order_id"]: p for p in result.per_order}
    # The crashing order's fill was skipped as a replay (no fill fields
    # recorded) but its terminal status still applied.
    assert "fill_size_base" not in per[crasher.order_id]
    # The healthy order was still processed in the same tick.
    assert per[healthy.order_id]["state"] == "filled"
    assert per[healthy.order_id]["fill_size_base"] == pytest.approx(0.2)
    fresh = OrderTracker(cfg.paths)
    assert fresh.get(healthy.order_id).state == "filled"
    assert fresh.get(crasher.order_id).state == "filled"


# ---------------------------------------------------------------------------
# R2B9 — late fills refuse finalized orders
# ---------------------------------------------------------------------------


def test_r2b9_late_fill_on_lost_order_is_refused(tmp_path):
    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(
        client_order_id="cli-late",
        account_id="live_main",
        strategy_id="s1",
        market="fake:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=1.0,
        initial_state="submitted",
    )
    for _ in range(tracker.lost_threshold):
        tracker.mark_not_found(order.order_id)
    assert tracker.get(order.order_id).state == "lost"

    fill = tracker.record_fill(
        order_id=order.order_id, price=100.0, size_base=1.0,
        fee_usd=1.0, source="live", cumulative_filled=1.0,
    )
    assert fill is None, "a finalized lost order must not book late fills"
    row = tracker.get(order.order_id)
    assert row.state == "lost"
    assert row.filled_size == pytest.approx(0.0)
    assert tracker.fills_for_order(order.order_id) == []

    # Non-terminal and cancel-completed orders still accept fills.
    live = tracker.register(
        client_order_id="cli-late-2",
        account_id="live_main",
        strategy_id="s1",
        market="fake:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=1.0,
        initial_state="submitted",
    )
    fill = tracker.record_fill(
        order_id=live.order_id, price=100.0, size_base=0.4,
        fee_usd=0.5, source="live", cumulative_filled=0.4,
    )
    assert fill is not None
    assert tracker.get(live.order_id).state == "partially_filled"


# ---------------------------------------------------------------------------
# R2B10 — expired approvals show "expired", not "rejected"
# ---------------------------------------------------------------------------


def test_r2b10_expired_approvals_get_their_own_terminal_state(tmp_path):
    from nerya.trading.approval import ApprovalGate
    from nerya.trading.approval_resume import _mark_expired

    cfg = _config(tmp_path, accounts=[_account("live_main", "live")])
    con = connect(cfg.paths.db)
    try:
        repo = ApprovalRepository(con)
        repo.insert(id="appr-exp-1", kind="trade_intent", expires_s=600, payload={})
        repo.insert(id="appr-exp-2", kind="trade_intent", expires_s=600, payload={})
        con.execute(
            "UPDATE approvals SET expires_at = ? WHERE id IN ('appr-exp-1', 'appr-exp-2')",
            (time.time() - 30,),
        )
        con.commit()
    finally:
        con.close()

    gate = ApprovalGate(cfg)
    # list_pending marks stale rows expired with the proper vocabulary.
    pending = gate.list_pending()
    assert pending == []
    con = connect(cfg.paths.db)
    try:
        states = {
            r["id"]: r["state"]
            for r in ApprovalRepository(con).list_pending()
        }
        row1 = ApprovalRepository(con).get("appr-exp-1")
        row2 = ApprovalRepository(con).get("appr-exp-2")
    finally:
        con.close()
    assert row1["state"] == "expired"
    assert row2["state"] == "expired"
    assert "appr-exp-1" not in states  # no longer pending

    # The resume path's _mark_expired uses the same write.
    _mark_expired(cfg, "appr-exp-2")
    con = connect(cfg.paths.db)
    try:
        row2 = ApprovalRepository(con).get("appr-exp-2")
    finally:
        con.close()
    assert row2["state"] == "expired"

    # Journal shows expiry (not rejection); nothing landed in the
    # rejected queue.
    trading_rows = jsonl.read_all(cfg.paths.journal("trading"))
    expired_rows = [
        r for r in trading_rows
        if r.get("kind") == "approval.expired"
        and r.get("approval_id") in ("appr-exp-1", "appr-exp-2")
    ]
    assert {r["approval_id"] for r in expired_rows} == {"appr-exp-1", "appr-exp-2"}
    rejected = jsonl.read_all(cfg.paths.approvals_rejected)
    assert not any(
        r.get("approval_id") in ("appr-exp-1", "appr-exp-2") for r in rejected
    )

"""Live trading-lifecycle regression tests.

Each test reproduces one of the four original breakages and asserts the
fix holds. A fake live connector stands in for the venue — it records
every ``place_order`` / ``cancel_order`` call and returns ccxt-shaped
acks so the executor runs through its real live path.

Reproduced scenarios (all four were broken before the P0 fixes):

1. **Canary approve → resume → order placed** (P0.2). Before: approval
   flipped a row but no order was ever placed; a manual retry tripped
   ``duplicate_intent``. Now: ``resume_approved`` replays the frozen
   plan and the executor places exactly one order.
2. **Immediate live fill → PositionBook + protection** (P0.3). Before:
   the executor's live path updated the tracker but never the
   PositionBook, so ``_maybe_attach_protection`` early-returned and no
   protection rule was created. Now: the fill is mirrored into the book
   atomically and a protection executor is persisted.
3. **Cancel calls the venue** (P0.5). Before: ``on_cancel`` only
   flipped local tracker state; ``connector.cancel_order`` was never
   called. Now: a live cancel hits the venue.
4. **``place_order:false`` blocks both paths** (P0.1). Before: the
   legacy ``submit_intent`` path bypassed the permission check. Now:
   both paths go through ``submit_trade_plan``'s blocker.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from nerya.connectors.base import Balance, Connector, OrderAck, Ticker
from nerya.connectors.registry import ConnectorRegistry
from nerya.core import jsonl, yaml_io
from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.trading.order_intents import SizingPolicy, TradeEntry, TradePlan
from nerya.trading.position_book import PositionBook
from nerya.trading.protection_store import ProtectionStore
from nerya.trading.submit import submit_trade_intent, submit_trade_plan


pytestmark = pytest.mark.smoke


class FakeLiveConnector(Connector):
    """A venue stand-in that records calls and returns filled acks."""

    venue = "FAKE"
    kind = "cex"

    def __init__(self) -> None:
        self.place_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self._next_id = 1

    def get_ticker(self, market: str) -> Ticker:
        return Ticker(
            market=market, bid=149.0, ask=151.0, mid=150.0, last=150.0,
            spread_bps=13.0, ts_ms=0, venue="FAKE",
        )

    def get_mark_price(self, market: str) -> float:
        return 150.0

    def get_balances(self) -> list[Balance]:
        return [Balance(asset="USDT", free=10_000.0, locked=0.0, total=10_000.0)]

    def place_order(self, **kwargs) -> OrderAck:
        self.place_calls.append(dict(kwargs))
        oid = f"fake-{self._next_id}"
        self._next_id += 1
        size = float(kwargs.get("size") or 0.0)
        return OrderAck(
            order_id=oid,
            client_order_id=str(kwargs.get("client_order_id") or ""),
            status="filled",
            market=str(kwargs.get("market") or ""),
            side=str(kwargs.get("side") or ""),
            price=150.0,
            size=size,
            filled=size,
            avg_price=150.0,
            fee_usd=size * 150.0 * 0.0005,
            attached_bracket_order_ids=(
                {"stop_loss": f"sl-{oid}", "take_profit": f"tp-{oid}"}
                if kwargs.get("stop_loss") or kwargs.get("take_profit")
                else {}
            ),
        )

    def cancel_order(self, *, market: str, order_id: str) -> OrderAck:
        self.cancel_calls.append({"market": market, "order_id": order_id})
        return OrderAck(
            order_id=order_id, client_order_id="", status="canceled",
            market=market, side="",
        )

    def get_order(self, *, market: str, order_id: str) -> OrderAck:
        return OrderAck(
            order_id=order_id, client_order_id="", status="filled",
            market=market, side="buy", filled=1.0, avg_price=150.0,
        )


def _live_config(tmp_path) -> tuple[Config, FakeLiveConnector]:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["runtime"]["live_trading_enabled"] = True
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "live_acct",
                    "exchange": "fake",
                    "venue": "fake",
                    "mode": "live",
                    "status": "active",
                    "live_trading_enabled": True,
                    "initial_balance_usd": 10_000,
                    "base_currency": "usdt",
                    "permissions": {
                        "read_balances": True,
                        "place_order": True,
                        "cancel_order": True,
                    },
                    "credentials": {"api_key_ref": "vault://k", "api_secret_ref": "vault://s"},
                }
            ]
        },
    )
    return cfg


def _paper_config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_main",
                    "exchange": "mock",
                    "venue": "mock",
                    "mode": "paper",
                    "status": "active",
                    "initial_balance_usd": 10_000,
                    "permissions": {
                        "read_balances": True,
                        "place_order": True,
                        "cancel_order": True,
                    },
                }
            ]
        },
    )
    return cfg


def _strategy(cfg: Config, sid: str, account_id: str, status: str = "paper", market: str = "mock:BTC/USDT"):
    yaml_io.dump(
        cfg.paths.strategy(sid) / "strategy.yml",
        {
            "id": sid,
            "status": status,
            "account_id": account_id,
            "markets": [market],
            "paper_trading_enabled": status == "paper",
            "live_trading_enabled": status == "live",
        },
    )
    yaml_io.dump(
        cfg.paths.strategy(sid) / "limits.yml",
        {
            "allowed_markets": [market],
            "min_confidence": 0,
            "max_stale_seconds": 60,
        },
    )


def _snapshot() -> dict[str, Any]:
    return {"price": 150.0, "age_s": 0, "source": "test"}


# ---------------------------------------------------------------------------
# Scenario 4: place_order:false blocks BOTH paths (P0.1)
# ---------------------------------------------------------------------------


def test_place_order_false_blocks_intent_path(tmp_path, monkeypatch):
    """P0.1: an account with ``place_order: false`` must be blocked on the
    legacy intent path too (previously only the plan path checked)."""
    cfg = _paper_config(tmp_path)
    # Flip the permission off.
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_main",
                    "exchange": "mock",
                    "venue": "mock",
                    "mode": "paper",
                    "status": "active",
                    "initial_balance_usd": 10_000,
                    "permissions": {
                        "read_balances": True,
                        "place_order": False,  # <-- blocked
                        "cancel_order": True,
                    },
                }
            ]
        },
    )
    # Paper mode doesn't trip the real-money blocker, so force a live
    # account to exercise the permission check.
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "live_acct",
                    "exchange": "fake",
                    "venue": "fake",
                    "mode": "live",
                    "status": "active",
                    "live_trading_enabled": True,
                    "initial_balance_usd": 10_000,
                    "base_currency": "usdt",
                    "permissions": {
                        "read_balances": True,
                        "place_order": False,  # <-- blocked
                        "cancel_order": True,
                    },
                    "credentials": {"api_key_ref": "vault://k", "api_secret_ref": "vault://s"},
                }
            ]
        },
    )
    cfg.data["runtime"]["live_trading_enabled"] = True
    _strategy(cfg, "s1", "live_acct", status="live", market="FAKE:BTC/USDT")

    fake = FakeLiveConnector()
    monkeypatch.setattr(ConnectorRegistry, "get", lambda self, aid, acfg: fake)

    out = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "s1",
            "account_id": "live_acct",
            "market": "FAKE:BTC/USDT",
            "side": "buy",
            "size": 100,
            "size_unit": "usd",
            "order_type": "market",
            "confidence": 1.0,
            "source": "agent",
        },
        market_snapshot=_snapshot(),
    )
    assert out["status"] == "rejected"
    assert out.get("execution_blocker") == "account_cannot_place_order"
    # No order was sent to the venue.
    assert fake.place_calls == []


# ---------------------------------------------------------------------------
# Scenario 2: immediate live fill → PositionBook + protection (P0.3)
# ---------------------------------------------------------------------------


def test_live_immediate_fill_updates_position_book_and_protection(tmp_path, monkeypatch):
    """P0.3: a live fill lands in PositionBook and a protection rule is
    attached (previously the book stayed empty and protection was skipped)."""
    from nerya.trading.order_intents import ProtectionRule, StopLossSpec, TakeProfitSpec

    cfg = _live_config(tmp_path)
    _strategy(cfg, "s1", "live_acct", status="live", market="FAKE:SOL/USDT")

    fake = FakeLiveConnector()
    monkeypatch.setattr(ConnectorRegistry, "get", lambda self, aid, acfg: fake)

    protection = ProtectionRule(
        strategy_id="s1",
        account_id="live_acct",
        market="FAKE:SOL/USDT",
        side="long",
        stop_loss=StopLossSpec(type="pct", value=0.05),
        take_profit=TakeProfitSpec(type="pct", value=0.10),
    )
    plan = TradePlan(
        action="open_position",
        strategy_id="s1",
        account_id="live_acct",
        market="FAKE:SOL/USDT",
        side="long",
        sizing=SizingPolicy(method="fixed_base", fixed_base=1.0),
        entry=TradeEntry(order_type="market"),
        protection=protection,
        confidence=1.0,
        source="agent",
    )
    out = submit_trade_plan(cfg, plan, market_snapshot=_snapshot())
    assert out["status"] == "filled", out

    # PositionBook now reflects the fill.
    book = PositionBook(cfg.paths)
    positions = book.open_positions(account_id="live_acct", strategy_id="s1")
    assert len(positions) == 1
    assert positions[0].market == "FAKE:SOL/USDT"
    assert positions[0].size_base > 0

    # A protection executor was persisted for the position.
    protos = ProtectionStore(cfg.paths).list_active()
    # The protection rule may be armed or exchange_armed; either way it exists.
    assert any(
        p.account_id == "live_acct" and p.market == "FAKE:SOL/USDT"
        for p in protos
    ), f"expected a protection rule for the position, got {[p.asdict() for p in protos]}"


# ---------------------------------------------------------------------------
# Scenario 3: cancel hits the venue (P0.5)
# ---------------------------------------------------------------------------


def test_live_cancel_calls_connector_cancel_order(tmp_path, monkeypatch):
    """P0.5: cancelling a live executor invokes the venue's cancel_order
    (previously only local tracker state was flipped)."""
    cfg = _live_config(tmp_path)
    _strategy(cfg, "s1", "live_acct", status="live", market="FAKE:SOL/USDT")

    fake = FakeLiveConnector()
    # Make place_order return an OPEN (not filled) order so we can cancel
    # it before it fills.
    fake.place_order = lambda **kw: OrderAck(  # type: ignore[assignment]
        order_id="fake-open-1",
        client_order_id=str(kw.get("client_order_id") or ""),
        status="new",
        market=str(kw.get("market") or ""),
        side=str(kw.get("side") or ""),
        price=150.0,
        size=float(kw.get("size") or 0.0),
        filled=0.0,
        avg_price=None,
    )
    # Keep the order open on poll so the executor doesn't finalise
    # before we cancel it.
    fake.get_order = lambda *, market, order_id: OrderAck(  # type: ignore[assignment]
        order_id=order_id, client_order_id="", status="open",
        market=market, side="buy", filled=0.0, avg_price=None,
    )
    monkeypatch.setattr(ConnectorRegistry, "get", lambda self, aid, acfg: fake)

    plan = TradePlan(
        action="open_position",
        strategy_id="s1",
        account_id="live_acct",
        market="FAKE:SOL/USDT",
        side="long",
        sizing=SizingPolicy(method="fixed_base", fixed_base=1.0),
        entry=TradeEntry(order_type="market"),
        confidence=1.0,
        source="agent",
    )
    out = submit_trade_plan(cfg, plan, market_snapshot=_snapshot())
    executor_id = out.get("executor_id")
    assert executor_id

    from nerya.trading.executors.orchestrator import ExecutorOrchestrator

    orch = ExecutorOrchestrator(cfg)
    run = orch.cancel(executor_id, reason="operator_cancel")
    assert run is not None

    # The venue received a real cancel_order call.
    assert len(fake.cancel_calls) >= 1, "expected connector.cancel_order to be called"
    assert fake.cancel_calls[0]["order_id"] == "fake-open-1"


# ---------------------------------------------------------------------------
# Scenario 1: canary approve → resume → order placed (P0.2)
# ---------------------------------------------------------------------------


def test_canary_approve_resume_places_order(tmp_path, monkeypatch):
    """P0.2: after an operator approves an escalated canary order, the
    original intent resumes and exactly one order is placed. Previously
    the approval only flipped a row and a retry hit ``duplicate_intent``."""
    from nerya.trading.approval import ApprovalGate
    from nerya.trading.approval_resume import resume_approved
    from nerya.trading.order_intents import ProtectionRule, StopLossSpec, TakeProfitSpec

    cfg = _live_config(tmp_path)
    _strategy(cfg, "s1", "live_acct", status="canary", market="FAKE:SOL/USDT")

    fake = FakeLiveConnector()
    monkeypatch.setattr(ConnectorRegistry, "get", lambda self, aid, acfg: fake)

    # Canary opens require a protection rule.
    protection = ProtectionRule(
        strategy_id="s1",
        account_id="live_acct",
        market="FAKE:SOL/USDT",
        side="long",
        stop_loss=StopLossSpec(type="pct", value=0.05),
        take_profit=TakeProfitSpec(type="pct", value=0.10),
    )
    plan = TradePlan(
        action="open_position",
        strategy_id="s1",
        account_id="live_acct",
        market="FAKE:SOL/USDT",
        side="long",
        sizing=SizingPolicy(method="fixed_base", fixed_base=1.0),
        entry=TradeEntry(order_type="market"),
        protection=protection,
        confidence=1.0,
        source="strategy_runtime",
    )
    # Canary forces per-trade approval; submit should return pending_approval.
    out = submit_trade_plan(cfg, plan, market_snapshot=_snapshot())
    assert out["status"] == "pending_approval", out
    approval_id = out["approval_id"]
    assert approval_id
    # No order placed yet.
    assert fake.place_calls == []

    # The frozen plan + snapshot are persisted on the approval record.
    pending = jsonl.read_all(cfg.paths.approvals_pending)
    assert any(r.get("approval_id") == approval_id and r.get("frozen_plan") for r in pending)

    # Simulate operator approval via the ApprovalGate (DB state flip).
    ApprovalGate(cfg).approve(approval_id)
    # Move the JSONL row to approved (mimic routes_approvals._move_record).
    import json as _json
    src = cfg.paths.approvals_pending
    moved = None
    kept = []
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = _json.loads(line)
        if rec.get("approval_id") == approval_id and moved is None:
            rec["state"] = "approved"
            moved = rec
            continue
        kept.append(line)
    src.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    cfg.paths.approvals_approved.parent.mkdir(parents=True, exist_ok=True)
    with cfg.paths.approvals_approved.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(moved) + "\n")

    # Resume the approved plan.
    result = resume_approved(cfg, approval_id)
    assert result["ok"], result
    resume_resp = result["resume_response"]
    assert resume_resp["status"] == "filled", resume_resp

    # Exactly one order placed (no duplicate_intent rejection).
    assert len(fake.place_calls) == 1, f"expected 1 order, got {len(fake.place_calls)}"

    # A duplicate callback/manual retry is consumed by the durable SQLite
    # claim and must never reach the exchange again.
    repeated = resume_approved(cfg, approval_id)
    assert repeated["ok"] is True
    assert repeated["already_resumed"] is True
    assert repeated["resume_in_progress"] is False
    assert len(fake.place_calls) == 1

    from nerya.db.repositories import ApprovalRepository
    from nerya.db.sqlite import connect

    con = connect(cfg.paths.db)
    try:
        approval_row = ApprovalRepository(con).get(approval_id)
    finally:
        con.close()
    assert approval_row is not None
    assert approval_row["state"] == "resumed"

from __future__ import annotations

import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_control_plane, routes_portfolio
from nerya.core import jsonl
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.sqlite import connect
from nerya.trading.account_refresh import (
    account_refresh_interval_seconds,
    refresh_account_marks,
)
from nerya.trading.account_snapshots import capture_snapshot, latest_snapshot
from nerya.trading.portfolio import get_pnl, get_portfolio_summary
from nerya.trading.position_book import PositionBook
from nerya.trading.reconciliation import ReconciliationReport, ReconciliationStore
from nerya.trading.submit import submit_trade_intent
from nerya.trading.virtual_ledger import open_ledger


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
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
    yaml_io.dump(
        cfg.paths.strategy("s1") / "strategy.yml",
        {
            "id": "s1",
            "status": "paper",
            "account_id": "paper_main",
            "markets": ["mock:BTC/USDT"],
            "paper_trading_enabled": True,
            "live_trading_enabled": False,
        },
    )
    yaml_io.dump(
        cfg.paths.strategy("s1") / "limits.yml",
        {
            "allowed_markets": ["mock:BTC/USDT"],
            "min_confidence": 0,
            "max_stale_seconds": 60,
            "approval_threshold_usd": 1,
        },
    )
    return cfg


def _add_strategy(cfg: Config, strategy_id: str, *, market: str = "mock:BTC/USDT") -> None:
    yaml_io.dump(
        cfg.paths.strategy(strategy_id) / "strategy.yml",
        {
            "id": strategy_id,
            "status": "paper",
            "account_id": "paper_main",
            "markets": [market],
            "paper_trading_enabled": True,
            "live_trading_enabled": False,
        },
    )


def test_account_refresh_updates_position_marks_snapshot_and_portfolio(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    ledger = open_ledger(cfg.paths, "paper_main", 10_000)
    ledger.apply_fill(
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size=0.1,
        fee_usd=0,
    )
    book = PositionBook(cfg.paths)
    opened = book.apply_fill(
        account_id="paper_main",
        strategy_id="s1",
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size_base=0.1,
    )

    def fake_ticker(market, *, allow_mock=None, config_like=None):
        return {
            "price": 55_000,
            "age_s": 0,
            "source": "test_feed",
            "_envelope": {"mode": "live", "source": "test_feed"},
        }

    monkeypatch.setattr("nerya.data.candles.fetch_public_ticker", fake_ticker)

    result = refresh_account_marks(cfg, account_id="paper_main", run_executors=False)

    assert result["ok"] is True
    refreshed = book.get_by_id(opened.position_id)
    assert refreshed is not None
    assert refreshed.mark_price == pytest.approx(55_000)
    assert refreshed.unrealized_pnl_usd == pytest.approx(500)
    snap = latest_snapshot(cfg.paths, "paper_main")
    assert snap is not None
    assert snap.nav_usd == pytest.approx(10_500)
    assert snap.unrealized_pnl_usd == pytest.approx(500)

    summary = get_portfolio_summary(cfg.paths)
    account = summary["accounts"][0]
    pos = account["positions"]["mock:BTC/USDT"]
    assert account["equity_usd"] == pytest.approx(10_500)
    assert pos["mark_price"] == pytest.approx(55_000)
    assert pos["market_value_usd"] == pytest.approx(5_500)


def test_strategy_cards_use_position_book_pnl(tmp_path):
    cfg = _config(tmp_path)
    ledger = open_ledger(cfg.paths, "paper_main", 10_000)
    ledger.apply_fill(
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size=0.1,
        fee_usd=0,
    )
    ledger.apply_fill(
        market="mock:BTC/USDT",
        side="sell",
        price=55_000,
        size=0.04,
        fee_usd=0.1,
    )
    book = PositionBook(cfg.paths)
    opened = book.apply_fill(
        account_id="paper_main",
        strategy_id="s1",
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size_base=0.1,
    )
    reduced = book.apply_fill(
        account_id="paper_main",
        strategy_id="s1",
        market="mock:BTC/USDT",
        side="sell",
        price=55_000,
        size_base=0.04,
        fee_usd=0.1,
    )
    assert reduced.position_id == opened.position_id

    handler = next(
        h for method, path, h in routes_portfolio.routes()
        if method == "POST" and path == "/strategy/list"
    )
    res = handler(SimpleNamespace(config=cfg), {})
    card = next(s for s in res["strategies"] if s["id"] == "s1")

    assert card["realized_pnl_usd"] == pytest.approx(200)
    assert card["unrealized_pnl_usd"] == pytest.approx(300)
    assert card["total_pnl_usd"] == pytest.approx(500)
    assert card["fees_usd"] == pytest.approx(0.1)
    assert card["open_positions_count"] == 1


def test_portfolio_surfaces_position_book_rows_when_ledger_nets_market(tmp_path):
    cfg = _config(tmp_path)
    _add_strategy(cfg, "s2")
    ledger = open_ledger(cfg.paths, "paper_main", 10_000)
    ledger.apply_fill(
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size=0.2,
        fee_usd=0,
    )
    ledger.apply_fill(
        market="mock:BTC/USDT",
        side="sell",
        price=51_000,
        size=0.19,
        fee_usd=0,
    )

    book = PositionBook(cfg.paths)
    book.apply_fill(
        account_id="paper_main",
        strategy_id="s1",
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size_base=0.2,
    )
    book.apply_fill(
        account_id="paper_main",
        strategy_id="s2",
        market="mock:BTC/USDT",
        side="sell",
        price=51_000,
        size_base=0.19,
    )

    summary = get_portfolio_summary(cfg.paths)
    position_rows = list(summary["accounts"][0]["positions"].values())
    btc_rows = [p for p in position_rows if p["market"] == "mock:BTC/USDT"]
    # v6: same (account, market) collapses to one merged row even when
    # multiple strategies contributed. Per-strategy slices live on
    # ``position_shares``; here we just sanity-check the merged size.
    assert len(btc_rows) == 1
    merged_row = btc_rows[0]
    assert merged_row["strategy_id"] in ("__merged__", "s1", "s2")
    assert merged_row["size"] == pytest.approx(0.01)

    handler = next(
        h for method, path, h in routes_control_plane.routes()
        if method == "POST" and path == "/portfolio/health"
    )
    health = handler(SimpleNamespace(config=cfg), {})
    account = health["accounts"][0]
    assert account["open_position_count"] == 1
    assert {p["market"] for p in account["open_positions"]} == {"mock:BTC/USDT"}

    # The shares table should still know about both strategies.
    shares = PositionBook(cfg.paths).list_shares(merged_row["position_id"])
    assert {s.strategy_id for s in shares} == {"s1", "s2"}

    snap = capture_snapshot(
        cfg,
        "paper_main",
        persist=False,
        marks={"mock:BTC/USDT": 52_000},
    )
    assert snap.nav_usd == pytest.approx(10_210)
    assert snap.open_order_notional_usd == pytest.approx(20_280)
    assert snap.unrealized_pnl_usd == pytest.approx(210)


def test_portfolio_pnl_reconciles_equity_realized_unrealized_and_fees(tmp_path):
    cfg = _config(tmp_path)
    _add_strategy(cfg, "s2")
    ledger = open_ledger(cfg.paths, "paper_main", 10_000)
    ledger.apply_fill(
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size=0.2,
        fee_usd=5,
    )
    ledger.apply_fill(
        market="mock:BTC/USDT",
        side="sell",
        price=51_000,
        size=0.19,
        fee_usd=4.845,
    )

    book = PositionBook(cfg.paths)
    book.apply_fill(
        account_id="paper_main",
        strategy_id="s1",
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size_base=0.2,
        fee_usd=5,
    )
    book.apply_fill(
        account_id="paper_main",
        strategy_id="s2",
        market="mock:BTC/USDT",
        side="sell",
        price=51_000,
        size_base=0.19,
        fee_usd=4.845,
    )
    capture_snapshot(
        cfg,
        "paper_main",
        persist=True,
        marks={"mock:BTC/USDT": 52_000},
    )

    pnl = get_pnl(cfg.paths)

    assert pnl["initial_equity_usd"] == pytest.approx(10_000)
    assert pnl["equity_usd"] == pytest.approx(10_200.155)
    assert pnl["unrealized_usd"] == pytest.approx(210)
    assert pnl["fees_usd"] == pytest.approx(9.845)
    assert pnl["realized_gross_usd"] == pytest.approx(0)
    assert pnl["realized_usd"] == pytest.approx(-9.845)
    assert pnl["total_pnl_usd"] == pytest.approx(200.155)
    assert (
        pnl["initial_equity_usd"]
        + pnl["realized_usd"]
        + pnl["unrealized_usd"]
    ) == pytest.approx(pnl["equity_usd"])


def test_position_book_read_models_recompute_unrealized_from_current_mark(tmp_path):
    cfg = _config(tmp_path)
    ledger = open_ledger(cfg.paths, "paper_main", 10_000)
    ledger.apply_fill(
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size=0.2,
        fee_usd=0,
    )
    opened = PositionBook(cfg.paths).apply_fill(
        account_id="paper_main",
        strategy_id="s1",
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size_base=0.2,
    )
    con = connect(cfg.paths.db)
    con.execute(
        """
        UPDATE positions
           SET mark_price = ?, unrealized_pnl_usd = ?
         WHERE position_id = ?
        """,
        (52_000, 999, opened.position_id),
    )

    summary = get_portfolio_summary(cfg.paths)
    position = summary["accounts"][0]["positions"]["mock:BTC/USDT"]
    assert position["mark_price"] == pytest.approx(52_000)
    assert position["unrealized_pnl_usd"] == pytest.approx(400)

    handler = next(
        h for method, path, h in routes_portfolio.routes()
        if method == "POST" and path == "/strategy/list"
    )
    result = handler(SimpleNamespace(config=cfg), {})
    card = next(s for s in result["strategies"] if s["id"] == "s1")
    assert card["unrealized_pnl_usd"] == pytest.approx(400)
    assert card["total_pnl_usd"] == pytest.approx(400)


def test_account_snapshot_asdict_includes_dashboard_balance_aliases(tmp_path):
    cfg = _config(tmp_path)
    snap = capture_snapshot(cfg, "paper_main", persist=False)

    data = snap.asdict()

    assert data["nav_usd"] == pytest.approx(10_000)
    assert data["total_usd"] == pytest.approx(10_000)
    assert data["equity_usd"] == pytest.approx(10_000)
    assert data["free_usd"] == pytest.approx(10_000)
    assert data["available_usd"] == pytest.approx(10_000)
    assert data["positions_value_usd"] == pytest.approx(0)


def test_control_orders_list_includes_strategy_history_orders_when_tracker_empty(tmp_path):
    cfg = _config(tmp_path)
    jsonl.append(
        cfg.paths.strategy_history("s1") / "intents.jsonl",
        {
            "session_id": "ses_1",
            "intent": {
                "intent_id": "int_1",
                "strategy_id": "s1",
                "account_id": "paper_main",
                "market": "mock:BTC/USDT",
                "side": "buy",
                "size": 100,
                "size_unit": "usd",
                "order_type": "market",
            },
        },
    )
    jsonl.append(
        cfg.paths.strategy_history("s1") / "orders.jsonl",
        {
            "session_id": "ses_1",
            "payload": {
                "order_id": "ord_1",
                "intent_id": "int_1",
                "status": "filled",
                "avg_price": 50_000,
                "filled_size": 0.002,
                "notional_usd": 100,
            },
        },
    )

    handler = next(
        h for method, path, h in routes_control_plane.routes()
        if method == "POST" and path == "/orders/list"
    )
    result = handler(SimpleNamespace(config=cfg), {"state": "recent", "limit": 20})

    assert len(result["orders"]) == 1
    row = result["orders"][0]
    assert row["order_id"] == "ord_1"
    assert row["account_id"] == "paper_main"
    assert row["strategy_id"] == "s1"
    assert row["market"] == "mock:BTC/USDT"
    assert row["side"] == "buy"
    assert row["state"] == "filled"


def test_recent_trades_backfills_side_from_matching_intent(tmp_path):
    cfg = _config(tmp_path)
    jsonl.append(
        cfg.paths.strategy_history("s1") / "intents.jsonl",
        {
            "session_id": "ses_1",
            "intent": {
                "intent_id": "int_1",
                "strategy_id": "s1",
                "account_id": "paper_main",
                "market": "mock:BTC/USDT",
                "side": "sell",
                "order_type": "market",
            },
        },
    )
    jsonl.append(
        cfg.paths.strategy_history("s1") / "fills.jsonl",
        {
            "session_id": "ses_1",
            "fill": {
                "order_id": "ord_1",
                "fill_id": "fil_1",
                "intent_id": "int_1",
                "market": "mock:BTC/USDT",
                "price": 50_000,
                "size": 0.002,
                "fee_usd": 0.01,
            },
        },
    )

    handler = next(
        h for method, path, h in routes_portfolio.routes()
        if method == "POST" and path == "/trading/recent_trades"
    )
    result = handler(SimpleNamespace(config=cfg), {"limit": 5})

    assert result["trades"][0]["side"] == "sell"


def test_reconciliation_reports_route_serializes_reports(tmp_path):
    cfg = _config(tmp_path)
    ReconciliationStore(cfg.paths).record(
        ReconciliationReport(
            report_id="rep_1",
            ts=time.time(),
            scope="account",
            severity="action_required",
            account_id="paper_main",
            summary={"issue_count": 1},
            issues=[{"kind": "balance_drift"}],
        )
    )

    handler = next(
        h for method, path, h in routes_control_plane.routes()
        if method == "POST" and path == "/reconciliation/reports"
    )
    result = handler(SimpleNamespace(config=cfg), {"limit": 10})

    assert result["reports"][0]["report_id"] == "rep_1"
    assert result["reports"][0]["summary"]["issue_count"] == 1
    assert result["worst_recent"]["report_id"] == "rep_1"


def test_reconciliation_run_route_serializes_report(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    report = ReconciliationReport(
        report_id="rep_run",
        ts=124.0,
        scope="global",
        severity="info",
        summary={"issue_count": 0},
    )

    monkeypatch.setattr(
        "nerya.trading.reconciliation.reconcile",
        lambda config, account_id=None, persist=True: report,
    )
    handler = next(
        h for method, path, h in routes_control_plane.routes()
        if method == "POST" and path == "/reconciliation/run"
    )
    result = handler(SimpleNamespace(config=cfg), {})

    assert result["report"]["report_id"] == "rep_run"
    assert result["report"]["severity"] == "info"


def test_legacy_submit_trade_intent_syncs_position_book_and_snapshot(tmp_path):
    cfg = _config(tmp_path)

    out = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "s1",
            "account_id": "paper_main",
            "market": "mock:BTC/USDT",
            "side": "buy",
            "size": 100,
            "size_unit": "usd",
            "order_type": "market",
            "confidence": 1.0,
            "source": "strategy_runtime",
        },
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )

    assert out["status"] == "filled"
    book = PositionBook(cfg.paths)
    positions = book.open_positions(account_id="paper_main")
    assert len(positions) == 1
    assert positions[0].market == "mock:BTC/USDT"
    # v6: positions row is merged; strategy attribution lives on the share.
    assert positions[0].is_merged
    assert positions[0].size_base > 0
    s1_share = book.get_share(
        strategy_id="s1", account_id="paper_main", market="mock:BTC/USDT",
    )
    assert s1_share is not None and s1_share.strategy_id == "s1"
    assert s1_share.size_share_base == pytest.approx(positions[0].size_base)
    assert latest_snapshot(cfg.paths, "paper_main") is not None


def test_account_refresh_defaults_to_five_minutes(tmp_path):
    cfg = _config(tmp_path)

    assert account_refresh_interval_seconds(cfg) == pytest.approx(300)

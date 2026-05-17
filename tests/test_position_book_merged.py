"""v6 merged-position semantics for :mod:`nerya.trading.position_book`.

Covers the behaviour change introduced by migration v6 / the
``position_shares`` rollout:

* same (account, market) collapses to one merged row across strategies
* opposite-direction strategy shares net at the merged level
* per-strategy share carries its OWN avg-entry and realized PnL —
  *not* the merged blended avg
* closing one strategy's share keeps the merged row alive when other
  shares still hold size; closing the last share closes the merged row
"""

from __future__ import annotations

import time
from copy import deepcopy

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.trading.position_book import (
    MERGED_STRATEGY_SENTINEL,
    PositionBook,
)


pytestmark = pytest.mark.smoke


def _make_paths(tmp_path) -> WorkspacePaths:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    # Touching the DB path provisions the schema (incl. migrations).
    paths = cfg.paths
    paths.db.parent.mkdir(parents=True, exist_ok=True)
    return paths


def _apply(book: PositionBook, *, strategy_id, side, size, price, market="mock:BTC/USDT"):
    return book.apply_fill(
        account_id="paper_main",
        strategy_id=strategy_id,
        market=market,
        side=side,
        price=price,
        size_base=size,
    )


def test_two_long_strategies_collapse_to_one_merged_row(tmp_path):
    paths = _make_paths(tmp_path)
    book = PositionBook(paths)

    _apply(book, strategy_id="s1", side="buy", size=0.5, price=100.0)
    _apply(book, strategy_id="s2", side="buy", size=0.3, price=110.0)

    merged_rows = book.open_positions(account_id="paper_main")
    assert len(merged_rows) == 1
    merged = merged_rows[0]
    assert merged.strategy_id == MERGED_STRATEGY_SENTINEL
    assert merged.size_base == pytest.approx(0.8)
    # Cost-basis weighted: (0.5*100 + 0.3*110) / 0.8 = 103.75
    assert merged.avg_entry_price == pytest.approx(103.75)
    assert merged.is_open

    shares = book.list_shares(merged.position_id)
    by_strategy = {s.strategy_id: s for s in shares}
    assert by_strategy["s1"].size_share_base == pytest.approx(0.5)
    assert by_strategy["s1"].avg_entry_share_price == pytest.approx(100.0)
    assert by_strategy["s2"].size_share_base == pytest.approx(0.3)
    assert by_strategy["s2"].avg_entry_share_price == pytest.approx(110.0)


def test_long_short_strategies_net_at_merged_level(tmp_path):
    paths = _make_paths(tmp_path)
    book = PositionBook(paths)

    _apply(book, strategy_id="long_bot", side="buy", size=0.5, price=100.0)
    _apply(book, strategy_id="short_bot", side="sell", size=0.3, price=110.0)

    merged = book.get_open_merged(account_id="paper_main", market="mock:BTC/USDT")
    assert merged is not None
    # Net size is +0.2 (still long overall) because the short share is
    # smaller than the long share.
    assert merged.size_base == pytest.approx(0.2)
    assert merged.side == "long"
    # Cost basis: 0.5 * 100 + (-0.3) * 110 = 50 - 33 = 17 → avg = 17 / 0.2 = 85
    assert merged.avg_entry_price == pytest.approx(85.0)

    shares = {s.strategy_id: s for s in book.list_shares(merged.position_id)}
    assert shares["long_bot"].size_share_base == pytest.approx(0.5)
    assert shares["short_bot"].size_share_base == pytest.approx(-0.3)
    assert shares["short_bot"].side == "short"


def test_share_close_keeps_merged_alive_via_other_strategies(tmp_path):
    paths = _make_paths(tmp_path)
    book = PositionBook(paths)

    _apply(book, strategy_id="s1", side="buy", size=0.5, price=100.0)
    _apply(book, strategy_id="s2", side="buy", size=0.3, price=110.0)
    # s1 closes out its slice at 120.
    _apply(book, strategy_id="s1", side="sell", size=0.5, price=120.0)

    merged = book.get_open_merged(account_id="paper_main", market="mock:BTC/USDT")
    assert merged is not None
    assert merged.is_open
    assert merged.size_base == pytest.approx(0.3)  # only s2 left
    assert merged.avg_entry_price == pytest.approx(110.0)

    s1_share = book.get_share(
        strategy_id="s1", account_id="paper_main", market="mock:BTC/USDT",
    )
    assert s1_share is None  # closed
    s1_closed = book.list_shares(merged.position_id, open_only=False)
    s1_history = [s for s in s1_closed if s.strategy_id == "s1"]
    assert len(s1_history) == 1
    # Realized = (120 - 100) * 0.5 = 10 USD on s1's own avg, not the
    # merged blended avg.
    assert s1_history[0].realized_pnl_share_usd == pytest.approx(10.0)
    assert s1_history[0].is_open is False

    # Merged realized reflects only the closed slice — s2 still long.
    assert merged.realized_pnl_usd == pytest.approx(10.0)


def test_share_can_flip_independently_of_merged_position(tmp_path):
    paths = _make_paths(tmp_path)
    book = PositionBook(paths)

    # Build merged long: s1 buys 0.5, s2 buys 0.5 → merged long 1.0.
    _apply(book, strategy_id="s1", side="buy", size=0.5, price=100.0)
    _apply(book, strategy_id="s2", side="buy", size=0.5, price=100.0)
    # s2 reverses: sells 0.8. That closes s2's 0.5 long + opens 0.3
    # short share. Merged size: 1.0 - 0.8 = +0.2 (still long via s1).
    _apply(book, strategy_id="s2", side="sell", size=0.8, price=110.0)

    merged = book.get_open_merged(account_id="paper_main", market="mock:BTC/USDT")
    assert merged is not None
    assert merged.size_base == pytest.approx(0.2)
    assert merged.side == "long"

    shares = {s.strategy_id: s for s in book.list_shares(merged.position_id)}
    assert shares["s1"].size_share_base == pytest.approx(0.5)
    assert shares["s1"].side == "long"
    # s2's open share is the flipped 0.3 short at 110.
    assert shares["s2"].size_share_base == pytest.approx(-0.3)
    assert shares["s2"].avg_entry_share_price == pytest.approx(110.0)
    assert shares["s2"].side == "short"

    # s2 realized PnL is on the closed long slice only: (110 - 100) * 0.5 = 5.
    closed_s2 = [
        s for s in book.list_shares(merged.position_id, open_only=False)
        if s.strategy_id == "s2" and s.closed_at is not None
    ]
    assert len(closed_s2) == 1
    assert closed_s2[0].realized_pnl_share_usd == pytest.approx(5.0)


def test_last_share_close_closes_merged_position(tmp_path):
    paths = _make_paths(tmp_path)
    book = PositionBook(paths)

    _apply(book, strategy_id="solo", side="buy", size=0.4, price=200.0)
    _apply(book, strategy_id="solo", side="sell", size=0.4, price=210.0)

    merged_open = book.open_positions(account_id="paper_main")
    assert merged_open == []  # closed

    # The closed merged row still exists in history with realized PnL.
    history = book.history(account_id="paper_main", market="mock:BTC/USDT")
    assert len(history) == 1
    closed = history[0]
    assert closed.is_open is False
    # (210 - 200) * 0.4 = 4 USD realized.
    assert closed.realized_pnl_usd == pytest.approx(4.0)


def test_get_open_returns_merged_only_when_strategy_has_share(tmp_path):
    paths = _make_paths(tmp_path)
    book = PositionBook(paths)

    _apply(book, strategy_id="s1", side="buy", size=0.5, price=100.0)

    # s1 sees the merged position via its share.
    pos_s1 = book.get_open(
        account_id="paper_main", strategy_id="s1", market="mock:BTC/USDT",
    )
    assert pos_s1 is not None
    assert pos_s1.is_merged
    assert pos_s1.size_base == pytest.approx(0.5)

    # s2 has no share so legacy-style lookup returns None even though
    # the merged position exists at the account+market level.
    pos_s2 = book.get_open(
        account_id="paper_main", strategy_id="s2", market="mock:BTC/USDT",
    )
    assert pos_s2 is None


def test_open_positions_by_strategy_filters_via_share(tmp_path):
    paths = _make_paths(tmp_path)
    book = PositionBook(paths)

    _apply(book, strategy_id="s1", side="buy", size=0.3, price=100.0)
    _apply(book, strategy_id="s2", side="buy", size=0.4, price=100.0)

    s1_view = book.open_positions(account_id="paper_main", strategy_id="s1")
    s2_view = book.open_positions(account_id="paper_main", strategy_id="s2")

    assert len(s1_view) == 1
    assert len(s2_view) == 1
    # Both strategies see the SAME merged row (same position_id), since
    # they share it.
    assert s1_view[0].position_id == s2_view[0].position_id
    assert s1_view[0].size_base == pytest.approx(0.7)


def test_max_position_size_usd_caps_merged_runup_across_strategies(tmp_path):
    """``max_position_size_usd`` on the strategy limits caps the
    **merged** notional, so strategy A and strategy B together can't
    push the (account, market) past the cap even when each individual
    order fits under ``max_single_order_usd``.
    """
    from copy import deepcopy

    from nerya.core import yaml_io
    from nerya.core.config import Config, DEFAULT_CONFIG
    from nerya.trading.submit import submit_trade_intent

    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    paths = _make_paths(tmp_path)
    cfg = Config(paths=paths, data=data)
    yaml_io.dump(
        paths.accounts_file,
        {
            "accounts": [{
                "id": "paper_main",
                "exchange": "mock",
                "venue": "mock",
                "mode": "paper",
                "status": "active",
                "initial_balance_usd": 100_000,
                "permissions": {
                    "read_balances": True, "place_order": True, "cancel_order": True,
                },
            }],
        },
    )
    for sid in ("a", "b"):
        yaml_io.dump(
            paths.strategy(sid) / "strategy.yml",
            {
                "id": sid, "status": "paper", "account_id": "paper_main",
                "markets": ["mock:BTC/USDT"],
                "paper_trading_enabled": True, "live_trading_enabled": False,
            },
        )
        yaml_io.dump(
            paths.strategy(sid) / "limits.yml",
            {
                "allowed_markets": ["mock:BTC/USDT"],
                "min_confidence": 0,
                "max_stale_seconds": 60,
                "approval_threshold_usd": 0,
                # Per-order is loose, but per-(account, market) is the cap
                # we want to enforce after merging.
                "max_single_order_usd": 100_000,
                "max_position_size_usd": 6_000,
            },
        )

    # A opens 0.08 BTC @ 50_000 → notional 4_000. Under both caps.
    out_a = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "a", "account_id": "paper_main",
            "market": "mock:BTC/USDT", "side": "buy",
            "size": 4_000, "size_unit": "usd",
            "order_type": "market", "confidence": 1.0,
            "source": "test",
        },
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )
    assert out_a["status"] == "filled", out_a

    # B now tries to add another 0.06 BTC @ 50_000 → 3_000. Alone it's
    # fine but merged would be 7_000 > 6_000 cap → reject.
    out_b = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "b", "account_id": "paper_main",
            "market": "mock:BTC/USDT", "side": "buy",
            "size": 3_000, "size_unit": "usd",
            "order_type": "market", "confidence": 1.0,
            "source": "test",
        },
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )
    assert out_b["status"] == "rejected"
    risk = out_b["risk_decision"]
    assert any(
        r.startswith("max_position_size_exceeded:mock:BTC/USDT")
        for r in risk["reasons"]
    ), risk["reasons"]

    # Closing side from either strategy must NOT be blocked even if
    # the merged is over cap. We can't blow the cap by reducing, and
    # operators always need an exit even after a misconfiguration.
    out_close = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "a", "account_id": "paper_main",
            "market": "mock:BTC/USDT", "side": "sell",
            "size": 0.04, "size_unit": "base",
            "order_type": "market", "confidence": 1.0,
            "source": "test",
            "meta": {"plan_action": "close_position"},
        },
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )
    assert out_close["status"] == "filled", out_close


def test_portfolio_merged_row_carries_per_strategy_shares(tmp_path):
    """``get_portfolio_summary`` must return one merged row per
    (account, market) AND embed a ``shares`` list so the dashboard can
    expand into per-strategy slices without a second round-trip.
    Per-share unrealized PnL must sum back to the merged unrealized
    so the UI never shows phantom PnL.
    """
    from copy import deepcopy

    from nerya.core import yaml_io
    from nerya.core.config import Config, DEFAULT_CONFIG
    from nerya.trading.portfolio import get_portfolio_summary

    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    paths = _make_paths(tmp_path)
    cfg = Config(paths=paths, data=data)
    yaml_io.dump(
        paths.accounts_file,
        {
            "accounts": [{
                "id": "paper_main",
                "exchange": "mock", "venue": "mock", "mode": "paper",
                "status": "active", "initial_balance_usd": 100_000,
                "permissions": {
                    "read_balances": True, "place_order": True, "cancel_order": True,
                },
            }],
        },
    )
    # Two strategies, same market — must collapse to one merged row
    # but the response should expose each strategy's slice.
    book = PositionBook(paths)
    book.apply_fill(
        account_id="paper_main", strategy_id="alpha",
        market="mock:BTC/USDT", side="buy",
        price=50_000.0, size_base=0.6,
    )
    book.apply_fill(
        account_id="paper_main", strategy_id="beta",
        market="mock:BTC/USDT", side="buy",
        price=55_000.0, size_base=0.4,
    )
    # Mark current at 60k so we have non-trivial unrealized PnL.
    merged_open = book.open_positions(account_id="paper_main")[0]
    book.update_mark(merged_open.position_id, 60_000.0)

    summary = get_portfolio_summary(paths)
    acct = next(a for a in summary["accounts"] if a["id"] == "paper_main")
    rows = acct["positions"]
    assert "mock:BTC/USDT" in rows, list(rows.keys())
    merged = rows["mock:BTC/USDT"]
    # Merged: size 1.0 at weighted avg (0.6*50k + 0.4*55k)/1.0 = 52k.
    assert merged["size"] == pytest.approx(1.0)
    assert merged["avg_price"] == pytest.approx(52_000.0)
    assert merged["mark_price"] == pytest.approx(60_000.0)
    # Unrealized = (60k - 52k) * 1.0 = 8_000.
    assert merged["unrealized_pnl_usd"] == pytest.approx(8_000.0)

    shares = merged["shares"]
    by_strategy = {s["strategy_id"]: s for s in shares}
    assert set(by_strategy) == {"alpha", "beta"}
    assert by_strategy["alpha"]["size_base"] == pytest.approx(0.6)
    assert by_strategy["alpha"]["avg_entry_price"] == pytest.approx(50_000.0)
    assert by_strategy["beta"]["size_base"] == pytest.approx(0.4)
    assert by_strategy["beta"]["avg_entry_price"] == pytest.approx(55_000.0)
    # Pro-rata unrealized — sum must equal the merged unrealized.
    total_share_unrealized = sum(s["unrealized_pnl_usd"] for s in shares)
    assert total_share_unrealized == pytest.approx(merged["unrealized_pnl_usd"])
    # And the share split mirrors share-size proportion.
    assert by_strategy["alpha"]["unrealized_pnl_usd"] == pytest.approx(4_800.0)  # 0.6 * 8000
    assert by_strategy["beta"]["unrealized_pnl_usd"] == pytest.approx(3_200.0)  # 0.4 * 8000


def test_reconcile_local_does_not_drift_on_merged_position(tmp_path):
    """The local reconciliation pass used to compare fills filtered by
    ``strategy_id`` — post-v6 the merged position carries
    ``strategy_id='__merged__'`` so the filter found no fills and
    fired a false ``position_fill_drift`` issue. The fix sums fills by
    ``(account, market)`` only.

    We exercise the JOIN explicitly by inserting fills rows for both
    strategies and verifying their sum reconciles with the merged
    size. Before the fix this asserted with ``net=0`` (zero rows
    matched the merged sentinel); after the fix it sums to 0.5.
    """
    from copy import deepcopy

    from nerya.core import yaml_io
    from nerya.core.config import Config, DEFAULT_CONFIG
    from nerya.core.ids import fill_id as _new_fill_id, order_id as _new_order_id
    from nerya.db.sqlite import connect
    from nerya.trading.reconciliation import reconcile_local

    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    paths = _make_paths(tmp_path)
    cfg = Config(paths=paths, data=data)
    yaml_io.dump(
        paths.accounts_file,
        {
            "accounts": [{
                "id": "paper_main",
                "exchange": "mock", "venue": "mock", "mode": "paper",
                "status": "active", "initial_balance_usd": 100_000,
                "permissions": {
                    "read_balances": True, "place_order": True, "cancel_order": True,
                },
            }],
        },
    )
    book = PositionBook(paths)
    book.apply_fill(
        account_id="paper_main", strategy_id="a",
        market="mock:BTC/USDT", side="buy",
        price=50_000.0, size_base=0.3,
    )
    book.apply_fill(
        account_id="paper_main", strategy_id="b",
        market="mock:BTC/USDT", side="buy",
        price=50_000.0, size_base=0.2,
    )

    # Insert the corresponding fills rows that would normally land via
    # OrderTracker.record_fill. Each strategy gets its own order_id so
    # the JOIN by (account, market) sums to the merged size.
    con = connect(paths.db)
    for strategy_id, size in (("a", 0.3), ("b", 0.2)):
        con.execute(
            """
            INSERT INTO fills(
                fill_id, order_id, client_order_id, account_id, strategy_id,
                executor_id, market, side, price, size_base, notional_usd,
                fee_usd, funding_usd, source, ts, intent_id, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_fill_id(), _new_order_id(), "", "paper_main", strategy_id,
                None, "mock:BTC/USDT", "buy", 50_000.0, size, size * 50_000.0,
                0.0, 0.0, "paper", 0.0, None, "{}",
            ),
        )
    con.commit()

    report = reconcile_local(paths, persist=False)
    drift_issues = [
        issue for issue in report.issues
        if issue.get("kind") == "position_fill_drift"
    ]
    assert drift_issues == [], drift_issues
    # Sanity: the merged row IS counted in summary.
    assert report.summary["open_positions"] == 1


def test_account_binding_mismatch_rejected_at_risk_gate(tmp_path):
    """A strategy bound to account A cannot trade against account B via
    an intent that names B in ``intent.account_id`` — the risk gate
    must reject before touching any of B's ledger/snapshot/positions.
    """
    from copy import deepcopy

    from nerya.core import yaml_io
    from nerya.core.config import Config, DEFAULT_CONFIG
    from nerya.trading.submit import submit_trade_intent

    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    paths = _make_paths(tmp_path)
    cfg = Config(paths=paths, data=data)
    yaml_io.dump(
        paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_a",
                    "exchange": "mock", "venue": "mock", "mode": "paper",
                    "status": "active", "initial_balance_usd": 50_000,
                    "permissions": {
                        "read_balances": True, "place_order": True, "cancel_order": True,
                    },
                },
                {
                    "id": "paper_b",
                    "exchange": "mock", "venue": "mock", "mode": "paper",
                    "status": "active", "initial_balance_usd": 50_000,
                    "permissions": {
                        "read_balances": True, "place_order": True, "cancel_order": True,
                    },
                },
            ],
        },
    )
    # Strategy is bound to A.
    yaml_io.dump(
        paths.strategy("alpha") / "strategy.yml",
        {
            "id": "alpha", "status": "paper", "account_id": "paper_a",
            "markets": ["mock:BTC/USDT"],
            "paper_trading_enabled": True, "live_trading_enabled": False,
        },
    )
    yaml_io.dump(
        paths.strategy("alpha") / "limits.yml",
        {
            "allowed_markets": ["mock:BTC/USDT"],
            "min_confidence": 0, "max_stale_seconds": 60,
            "approval_threshold_usd": 0,
            "max_single_order_usd": 100_000,
        },
    )

    # Caller naively (or maliciously) routes the intent at account B.
    out = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "alpha", "account_id": "paper_b",
            "market": "mock:BTC/USDT", "side": "buy",
            "size": 1_000, "size_unit": "usd",
            "order_type": "market", "confidence": 1.0,
            "source": "test",
        },
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )
    assert out["status"] == "rejected", out
    risk = out["risk_decision"]
    assert any(
        r.startswith("account_binding_mismatch:")
        for r in risk["reasons"]
    ), risk["reasons"]
    # Hint catalog wires this to the strategy rebind page.
    hint_titles = [h.get("title") for h in risk.get("fix_hints", [])]
    assert "Intent targets the wrong account" in hint_titles, hint_titles

    # And the legitimate routing against paper_a still works.
    ok = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "alpha", "account_id": "paper_a",
            "market": "mock:BTC/USDT", "side": "buy",
            "size": 1_000, "size_unit": "usd",
            "order_type": "market", "confidence": 1.0,
            "source": "test",
        },
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )
    assert ok["status"] == "filled", ok


def test_snapshot_freshness_gate_exempts_position_reducing_intents(tmp_path, monkeypatch):
    """When the balance loop is stalled, the risk gate must still let
    operators flatten exposure. Explicit close/reduce plans AND ad-hoc
    sells that shrink the merged size both bypass the freshness gate,
    while opening intents are still rejected.
    """
    from copy import deepcopy

    from nerya.core import yaml_io
    from nerya.core.config import Config, DEFAULT_CONFIG
    from nerya.trading import account_snapshots as snap_mod
    from nerya.trading.account_snapshots import AccountSnapshot
    from nerya.trading.submit import submit_trade_intent
    from nerya.trading import risk as risk_mod

    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    paths = _make_paths(tmp_path)
    cfg = Config(paths=paths, data=data)
    yaml_io.dump(
        paths.accounts_file,
        {
            "accounts": [{
                "id": "paper_main",
                "exchange": "mock", "venue": "mock", "mode": "paper",
                "status": "active", "initial_balance_usd": 100_000,
                "permissions": {
                    "read_balances": True, "place_order": True, "cancel_order": True,
                },
            }],
        },
    )
    yaml_io.dump(
        paths.strategy("s1") / "strategy.yml",
        {
            "id": "s1", "status": "paper", "account_id": "paper_main",
            "markets": ["mock:BTC/USDT"],
            "paper_trading_enabled": True, "live_trading_enabled": False,
        },
    )
    yaml_io.dump(
        paths.strategy("s1") / "limits.yml",
        {
            "allowed_markets": ["mock:BTC/USDT"],
            "min_confidence": 0, "max_stale_seconds": 60,
            "approval_threshold_usd": 0,
            "max_single_order_usd": 100_000,
        },
    )

    # First trade: open a 1 BTC long while the snapshot path is healthy.
    out_open = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "s1", "account_id": "paper_main",
            "market": "mock:BTC/USDT", "side": "buy",
            "size": 1.0, "size_unit": "base",
            "order_type": "market", "confidence": 1.0,
            "source": "test",
        },
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )
    assert out_open["status"] == "filled", out_open

    # Now stub fresh_snapshot so risk sees a stale + unhealthy snapshot
    # at evaluation time (we can't rely on the on-demand refresh path
    # because it always brings a paper snapshot back to ts=now).
    def _stale_snap(config, account_id, *, max_age_s=None, profile=None):
        return AccountSnapshot(
            snapshot_id="stale-test",
            account_id=account_id,
            ts=time.time() - 10_000,
            source="paper",
            nav_usd=99_000.0,
            cash_by_asset={"USDT": 90_000.0},
            free_by_asset={"USDT": 90_000.0},
            health="auth_failed",
        )

    monkeypatch.setattr(risk_mod, "fresh_snapshot", _stale_snap)

    # 1. Adding exposure on the same long → should be rejected because
    # the snapshot is stale AND unhealthy.
    out_add = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "s1", "account_id": "paper_main",
            "market": "mock:BTC/USDT", "side": "buy",
            "size": 0.1, "size_unit": "base",
            "order_type": "market", "confidence": 1.0,
            "source": "test",
        },
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )
    assert out_add["status"] == "rejected", out_add
    add_reasons = out_add["risk_decision"]["reasons"]
    assert any(r.startswith("account_snapshot_stale:") for r in add_reasons), add_reasons
    assert any(r.startswith("account_snapshot_health_auth_failed") for r in add_reasons), add_reasons
    assert not any(r.startswith("account_snapshot_stale_exempt") for r in add_reasons)

    # 2. Sell that mathematically reduces the long (0.3 of the 1.0)
    # should be allowed despite stale+unhealthy snapshot, with the
    # warning surfaced as an ``_exempt`` reason.
    out_reduce = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "s1", "account_id": "paper_main",
            "market": "mock:BTC/USDT", "side": "sell",
            "size": 0.3, "size_unit": "base",
            "order_type": "market", "confidence": 1.0,
            "source": "test",
        },
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )
    assert out_reduce["status"] == "filled", out_reduce
    reduce_reasons = out_reduce["risk_decision"]["reasons"]
    assert any(r.startswith("account_snapshot_stale_exempt:") for r in reduce_reasons), reduce_reasons
    assert any(r.startswith("account_snapshot_health_exempt:auth_failed") for r in reduce_reasons), reduce_reasons

    # 3. Explicit close_position plan tag also bypasses the gate.
    out_close = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "s1", "account_id": "paper_main",
            "market": "mock:BTC/USDT", "side": "sell",
            "size": 0.2, "size_unit": "base",
            "order_type": "market", "confidence": 1.0,
            "source": "test",
            "meta": {"plan_action": "close_position"},
        },
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )
    assert out_close["status"] == "filled", out_close
    close_reasons = out_close["risk_decision"]["reasons"]
    assert any(r.startswith("account_snapshot_stale_exempt:") for r in close_reasons), close_reasons


def test_strategy_close_preview_uses_share_size_not_merged_net(tmp_path):
    """Operator clicks "close all positions for s1" when s1 is long 0.5
    and s2 is short 0.3 on the same merged position. The close plan
    must sell **0.5** (s1's full long share), not 0.2 (the merged net
    long), otherwise s1 ends up flipping short and s2 keeps its short.
    """
    from types import SimpleNamespace

    from nerya.api import routes_strategy
    from nerya.core import jsonl  # noqa: F401  # makes side-effect imports load
    from nerya.core.config import Config, DEFAULT_CONFIG

    paths = _make_paths(tmp_path)
    book = PositionBook(paths)
    _apply(book, strategy_id="long_bot", side="buy", size=0.5, price=100.0)
    _apply(book, strategy_id="short_bot", side="sell", size=0.3, price=110.0)

    # Stub strategy.yml so routes_strategy can resolve the strategy.
    cfg = Config(paths=paths, data=deepcopy(DEFAULT_CONFIG))
    (paths.strategy("long_bot") / "strategy.yml").parent.mkdir(parents=True, exist_ok=True)
    (paths.strategy("long_bot") / "strategy.yml").write_text(
        'id: long_bot\naccount_id: paper_main\nmarkets: ["mock:BTC/USDT"]\nstatus: paper\n',
        encoding="utf-8",
    )

    handler = next(
        h for method, path, h in routes_strategy.routes()
        if method == "POST" and path == "/strategy/close_positions"
    )
    preview = handler(SimpleNamespace(config=cfg), {
        "strategy_id": "long_bot", "dry_run": True,
    })
    assert preview["ok"] is True
    assert preview["count"] == 1
    row = preview["positions"][0]
    # Must reflect long_bot's share, not the merged 0.2 net.
    assert row["strategy_id"] == "long_bot"
    assert row["side"] == "long"
    assert row["size_base"] == pytest.approx(0.5)


def test_unique_index_blocks_second_open_row_per_account_market(tmp_path):
    """The v6 unique partial index protects the merge invariant against
    legacy callers that might bypass ``apply_fill`` and INSERT
    directly. We exercise it via raw SQL to be sure."""
    import sqlite3
    import time as _time
    from nerya.db.sqlite import connect

    paths = _make_paths(tmp_path)
    book = PositionBook(paths)
    _apply(book, strategy_id="s1", side="buy", size=0.1, price=100.0)

    con = connect(paths.db)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            """
            INSERT INTO positions (
                position_id, account_id, strategy_id, market, venue, side,
                size_base, avg_entry_price, mark_price, liquidation_price,
                realized_pnl_usd, unrealized_pnl_usd, fees_usd, funding_usd,
                leverage, source, executor_id, protection_id,
                opened_at, updated_at, closed_at, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pos_dupe", "paper_main", "rogue", "mock:BTC/USDT", "mock", "long",
                0.5, 100.0, None, None,
                0.0, 0.0, 0.0, 0.0,
                1.0, "paper", None, None,
                _time.time(), _time.time(), None, "{}",
            ),
        )

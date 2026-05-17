"""Regression: ``ctx.portfolio.positions(market)`` must return the
strategy's own share, not the merged ``__merged__`` row.

Background
----------

Before v6 each strategy owned its own ``positions`` row keyed by
``(account_id, strategy_id, market)``. Auto-generated scalping
templates therefore did::

    positions = ctx.portfolio.positions(market)
    position = positions[0] if positions else None
    if position:
        qty = abs(float(position.get('size')))
        if should_exit:
            ctx.trading.submit_intent(side='sell', size=qty, ...)

That contract was broken by v6: the positions row became merged across
strategies with ``strategy_id='__merged__'``. The legacy facade kept
returning the merged row, so a scalper running alongside others on
``binance:BTCUSDT`` saw ``size = sum of all strategies`` and tried to
close that gigantic position. Every "close" sell instead **added** to
the short (because ``side='sell'`` against an already-short position
grows the absolute exposure when the strategy template is direction-
agnostic), driving an exponential runaway from a single shared
short into hundreds of thousands of base units within an hour.

This test pins down the fix in :class:`StrategyPortfolio` so future
refactors can't silently regress to the merged-row view.
"""

from __future__ import annotations

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.strategies.context import StrategyPortfolio
from nerya.trading.position_book import PositionBook


pytestmark = pytest.mark.smoke


def _seed_two_strategy_short(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    book = PositionBook(paths)
    # Two strategies open opposing shorts on the same merged position.
    # Strategy A shorts 1 BTC, strategy B shorts 2 BTC. Merged total
    # is -3 BTC.
    book.apply_fill(
        account_id="paper_main",
        strategy_id="strat_a",
        market="binance:BTCUSDT",
        side="sell",
        price=80_000.0,
        size_base=1.0,
        venue="binance",
        source="paper",
    )
    book.apply_fill(
        account_id="paper_main",
        strategy_id="strat_b",
        market="binance:BTCUSDT",
        side="sell",
        price=80_000.0,
        size_base=2.0,
        venue="binance",
        source="paper",
    )
    return paths, book


def test_portfolio_positions_returns_share_not_merged(tmp_path):
    paths, _ = _seed_two_strategy_short(tmp_path)

    facade_a = StrategyPortfolio(paths=paths, strategy_id="strat_a")
    rows_a = facade_a.positions("binance:BTCUSDT")
    assert len(rows_a) == 1
    pos_a = rows_a[0]
    # The bug we're guarding against: pos_a.size used to be -3.0 (the
    # MERGED total). It MUST be -1.0 — strat_a's own slice — otherwise
    # the scalper template's ``qty = abs(size)`` reads 3.0 and a single
    # market sell flips the merged from -3 to -6 instantly.
    assert pos_a.size == pytest.approx(-1.0)
    assert pos_a.avg_price == pytest.approx(80_000.0)

    facade_b = StrategyPortfolio(paths=paths, strategy_id="strat_b")
    rows_b = facade_b.positions("binance:BTCUSDT")
    assert len(rows_b) == 1
    pos_b = rows_b[0]
    assert pos_b.size == pytest.approx(-2.0)


def test_portfolio_positions_unfiltered_returns_only_owned_shares(tmp_path):
    paths, _ = _seed_two_strategy_short(tmp_path)

    rows_a = StrategyPortfolio(paths=paths, strategy_id="strat_a").positions()
    assert len(rows_a) == 1
    assert rows_a[0].size == pytest.approx(-1.0)


def test_portfolio_position_helper_returns_own_share(tmp_path):
    paths, _ = _seed_two_strategy_short(tmp_path)

    pos_a = StrategyPortfolio(paths=paths, strategy_id="strat_a").position(
        "binance:BTCUSDT"
    )
    assert pos_a is not None
    assert pos_a.size == pytest.approx(-1.0)


def test_legacy_facade_without_strategy_id_does_not_error(tmp_path):
    """Admin / SDK callers without a strategy_id fall through to the
    legacy ``get_positions(paths)`` aggregate. The aggregate needs an
    accounts.yml + snapshot to return anything, so we just guarantee
    the call doesn't blow up — backwards-compat handling is verified
    in the existing ``test_portfolio_merged_row_carries_per_strategy_shares``
    end-to-end fixture in ``test_position_book_merged.py``.
    """

    paths, _ = _seed_two_strategy_short(tmp_path)
    rows = StrategyPortfolio(paths=paths).positions("binance:BTCUSDT")
    assert isinstance(rows, list)


def test_share_size_flips_correctly_on_close_intent(tmp_path):
    """End-to-end protection against the runaway bug.

    A strategy opens a 1 BTC short, then issues a "close" by reading
    its own share size and selling that amount. Before the fix the
    close would read the merged total (which could be much larger
    than 1 BTC if other strategies were also short) and the resulting
    sell would *expand* the merged short instead of flattening this
    strategy's slice. After the fix the strategy reads its own slice
    (-1.0) and a corresponding ``buy`` of 1.0 flattens its share to
    zero while leaving the sibling strategy's share untouched.
    """

    paths, book = _seed_two_strategy_short(tmp_path)
    facade_a = StrategyPortfolio(paths=paths, strategy_id="strat_a")
    pos_a = facade_a.position("binance:BTCUSDT")
    assert pos_a is not None
    qty_to_close = abs(pos_a.size)  # 1.0, not 3.0
    assert qty_to_close == pytest.approx(1.0)

    book.apply_fill(
        account_id="paper_main",
        strategy_id="strat_a",
        market="binance:BTCUSDT",
        side="buy",
        price=80_000.0,
        size_base=qty_to_close,
        venue="binance",
        source="paper",
    )

    facade_a_after = StrategyPortfolio(paths=paths, strategy_id="strat_a")
    rows_a_after = facade_a_after.positions("binance:BTCUSDT")
    assert rows_a_after == [] or abs(rows_a_after[0].size) < 1e-9

    facade_b = StrategyPortfolio(paths=paths, strategy_id="strat_b")
    pos_b = facade_b.position("binance:BTCUSDT")
    assert pos_b is not None
    assert pos_b.size == pytest.approx(-2.0), "sibling share must be untouched"

    merged = book.get_open_merged(account_id="paper_main", market="binance:BTCUSDT")
    assert merged is not None
    assert merged.size_base == pytest.approx(-2.0), (
        "after strat_a closes, merged should only reflect strat_b's -2 BTC"
    )

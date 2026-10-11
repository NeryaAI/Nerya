import pytest

from nerya.trading.position_book import PositionBook
from nerya.trading.instruments import position_bucket
from nerya.core.errors import TradingError
from test_trading_kernel_safety import cfg

pytestmark = pytest.mark.smoke


def test_same_strategy_long_and_short_are_separate_broker_positions(cfg):
    book = PositionBook(cfg.paths)
    args = dict(account_id="acct_1", strategy_id="alpha", market="MOCK:SOLUSDT", price=100, size_base=1)
    long = book.apply_fill(**args, side="buy", position_side="long", fill_id="long")
    short = book.apply_fill(**args, side="sell", position_side="short", fill_id="short")
    assert long.position_id != short.position_id
    assert len(book.open_positions(account_id="acct_1")) == 2
    assert book.apply_fill(**args, side="buy", position_side="long", fill_id="long").position_id == long.position_id
    assert book.get_share(strategy_id="alpha", account_id="acct_1", market="MOCK:SOLUSDT", position_side="long").size_share_base == 1
    assert book.get_share(strategy_id="alpha", account_id="acct_1", market="MOCK:SOLUSDT", position_side="short").size_share_base == -1
    with pytest.raises(ValueError, match="ambiguous_position_side"):
        book.get_share(strategy_id="alpha", account_id="acct_1", market="MOCK:SOLUSDT")
    book.apply_fill(**{**args, "price": 110}, side="sell", position_side="long", fill_id="close-long")
    assert book.get_open_merged(account_id="acct_1", market="MOCK:SOLUSDT", position_side="long") is None
    assert book.get_open_merged(account_id="acct_1", market="MOCK:SOLUSDT", position_side="short").size_base == -1
    book.close()


def test_existing_one_way_share_behavior_is_preserved(cfg):
    book = PositionBook(cfg.paths)
    args = dict(account_id="acct_1", market="MOCK:SOLUSDT", price=100, size_base=1)
    first = book.apply_fill(**args, strategy_id="alpha", side="buy", fill_id="old-1")
    second = book.apply_fill(**args, strategy_id="beta", side="buy", fill_id="old-2")
    assert first.position_id == second.position_id
    assert second.position_side == "net" and second.size_base == 2
    book.close()


def test_broker_selectors_have_one_unambiguous_position_bucket():
    assert position_bucket({"connector_params": {"positionIdx": 2}}) == "short"
    assert position_bucket({"connector_params": {"positionSide": "LONG"}}) == "long"
    with pytest.raises(TradingError, match="conflicting"):
        position_bucket({"position_side": "long", "connector_params": {"positionIdx": 2}})

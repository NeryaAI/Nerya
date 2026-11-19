"""Prediction display projection uses recorded strategy holdings and honest values."""
from types import SimpleNamespace
import time
import pytest
from nerya.core.errors import TradingError

from nerya.trading.account_snapshots import AccountSnapshot, _capture_instruments
from nerya.trading.portfolio import _snapshot_cash_usd, _position_book_rows, decorate_instrument, get_portfolio_summary, _prediction_fees
from nerya.trading.position_book import Position, PositionBook
from test_polymarket_connector import TOKEN_ID

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize('strategy,parts,expected', [
    ('first', [('first', 49.5), ('second', 49.5)], 49.5),
    ('third', [('first', 49.5), ('second', 49.5)], 0),
    ('third', [('first', 49.5)], None),
    ('third', [], None),
])
def test_agent_position_receipt_distinguishes_account_and_owned(monkeypatch, strategy, parts, expected):
    from nerya.tools.native.bootstrap import _wrap_portfolio_positions
    from nerya.tools.types import ToolCall
    from nerya.trading import portfolio

    monkeypatch.setattr(portfolio, 'get_positions', lambda paths: [{
        'instrument': {'type': 'prediction_outcome'}, 'size_base': 99,
        'shares': [{'strategy_id': sid, 'size_base': qty} for sid, qty in parts],
    }])
    deps = SimpleNamespace(active_strategy_id=strategy, config=SimpleNamespace(paths=None))
    result = _wrap_portfolio_positions(deps)(ToolCall(name='portfolio_positions', arguments={}))
    row, = result.content[0].data['positions']
    assert row['size_base'] == 99
    assert row['strategy_position'] == {'strategy_id': strategy, 'owned_shares': expected}


def test_pusd_and_zero_cash_share_the_snapshot_valuation():
    assert _snapshot_cash_usd({'PUSD': 125}) == 125
    assert _snapshot_cash_usd({'PUSD': 0}) == 0
    assert _snapshot_cash_usd({'UNPRICED': 12}) is None


@pytest.mark.parametrize('mark,value,pnl', [(0, 0, -4), (None, None, None), (0.7, 7, 3)])
def test_position_marks_distinguish_zero_from_unavailable(mark, value, pnl):
    p = Position('position', 'account', 'strategy', 'POLYMARKET:'+TOKEN_ID, 'POLYMARKET',
                 'long', 10, 0.4, mark_price=mark)
    row = _position_book_rows([p], {}, paths=None)[p.market]
    assert row['mark_price'] == mark and row['market_value_usd'] == value
    if pnl is None:
        assert row['unrealized_pnl_usd'] is None
    else:
        assert row['unrealized_pnl_usd'] == pytest.approx(pnl)


def test_metadata_failure_keeps_strategy_identity_and_marks_stale():
    market = 'POLYMARKET:'+TOKEN_ID
    row = {'market': market, 'size_base': 10}
    decorate_instrument(row, None, defaults={'type': 'prediction_outcome', 'quantity_unit': 'shares'})
    assert row['market'] == market and row['instrument']['data_status'] == 'unavailable'
    snap = AccountSnapshot('s', 'a', time.time()-3600, 'live', 100, meta={'instruments': {
        market: {'type': 'prediction_outcome', 'label': 'Event', 'outcome': 'No', 'data_status': 'available'}}})
    decorate_instrument(row, snap)
    assert row['instrument']['data_status'] == 'stale'
    assert snap.meta['instruments'][market]['data_status'] == 'available'


@pytest.mark.parametrize('value',[-4,0,None])
def test_prediction_pnl_uses_local_strategy_marks_not_account_snapshot(value):
    from nerya.trading.portfolio import _summary_unrealized_usd
    summary={'accounts':[{'pnl_scope':'strategy','snapshot':{'unrealized_pnl_usd':999},
                          'positions':{'outcome':{'unrealized_pnl_usd':value}}}]}
    assert _summary_unrealized_usd(summary)==value


@pytest.mark.parametrize('venue,kind', [('polymarket', 'prediction_market'), ('mock', 'cex')])
def test_public_instruments_are_collected_only_for_owned_positions(tmp_path, monkeypatch, venue, kind):
    from test_wallet_swap_approval import _config
    from nerya.core import yaml_io
    from nerya.trading.accounts import load_account_profiles
    cfg = _config(tmp_path)
    yaml_io.dump(cfg.paths.accounts_file, {'accounts':[{'id':'pm','venue':venue,'kind':kind,'mode':'paper'}]})
    book = PositionBook(cfg.paths)
    market = 'POLYMARKET:'+TOKEN_ID
    book.apply_fill(account_id='pm', strategy_id='s', market=market, side='buy', price=.4,
                    size_base=10, venue='polymarket', source='paper')
    configs = []; markets = []
    def build(config, **kwargs):
        configs.append(config)
        return SimpleNamespace(get_instrument=lambda m: markets.append(m) or {'type': 'prediction_outcome', 'label': 'Event', 'data_status': 'available'})
    monkeypatch.setattr('nerya.connectors.registry.build_connector', build)
    result = _capture_instruments(cfg, load_account_profiles(cfg.paths)['pm'])
    assert list(result) == [market] and markets == [market]
    assert configs == [{'venue': 'polymarket', 'live': False, 'provider_config': {}}]


@pytest.mark.parametrize('available', [True, False])
def test_new_position_display_reads_public_metadata_without_changing_snapshot(tmp_path, monkeypatch, available):
    from test_wallet_swap_approval import _config
    from nerya.core import yaml_io
    from nerya.trading.accounts import load_account_profiles
    from nerya.trading import portfolio
    from nerya.api import routes_accounts
    monkeypatch.setattr('nerya.trading.account_snapshots.time.time', lambda: 1700000000.0)
    cfg = _config(tmp_path)
    yaml_io.dump(cfg.paths.accounts_file, {'accounts': [{'id': 'pm', 'venue': 'polymarket',
        'kind': 'prediction_market', 'mode': 'paper', 'initial_balance_usd': 1000}]})
    market = 'POLYMARKET:' + TOKEN_ID
    snapshot = AccountSnapshot('before-fill', 'pm', time.time(), 'paper', 1000,
                               cash_by_asset={'PUSD': 1000})
    PositionBook(cfg.paths).apply_fill(account_id='pm', strategy_id='s', market=market,
        side='buy', price=.4, size_base=10, venue='polymarket', source='paper')
    monkeypatch.setattr(portfolio, 'latest_snapshot', lambda *a: snapshot)
    monkeypatch.setattr(routes_accounts, 'latest_snapshot', lambda *a: snapshot)
    reads = []
    def metadata(selected):
        reads.append(selected)
        if not available:
            raise TradingError('public metadata unavailable')
        return {'type': 'prediction_outcome', 'label': 'New event', 'outcome': 'No',
                'quantity_unit': 'shares', 'data_status': 'available'}
    monkeypatch.setattr('nerya.connectors.registry.build_connector',
                        lambda *a, **kw: SimpleNamespace(get_instrument=metadata))
    row = get_portfolio_summary(cfg.paths)['accounts'][0]['positions'][market]
    detail = routes_accounts._account_summary(SimpleNamespace(config=cfg),
        load_account_profiles(cfg.paths)['pm'])['open_positions'][0]
    assert row['instrument'] == detail['instrument']
    assert row['instrument']['data_status'] == ('available' if available else 'unavailable')
    assert row['instrument']['quantity_unit'] == 'shares'
    if available:
        assert row['instrument']['label'] == 'New event'
    assert row['shares'][0]['strategy_id'] == 's'
    assert reads == [market]  # Both pages reuse the bounded public display cache.
    assert snapshot.nav_usd == 1000 and snapshot.meta == {}
    # Turning the module off takes precedence even over a warm public cache.
    snapshot.meta['instruments'] = {market: row['instrument']}
    yaml_io.dump(cfg.paths.config, {'modules': {'prediction_market': 'disabled'}})
    disabled = get_portfolio_summary(cfg.paths)['accounts'][0]['positions'][market]
    assert disabled['instrument']['data_status'] == ('stale' if available else 'unavailable')
    assert disabled['instrument']['quantity_unit'] == 'shares'
    assert reads == [market]


@pytest.mark.parametrize('mark,nav', [(0, 995.99), (.5, 1000.99)])
def test_paper_display_uses_current_ledger_after_fill_and_preserves_zero_mark(tmp_path, monkeypatch, mark, nav):
    from test_wallet_swap_approval import _config
    from nerya.core import yaml_io
    from nerya.trading import portfolio
    from nerya.api import routes_accounts
    from nerya.trading.accounts import load_account_profiles
    from nerya.trading.virtual_ledger import open_ledger
    cfg = _config(tmp_path)
    yaml_io.dump(cfg.paths.accounts_file, {'accounts': [{'id': 'pm', 'venue': 'polymarket',
        'kind': 'prediction_market', 'mode': 'paper', 'base_currency': 'PUSD', 'initial_balance_usd': 1000}]})
    market = 'POLYMARKET:' + TOKEN_ID
    before = AccountSnapshot('pre-trade', 'pm', time.time()-60, 'paper', 1000, cash_by_asset={'PUSD': 1000})
    monkeypatch.setattr(portfolio, 'latest_snapshot', lambda *a: before)
    monkeypatch.setattr(routes_accounts, 'latest_snapshot', lambda *a: before)
    monkeypatch.setattr('nerya.trading.account_snapshots._public_instrument',
                        lambda *a: {'data_status': 'unavailable', 'as_of': time.time()})
    monkeypatch.setattr('nerya.trading.capital.CapitalReservationStore.total_blocked_usd', lambda *a: 2.5)
    ledger = open_ledger(cfg.paths, 'pm', 1000)
    ledger.apply_fill(market=market, side='buy', price=.4, size=10, fee_usd=.01)
    book = PositionBook(cfg.paths)
    position = book.apply_fill(account_id='pm', strategy_id='s', market=market,
                              side='buy', price=.4, size_base=10, venue='polymarket', source='paper')
    book.update_mark(position.position_id, mark_price=mark)
    summary = get_portfolio_summary(cfg.paths)['accounts'][0]
    detail = routes_accounts._account_summary(SimpleNamespace(config=cfg), load_account_profiles(cfg.paths)['pm'])
    assert summary['cash_usd'] == pytest.approx(995.99)
    assert summary['equity_usd'] == pytest.approx(nav)
    assert summary['available_usd'] == pytest.approx(993.49) and summary['locked_usd'] == 2.5
    assert detail['snapshot']['total_usd'] == pytest.approx(nav)
    assert detail['snapshot']['cash_usd'] == pytest.approx(995.99)
    assert before.nav_usd == 1000 and before.cash_by_asset == {'PUSD': 1000}
    ledger.apply_fill(market=market, side='sell', price=.5, size=10, fee_usd=.01)
    book.apply_fill(account_id='pm', strategy_id='s', market=market,
                    side='sell', price=.5, size_base=10, venue='polymarket', source='paper')
    closed = get_portfolio_summary(cfg.paths)['accounts'][0]
    assert closed['cash_usd'] == pytest.approx(1000.98) and closed['equity_usd'] == pytest.approx(1000.98)


def test_nav_is_whole_account_but_positions_are_strategy_book_only(tmp_path, monkeypatch):
    from test_wallet_swap_approval import _config
    from nerya.core import yaml_io
    from nerya.trading import portfolio
    cfg = _config(tmp_path)
    yaml_io.dump(cfg.paths.accounts_file, {'accounts': [{'id': 'pm', 'venue': 'polymarket',
        'kind': 'prediction_market', 'mode': 'shadow', 'status': 'active'}]})
    market = 'POLYMARKET:'+TOKEN_ID
    book = PositionBook(cfg.paths)
    book.apply_fill(account_id='pm', strategy_id='s', market=market, side='buy', price=.4,
                    size_base=10, venue='polymarket', source='paper')
    snapshot = AccountSnapshot('s', 'pm', time.time(), 'shadow', 250,
        cash_by_asset={'PUSD': 125}, free_by_asset={'PUSD': 100}, locked_by_asset={'PUSD': 25},
        meta={'positions': [{'market': 'external', 'size': 999}], 'positions_value_usd': 125,
              'instruments': {market: {'type': 'prediction_outcome', 'label': 'Event', 'outcome': 'No', 'data_status': 'available'}}})
    monkeypatch.setattr(portfolio, 'latest_snapshot', lambda *a: snapshot)
    result = get_portfolio_summary(cfg.paths)['accounts'][0]
    assert result['mode'] == 'shadow'
    assert result['equity_usd'] == 250 and result['cash_usd'] == 125
    assert result['available_usd'] == 100 and result['locked_usd'] == 25
    assert list(result['positions']) == [market]
    assert result['positions'][market]['instrument']['outcome'] == 'No'
    assert portfolio.get_pnl(cfg.paths)['total_pnl_usd'] is None  # Unverified fees are not zero.
    monkeypatch.setattr(portfolio,'_prediction_fees',lambda *args:0.0)
    assert portfolio.get_pnl(cfg.paths)['total_pnl_usd'] == 0  # External NAV is not strategy profit.
    snapshot.health = 'auth_error'
    unavailable = get_portfolio_summary(cfg.paths)['accounts'][0]
    assert unavailable['cash_usd'] is None and unavailable['equity_usd'] is None


@pytest.mark.parametrize('status,expected',[('chain_verified',0.0),('unverified',None),('reported',None)])
def test_unknown_fee_is_not_a_zero_fee_in_strategy_history(tmp_path,status,expected):
    from nerya.trading.order_tracker import OrderTracker
    from nerya.core.paths import WorkspacePaths
    paths=WorkspacePaths(root=tmp_path); tracker=OrderTracker(paths)
    order=tracker.register(client_order_id='fee',account_id='pm',strategy_id='s',market='POLYMARKET:'+TOKEN_ID,
                           side='buy',order_type='limit',size_base=1,notional_usd=.4)
    tracker.record_fill(order_id=order.order_id,price=.4,size_base=1,fee_usd=0,source='live',meta={'fee_status':status})
    assert _prediction_fees(paths,'pm',1)==expected

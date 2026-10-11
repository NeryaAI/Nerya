import pytest

from nerya.connectors.prediction_data import portfolio_value, positions
from nerya.connectors.polymarket import PolymarketConnector
from nerya.core.errors import TradingError

pytestmark = pytest.mark.smoke
OWNER = '0x' + 'a' * 40


def test_value_uses_canonical_v2_envelope_and_includes_total_portfolio():
    calls = []
    def get(base, path, *, params):
        calls.append((path, params))
        return {'data': {'proxy_wallet': OWNER, 'value': '123.45'}}
    assert portfolio_value(get, 'https://data.test', OWNER) == 123.45
    assert calls == [('v2/value', {'user': OWNER})]


@pytest.mark.parametrize('data', [None, [], {}, {'proxy_wallet': OWNER, 'value': None},
    {'proxy_wallet': OWNER, 'value': 'NaN'}, {'proxy_wallet': OWNER, 'value': '-1'},
    {'proxy_wallet': '0x' + 'b' * 40, 'value': 12}, {'proxy_wallet': OWNER, 'value': True}])
def test_absent_or_wrong_wallet_value_is_not_zero(data):
    with pytest.raises(TradingError):
        portfolio_value(lambda *a, **kw: {'data': data}, 'https://data.test', OWNER)


def test_positions_keep_anchor_and_filters_across_opaque_cursor_pages():
    calls = []
    def get(base, path, *, params):
        calls.append((path, params))
        return {'data': [{'proxy_wallet': OWNER, 'token_id': str(len(calls))}],
                'pagination': {'next_cursor': 'opaque/+' if len(calls) == 1 else None,
                               'has_more': len(calls) == 1}}
    assert len(positions(get, '', OWNER, status='REDEEMABLE')) == 2
    assert calls[1][1]['cursor'] == 'opaque/+'
    assert calls[1][1]['user'] == OWNER and calls[1][1]['status'] == 'REDEEMABLE'
    assert calls[1][1]['filter_amount'] == 0 and calls[1][1]['include_archived'] == 'true'
    assert all('offset' not in params for _, params in calls)


@pytest.mark.parametrize('page', [{}, {'next_cursor': ''}, {'next_cursor': None, 'has_more': True},
                                {'next_cursor': 'repeat', 'has_more': True}])
def test_malformed_or_repeating_cursor_never_returns_partial_inventory(page):
    with pytest.raises(TradingError):
        positions(lambda *a, **kw: {'data': [], 'pagination': page}, '', OWNER, max_pages=2)


def test_connector_no_longer_uses_v1_positions_for_nav():
    conn = PolymarketConnector(funder=OWNER)
    conn._get = lambda base, path, **kw: {'data': {'proxy_wallet': OWNER, 'value': 0}} if path == 'v2/value' else None
    assert conn.get_positions_value() == 0


def test_gtd_rejects_two_minutes_without_posting(monkeypatch, tmp_path):
    import time
    from test_prediction_execution import sdk_connector
    from test_polymarket_connector import TOKEN_ID
    conn, calls = sdk_connector(monkeypatch, tmp_path)
    with pytest.raises(TradingError, match='180 seconds'):
        conn.place_order(market=TOKEN_ID, side='buy', order_type='limit', size=10, price=.5,
                         time_in_force='GTD', client_order_id='too-soon',
                         extra_params={'expiration': int(time.time()) + 120})
    assert not any(call[0] == 'post' for call in calls)

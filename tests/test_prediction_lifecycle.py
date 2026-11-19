import pytest

from nerya.core.errors import TradingError
from test_prediction_execution import offline, sdk_connector  # noqa: F401
from test_polymarket_connector import TOKEN_ID

pytestmark = pytest.mark.smoke


def order(state='LIVE', *, side='BUY', matched='4'):
    return {'id': 'hash', 'asset_id': TOKEN_ID, 'side': side, 'status': state,
            'original_size': '10', 'size_matched': matched, 'price': '.5', 'associate_trades': ['t1']}


def trade(state='MATCHED', *, size='4'):
    return {'id': 't1', 'taker_order_id': 'hash', 'asset_id': TOKEN_ID, 'side': 'BUY',
            'size': size, 'price': '.48', 'status': state, 'transaction_hash': 'tx' if state=='CONFIRMED' else None}


@pytest.mark.parametrize('trade_state,expected', [('FAILED', 'canceled'), ('MATCHED', 'new'), ('CONFIRMED', 'canceled')])
def test_cancel_terminal_distinguishes_failed_pending_and_settled(monkeypatch, tmp_path, trade_state, expected):
    conn, _ = sdk_connector(monkeypatch, tmp_path); client = conn._sdk()
    client.get_order = lambda oid: order('CANCELED')
    client.get_trades = lambda params: [trade(trade_state)]
    monkeypatch.setattr(conn, '_sdk', lambda: client)
    ack = conn.get_order(market=TOKEN_ID, order_id='hash')
    assert ack.status == expected
    assert ack.filled == (4 if trade_state == 'CONFIRMED' else 0)


@pytest.mark.parametrize('state,locked', [('MATCHED', 5), ('CONFIRMED', 3)])
def test_matched_cash_stays_reserved_until_settlement(monkeypatch, tmp_path, state, locked):
    conn, _ = sdk_connector(monkeypatch, tmp_path); client = conn._sdk()
    client.get_open_orders = lambda: [order()]
    client.get_trades = lambda params: [trade(state)]
    monkeypatch.setattr(conn, '_sdk', lambda: client)
    balance = conn.get_balances()[0]
    assert balance.locked == locked and balance.free == 10-locked


def test_orders_missing_from_open_book_keep_pending_reservations(monkeypatch, tmp_path):
    conn, _ = sdk_connector(monkeypatch, tmp_path); client = conn._sdk()
    conn._save_send('pending', {'client_order_id':'pending', 'order_id':'hash', 'status':'submitted',
                              'spec':{'side':'buy','token':TOKEN_ID}})
    client.get_open_orders = lambda: []
    client.get_order = lambda oid: order('CANCELED')
    client.get_trades = lambda params: [trade()]
    monkeypatch.setattr(conn, '_sdk', lambda: client)
    assert conn.get_balances()[0].locked == 2


def test_pending_sell_inventory_cannot_be_sold_twice(monkeypatch, tmp_path):
    conn, calls = sdk_connector(monkeypatch, tmp_path); client = conn._sdk()
    client.get_open_orders = lambda: [order(side='SELL')]
    client.get_trades = lambda params: [trade()]
    monkeypatch.setattr(conn, '_sdk', lambda: client)
    with pytest.raises(TradingError, match='unreserved'):
        conn.place_order(market=TOKEN_ID, side='sell', order_type='limit', size=1, price=.5, client_order_id='second')
    assert not any(call[0]=='post' for call in calls)


def test_limit_buy_checks_unreserved_collateral_before_signing(monkeypatch,tmp_path):
    conn,calls=sdk_connector(monkeypatch,tmp_path); client=conn._sdk()
    client.get_balance_allowance=lambda params:{'balance':'1000000','allowances':{}}
    monkeypatch.setattr(conn,'_sdk',lambda:client)
    with pytest.raises(TradingError,match='collateral balance'):
        conn.place_order(market=TOKEN_ID,side='buy',order_type='limit',size=10,price=.5,client_order_id='insufficient')
    assert not any(call[0] in ('sign','post') for call in calls)


def test_share_precision_is_checked_against_sdk_contract(monkeypatch,tmp_path):
    conn,calls=sdk_connector(monkeypatch,tmp_path)
    with pytest.raises(TradingError,match='SDK precision'):
        conn.place_order(market=TOKEN_ID,side='buy',order_type='limit',size=10.001,price=.5,client_order_id='precision')
    assert not any(call[0]=='post' for call in calls)


def test_fee_rpc_failure_does_not_block_sending_cancel(monkeypatch, tmp_path):
    conn, _ = sdk_connector(monkeypatch, tmp_path); client = conn._sdk(); sent=[]
    client.cancel_order = lambda payload: sent.append(payload.orderID) or {'canceled':[payload.orderID]}
    monkeypatch.setattr(conn, '_sdk', lambda: client)
    conn.settlement_rpc_url = 'https://fixture.invalid'; conn.funder = '0x'+'1'*40
    def offline_rpc(*a, **kw): raise TradingError('fee evidence unavailable', ambiguous=True)
    monkeypatch.setattr('nerya.connectors.prediction_fees.confirmed_order_fees', offline_rpc)
    ack = conn.cancel_order(market=TOKEN_ID, order_id='hash')
    assert sent == ['hash']
    assert ack.status == 'new' and ack.filled == 0 and ack.raw['cancel_confirmed'] is True
    assert ack.raw['settlement_pending'] is True


@pytest.mark.parametrize('stamp', [None, 'bad', True, -1, '1', '9999999999999'])
def test_invalid_book_time_rejected_before_signing(monkeypatch, tmp_path, stamp):
    conn, calls = sdk_connector(monkeypatch, tmp_path)
    get = conn._get
    def response(base, path, **kw):
        result = get(base, path, **kw)
        if path == 'book': result['timestamp'] = stamp
        return result
    monkeypatch.setattr(conn, '_get', response)
    with pytest.raises(TradingError, match='timestamp'):
        conn.place_order(market=TOKEN_ID, side='buy', order_type='limit', size=10, price=.5, client_order_id='stale')
    assert calls == []


def test_even_slightly_future_book_is_rejected_before_signing(monkeypatch, tmp_path):
    conn, calls = sdk_connector(monkeypatch, tmp_path)
    monkeypatch.setattr('nerya.connectors.polymarket.time.time', lambda: 1760000000)
    get = conn._get
    def response(base, path, **kwargs):
        result = get(base, path, **kwargs)
        if path == 'book':
            result['timestamp'] = 1760000000001
        return result
    monkeypatch.setattr(conn, '_get', response)
    with pytest.raises(TradingError, match='timestamp'):
        conn.place_order(market=TOKEN_ID, side='buy', order_type='limit', size=10, price=.5, client_order_id='future')
    assert calls == []

import base64
from copy import deepcopy

import base58
import pytest
from nacl.signing import SigningKey

from nerya.connectors.solana_native import SolanaNative
from nerya.core.errors import TradingError

pytestmark = pytest.mark.smoke
KEY = bytes(range(32)).hex()
OWNER = base58.b58encode(bytes(SigningKey(bytes.fromhex(KEY)).verify_key)).decode()
INPUT = 'So11111111111111111111111111111111111111112'
OUTPUT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


def transaction():
    # One signer, one static key, no instructions; signing is real, RPC is fake.
    message = b'\x80\x01\x00\x00\x01' + base58.b58decode(OWNER) + bytes(32) + b'\x00\x00'
    return base64.b64encode(b'\x01' + bytes(64) + message).decode()


def document():
    return {'inputMint': INPUT, 'outputMint': OUTPUT, 'inAmount': '100000000', 'outAmount': '10000000',
            'otherAmountThreshold': '9950000', 'swapMode': 'ExactIn', 'slippageBps': 50,
            'router': 'metis', 'taker': OWNER, 'signatureFeePayer': OWNER, 'gasless': False,
            'signatureFeeLamports': 5000, 'prioritizationFeeLamports': 1000, 'rentFeeLamports': 2039280,
            'requestId': 'immutable-request', 'transaction': transaction(), 'lastValidBlockHeight': '200'}


class Http:
    def __init__(self):
        self.calls = []
        self.doc = document()
        self.fail = False
        self.before_post = lambda: None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == 'GET':
            return 200, deepcopy(self.doc)
        self.before_post()
        if self.fail:
            raise TimeoutError('response lost')
        signed = base64.b64decode(kwargs['body']['signedTransaction'])
        return 200, {'status': 'Success', 'signature': base58.b58encode(signed[1:65]).decode(),
                     'totalOutputAmount': '999999999'}  # Intentionally not trusted.


def connector():
    http = Http()
    conn = SolanaNative(live=True, transport=http, jupiter_api_key='synthetic-api-key')
    conn._rpc = lambda method, args: 100 if method == 'getBlockHeight' else {'value': {'err': None}}
    conn.wait_for_signature = lambda signature: {'confirmationStatus': 'confirmed', 'slot': 101}
    conn.transaction_output = lambda *args: 9.98
    conn.transaction_fee = lambda *args: {'status': 'observed', 'amount_base': '6000', 'asset': 'SOL', 'decimals': 9}
    return conn, http


def quote(conn):
    return conn.quote_jupiter(input_mint=INPUT, output_mint=OUTPUT, amount_in_raw=100000000,
                              slippage_bps=50, taker=OWNER)


def send(conn, doc, callback=None):
    return conn.swap(input_mint=INPUT, output_mint=OUTPUT, amount_in_raw=100000000,
                     slippage_bps=50, signer_private_key=KEY, quote=doc, on_broadcast=callback)


def test_v2_quotes_and_real_signatures_use_order_execute_with_chain_receipt():
    conn, http = connector()
    doc = quote(conn)
    persisted = []
    http.before_post = lambda: persisted[0]['tx_hash']
    result = send(conn, doc, persisted.append)
    assert result['confirmed'] and result['amount_out'] == 9.98
    assert result['network_fee']['amount_base'] == '6000'
    assert persisted[0]['tx_hash'] == result['signature']
    assert http.calls[0][1].endswith('/swap/v2/order') and http.calls[1][1].endswith('/swap/v2/execute')
    assert http.calls[0][2]['params']['slippageBps'] == '50'
    assert http.calls[0][2]['params']['jitoTipLamports'] == '0'
    assert http.calls[1][2]['body']['requestId'] == 'immutable-request'
    assert 'synthetic-api-key' not in repr(conn)


@pytest.mark.parametrize('changes', [
    {'inAmount': '1'}, {'outputMint': INPUT}, {'otherAmountThreshold': '1'}, {'slippageBps': 100},
    {'swapMode': 'ExactOut'}, {'router': 'jupiterz'}, {'gasless': True},
    {'signatureFeePayer': 'foreign'}, {'prioritizationFeeLamports': 2_000_001},
    {'rentFeeLamports': 10_000_001}, {'outAmount': 'NaN'}, {'inAmount': True},
])
def test_invalid_quotes_never_sign_or_submit(changes):
    conn, http = connector()
    http.doc.update(changes)
    with pytest.raises(TradingError):
        quote(conn)
    assert not any(call[0] == 'POST' for call in http.calls)


def test_submit_timeout_preserves_signature_and_never_automatically_reposts():
    conn, http = connector()
    http.fail = True
    persisted = []
    with pytest.raises(TradingError) as error:
        send(conn, quote(conn), persisted.append)
    assert error.value.ambiguous and persisted[0]['jupiter_request_id'] == 'immutable-request'
    assert len([call for call in http.calls if call[0] == 'POST']) == 1


@pytest.mark.parametrize('reason', ['simulation', 'expired', 'empty'])
def test_unexecutable_quote_cannot_broadcast(reason):
    conn, http = connector()
    doc = quote(conn)
    if reason == 'simulation':
        conn._rpc = lambda method, args: 100 if method == 'getBlockHeight' else {'value': {'err': 'revert'}}
    elif reason == 'expired':
        doc['lastValidBlockHeight'] = '1'
    else:
        doc['transaction'] = ''
    with pytest.raises(TradingError):
        send(conn, doc)
    assert not any(call[0] == 'POST' for call in http.calls)


def test_dex_exclusions_force_metis_instead_of_leaking_to_another_router():
    conn, http = connector()
    conn.jupiter_exclude_dexes = ('Some DEX',)
    quote(conn)
    assert http.calls[0][2]['params']['excludeRouters'] == 'jupiterz,dflow,okx'
    http.doc['router'] = 'dflow'
    with pytest.raises(TradingError, match='exclusions'):
        quote(conn)


def test_missing_key_is_an_explicit_dependency_error():
    conn, http = connector()
    conn.jupiter_api_key = ''
    with pytest.raises(TradingError, match='API key'):
        quote(conn)
    assert http.calls == []


def test_wallet_quote_reserves_network_and_rent_caps_not_zero(monkeypatch):
    from nerya.wallet.providers.self_custody import SelfCustodyWallet
    conn,_=connector()
    conn.get_mint_decimals=lambda _:6
    wallet=SelfCustodyWallet()
    monkeypatch.setattr(wallet,'_solana_connector',lambda **kw:conn)
    monkeypatch.setattr('nerya.financial.adapters.usd_price',lambda symbol,**kw:__import__('decimal').Decimal(200))
    quote=wallet._solana_quote(token_in=INPUT,token_out=OUTPUT,amount_in=.1,slippage_bps=50)
    assert quote.gas_cost_usd==.4
    assert quote.extra['gas_budget']['amount']=='0.012'
    assert quote.extra['gas_budget']['rent_is_refundable_reserve'] is True

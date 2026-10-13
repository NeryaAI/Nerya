"""Offline tests against official Perpl wire shapes; no network writes."""
from copy import deepcopy
import base64
import hashlib
import json
import shutil
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from competition.monad.perpl.trading import API, RPC, NETWORKS, PerplTransport, TRADING_COMPONENTS, scaled, signed_headers
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.financial.contracts import FinancialContext, FinancialError
from nerya.financial.gateway import FinancialGateway
from nerya.security.secrets import SecretVault
from nerya.trading.components import TradingComponentRegistry

pytestmark = pytest.mark.smoke

ADDRESS = '0x' + '1'*40
SEED = bytes(range(32))
PUBLIC = Ed25519PrivateKey.from_private_bytes(SEED).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def market():
    return {'id': 10, 'name': 'MON', 'instance_id': 1, 'order_max_market_slippage_bps': 100, 'order_max_neg_pnl_collat_bps': 1000,
            'config': {'is_open': True, 'price_decimals': 6, 'size_decimals': 0,
                       'initial_margin': 1000, 'min_posting_amount': '0', 'min_settle_amount': '0',
                       'taker_fee': 345, 'taker_fees': [345]},
            'state': {'at': {'t': int(time.time()*1000), 'b': 100}, 'mrk': 25000, 'bid': 24999, 'ask': 25001}}


def request(**parameters):
    return {'kind': 'trade', 'chain': 'monad', 'account_id': 'perpl-main', 'market': 'PERPL:MON/AUSD',
            'component_id': 'user:perpl:perpl', 'parameters': parameters,
            'plan': {'strategy_id': 'mon-breakout', 'account_id': 'perpl-main', 'market': 'PERPL:MON/AUSD',
                     'action': 'open_position', 'side': 'long', 'sizing': {'method': 'fixed_base', 'fixed_base': 100},
                     'entry': {'order_type': 'market', 'time_in_force': 'ioc'}, 'meta': {'leverage': 2}}}


class FakeHTTP:
    def __init__(self):
        self.api, self.rpc = API, RPC
        self.rpc_chain = 143
        self.market = market()
        self.address = ADDRESS
        self.chain = 143
        self.lfr = 40
        self.positions = []
        self.orders = []
        self.history = []
        self.post = {'mt': 31, 'cid': 41, 'status': {'code': 0}, 'statuses': [{'code': 0}]}
        self.fail_post = False
        self.calls = []

    def request(self, method, url, *, headers=None, body=b''):
        self.calls.append((method, url, headers or {}, body))
        at = {'t': int(time.time()*1000), 'b': 100}
        if url == self.rpc:
            if json.loads(body).get('method') == 'eth_getTransactionReceipt':
                return 200, {'result': {'transactionHash': json.loads(body)['params'][0], 'status': '0x1'}}
            return 200, {'result': hex(self.rpc_chain)}
        target = url.removeprefix(self.api)
        if target == '/v1/pub/context':
            return 200, {'chain': {'chain_id': self.chain}, 'markets': [deepcopy(self.market)], 'instances':[{'id':1,'address':ADDRESS}]}
        canonical = '\n'.join((str(self.chain), method, target, headers['X-API-Timestamp'], headers['X-API-Nonce'], hashlib.sha256(body).hexdigest())).encode()
        signature = base64.urlsafe_b64decode(headers['X-API-Signature'] + '==')
        Ed25519PrivateKey.from_private_bytes(SEED).public_key().verify(signature, canonical)
        assert headers['X-API-Key'] == 'opaque-test-token'
        if target == '/v1/trading/wallet':
            return 200, {'mt': 19, 'at': at, 'addr': self.address, 'as': [{'id': 7, 'in': 1, 'fr': False, 'fw': True, 'ft': 0, 'lfr': self.lfr, 'b': '1000000000', 'lb': '10000000'}]}
        if target == '/v1/trading/positions':
            return 200, {'mt': 26, 'at': at, 'd': deepcopy(self.positions)}
        if target.startswith('/v1/trading/order-history?'):
            return 200, {'d': deepcopy(self.history), 'np': ''}
        if target == '/v1/trading/orders' and method == 'GET':
            return 200, {'mt': 23, 'at': at, 'd': deepcopy(self.orders)}
        if target == '/v1/trading/orders' and method == 'POST':
            if self.fail_post:
                raise TimeoutError('uncertain network result')
            return 200, deepcopy(self.post)
        raise AssertionError((method, target))


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = deepcopy(DEFAULT_CONFIG)
    data['financial'] = {'enabled': True}
    data['runtime']['live_trading_enabled'] = True
    data['plugins']['perpl'] = {'accounts': {'perpl-main': {'chain_id': 143, 'instance_id': 1,
        'wallet_address': ADDRESS, 'exchange_account_id': 7, 'api_key_ref': 'vault://perpl-token',
        'private_key_ref': 'vault://perpl-seed', 'public_key_hex': PUBLIC,
        'approved_scopes': ['perpl:read', 'perpl:trade']}}}
    cfg = Config(WorkspacePaths(tmp_path), data)
    cfg.paths.accounts.mkdir(parents=True)
    yaml_io.dump(cfg.paths.accounts_file, {'accounts': [{'id': 'perpl-main', 'venue': 'perpl', 'kind': 'dex',
        'mode': 'live', 'status': 'active', 'live_trading_enabled': True, 'base_currency': 'AUSD',
        'permissions': {'place_order': True, 'cancel_order': True, 'read_balances': True}}]})
    vault = SecretVault.open(cfg.paths.vault_enc)
    vault.put(name='perpl-token', value='opaque-test-token', kind='api_key', scope=['perpl:read', 'perpl:trade'])
    vault.put(name='perpl-seed', value=SEED.hex(), kind='private_key', scope=['perpl:read', 'perpl:trade'])
    http = FakeHTTP()
    adapter = PerplTransport(cfg, request(), http=http, vault=vault)
    # Transport tests isolate risk; a separate test uses the real RiskGate.
    monkeypatch.setattr(adapter, '_risk', lambda *a: {'decision': 'allow', 'test_only': True})
    return cfg, vault, http, adapter


def prepare_gateway(env, req=None):
    cfg, _, _, adapter = env
    req = req or request()
    gateway = FinancialGateway(cfg, adapters={'trade': adapter})
    ctx = FinancialContext('operator', frozenset({'api:all'}))
    action = gateway.prepare(ctx, req, action_key='test-order')
    approval = gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])
    assert approval['status'] == 'approval_required'
    with gateway.store.transaction() as con:
        con.execute("UPDATE approvals SET state='approved' WHERE id=?", (approval['approval_id'],))
    cfg.data['runtime']['financial_action_id'] = action['action_id']
    return gateway, ctx, action


def posts(http):
    return [c for c in http.calls if c[0] == 'POST' and c[1] == http.api+'/v1/trading/orders']


def test_official_canonical_worked_example():
    headers = signed_headers('token', SEED, 'GET', '/v1/trading/fills?count=1', timestamp=1751932800000, nonce='9Nq0Yp3kZ2c1aVb7')
    expected = b'143\nGET\n/v1/trading/fills?count=1\n1751932800000\n9Nq0Yp3kZ2c1aVb7\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    Ed25519PrivateKey.from_private_bytes(SEED).public_key().verify(base64.urlsafe_b64decode(headers['X-API-Signature']+'=='), expected)
    assert '=' not in headers['X-API-Signature']


@pytest.mark.parametrize('value,decimals,expected', [('95000', 1, 950000), ('0.1', 5, 10000), ('0.024202', 6, 24202)])
def test_official_decimal_scaling(value, decimals, expected):
    assert scaled(value, decimals) == expected


@pytest.mark.parametrize('value,decimals', [('1.5', 0), ('0.0000001', 6), ('NaN', 6), (True, 1), (str(2**64), 0)])
def test_invalid_tick_lot_and_overflow(value, decimals):
    with pytest.raises(FinancialError):
        scaled(value, decimals)


def test_real_rest_payload_and_admission_is_not_fill(env):
    _, _, http, _ = env
    gateway, ctx, action = prepare_gateway(env)
    result = gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])
    assert result['state'] == 'submitted'
    assert json.loads(posts(http)[0][3]) == {'mt': 30, 'sn': 41, 'd': [{'mkt': 10, 'acc': 7, 'lb': 0, 't': 1, 'p': 0, 's': 100, 'ms': 50, 'mnp': 0, 'fl': 4, 'lv': 200, 'rq': 41}]}
    assert result['submission']['request_id'] == 41
    gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])
    assert len(posts(http)) == 1


def test_uncertainty_survives_new_transport_never_resubmits(env):
    cfg, vault, http, _ = env
    gateway, ctx, action = prepare_gateway(env)
    http.fail_post = True
    result = gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])
    assert result['state'] == 'unconfirmed' and result['submission']['request_id'] == 41
    rebuilt = PerplTransport(cfg, request(), http=http, vault=vault)
    receipt = FinancialGateway(cfg, adapters={'trade': rebuilt}).reconcile(ctx, action['action_id'])
    assert receipt['state'] == 'needs_recovery'
    assert len(posts(http)) == 1
    old, first = rebuilt._claim(action['action_id'], 50, action['quote']['perpl_order'])
    assert first is False and old['rq'] == 41
    other, first = rebuilt._claim('next-action', 50, action['quote']['perpl_order'])
    assert first and other['rq'] == 51


@pytest.mark.parametrize('code', [400, 403, 429, 503])
def test_batch_per_order_rejection_not_200_success(env, code):
    _, _, http, _ = env
    http.post['statuses'][0]['code'] = code
    gateway, ctx, action = prepare_gateway(env)
    assert gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])['state'] == 'rejected'


def test_fill_requires_real_order_accounting(env):
    _, _, http, _ = env
    gateway, ctx, action = prepare_gateway(env)
    gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])
    http.history = [{'rq': 41, 'mkt': 10, 'acc': 7, 'oid': 9, 'st': 4, 'sr': 43, 'os': 100, 'fs': 100, 'fp': 25000, 'f': '863', 'at': {'t': int(time.time()*1000), 'txid': '0x'+'a'*64}}]
    result = gateway.reconcile(ctx, action['action_id'])
    assert result['state'] == 'confirmed'
    assert result['receipt']['settled_notional_usd'] == '2.500'
    assert result['receipt']['settled_fee_usd'] == '0.000863'


def test_missing_fill_receipt_retains_recovery(env):
    _, _, http, adapter = env
    quote = adapter.quote(request())
    http.history = [{'rq': 41, 'mkt': 10, 'acc': 7, 'st': 4, 'os': 100, 'fs': 100}]
    assert adapter.status(request(), quote, {'chain_id': 143, 'account_id': 7, 'market_id': 10, 'request_id': 41, 'operation': 'order'})['state'] == 'needs_recovery'


def test_reduce_only_and_position_size(env):
    _, _, http, adapter = env
    req = request(reduce_only=True, position_id=8)
    req['plan']['action'] = 'reduce_position'
    http.positions = [{'mkt': 10, 'acc': 7, 'pid': 8, 'st': 1, 'sd': 1, 's': 100}]
    quote = adapter.quote(req)
    assert quote['perpl_order']['t'] == 3 and float(quote['spend_usd']) < 0.01
    http.positions[0]['s'] = 99
    with pytest.raises(FinancialError, match='close_exceeds'):
        adapter.validate(req, quote)


def test_cancel_owned_order_and_official_payload(env):
    _, _, http, _ = env
    req = request(operation='cancel', order_id=9)
    req['plan']['action'] = 'cancel_executor'
    http.orders = [{'acc': 7, 'mkt': 10, 'oid': 9}]
    gateway, ctx, action = prepare_gateway(env, req)
    assert gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])['state'] == 'submitted'
    assert json.loads(posts(http)[0][3])['d'][0] == {'rq': 41, 'mkt': 10, 'acc': 7, 'oid': 9, 't': 5, 's': 0, 'fl': 0, 'lv': 0, 'lb': 0}
    http.orders = []
    http.history = [{'rq': 41, 'acc': 7, 'mkt': 10, 'oid': 9, 'st': 5, 'sr': 28, 'fs': 0, 'os': 0}]
    assert gateway.reconcile(ctx, action['action_id'])['state'] == 'confirmed'


def test_identity_scope_and_signer_fail_closed(env):
    _, _, http, adapter = env
    http.address = '0x'+'2'*40
    with pytest.raises(FinancialError, match='wallet_identity'):
        adapter.quote(request())
    http.address = ADDRESS
    http.chain = 10143
    with pytest.raises(FinancialError, match='chain_identity'):
        adapter.quote(request())
    http.chain = 143
    adapter.settings['public_key_hex'] = '00'*32
    with pytest.raises(FinancialError, match='signer_unavailable'):
        adapter.quote(request())
    assert not posts(http)


def test_direct_execute_without_gateway_denied(env):
    _, _, http, adapter = env
    quote = adapter.quote(request())
    with pytest.raises(FinancialError, match='reserved_gateway'):
        adapter.execute(request(), quote, lambda ref: None)
    assert not posts(http)


def test_real_risk_gate_is_not_skipped(env):
    cfg, vault, http, _ = env
    adapter = PerplTransport(cfg, request(), http=http, vault=vault)
    with pytest.raises(FinancialError, match='risk_gate_reject'):
        adapter.quote(request())
    assert not posts(http)


def test_component_contract_and_automatic_workspace_loading(env):
    cfg, _, _, _ = env
    spec = TRADING_COMPONENTS[0]
    assert spec.actions == ('trade',) and spec.market_types == ('linear',) and spec.cancel and spec.account_state
    spec.validate_request(request(slippage_bps=50))
    target = cfg.paths.root/'plugins'/'perpl'
    shutil.copytree('competition/monad/perpl', target, ignore=shutil.ignore_patterns('__pycache__'))
    registry = TradingComponentRegistry(cfg.paths.root)
    registry.reload(cfg)
    assert not registry.errors
    bound = registry.resolve(request())
    assert bound.spec.trade_transport == 'onchain_perpetual'
    assert bound.build(cfg, request()).component_binding['id'] == 'user:perpl:perpl'


def test_limit_tick_and_fee_tier(env):
    _, _, http, adapter = env
    req = request()
    req['plan']['entry'] = {'order_type': 'limit', 'limit_price': '0.024201', 'time_in_force': 'post_only'}
    assert adapter.quote(req)['perpl_order']['p'] == 24201
    req['plan']['entry']['limit_price'] = '0.0242011'
    with pytest.raises(FinancialError, match='off_tick'):
        adapter.quote(req)


def test_binding_rotation_blocks_existing_quote(env):
    _, vault, _, adapter = env
    req = request()
    quote = adapter.quote(req)
    vault.put(name='perpl-token', value='changed-token', kind='api_key', scope=['perpl:read', 'perpl:trade'])
    with pytest.raises(FinancialError, match='binding_changed'):
        adapter.validate(req, quote)


def test_read_only_key_cannot_send(env):
    _, _, http, adapter = env
    req = request()
    quote = adapter.quote(req)
    adapter.settings['approved_scopes'] = ['perpl:read']
    with pytest.raises(FinancialError):
        adapter.execute(req, quote, lambda ref: None)
    assert not posts(http)


def test_closed_candle_replay_costs_and_no_future_signal(tmp_path):
    import subprocess
    script = r"""
import assert from 'node:assert/strict';
import {backtest} from './competition/monad/perpl/backtest.mjs';
const first = 1700002800000;
const candles = Array.from({length: 40}, (_, i) => {const p=0.02+i*0.0001; return {time:first+i*3600000,open:p,high:p*1.01,low:p*.99,close:p};});
const data = {chainId:143,symbol:'MON',takerFeeBps:3.45,fetchedAt:new Date(first+40*3600000).toISOString(),closedHourlyCandles:candles};
const result = backtest(data);
assert.equal(result.orders[0].fillTime, first+18*3600000);
assert.equal(result.trades.length, 1);
assert(result.summary.feeCost > 0 && result.summary.slippageCost > 0);
assert(Math.abs(result.summary.netPnl-(result.summary.grossPnl-result.summary.feeCost-result.summary.slippageCost)) < 1e-8);
const future = structuredClone(data); future.closedHourlyCandles[35].high=1; future.closedHourlyCandles[35].close=.5;
assert.deepEqual(backtest(future).orders[0], result.orders[0]);
const open = structuredClone(data); open.fetchedAt=new Date(first+39*3600000).toISOString();
assert.throws(() => backtest(open), /Incomplete/);
const gap = structuredClone(data); gap.closedHourlyCandles[20].time+=3600000;
assert.throws(() => backtest(gap), /non-contiguous/);
"""
    subprocess.run(['node', '--input-type=module', '-e', script], check=True, capture_output=True)


def test_testnet_signature_and_dynamic_mon_market(env, monkeypatch):
    cfg, vault, http, _ = env
    cfg.data['plugins']['perpl']['accounts']['perpl-main']['chain_id'] = 10143
    req = request()
    req['chain'] = 'monad-testnet'
    http.api, http.rpc = NETWORKS[10143]
    http.chain = http.rpc_chain = 10143
    http.market['name'] = 'MON Perp'
    http.market['id'] = 64  # Official testnet MON differs from mainnet 10.
    adapter = PerplTransport(cfg, req, http=http, vault=vault)
    monkeypatch.setattr(adapter, '_risk', lambda *a: {'decision': 'allow'})
    testenv = (cfg, vault, http, adapter)
    gateway, ctx, action = prepare_gateway(testenv, req)
    result = gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])
    assert result['state'] == 'submitted' and result['submission']['chain_id'] == 10143
    payload = json.loads(posts(http)[0][3])
    assert payload['d'][0]['mkt'] == 64
    assert posts(http)[0][1].startswith('https://testnet.perpl.xyz/api/')


def test_api_rpc_mismatch_denied(env):
    cfg, vault, http, _ = env
    cfg.data['plugins']['perpl']['accounts']['perpl-main'].update(chain_id=10143, api_url=API)
    req = request(); req['chain'] = 'monad-testnet'
    with pytest.raises(FinancialError, match='configured_network_mismatch'):
        PerplTransport(cfg, req, http=http, vault=vault)
    cfg.data['plugins']['perpl']['accounts']['perpl-main'].pop('api_url')
    adapter = PerplTransport(cfg, req, http=http, vault=vault)
    http.api, http.rpc = NETWORKS[10143]; http.chain = 10143
    with pytest.raises(FinancialError, match='chain_identity_mismatch'):
        adapter._context(req)  # RPC still returning mainnet 143.


def test_testnet_enrollment_official_schema_and_proof(env):
    from pathlib import Path
    from eth_account import Account
    from eth_utils import keccak
    from competition.monad.perpl.setup import enroll_testnet, validate_enrollment
    cfg, vault, _, _ = env
    wallet = Account.from_key(bytes([42])*32)  # ephemeral offline signer, not parent wallet
    now = int(time.time()*1000)
    expires = now+86400000
    schema = json.loads(Path('competition/monad/perpl/enrollment-schema.json').read_text())
    class EnrollmentHTTP:
        def __init__(self): self.posts=[]
        def request(self, method, url, *, headers, body):
            data=json.loads(body); self.posts.append(data)
            assert headers['Origin']=='https://testnet.perpl.xyz'
            if url.endswith('/payload'):
                typed=deepcopy(schema)
                typed['message'].update(signer=wallet.address,publicKey=base64.urlsafe_b64encode(bytes.fromhex(PUBLIC)).decode().rstrip('='),label=data['label'],expiresAt=str(expires),time=hex(now))
                self.typed=typed
                return 200, {'typed_data':typed,'mac':'server-test-mac'}
            assert data['typed_data']==self.typed and data['mac']=='server-test-mac'
            message=validate_enrollment(self.typed,address=wallet.address,public_key='0x'+PUBLIC,scope_mask=3,expires_at=expires,label='offline test')
            assert Account.recover_message(message,signature=data['signature'])==wallet.address
            Ed25519PrivateKey.from_private_bytes(SEED).public_key().verify(bytes.fromhex(data['pop_signature'][2:]),keccak(b'\x19'+message.version+message.header+message.body))
            return 200, {'api_key':{'api_key':'new-opaque-token','address':wallet.address,'scope_mask':3,'expires_at':expires}}
    http=EnrollmentHTTP()
    result=enroll_testnet(vault=vault,seed_ref='vault://perpl-seed',token_name='new-token',address=wallet.address,
                         wallet_sign=lambda message:'0x'+wallet.sign_message(message).signature.hex(),label='offline test',
                         expires_at=expires,operator_authorized=True,http=http)
    assert result['api_key_ref']=='vault://new-token' and 'new-opaque-token' not in json.dumps(result)
    assert vault.resolve('new-token',required_scope='perpl:trade')=='new-opaque-token'
    for key,value in [('scope','1'),('signer','0x'+'9'*40),('publicKey','bad'),('builderId','1')]:
        bad=deepcopy(http.typed); bad['message'][key]=value
        with pytest.raises(FinancialError):
            validate_enrollment(bad,address=wallet.address,public_key=PUBLIC,scope_mask=3,expires_at=expires,label='offline test')
    for key,value in [('chainId','0x8f'),('verifyingContract','0x'+'9'*40)]:
        bad=deepcopy(http.typed); bad['domain'][key]=value
        with pytest.raises(FinancialError):
            validate_enrollment(bad,address=wallet.address,public_key=PUBLIC,scope_mask=3,expires_at=expires,label='offline test')


def test_profile_bootstrap_and_forwarding_abi(env):
    from datetime import datetime, timezone
    from eth_account import Account
    from eth_abi import encode
    from competition.monad.perpl.setup import connect_testnet_profile, read_testnet_account, forwarding_calldata, EXCHANGE, RPC as TEST_RPC
    wallet=Account.from_key(bytes([43])*32)
    now=int(time.time()*1000)
    iso=datetime.fromtimestamp(now/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    message=('testnet.perpl.xyz wants you to sign in with your Ethereum account:\n'+wallet.address+
      '\n\nBy signing this message I acknowledge the risks of using this protocol and accept the terms of use\n\nURI: ://\nVersion: 1\nChain ID: 10143\nNonce: test-profile-nonce-123456789\nIssued At: '+iso+
      '\nResources:\n- ://\n- https://perpl.xyz/terms-of-use\n- https://perpl.xyz/privacy-policy')
    class ProfileHTTP:
        def __init__(self):self.calls=[];self.message=message
        def request(self,method,url,*,headers,body):
            data=json.loads(body);self.calls.append((url,data))
            if url.endswith('/auth/payload'):
                assert data=={'chain_id':10143,'address':wallet.address}
                return 200,{'message':self.message,'nonce':'test-profile-nonce-123456789','issued_at':now,'mac':'test-mac'}
            assert url.endswith('/auth/connect') and data['chain_id']==10143
            from eth_account.messages import encode_defunct
            assert Account.recover_message(encode_defunct(text=data['message']),signature=data['signature'])==wallet.address
            return 200, {'nonce':'never-return-this-session','expires_at':now+86400000}
    http=ProfileHTTP()
    with pytest.raises(FinancialError,match='terms_acknowledgment'):
        connect_testnet_profile(address=wallet.address,wallet_sign=lambda _:None,operator_authorized=True,http=http)
    assert not http.calls
    result=connect_testnet_profile(address=wallet.address,wallet_sign=lambda m:'0x'+wallet.sign_message(m).signature.hex(),terms_acknowledged=True,operator_authorized=True,http=http)
    assert result['status']=='profile_connected' and 'never-return-this-session' not in json.dumps(result)
    http.message=message.replace('Chain ID: 10143','Chain ID: 143')
    with pytest.raises(FinancialError,match='identity_or_terms'):
        connect_testnet_profile(address=wallet.address,wallet_sign=lambda _:None,terms_acknowledged=True,operator_authorized=True,http=http)
    class RpcHTTP:
        def request(self,method,url,*,headers,body):
            assert url==TEST_RPC
            data=json.loads(body)
            if data['method']=='eth_chainId':return 200,{'result':'0x279f'}
            assert data['method']=='eth_call' and data['params'][0]['to']==EXCHANGE
            return 200,{'result':'0x'+encode(['(uint256,uint256,uint256,uint8,address,(uint256,uint256,uint256,uint256))'],[(127,100000000,0,0,wallet.address,(0,0,0,0))]).hex()}
    assert read_testnet_account(wallet.address,http=RpcHTTP())['account_id']==127
    tx=forwarding_calldata()
    assert tx['to']==EXCHANGE and tx['chain_id']==10143 and len(tx['data'])==74
    from eth_utils import keccak
    assert tx['data']== '0x'+(keccak(text='allowOrderForwarding(bool)')[:4]+encode(['bool'],[True])).hex()


def test_enroll_404_reports_profile_prerequisite(env):
    from pathlib import Path
    from eth_account import Account
    from competition.monad.perpl.setup import enroll_testnet
    _,vault,_,_=env
    wallet=Account.from_key(bytes([44])*32);now=int(time.time()*1000);expires=now+86400000
    schema=json.loads(Path('competition/monad/perpl/enrollment-schema.json').read_text())
    class MissingProfileHTTP:
        def request(self,method,url,*,headers,body):
            if url.endswith('/enroll'):return 404,{}
            data=json.loads(body); typed=deepcopy(schema)
            typed['message'].update(signer=wallet.address,publicKey=base64.urlsafe_b64encode(bytes.fromhex(PUBLIC)).decode().rstrip('='),label=data['label'],expiresAt=str(expires),time=hex(now))
            return 200,{'typed_data':typed,'mac':'test'}
    with pytest.raises(FinancialError,match='target_profile_not_found_connect_wallet_first'):
        enroll_testnet(vault=vault,seed_ref='vault://perpl-seed',token_name='must-not-exist',address=wallet.address,
           wallet_sign=lambda m:'0x'+wallet.sign_message(m).signature.hex(),expires_at=expires,operator_authorized=True,http=MissingProfileHTTP())
    assert 'must-not-exist' not in [m.name for m in vault.list()]


def test_actual_absent_account_revert_is_not_rpc_outage():
    from eth_abi import encode
    from competition.monad.perpl.setup import read_testnet_account
    class AbsentHTTP:
        def request(self,method,url,*,headers,body):
            if json.loads(body)['method']=='eth_chainId':return 200,{'result':'0x279f'}
            return 200,{'error':{'code':3,'message':'execution reverted','data':'0x03a0e277'+encode(['address'],[ADDRESS]).hex()}}
    result=read_testnet_account(ADDRESS,http=AbsentHTTP())
    assert result['exists'] is False and result['account_id'] is None and result['reason']=='AccountDoesNotExist(address)'


def test_actual_risk_receives_live_envelope_and_preserves_escalation(env,monkeypatch):
    from nerya.trading.risk import RiskDecision
    cfg,vault,http,_=env
    adapter=PerplTransport(cfg,request(),http=http,vault=vault)
    req=request();req['plan']['plan_id']='real-plan-id';req['plan']['source']='operator'
    seen={}
    def evaluate(self,intent,**kwargs):
        seen.update(kwargs)
        assert intent.meta['leverage']==2
        return RiskDecision(intent.intent_id,'escalate',['operator_agent_trade_approval_required'])
    monkeypatch.setattr('nerya.trading.risk.RiskGate.evaluate',evaluate)
    result=adapter._risk(req,http.market)
    assert result['decision']=='escalate' and seen['market_snapshot']['_envelope']['mode']=='live'
    assert seen['market_snapshot']['_envelope']['chain_id']==143


def test_quote_never_outlives_30_second_evidence(env):
    _,_,http,adapter=env
    http.market['state']['at']['t']=int((time.time()-12)*1000)
    quote=adapter.quote(request())
    assert quote['expires_at']<=http.market['state']['at']['t']/1000+30
    assert quote['expires_at']<=time.time()+30


def test_parent_process_authorization_is_testnet_only_and_does_not_write(tmp_path):
    import importlib.util
    from pathlib import Path
    path=Path('/Users/rick/Documents/Project/Nerya/pitch-video/monad-metropolis-20261013/v3/perpl-gateway.py')
    if not path.exists():pytest.skip('local parent harness not included in portable repository')
    spec=importlib.util.spec_from_file_location('perpl_parent_test',path)
    parent=importlib.util.module_from_spec(spec);spec.loader.exec_module(parent)
    data=deepcopy(DEFAULT_CONFIG)
    data['plugins']['perpl']={'accounts':{parent.ACCOUNT:{'chain_id':10143,'api_url':'https://testnet.perpl.xyz/api',
        'rpc_url':'https://testnet-rpc.monad.xyz','wallet_address':parent.ADDRESS}}}
    cfg=Config(WorkspacePaths(parent.WORKSPACE),data)
    req=parent.request('open',100)
    before=cfg.paths.config.read_bytes() if cfg.paths.config.exists() else None
    cron=deepcopy(cfg.data.get('triggers'))
    parent.authorize_testnet_process(cfg,req)
    assert cfg.live_trading_enabled() and cfg.data.get('triggers')==cron
    assert (cfg.paths.config.read_bytes() if cfg.paths.config.exists() else None)==before
    bad=deepcopy(req);bad['chain']='monad'
    with pytest.raises(FinancialError,match='scope_mismatch'):parent.authorize_testnet_process(cfg,bad)
    bad=deepcopy(req);bad['plan']['meta']['leverage']=2
    with pytest.raises(FinancialError,match='plan_mismatch'):parent.authorize_testnet_process(cfg,bad)


def test_parent_stable_plan_id_and_existing_refresh_contract(tmp_path,monkeypatch):
    import importlib.util
    from pathlib import Path
    path=Path('/Users/rick/Documents/Project/Nerya/pitch-video/monad-metropolis-20261013/v3/perpl-gateway.py')
    if not path.exists():pytest.skip('local parent harness not shipped with portable repo')
    spec=importlib.util.spec_from_file_location('perpl_refresh_test',path)
    parent=importlib.util.module_from_spec(spec);spec.loader.exec_module(parent)
    assert parent.request('open',100,run_id='same')==parent.request('open',100,run_id='same')
    assert parent.request('open',100,run_id='other')['plan']['plan_id']!=parent.request('open',100,run_id='same')['plan']['plan_id']
    req=parent.request('open',100,run_id='same')
    old={'action_id':'existing-identity','request':req,'revision':3,'state':'prepared','submission':None}
    changed={**old,'revision':4,'quote_hash':'new-hash','quote':{'risk_usd':'2.5','perpl_order':{'lv':100}}}
    calls=[]
    class Gateway:
        class Store:
            def get_action(self,aid,context,internal):
                assert aid=='existing-identity' and internal
                return changed
        store=Store()
        def refresh(self,context,aid,*,expected_revision):
            calls.append((aid,expected_revision));return {'action_id':aid}
    monkeypatch.setattr(parent,'authorize_testnet_process',lambda cfg,r:None)
    monkeypatch.setattr(parent,'refresh_snapshot',lambda cfg,r:None)
    monkeypatch.setattr(parent,'save_summary',lambda row:None)
    result=parent.refresh_existing(None,Gateway(),old,expected_revision=3)
    assert result['action_id']=='existing-identity' and result['revision']==4 and calls==[('existing-identity',3)]
    with pytest.raises(FinancialError,match='revision_conflict'):
        parent.refresh_existing(None,Gateway(),old,expected_revision=2)
    with pytest.raises(FinancialError,match='immutable'):
        parent.refresh_existing(None,Gateway(),{**old,'submission':{'request_id':41}},expected_revision=3)


def test_market_move_within_original_slippage_budget_not_double_buffered(env):
    _,_,http,adapter=env
    req=request(slippage_bps=50);quote=adapter.quote(req)
    assert quote['execution_price_ceiling_scaled']==25126
    assert quote['perpl_order']['p']==0 and quote['raw_order']['p']==0
    http.market['state'].update(mrk=25090,ask=25100,bid=25080)
    adapter.validate(req,quote)
    http.market['state'].update(mrk=25127,ask=25128,bid=25126)
    with pytest.raises(FinancialError,match='current_cost_exceeds_frozen_quote'):
        adapter.validate(req,quote)


def test_fee_change_still_rejected_inside_price_budget(env):
    _,_,http,adapter=env
    req=request();quote=adapter.quote(req)
    http.market['config']['taker_fees']=[500]
    with pytest.raises(FinancialError,match='current_cost_exceeds_frozen_quote'):
        adapter.validate(req,quote)


def test_negative_pnl_collateral_explicit_bounded_and_reserved(env):
    from decimal import Decimal
    _,_,http,adapter=env
    strict=adapter.quote(request())
    assert strict['perpl_order']['mnp']==0
    req=request(max_negative_pnl_bps=100)
    quote=adapter.quote(req)
    assert quote['perpl_order']['mnp']==100
    extra=Decimal(quote['risk_usd'])*Decimal('0.01')
    assert Decimal(quote['negative_pnl_collateral_budget_usd'])==extra
    assert Decimal(quote['spend_usd'])==Decimal(strict['spend_usd'])+extra
    adapter.validate(req,quote)
    with pytest.raises(FinancialError):
        adapter.quote(request(max_negative_pnl_bps=101))
    http.market['order_max_neg_pnl_collat_bps']=50
    with pytest.raises(FinancialError,match='negative_pnl_budget_out_of_range'):
        adapter.quote(req)


def test_actual_sr44_fr8_receipt_remains_rejected(env):
    _,_,http,adapter=env
    req=request();quote=adapter.quote(req)
    http.history=[{'rq':1,'acc':7,'mkt':10,'oid':4632599527425,'st':7,'sr':44,'fr':8,'fs':0,'os':100,'mnp':0}]
    receipt=adapter.status(req,quote,{'chain_id':143,'account_id':7,'market_id':10,'request_id':1,'operation':'order'})
    assert receipt['state']=='rejected' and receipt['provider_failure']==8
    assert receipt['provider_failure_name']=='ExceedsMaxNegPnlCollat'
    assert not posts(http)


def test_executed_market_wire_preserves_frozen_ms_and_negative_pnl(env):
    _,_,http,_=env
    req=request(slippage_bps=50,max_negative_pnl_bps=100)
    gateway,ctx,action=prepare_gateway(env,req)
    gateway.execute(ctx,action['action_id'],quote_hash=action['quote_hash'])
    wire=json.loads(posts(http)[0][3])['d'][0]
    assert wire['p']==0 and wire['fl']==4 and wire['ms']==50 and wire['mnp']==100 and wire['lv']==200
    assert float(action['quote']['negative_pnl_collateral_budget_usd'])>0
    assert len(posts(http))==1


@pytest.mark.parametrize('fee,price,filled,state,expected', [
    ('400',25000,40,5,'confirmed'),
    ('1000000',25000,100,4,'needs_recovery'),
    ('863',26000,100,4,'needs_recovery'),
])
def test_terminal_partial_and_actual_budget_breach(env,fee,price,filled,state,expected):
    _,_,http,adapter=env
    req=request(max_negative_pnl_bps=100);quote=adapter.quote(req)
    http.history=[{'rq':41,'mkt':10,'acc':7,'oid':9,'st':state,'sr':16 if state==5 else 43,
        'os':100,'fs':filled,'fp':price,'f':fee,'at':{'t':int(time.time()*1000),'txid':'0x'+'a'*64}}]
    receipt=adapter.status(req,quote,{'chain_id':143,'account_id':7,'market_id':10,'request_id':41,'operation':'order'})
    assert receipt['state']==expected and receipt['execution_confirmed'] is True
    assert receipt['filled_size']==str(filled)
    assert receipt['partial']==(filled<100)
    assert receipt['operator_action_required']==(expected=='needs_recovery')
    if expected=='needs_recovery':
        assert receipt['budget_breaches'] and receipt['reason']=='actual_execution_exceeds_frozen_budget'
    assert not posts(http)


def test_budget_breach_keeps_gateway_reservation(env):
    _,_,http,_=env
    gateway,ctx,action=prepare_gateway(env)
    gateway.execute(ctx,action['action_id'],quote_hash=action['quote_hash'])
    http.history=[{'rq':41,'mkt':10,'acc':7,'oid':9,'st':4,'sr':43,'os':100,'fs':100,'fp':25000,
        'f':'1000000','at':{'t':int(time.time()*1000),'txid':'0x'+'a'*64}}]
    result=gateway.reconcile(ctx,action['action_id'])
    assert result['state']=='needs_recovery' and result['receipt']['execution_confirmed']
    with gateway.store.transaction() as con:
        row=con.execute('SELECT state FROM capital_reservations WHERE reservation_id=?',('financial:'+action['action_id'],)).fetchone()
    assert row['state']=='reserved'
    gateway.execute(ctx,action['action_id'],quote_hash=action['quote_hash'])
    assert len(posts(http))==1


def test_bare_txid_normalization_and_receipt_query(env):
    from competition.monad.perpl.trading import transaction_hash
    _,_,http,adapter=env
    for value in ['a'*64,'0x'+'A'*64]:assert transaction_hash(value)=='0x'+'a'*64
    for value in ['a'*63,'g'*64,'***REDACTED***',None]:
        with pytest.raises(FinancialError):transaction_hash(value)
    req=request();quote=adapter.quote(req)
    http.history=[{'rq':41,'mkt':10,'acc':7,'oid':9,'st':4,'sr':43,'t':1,'lv':200,'fl':4,
        'os':100,'fs':100,'fp':25000,'f':'863','at':{'t':int(time.time()*1000),'txid':'a'*64,'b':100,'l':4}}]
    ref={'chain_id':143,'account_id':7,'market_id':10,'request_id':41,'operation':'order'}
    receipt=adapter.status(req,quote,ref)
    assert receipt['state']=='confirmed' and receipt['transaction_hash']=='0x'+'a'*64
    rpc=[json.loads(c[3]) for c in http.calls if c[1]==RPC and json.loads(c[3]).get('method')=='eth_getTransactionReceipt']
    assert rpc[-1]['params']==['0x'+'a'*64]


def test_explicit_original_fill_remediation_preserves_identity(env):
    from competition.monad.perpl.recovery import verify_original_fill
    _,_,http,adapter=env
    req=request();quote=adapter.quote(req)
    quote['component_binding']={'id':'user:perpl:perpl','revision':'original-immutable'}
    http.history=[{'rq':41,'mkt':10,'acc':7,'oid':9,'st':4,'sr':43,'t':1,'lv':200,'fl':4,
        'os':100,'fs':100,'fp':25000,'f':'863','at':{'t':int(time.time()*1000),'txid':'a'*64,'b':100,'l':4}}]
    original=http.request
    def request_http(method,url,*,headers=None,body=b''):
        if url==RPC and json.loads(body).get('method')=='eth_getTransactionReceipt':
            return 200,{'result':{'status':'0x1','transactionHash':'0x'+'a'*64,'blockNumber':'0x64','to':ADDRESS,'transactionIndex':'0x0',
                                'logs':[{'logIndex':'0x5','transactionHash':'0x'+'a'*64,'address':ADDRESS,
                                  'topics':['0xb585865280072cb0f2cb5cf0f49f0e0224c09fa012c92c5f33eabf10287b8beb'],
                                  'data':'0x'+''.join(format(v,'064x') for v in [10,7,41,0,0,25000,100])}]}}
        return original(method,url,headers=headers,body=body)
    http.request=request_http
    ref={'chain_id':143,'account_id':7,'market_id':10,'request_id':41,'operation':'order'}
    proof=verify_original_fill(adapter,req,quote,ref)
    assert proof['execution_confirmed'] and not proof['gateway_state_changed']
    assert proof['settled_fee_usd']=='0.000863' and proof['original_component_binding']==quote['component_binding']
    assert not posts(http)
    bad=deepcopy(ref);bad['request_id']=42
    with pytest.raises(FinancialError,match='not_found'):verify_original_fill(adapter,req,quote,bad)


def test_parent_position_history_owns_link_when_snapshot_rq_zero(monkeypatch):
    import importlib.util
    from pathlib import Path
    path=Path('/Users/rick/Documents/Project/Nerya/pitch-video/monad-metropolis-20261013/v3/perpl-gateway.py')
    if not path.exists():pytest.skip('parent harness unavailable')
    sp=importlib.util.spec_from_file_location('p_history',path);parent=importlib.util.module_from_spec(sp);sp.loader.exec_module(parent)
    import competition.monad.perpl.recovery as recovery
    proof={'request_id':2,'order_id':900,'transaction_hash':'0x'+'a'*64,'block_number':100,
           'order':{'fs':100,'fp':2419,'lv':100,'at':{'tx':3}}}
    monkeypatch.setattr(recovery,'verify_original_fill',lambda *a:proof)
    position={'pid':901,'acc':7,'mkt':64,'rq':0,'oid':0,'st':1,'sd':1,'s':100,'ep':2419,'lv':100,'c':'2421000','fee':'835','ots':{'b':100,'tx':3}}
    event={**position,'sr':21,'rq':2,'oid':900,'at':{'b':100,'tx':3,'t':1000,'txid':'a'*64}}
    class Adapter:
        def _request(self,*args,**kwargs):return {'d':[event],'np':''}
    class Gateway:
        def adapter(self,*a,**k):return Adapter()
    opened={'request':{},'quote':{},'submission':{'account_id':7,'market_id':64}}
    result=parent.verify_demo_position_owner(None,Gateway(),opened,position)
    assert result['position_id']==901 and result['request_id']==2
    bad={**position,'s':101}
    with pytest.raises(FinancialError,match='changed_from_verified_open'):
        parent.verify_demo_position_owner(None,Gateway(),opened,bad)
    event['rq']=3
    with pytest.raises(FinancialError,match='history_link'):
        parent.verify_demo_position_owner(None,Gateway(),opened,position)


def test_trusted_strategy_producer_overrides_only_after_context_validation(env,monkeypatch):
    from nerya.financial.contracts import FinancialContext
    from nerya.trading.risk import RiskDecision
    cfg,vault,http,_=env
    req=request();req['plan']['source']='operator';req['plan']['meta']['original_source']='operator'
    cfg.data['runtime']['strategy_financial_run_id']='test-live-run'
    context=FinancialContext('strategy:mon-breakout',frozenset({'write:trade'}),task_kind='strategy_script',task_id='mon-breakout',strategy_run_id='test-live-run')
    monkeypatch.setattr('nerya.financial.contracts.context_from_config',lambda *a:context)
    calls=[]
    monkeypatch.setattr('nerya.financial.strategy_runtime.validate_context',lambda *a,**k:calls.append('validated'))
    def risk(self,intent,**kwargs):
        assert intent.source=='strategy_runtime' and 'original_source' not in intent.meta
        return RiskDecision(intent.intent_id,'allow',['ok'])
    monkeypatch.setattr('nerya.trading.risk.RiskGate.evaluate',risk)
    adapter=PerplTransport(cfg,req,http=http,vault=vault)
    assert adapter._risk(req,http.market)['decision']=='allow' and calls==['validated']
    cfg.data['runtime'].pop('strategy_financial_run_id')
    assert adapter._producer_source(req)=='operator'
    cfg.data['runtime']['strategy_financial_run_id']='test-live-run'
    def denied(*a,**k):raise FinancialError('strategy_financial_run_not_active',403)
    monkeypatch.setattr('nerya.financial.strategy_runtime.validate_context',denied)
    with pytest.raises(FinancialError,match='run_not_active'):adapter._producer_source(req)


def test_pre_claim_denial_is_failed_before_submission(env,monkeypatch):
    cfg,_,http,adapter=env
    gateway,ctx,action=prepare_gateway(env)
    monkeypatch.setattr(adapter,'_risk',lambda *a:{'decision':'escalate'})
    # First execute already obtained approval; simulate the explicit missing approval
    # transport gate after quote without making any network order request.
    def reject(*a,**k):raise FinancialError('perpl_strategy_producer_mismatch',403)
    monkeypatch.setattr(adapter,'_execute',reject)
    result=gateway.execute(ctx,action['action_id'],quote_hash=action['quote_hash'])
    assert result['state']=='failed_before_submission' and not result['submission']
    assert not posts(http)


def test_strategy_source_uses_real_lease_validation(env):
    from nerya.financial.strategy_runtime import bind_run,finish_run
    cfg,vault,http,_=env
    strategy=cfg.paths.strategy('mon-breakout');strategy.mkdir(parents=True)
    yaml_io.dump(strategy/'strategy.yml',{'id':'mon-breakout','status':'live','account_id':'perpl-main','markets':['PERPL:MON/AUSD']})
    bound,context=bind_run(cfg,strategy_id='mon-breakout',run_id='source-real-lease',mode='live',max_run_seconds=60)
    adapter=PerplTransport(bound,request(),http=http,vault=vault)
    assert adapter._producer_source(request())=='strategy_runtime'
    finish_run(bound,context.strategy_run_id)
    with pytest.raises(FinancialError,match='strategy_financial_run_not_active'):
        adapter._producer_source(request())

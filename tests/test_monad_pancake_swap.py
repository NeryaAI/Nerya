"""Real adapter + gateway with fake RPC and published test key; no broadcast."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import time

import pytest
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core.errors import TradingError
from nerya.financial.contracts import FinancialContext, FinancialError
from nerya.financial.defi.calls import address, topic_address
from nerya.financial.defi.pancake_swap import (PancakeSwapTransport, SWAP_SCHEMA, FACTORY,
    ROUTER, QUOTER, SWAP_TUPLE, QUOTE_TUPLE, SWAP_EVENT, UNI_SWAP_EVENT)
from nerya.financial.gateway import FinancialGateway
from nerya.trading.components import BoundComponent, TradingComponentSpec

pytestmark = pytest.mark.smoke
KEY = '0x' + '07' * 32  # Published deterministic test vector, not a configured key.
SENDER = Account.from_key(KEY).address
WMON = address('0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A')
CHOG = address('0x350035555E10d9AfAF1566AaebfCeD5BA6C27777')
POOL = address('0x57390310FE6542dC2Ae696FfE5a6f56B84fC4229')
FEED = address('0x' + '9' * 40)
CODE = '0x6001'
CODE_HASH = '0x' + keccak(bytes.fromhex(CODE[2:])).hex()
BLOCK_HASH = '0x' + 'a' * 64
TX_HASH = '0x' + 'b' * 64
COMPONENT = 'builtin:monad_pancake_swap'


def request(**changes):
    return {'kind': 'swap', 'component_id': COMPONENT, 'wallet_id': 'wallet',
        'chain': 'monad', 'protocol': 'pancakeswap_v3', 'asset': WMON, 'amount': '10',
        'slippage_bps': 100, 'parameters': {'pool_id': 'chog_wmon', 'token_out': CHOG}, **changes}


class Rpc:
    def __init__(self):
        self.chain_id = 143
        self.allowance = 10**20
        self.balance = 10**21
        self.native = 10**18
        self.output = 20 * 10**18
        self.price = 2500000
        self.gas_price = 10**9
        self.age = 0
        self.feed_age = 0
        self.pool = POOL
        self.code_overrides = {}
        self.calls = []
        self.sent = []
        self.receipt = None
        self.transaction = None
        self.block_hash = BLOCK_HASH
        self.after_callback = None
        self.decimals = 18
        self.factory = FACTORY
        self.router = ROUTER
        self.quoter = QUOTER
        self.protocol_fee = 65535

    def get_erc20_decimals(self, token):
        return self.decimals

    def get_block_number(self):
        return 102

    def _rpc(self, method, args):
        self.calls.append((method, args))
        if method == 'eth_chainId': return hex(self.chain_id)
        if method == 'eth_getCode': return self.code_overrides.get(args[0].lower(), CODE)
        if method == 'eth_gasPrice': return hex(self.gas_price)
        if method == 'eth_estimateGas':
            # Same behavior as a real router lacking allowance.
            if self.allowance < 10**19: raise AssertionError('gas estimation before finite approval')
            return hex(180000)
        if method == 'eth_getBalance': return hex(self.native)
        if method == 'eth_getBlockByNumber': return {'hash': self.block_hash, 'number': '0x64',
            'timestamp': hex(int(time.time()) - self.age)}
        if method == 'eth_getTransactionReceipt': return self.receipt
        if method == 'eth_getTransactionByHash': return self.transaction
        if method == 'eth_call':
            selector, target = args[0]['data'][:10], args[0]['to'].lower()
            for sig, typ, values in [
                ('getPool(address,address,uint24)', ['address'], [self.pool]),
                ('factory()', ['address'], [self.factory]),
                ('WETH9()', ['address'], [WMON]),
                ('token0()', ['address'], [CHOG]), ('token1()', ['address'], [WMON]),
                ('fee()', ['uint24'], [10000]), ('liquidity()', ['uint128'], [10**22]),
                ('slot0()', ['uint160', 'int24', 'uint16', 'uint16', 'uint16', 'uint32', 'bool'],
                    [2**96, 0, 0, 0, 0, self.protocol_fee, True]),
                ('decimals()', ['uint8'], [8 if target == FEED.lower() else self.decimals]),
                ('balanceOf(address)', ['uint256'], [self.balance]),
                ('allowance(address,address)', ['uint256'], [self.allowance]),
                ('latestRoundData()', ['uint80', 'int256', 'uint256', 'uint256', 'uint80'],
                    [1, self.price, 1, int(time.time()) - self.feed_age, 1]),
                ('quoteExactInputSingle(' + QUOTE_TUPLE + ')', ['uint256', 'uint160', 'uint32', 'uint256'],
                    [self.output, 2**96, 3, 160000]),
            ]:
                if selector == '0x' + keccak(text=sig)[:4].hex():
                    if sig.startswith('quoteExact'):
                        values_in = decode([QUOTE_TUPLE], bytes.fromhex(args[0]['data'][10:]))[0]
                        assert values_in == (WMON.lower(), CHOG.lower(), 10**19, 10000, 0)
                        assert target == self.quoter.lower() and args[1] in ('0x64', 'latest')
                    return '0x' + encode(typ, values).hex()
        raise AssertionError((method, args))

    def send_raw_transaction(self, **kw):
        # Uses the actual EvmFunds owned signer path, but never sends to a network.
        assert Account.from_key(kw['signer_private_key']).address == SENDER
        assert kw['to'].lower() == self.router.lower() and kw['value'] == 0 and not kw['confirm']
        self.sent.append(kw['data'])
        kw['on_broadcast']({'tx_hash': TX_HASH, 'nonce': 1})
        if self.after_callback: self.after_callback()
        return {'tx_hash': TX_HASH}


def setup(tmp_path, monkeypatch):
    data = deepcopy(DEFAULT_CONFIG)
    data['runtime']['live_trading_enabled'] = True
    contracts = {'factory': FACTORY, 'router': ROUTER, 'quoter': QUOTER, 'mon_feed': FEED}
    deployment = {'reviewed': True, 'contracts': contracts,
        'code_hashes': {key: CODE_HASH for key in contracts},
        'pools': {'chog_wmon': {'reviewed': True, 'address': POOL, 'token0': CHOG, 'token1': WMON,
            'fee': 10000, 'tick_spacing': 200, 'code_hash': CODE_HASH}},
        'tokens': {token.lower(): {'reviewed': True, 'code_hash': CODE_HASH,
            'transfer_behavior': 'standard', 'decimals': 18} for token in (CHOG, WMON)},
        'price_feeds': {WMON.lower(): {'reviewed': True, 'address': FEED, 'heartbeat_seconds': 3600}}}
    data['financial'] = {'enabled': True, 'wallet_permissions': {'wallet': {'swap': True}},
        'defi': {'max_gas_limit': 500000, 'deployments': {'monad': {'pancakeswap_v3': deployment}}}}
    raw = {'address': SENDER, 'signer_ref': 'vault://fake-test-key', 'native_symbol': 'MON',
           'rpc_urls': {'monad': 'https://rpc.invalid'}, 'token_symbols': {WMON: 'MON', CHOG: 'CHOG'}}
    data['wallet'] = {'providers': {'wallet': {'provider': 'self_custody', 'config': raw}}}
    cfg = Config(WorkspacePaths(tmp_path), data)
    rpc = Rpc()
    provider = SimpleNamespace(id='self_custody', _resolve_signer_key=lambda: KEY)
    factory = lambda c, r: PancakeSwapTransport(c, r, connector=rpc, provider=provider, wallet_config=raw)
    # Register only inside the test process. Parent owns source registration.
    import nerya.trading.components as components
    original = components._builtin_components
    def registered():
        rows = original()
        rows[COMPONENT] = BoundComponent(COMPONENT, 'c' * 64,
            TradingComponentSpec('monad_pancake_swap', '1', ('swap',), ('spot',), factory, parameters_schema=SWAP_SCHEMA))
        return rows
    monkeypatch.setattr(components, '_builtin_components', registered)
    def forbid_ticker(_):
        raise AssertionError('gas must use reviewed on-chain MON feed')
    monkeypatch.setattr('nerya.financial.defi.transport.usd_price', forbid_ticker)
    return cfg, rpc, factory(cfg, request()), deployment


def receipt(rpc, quote, output=None):
    output = output or 199 * 10**17  # Actual output differs from quoted 20 tokens.
    def event(emitter, signature, topics, types, values, index):
        return {'address': emitter, 'topics': ['0x' + keccak(text=signature).hex(), *topics],
            'data': '0x' + encode(types, values).hex(), 'logIndex': hex(index),
            'transactionHash': TX_HASH, 'blockHash': BLOCK_HASH, 'removed': False}
    rpc.receipt = {'status': '0x1', 'transactionHash': TX_HASH, 'blockNumber': '0x64',
        'blockHash': BLOCK_HASH, 'gasUsed': hex(150000), 'effectiveGasPrice': hex(10**9), 'logs': [
            event(WMON, 'Transfer(address,address,uint256)', [topic_address(SENDER), topic_address(POOL)], ['uint256'], [10**19], 0),
            event(CHOG, 'Transfer(address,address,uint256)', [topic_address(POOL), topic_address(SENDER)], ['uint256'], [output], 1),
            event(POOL, SWAP_EVENT, [topic_address(ROUTER), topic_address(SENDER)],
                ['int256', 'int256', 'uint160', 'uint128', 'int24', 'uint128', 'uint128'],
                [-output, 10**19, 2**96, 10**22, 0, 0, 10**16], 2)]}
    rpc.transaction = {'hash': TX_HASH, 'from': SENDER, 'to': ROUTER, 'value': '0x0',
                       'input': quote['transaction']['data']}


def test_real_quoter_and_deadline_router_abi(tmp_path, monkeypatch):
    cfg, rpc, adapter, _ = setup(tmp_path, monkeypatch)
    quote = adapter.quote(request())
    assert quote['risk_usd'] == '0.250' and quote['chain_id'] == 143
    assert quote['minimum_out_base'] == str(198 * 10**17)
    assert quote['transaction']['data'][:10] == '0x414bf389'
    wire = decode([SWAP_TUPLE], bytes.fromhex(quote['transaction']['data'][10:]))[0]
    assert wire == (WMON.lower(), CHOG.lower(), 10000, SENDER.lower(),
                    quote['deadline'], 10**19, 198 * 10**17, 0)
    adapter.validate(request(), quote)
    assert len([c for c in rpc.calls if c[0] == 'eth_call' and c[1][0]['to'].lower() == QUOTER.lower()]) == 2
    assert not rpc.sent


def test_protocol_expired_before_signing_returns_terminal_without_submission(tmp_path,monkeypatch):
    cfg,rpc,adapter,_=setup(tmp_path,monkeypatch)
    quote=adapter.quote(request())
    quote['expires_at']=time.time()-1
    cfg.data['runtime']['financial_action_id']='fixture-pre-sign'
    result=adapter.execute(request(),quote,lambda _:pytest.fail('unexpected submission callback'))
    assert result['state']=='failed_before_submission'
    assert result['reason']=='financial_quote_expired'
    assert result['submission_phase']=='evm_pre_sign_validation'
    assert not rpc.sent


def test_swap_deadline_starts_after_slow_balance_reads_with_bounded_window(tmp_path, monkeypatch):
    cfg, rpc, adapter, _ = setup(tmp_path, monkeypatch)
    cfg.data['financial']['defi']['quote_window_seconds'] = 120
    clock = [time.time()]
    monkeypatch.setattr('nerya.financial.defi.pancake_swap.time.time', lambda: clock[0])
    original = rpc._rpc
    def delayed(method, args):
        if method == 'eth_getBalance':clock[0] += 45
        if method == 'eth_estimateGas':
            wire = decode([SWAP_TUPLE], bytes.fromhex(args[0]['data'][10:]))[0]
            assert wire[4] == int(clock[0]) + 120
        return original(method, args)
    rpc._rpc = delayed
    quote = adapter.quote(request())
    assert quote['deadline'] == int(clock[0]) + 120
    assert quote['expires_at'] == quote['deadline']
    assert quote['minimum_out_base'] == str(198 * 10**17)
    cfg.data['financial']['defi']['quote_window_seconds'] = 121
    with pytest.raises(FinancialError, match='invalid_protocol_quote_window'):
        adapter.quote(request())


@pytest.mark.parametrize('change', [
    {'chain': 'solana'}, {'chain': 'base'}, {'protocol': 'uniswap_v3'}, {'kind': 'trade'},
])
def test_only_monad_pancake_swap_supported(change):
    with pytest.raises(FinancialError, match='unsupported_monad'):
        PancakeSwapTransport(None, request(**change))


@pytest.mark.parametrize('attribute,value,reason', [
    ('chain_id', 1, 'rpc_chain_identity_mismatch'), ('age', 31, 'swap_chain_state_stale'),
    ('feed_age', 3601, 'swap_input_price_stale'), ('balance', 1, 'swap_input_balance_insufficient'),
    ('native', 1, 'swap_gas_balance_insufficient'), ('allowance', 2**256 - 1, 'finite_protocol_allowance_required'),
    ('pool', SENDER, 'swap_pool_identity_changed'), ('decimals', 6, 'swap_token_decimals_changed'),
])
def test_chain_balance_allowance_and_freshness_fail_closed(tmp_path, monkeypatch, attribute, value, reason):
    _, rpc, adapter, _ = setup(tmp_path, monkeypatch)
    setattr(rpc, attribute, value)
    with pytest.raises((FinancialError, TradingError), match=reason): adapter.quote(request())
    assert not rpc.sent


@pytest.mark.parametrize('target,reason', [(ROUTER, 'protocol_contract_code_changed'),
    (POOL, 'swap_pool_code_changed'), (CHOG, 'swap_token_code_changed')])
def test_code_changes_block_execution(tmp_path, monkeypatch, target, reason):
    _, rpc, adapter, _ = setup(tmp_path, monkeypatch)
    quote = adapter.quote(request())
    rpc.code_overrides[target.lower()] = '0x6002'
    with pytest.raises((FinancialError, TradingError), match=reason): adapter.validate(request(), quote)


def test_unknown_tax_tokens_and_unreviewed_pool_rejected(tmp_path, monkeypatch):
    _, _, adapter, deployment = setup(tmp_path, monkeypatch)
    deployment['tokens'][CHOG.lower()]['transfer_behavior'] = 'fee_on_transfer'
    with pytest.raises(FinancialError, match='nonstandard_token'): adapter.quote(request())
    deployment['tokens'][CHOG.lower()]['transfer_behavior'] = 'standard'
    deployment['pools']['chog_wmon']['reviewed'] = False
    with pytest.raises(FinancialError, match='pool_not_reviewed'): adapter.quote(request())


def test_frozen_plan_permissions_and_requote(tmp_path, monkeypatch):
    cfg, rpc, adapter, _ = setup(tmp_path, monkeypatch)
    quote = adapter.quote(request())
    altered = deepcopy(quote); altered['transaction']['data'] = '0x'
    with pytest.raises(FinancialError, match='swap_plan_mismatch'): adapter.validate(request(), altered)
    altered = deepcopy(quote); altered['exposure_asset_amounts'] = {}
    with pytest.raises(FinancialError, match='accounting_plan'): adapter.validate(request(), altered)
    with pytest.raises(FinancialError, match='identity_mismatch'): adapter.validate(request(amount='11'), quote)
    rpc.output = 197 * 10**17
    with pytest.raises(FinancialError, match='requote_slippage'): adapter.validate(request(), quote)
    rpc.output = 20 * 10**18
    cfg.data['financial']['wallet_permissions']['wallet']['swap'] = False
    with pytest.raises(FinancialError, match='permission_disabled'): adapter.validate(request(), quote)
    cfg.data['financial']['wallet_permissions']['wallet']['swap'] = True
    rpc.block_hash = '0x' + 'd' * 64
    with pytest.raises(FinancialError, match='reorganized'): adapter.validate(request(), quote)


@pytest.mark.parametrize('mutation,reason', [
    ('expired', 'financial_quote_expired'), ('allowance', 'finite_protocol_allowance_required'),
    ('price', 'swap_input_valuation_changed'), ('gas_price', 'protocol_gas_quote_changed'),
    ('wallet', 'swap_request_identity_mismatch'), ('schema', 'invalid_swap_parameters'),
])
def test_execution_rechecks_frozen_expiry_and_mutable_fences(tmp_path, monkeypatch, mutation, reason):
    _, rpc, adapter, _ = setup(tmp_path, monkeypatch)
    quote = adapter.quote(request()); req = request()
    if mutation == 'expired': quote['expires_at'] = time.time() - 1
    if mutation == 'allowance': rpc.allowance = 0
    if mutation == 'price': rpc.price += 1
    if mutation == 'gas_price': rpc.gas_price += 1
    if mutation == 'wallet': req['wallet_id'] = 'other'
    if mutation == 'schema': req['parameters']['recipient'] = SENDER
    with pytest.raises(FinancialError, match=reason): adapter.validate(req, quote)
    assert not rpc.sent


def test_actual_fill_transfers_and_idempotent_status(tmp_path, monkeypatch):
    _, rpc, adapter, _ = setup(tmp_path, monkeypatch)
    quote = adapter.quote(request()); receipt(rpc, quote)
    result = adapter.status(request(), quote, {'transaction_hash': TX_HASH})
    assert result['state'] == 'confirmed' and result['amount_out'] == '19.9'
    assert result['amount_out_base'] != quote['amount_out_base']
    assert result['settled_notional_usd'] == '0.250'
    assert Decimal(result['settled_fee_usd']) == Decimal('0.000003750')
    assert adapter.status(request(), quote, {'transaction_hash': TX_HASH}) == result
    assert not rpc.sent


@pytest.mark.parametrize('mutation,reason', [
    ('missing', 'event_missing_or_ambiguous'), ('tax', 'transfer_tax_or_accounting'),
    ('pool', 'event_missing_or_ambiguous'), ('router', 'event_missing_or_ambiguous'),
    ('duplicate', 'event_missing_or_ambiguous'), ('transaction', 'transaction_parameters_mismatch'),
    ('log_identity', 'receipt_log_identity_mismatch'), ('malformed', 'receipt_evidence_malformed'),
])
def test_bad_receipts_need_recovery_not_quote_fill(tmp_path, monkeypatch, mutation, reason):
    _, rpc, adapter, _ = setup(tmp_path, monkeypatch)
    quote = adapter.quote(request()); receipt(rpc, quote)
    logs = rpc.receipt['logs']
    if mutation == 'missing': logs.pop(1)
    if mutation == 'tax': logs[1]['data'] = '0x' + encode(['uint256'], [18 * 10**18]).hex()
    if mutation == 'pool': logs[2]['address'] = SENDER
    if mutation == 'router': logs[2]['topics'][1] = topic_address(SENDER)
    if mutation == 'duplicate': logs.append(deepcopy(logs[2]))
    if mutation == 'transaction': rpc.transaction['input'] = '0x'
    if mutation == 'log_identity': logs[1]['transactionHash'] = '0x' + 'd' * 64
    if mutation == 'malformed': logs[2]['data'] = '0x01'
    result = adapter.status(request(), quote, {'transaction_hash': TX_HASH})
    assert result['state'] == 'needs_recovery' and reason in result['reason']
    assert 'amount_out_base' not in result


def test_pending_reverted_and_reorg_receipts(tmp_path, monkeypatch):
    _, rpc, adapter, _ = setup(tmp_path, monkeypatch)
    quote = adapter.quote(request()); submission = {'transaction_hash': TX_HASH}
    assert adapter.status(request(), quote, {})['state'] == 'unconfirmed'
    assert adapter.status(request(), quote, submission)['state'] == 'confirming'
    receipt(rpc, quote); rpc.receipt['status'] = '0x0'
    result = adapter.status(request(), quote, submission)
    assert result['state'] == 'rejected' and result['settled_notional_usd'] == '0'
    rpc.receipt['status'] = '0x1'; rpc.block_hash = '0x' + 'd' * 64
    assert adapter.status(request(), quote, submission)['reason'] == 'transaction_block_reorganized'


def test_gateway_prepares_component_and_finite_prerequisite(tmp_path, monkeypatch):
    cfg, rpc, _, _ = setup(tmp_path, monkeypatch); rpc.allowance = 0
    ctx = FinancialContext('operator', frozenset({'api:all'})); gateway = FinancialGateway(cfg)
    action = gateway.prepare(ctx, request(), action_key='missing-allowance')
    assert action['quote']['component_binding']['id'] == COMPONENT
    result = gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])
    assert result['status'] == 'prerequisite_required'
    assert result['required_action']['amount'] == '10'
    assert result['required_action']['spender'].lower() == ROUTER.lower()
    assert gateway.store.get_action(action['action_id'], ctx)['state'] == 'awaiting_prerequisite'
    assert not rpc.sent


def test_gateway_approval_does_not_resolve_signer_or_send(tmp_path, monkeypatch):
    cfg, rpc, _, _ = setup(tmp_path, monkeypatch)
    ctx = FinancialContext('operator', frozenset({'api:all'})); gateway = FinancialGateway(cfg)
    action = gateway.prepare(ctx, request(), action_key='needs-approval')
    assert gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])['status'] == 'approval_required'
    assert not rpc.sent


def test_gateway_grant_owned_signer_callback_and_restart_recovery(tmp_path, monkeypatch):
    cfg, rpc, _, _ = setup(tmp_path, monkeypatch)
    issuer = FinancialContext('operator', frozenset({'api:all'}))
    ctx = replace(issuer, task_kind='scheduled_agent', task_id='monad-meme', security_revision='v1')
    gateway = FinancialGateway(cfg)
    policy = {'actions': ['swap'], 'resources': {'wallets': ['wallet'], 'assets': [WMON, CHOG],
        'chains': ['monad'], 'components': [COMPONENT], 'protocols': ['pancakeswap_v3'], 'routers': [address(ROUTER)]},
        'limits': {'single_usd': '0.3', 'rolling_24h_usd': '0.3', 'total_usd': '0.3', 'fee_usd': '0.01',
                   'slippage_bps': 100, 'asset_amounts': {WMON: '10', CHOG: '20', 'NATIVE': '0.001'}}}
    grant = gateway.store.create_grant(issuer, task_kind=ctx.task_kind, task_id=ctx.task_id,
                                       security_revision='v1', policy=policy)
    gateway.store.approve_grant(grant['grant_id'], issuer, expected_revision=1)
    action = gateway.prepare(ctx, request(), action_key='owned-send')
    def after_callback():
        persisted = gateway.store.get_action(action['action_id'], ctx, internal=True)
        assert persisted['submission']['transaction_hash'] == TX_HASH
        assert persisted['submission']['action_id'] == action['action_id']
        raise TimeoutError('response lost after persisted identity')
    rpc.after_callback = after_callback
    first = gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])
    assert first.get('state') == 'unconfirmed' and len(rpc.sent) == 1, first
    duplicate = FinancialGateway(cfg).execute(ctx, action['action_id'], quote_hash=action['quote_hash'])
    assert duplicate['duplicate'] and len(rpc.sent) == 1
    frozen = gateway.store.get_action(action['action_id'], ctx, internal=True)['quote']
    receipt(rpc, frozen)
    resolved = FinancialGateway(cfg).reconcile(ctx, action['action_id'])
    assert resolved['state'] == 'confirmed' and resolved['receipt']['amount_out'] == '19.9'
    assert FinancialGateway(cfg).reconcile(ctx, action['action_id'])['state'] == 'confirmed'
    assert len(rpc.sent) == 1
    second = gateway.prepare(ctx, request(), action_key='over-asset-budget')
    assert gateway.execute(ctx, second['action_id'], quote_hash=second['quote_hash'])['status'] == 'approval_required'
    assert len(rpc.sent) == 1


def setup_testnet(tmp_path, monkeypatch, variant='pancakeswap_v3'):
    cfg, rpc, _, deployment = setup(tmp_path, monkeypatch)
    cfg.data['financial']['defi']['deployments']['monad-testnet'] = {variant: deployment}
    del cfg.data['financial']['defi']['deployments']['monad']
    deployment.update(chain_id=10143, testnet=True, official=False,
        deployment_origin='canonical_test_deployment', protocol_variant=variant,
        source_revision='canonical-fixture-source-v1', wrapped_native=WMON)
    # Distinct deployment addresses; fixture tokens are deterministic test coins.
    rpc.factory, rpc.router, rpc.quoter = [address('0x' + digit * 40) for digit in ('4', '5', '6')]
    deployment['contracts'].update(factory=rpc.factory, router=rpc.router, quoter=rpc.quoter)
    rpc.chain_id = 10143
    if variant == 'uniswap_v3': rpc.protocol_fee = 0
    provider = SimpleNamespace(id='self_custody', _resolve_signer_key=lambda: KEY)
    raw = cfg.data['wallet']['providers']['wallet']['config']
    raw['rpc_urls'] = {'monad-testnet': 'https://rpc.invalid'}
    factory = lambda c, r: PancakeSwapTransport(c, r, connector=rpc, provider=provider, wallet_config=raw)
    import nerya.trading.components as components
    original = components._builtin_components
    def registered():
        rows = original()
        rows[COMPONENT] = BoundComponent(COMPONENT, 'e' * 64, TradingComponentSpec(
            'monad_pancake_swap', '1', ('swap',), ('spot',), factory, parameters_schema=SWAP_SCHEMA))
        return rows
    monkeypatch.setattr(components, '_builtin_components', registered)
    return cfg, rpc, factory, deployment


def test_testnet_300_second_window_requotes_after_150_seconds(tmp_path, monkeypatch):
    cfg,rpc,factory,_=setup_testnet(tmp_path,monkeypatch,'uniswap_v3')
    cfg.data['financial']['defi']['quote_window_seconds']=300
    req=request(chain='monad-testnet',protocol='uniswap_v3')
    adapter=factory(cfg,req)
    clock=[time.time()]
    monkeypatch.setattr('nerya.financial.defi.pancake_swap.time.time',lambda:clock[0])
    quote=adapter.quote(req)
    assert quote['deadline']==int(clock[0])+300
    clock[0]+=150
    adapter.validate(req,quote)
    selector='0x'+keccak(text='quoteExactInputSingle('+QUOTE_TUPLE+')')[:4].hex()
    quotes=[row for row in rpc.calls if row[0]=='eth_call' and row[1][0]['to'].lower()==rpc.quoter.lower() and row[1][0]['data'].startswith(selector)]
    assert len(quotes)==2
    assert not rpc.sent
    cfg.data['financial']['defi']['quote_window_seconds']=301
    with pytest.raises(FinancialError,match='invalid_protocol_quote_window'):adapter.quote(req)


def test_mainnet_rejects_300_second_window(tmp_path, monkeypatch):
    cfg,rpc,adapter,_=setup(tmp_path,monkeypatch)
    cfg.data['financial']['defi']['quote_window_seconds']=300
    with pytest.raises(FinancialError,match='invalid_protocol_quote_window'):adapter.quote(request())


@pytest.mark.parametrize('variant', ['pancakeswap_v3', 'uniswap_v3'])
def test_testnet_reviewed_config_quotes_and_confirms_canonical_abi(tmp_path, monkeypatch, variant):
    cfg, rpc, factory, _ = setup_testnet(tmp_path, monkeypatch, variant)
    req = request(chain='monad-testnet', protocol=variant); adapter = factory(cfg, req)
    quote = adapter.quote(req); adapter.validate(req, quote)
    assert quote['chain_id'] == 10143 and quote['network']['testnet'] is True
    assert quote['network']['deployment_origin'] == 'canonical_test_deployment'
    assert quote['network']['valuation_mode'] == 'test_units'
    assert quote['transaction']['to'] == rpc.router
    receipt(rpc, quote)
    rpc.transaction['to'] = rpc.router
    log = rpc.receipt['logs'][2]
    log['topics'][1] = topic_address(rpc.router)
    if variant == 'uniswap_v3':
        log['topics'][0] = '0x' + keccak(text=UNI_SWAP_EVENT).hex()
        log['data'] = '0x' + encode(['int256', 'int256', 'uint160', 'uint128', 'int24'],
            [-199 * 10**17, 10**19, 2**96, 10**22, 0]).hex()
    result = adapter.status(req, quote, {'transaction_hash': TX_HASH})
    assert result['state'] == 'confirmed' and result['amount_out'] == '19.9'
    assert result['network'] == quote['network'] and not rpc.sent


@pytest.mark.parametrize('field,value,reason', [
    ('chain_id', 143, 'deployment_identity'), ('testnet', False, 'deployment_identity'),
    ('official', True, 'deployment_identity'), ('deployment_origin', 'official', 'deployment_identity'),
    ('protocol_variant', 'unknown', 'canonical_source'), ('source_revision', '', 'canonical_source'),
])
def test_testnet_rejects_wrong_identity_and_false_official_claim(tmp_path, monkeypatch, field, value, reason):
    cfg, rpc, factory, deployment = setup_testnet(tmp_path, monkeypatch)
    deployment[field] = value
    with pytest.raises(FinancialError, match=reason): factory(cfg, request(chain='monad-testnet'))
    assert not rpc.sent


def test_testnet_mainnet_rpc_and_missing_code_evidence_rejected(tmp_path, monkeypatch):
    cfg, rpc, factory, deployment = setup_testnet(tmp_path, monkeypatch)
    rpc.chain_id = 143
    with pytest.raises(TradingError, match='rpc_chain_identity'): factory(cfg, request(chain='monad-testnet'))
    rpc.chain_id = 10143; deployment['code_hashes'].pop('router')
    with pytest.raises(TradingError, match='contract_evidence_missing'): factory(cfg, request(chain='monad-testnet'))


@pytest.mark.parametrize('variant', ['pancakeswap_v3', 'uniswap_v3'])
def test_testnet_gateway_finite_approval_and_permission_gates(tmp_path, monkeypatch, variant):
    cfg, rpc, _, _ = setup_testnet(tmp_path, monkeypatch, variant); rpc.allowance = 0
    ctx = FinancialContext('operator', frozenset({'api:all'})); gateway = FinancialGateway(cfg)
    req = request(chain='monad-testnet', protocol=variant)
    action = gateway.prepare(ctx, req, action_key='testnet-only')
    required = gateway.execute(ctx, action['action_id'], quote_hash=action['quote_hash'])
    assert required['required_action']['chain'] == 'monad-testnet'
    assert required['required_action']['spender'] == rpc.router
    rpc.allowance = 10**20
    reviewed = gateway.prepare(ctx, req, action_key='testnet-approval')
    assert gateway.execute(ctx, reviewed['action_id'], quote_hash=reviewed['quote_hash'])['status'] == 'approval_required'
    cfg.data['financial']['wallet_permissions']['wallet']['swap'] = False
    with pytest.raises(FinancialError, match='permission_disabled'):
        gateway.execute(ctx, reviewed['action_id'], quote_hash=reviewed['quote_hash'])
    assert not rpc.sent

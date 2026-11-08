"""Real SDK/gateway LP context binding, with isolated transports only."""
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
import time

import pytest

from nerya.approval_service import ApprovalService
from nerya.core import yaml_io
from nerya.financial.contracts import FinancialContext, FinancialError
from nerya.financial.defi.book import ProtocolBook
from nerya.financial.defi.uniswap import UniswapFunds
from nerya.financial.gateway import FinancialGateway
from nerya.sdk.financial_api import FinancialAPI
from test_lp_strategy_scaffold import offline, real_context
import test_uniswap_execution as lp

pytestmark = pytest.mark.smoke


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    ctx = real_context(tmp_path, monkeypatch, bind_validation=False)
    ctx.rpc_calls = []
    rpc = ctx.rpc._rpc

    def recorded(method, args):
        ctx.rpc_calls.append((method, deepcopy(args)))
        return rpc(method, args)

    monkeypatch.setattr(ctx.rpc, '_rpc', recorded)
    ctx.validations = []
    validate = UniswapFunds.validate

    def checked(adapter, request, quote):
        ctx.validations.append((deepcopy(adapter.config.data['runtime']), deepcopy(request), deepcopy(quote)))
        return validate(adapter, request, quote)

    monkeypatch.setattr(UniswapFunds, 'validate', checked)
    ctx.signatures = []
    provider = FinancialGateway(ctx.runtime_config).adapter(lp.request()).provider

    def fake_key():
        ctx.signatures.append('isolated-fake-signer')
        return 'not-a-private-key'

    monkeypatch.setattr(provider, '_resolve_signer_key', fake_key)
    from eth_account import Account
    monkeypatch.setattr(Account, 'from_key', lambda key: SimpleNamespace(address=lp.SENDER))

    def fake_send(**kwargs):
        assert kwargs['signer_private_key'] == 'not-a-private-key'
        ctx.rpc.sends += 1
        kwargs['on_broadcast']({'tx_hash': lp.TX_HASH, 'nonce': 0})
        return {'tx_hash': lp.TX_HASH}

    monkeypatch.setattr(ctx.rpc, 'send_raw_transaction', fake_send)
    return ctx


def prepare(ctx):
    return ctx.financial.prepare(lp.request(), client_request_id='entry')


def approve(ctx, action):
    pending = ctx.financial.execute(action['action_id'], quote_hash=action['quote_hash'])
    assert pending['status'] == 'approval_required'
    assert ctx.rpc.sends == 0 and ctx.signatures == []
    # Only the tmp_path approval store is authorized by this fake operator.
    service = ApprovalService(ctx.runtime_config)
    resolved = service.move(pending['approval_id'], state='approved',
                            resolver_actor_id='offline-operator', operator_authorized=True)
    assert resolved['state'] == 'approved'


def test_sdk_entry_requires_approval_without_ambient_context(tmp_path, monkeypatch):
    ctx = real_context(tmp_path, monkeypatch, bind_validation=False)
    original = deepcopy(ctx.runtime_config.data)
    action = ctx.financial.prepare(lp.request(), client_request_id='entry')
    result = ctx.financial.execute(action['action_id'], quote_hash=action['quote_hash'])
    assert result['status'] == 'approval_required'
    saved = ctx.financial.get(action['action_id'])
    assert saved['state'] == 'awaiting_approval'
    assert saved['request'] == action['request']
    assert saved['quote_hash'] == action['quote_hash']
    assert len(FinancialGateway(ctx.runtime_config).store.list_actions(ctx.identity)) == 1
    assert ctx.rpc.sends == 0
    assert ctx.runtime_config.data == original


def test_exact_context_refresh_submission_duplicate_and_durable_recovery(ctx):
    # An ambient identity cannot replace the producer passed by the SDK.
    ctx.runtime_config.data['runtime']['financial_context'] = {'actor_id': 'foreign'}
    original_config = deepcopy(ctx.runtime_config.data)
    action = prepare(ctx)
    gateway = FinancialGateway(ctx.runtime_config)
    stored = gateway.store.get_action(action['action_id'], ctx.identity, internal=True)
    expected = {**asdict(ctx.identity), 'scopes': sorted(ctx.identity.scopes)}
    assert stored['context'] == expected
    assert stored['quote']['position_owner'] == 'strategy:lp_fixture'
    refreshed = ctx.financial.refresh(action['action_id'], expected_revision=action['revision'])
    assert refreshed['revision'] == action['revision'] + 1
    assert ctx.validations[-1][0]['financial_context'] == expected
    approve(ctx, refreshed)
    result = ctx.financial.execute(action['action_id'], quote_hash=refreshed['quote_hash'])
    assert result['state'] == 'submitted', result
    assert ctx.rpc.sends == 1 and len(ctx.signatures) == 1
    assert len(ctx.validations) == 4  # refresh, approval, pre-reservation, pre-send
    for runtime, request, quote in ctx.validations:
        assert runtime['financial_context'] == expected
        assert request == action['request']
        assert quote['position_owner'] == 'strategy:lp_fixture'
    assert ctx.validations[-1][0]['financial_action_id'] == action['action_id']
    calls = deepcopy(ctx.rpc_calls)
    again = ctx.financial.execute(action['action_id'], quote_hash=refreshed['quote_hash'])
    assert again['duplicate'] and again['state'] == 'submitted'
    assert ctx.rpc_calls == calls and ctx.rpc.sends == 1 and len(ctx.signatures) == 1
    quote = gateway.store.get_action(action['action_id'], ctx.identity, internal=True)['quote']
    ctx.rpc.minted, ctx.rpc.liquidity = True, 2000000000
    ctx.rpc.receipt = lp.receipt(quote)
    ctx.rpc.transaction = dict(hash=lp.TX_HASH, **{'from': lp.SENDER}, to=lp.MANAGER,
                               input=quote['transaction']['data'], value='0x0')
    # A different operator can reconcile; the claim still belongs to the original strategy.
    operator = FinancialContext('offline-operator', frozenset({'admin:ops'}))
    confirmed = gateway.reconcile(operator, action['action_id'])
    assert confirmed['state'] == 'confirmed', confirmed
    book = ProtocolBook(ctx.runtime_config)
    with gateway.store.transaction() as con:
        event = con.execute('SELECT position_key,owner_id FROM protocol_position_events WHERE action_id=?',
                            (action['action_id'],)).fetchone()
    assert event['owner_id'] == 'strategy:lp_fixture'
    key = event['position_key']
    assert book.claims(key, 'strategy:lp_fixture')[0]['liquidity'] == 2000000000
    assert book.claims(key, 'actor:offline-operator')[0] == {}
    assert ctx.runtime_config.data == original_config


@pytest.mark.parametrize('mutation,code', [
    ('actor', 'financial_owner_mismatch'),
    ('run', 'financial_run_binding_mismatch'),
    ('strategy_run', 'financial_strategy_run_binding_mismatch'),
    ('revision', 'financial_task_security_revision_changed'),
    ('source', 'financial_task_security_revision_changed'),
    ('expired', 'strategy_financial_run_not_active'),
    ('finished', 'strategy_financial_run_not_active'),
    ('resource', 'strategy_financial_wallet_mismatch'),
])
def test_durable_fences_precede_adapter_validation_and_preserve_action(ctx, mutation, code):
    action = prepare(ctx)
    identity = ctx.identity
    if mutation == 'actor':
        ctx.identity = replace(identity, actor_id='foreign')
    elif mutation == 'run':
        ctx.identity = replace(identity, run_id='foreign-run')
    elif mutation == 'strategy_run':
        ctx.identity = replace(identity, strategy_run_id='foreign-tick')
    elif mutation == 'revision':
        ctx.identity = replace(identity, security_revision='foreign-revision')
    elif mutation == 'source':
        yaml_io.dump(ctx.runtime_config.paths.strategy('lp_fixture') / 'strategy.yml',
                     {'id': 'lp_fixture', 'accounts': ['fixture-account'],
                      'markets': ['UNISWAP_V3:ETH/USDC'], 'params': {'changed': True}})
    elif mutation == 'resource':
        yaml_io.dump(ctx.runtime_config.paths.accounts_file,
                     {'accounts': [{'id': 'fixture-account', 'kind': 'chain', 'wallet_id': 'foreign'}]})
    else:
        with FinancialGateway(ctx.runtime_config).store.transaction() as con:
            if mutation == 'expired':
                con.execute('UPDATE strategy_financial_runs SET lease_until=? WHERE run_id=?',
                            (time.time() - 1, identity.strategy_run_id))
            else:
                con.execute("UPDATE strategy_financial_runs SET state='finished' WHERE run_id=?",
                            (identity.strategy_run_id,))
    calls = deepcopy(ctx.rpc_calls)
    with pytest.raises(FinancialError, match='^' + code + '$'):
        ctx.financial.execute(action['action_id'], quote_hash=action['quote_hash'])
    assert ctx.validations == [] and ctx.rpc_calls == calls
    saved = FinancialGateway(ctx.runtime_config).store.get_action(action['action_id'], identity, internal=True)
    assert saved['state'] == 'prepared' and saved['request'] == action['request']
    assert saved['quote_hash'] == action['quote_hash'] and saved['submission'] == {}
    assert ctx.rpc.sends == 0 and ctx.signatures == []


@pytest.mark.parametrize('gate', ['hash', 'expired_quote', 'allowance', 'gas'])
def test_quote_and_preflight_gates_remain_closed(ctx, monkeypatch, gate):
    if gate == 'allowance':
        ctx.rpc.allowance = 0
    action = prepare(ctx)
    quote_hash = action['quote_hash']
    if gate == 'hash':
        quote_hash = 'changed'
        code = 'financial_quote_changed'
    elif gate == 'expired_quote':
        # Keep independent account freshness valid to isolate quote expiry.
        ctx.runtime_config.data['financial']['max_account_age_seconds'] = 120
        monkeypatch.setattr(time, 'time', lambda: action['quote']['expires_at'] + 1)
        code = 'financial_quote_expired'
    elif gate == 'allowance':
        ctx.rpc.allowance = 0
        code = 'finite_protocol_allowance_required'
    else:
        rpc = ctx.rpc._rpc
        monkeypatch.setattr(ctx.rpc, '_rpc', lambda method, args:
                            hex(10**18) if method == 'eth_estimateGas' else rpc(method, args))
        code = 'protocol_gas_limit_changed'
    if gate == 'allowance':
        result = ctx.financial.execute(action['action_id'], quote_hash=quote_hash)
        assert result['status'] == 'prerequisite_required'
        assert result['required_actions']
    else:
        with pytest.raises(FinancialError, match='^' + code + '$'):
            ctx.financial.execute(action['action_id'], quote_hash=quote_hash)
    saved = ctx.financial.get(action['action_id'])
    assert saved['request'] == action['request'] and saved['quote_hash'] == action['quote_hash']
    assert saved['state'] == ('awaiting_prerequisite' if gate == 'allowance' else 'prepared')
    assert ctx.rpc.sends == 0 and ctx.signatures == []


def test_account_state_receives_context_without_mutating_config(ctx, monkeypatch):
    original = deepcopy(ctx.runtime_config.data)
    observed = []
    account_state = UniswapFunds.account_state

    def checked(adapter, request):
        observed.append(adapter.config.get('runtime.financial_context'))
        return account_state(adapter, request)

    monkeypatch.setattr(UniswapFunds, 'account_state', checked)
    assert ctx.financial.account_state(lp.request())['health'] == 'ok'
    assert observed == [{**asdict(ctx.identity), 'scopes': sorted(ctx.identity.scopes)}]
    assert ctx.runtime_config.data == original
    assert ctx.rpc.sends == 0 and ctx.signatures == []


def test_paper_facade_never_constructs_funds_adapter(ctx, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('paper must not construct an adapter')
    monkeypatch.setattr(FinancialGateway, 'adapter', denied)
    paper = FinancialAPI(ctx.runtime_config, context_provider=lambda write: ctx.identity, read_only=True)
    for operation in (lambda: paper.prepare(lp.request(), client_request_id='paper'),
                      lambda: paper.execute('none', quote_hash='none')):
        with pytest.raises(FinancialError, match='paper_run_denies_funds_execution'):
            operation()
    assert ctx.rpc.sends == 0 and ctx.signatures == []

"""Offline authoring contract: real SDK/gateway/Aave, existing RPC fixture only."""
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

from nerya.core import yaml_io
from nerya.core.errors import TradingError
from nerya.financial.contracts import FinancialError, normalize_request
from nerya.financial.store import FinancialStore
from nerya.financial.strategy_runtime import bind_run
from nerya.sdk.financial_api import FinancialAPI
from nerya.strategies import ResultBuilder
from nerya.trading.approval import ApprovalGate
from nerya.financial.defi.calls import topic_address
import test_aave_execution as aave
from test_aave_execution import ASSET, POOL, setup

pytestmark = pytest.mark.smoke
TEMPLATE = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/templates/aave_supply.py.j2'


@pytest.fixture
def example():
    module = {}
    exec(compile(TEMPLATE.read_text(), str(TEMPLATE), 'exec'), module)
    return module


@pytest.fixture
def context(tmp_path, monkeypatch):
    def offline(*args, **kwargs):
        raise AssertionError('NETWORK_DISABLED')
    monkeypatch.setattr(socket.socket, 'connect', offline)
    monkeypatch.setattr(socket, 'create_connection', offline)
    signer_key = bytes([42]) * 32  # Existing offline fixture, no operator key.
    monkeypatch.setattr(aave, 'SENDER', Account.from_key(signer_key).address)
    config, rpc, provider, identity = setup(tmp_path, monkeypatch)
    config.data['financial']['account_limits'] = {
        'wallet:wallet': {'single_usd': '100', 'rolling_24h_usd': '1000'}}
    provider._resolve_signer_key = lambda: signer_key
    yaml_io.dump(config.paths.accounts_file, {'accounts': [
        {'id': 'fixture-account', 'kind': 'chain', 'wallet_id': 'wallet'}]})
    yaml_io.dump(config.paths.strategy('fixture-aave') / 'strategy.yml',
                 {'id': 'fixture-aave', 'accounts': ['fixture-account']})
    config, identity = bind_run(config, strategy_id='fixture-aave',
                                run_id='fixture-run', mode='live')
    saved, calls, rpc_calls = {}, [], []
    original_rpc = rpc._rpc

    def observed_rpc(method, args):
        rpc_calls.append((method, args))
        return original_rpc(method, args)

    rpc._rpc = observed_rpc

    def resource(request):
        calls.append(dict(request))

    ctx = SimpleNamespace(
        mode='live', result=ResultBuilder(),
        state=SimpleNamespace(get=saved.get, set=saved.__setitem__),
        financial=FinancialAPI(config, context_provider=lambda write: identity,
                               validate_resource=resource),
        saved=saved, calls=calls, rpc_calls=rpc_calls, rpc=rpc, provider=provider, config=config, identity=identity,
    )
    return ctx


def request(kind='lend_supply'):
    return dict(kind=kind, component_id='builtin:aave', wallet_id='wallet',
                protocol='aave_v3', chain='base', asset=ASSET, amount='10', parameters={})


def tick(example, ctx, **changes):
    args = dict(request=request(), operation_key='supply-cycle-1',
                data_ready=True, decision=True)
    args.update(changes)
    return example['aave_supply_tick'](ctx, **args)


def test_supply_uses_real_financial_envelope_and_preserves_pending_approval(example, context):
    result = tick(example, context)
    assert result.status.value == 'pending_approval'
    receipt = result.metadata['financial_action']
    action = context.financial.get(receipt['action_id'])
    assert action['request'] == request()
    assert normalize_request(action['request']) == request()
    assert action['quote']['transaction']['to'] == POOL
    assert action['quote']['component_binding']['id'] == 'builtin:aave'
    assert action['state'] == 'awaiting_approval'
    assert result.approval_id == action['approval_id']
    assert context.saved['aave_supply_action']['action_id'] == action['action_id']
    assert context.rpc.sends == 0


def submit(ctx, result):
    action_id = result.metadata['financial_action']['action_id']
    action = ctx.financial.get(action_id)
    ApprovalGate(ctx.config).approve(action['approval_id'])
    receipt = ctx.financial.execute(action_id, quote_hash=action['quote_hash'])
    assert receipt['state'] == 'submitted'
    return ctx.financial.get(action_id)


def chain_receipt(ctx, *, reverted=False, reorg=False):
    """Supply event/transaction bytes checked by the real Aave receipt verifier."""
    ctx.rpc.receipt = {
        'status': '0x0' if reverted else '0x1', 'blockNumber': '0x64',
        'blockHash': '0x' + 'c' * 64 if reorg else aave.BLOCK_HASH,
        'gasUsed': hex(150000), 'effectiveGasPrice': hex(10**9),
        'logs': [{'address': POOL, 'topics': [
            '0x' + keccak(text='Supply(address,address,address,uint256,uint16)').hex(),
            topic_address(ASSET), topic_address(aave.SENDER), '0x' + '0' * 64],
            'data': '0x' + encode(['address', 'uint256'], [aave.SENDER, 10 * 10**6]).hex(),
            'logIndex': '0x1'}],
    }
    calldata = '0x' + keccak(text='supply(address,uint256,address,uint16)')[:4].hex()
    calldata += encode(['address', 'uint256', 'address', 'uint16'],
                       [ASSET, 10 * 10**6, aave.SENDER, 0]).hex()
    ctx.rpc.transaction = dict(hash=aave.TX_HASH, to=POOL, input=calldata, value='0x0')
    ctx.rpc.transaction['from'] = aave.SENDER
    if not reverted and not reorg:
        ctx.rpc.supply += 10 * 10**6


def test_confirmed_owned_supply_allows_real_withdraw_envelope(example, context):
    submit(context, tick(example, context))
    chain_receipt(context)
    confirmed = tick(example, context)
    assert confirmed.status.value == 'filled', confirmed.metadata['financial_action']['receipt']
    assert confirmed.metadata['financial_action']['receipt']['protocol_effect']['amount_base'] == str(10 * 10**6)
    result = tick(example, context, request=request('lend_withdraw'), operation_key='withdraw-cycle-1')
    assert result.status.value == 'pending_approval'
    action = context.financial.get(result.metadata['financial_action']['action_id'])
    assert action['request'] == request('lend_withdraw')
    assert normalize_request(action['request']) == request('lend_withdraw')
    expected = '0x' + keccak(text='withdraw(address,uint256,address)')[:4].hex()
    expected += encode(['address', 'uint256', 'address'], [ASSET, 10 * 10**6, aave.SENDER]).hex()
    transactions = [args[0] for method, args in context.rpc_calls if method == 'eth_estimateGas']
    assert transactions[-1]['data'] == expected
    assert context.rpc.sends == 1  # Withdrawal awaits its own approval.


def test_wallet_balance_does_not_authorize_strategy_withdrawal(example, context):
    with pytest.raises(FinancialError, match='protocol_exit_exceeds_owned_claim'):
        tick(example, context, request=request('lend_withdraw'))
    assert context.rpc.sends == 0


@pytest.mark.parametrize('outcome,expected,state', [
    ('pending', 'submitted', 'confirming'),
    ('reverted', 'rejected', 'rejected'),
    ('reorg', 'error', 'unconfirmed'),
])
def test_real_receipts_preserve_identity_and_never_resubmit(example, context, outcome, expected, state):
    action = submit(context, tick(example, context))
    if outcome != 'pending':
        chain_receipt(context, reverted=outcome == 'reverted', reorg=outcome == 'reorg')
    result = tick(example, context)
    assert result.status.value == expected, result.metadata['financial_action']['receipt']
    receipt = result.metadata['financial_action']
    assert receipt['state'] == state and receipt['action_id'] == action['action_id']
    assert receipt['submission']['transaction_hash'] == aave.TX_HASH
    tick(example, context)
    if outcome != 'reverted':
        blocked = tick(example, context, operation_key='different-cycle')
        assert blocked.metadata['financial_action']['action_id'] == action['action_id']
    assert context.rpc.sends == 1
    assert len(FinancialStore(context.config).list_actions(context.identity)) == 1


@pytest.mark.parametrize('receipt', [None, {}, {'state': 'future_unknown_state'}, {'status': 'ok'}])
def test_unknown_receipt_cannot_be_success(example, context, receipt):
    result = example['financial_result'](context, receipt)
    assert result.status.value == 'error' and result.error_kind == 'execution_unknown'
    assert result.metadata['financial_action'] == receipt


@pytest.mark.parametrize('gates', [
    {'data_ready': False}, {'data_ready': None}, {'data_ready': 1},
    {'decision': False}, {'decision': None}, {'decision': 'true'},
])
def test_no_data_or_false_gate_makes_no_financial_calls(example, context, gates):
    result = tick(example, context, **gates)
    assert result.status.value == 'hold'
    assert context.calls == [] and context.saved == {} and context.rpc.sends == 0


@pytest.mark.parametrize('mode', ['paper', 'shadow', 'backtest'])
def test_nonlive_mode_makes_no_financial_calls(example, context, mode):
    context.mode = mode
    assert tick(example, context).status.value == 'hold'
    assert context.calls == [] and context.saved == {} and context.rpc.sends == 0


@pytest.mark.parametrize('field', ['wallet_id', 'chain', 'asset', 'amount', 'component_id', 'protocol'])
def test_missing_explicit_binding_stops_before_financial_calls(example, context, field):
    raw = request()
    raw.pop(field)
    assert tick(example, context, request=raw).error_kind == 'resource_binding'
    assert context.calls == [] and context.saved == {}


@pytest.mark.parametrize('patch,error', [
    ({'parameters': {'asset': ASSET, 'amount': '10'}}, 'invalid_component_parameters'),
    ({'pool_id': 'invented'}, 'unknown_financial_parameter'),
    ({'wallet_id': 'foreign-wallet'}, 'strategy_financial_wallet_mismatch'),
    ({'account_id': 'foreign-account'}, 'unknown account_id'),
])
def test_real_contract_rejects_wrong_envelope_and_resources(example, context, patch, error):
    with pytest.raises((FinancialError, TradingError), match=error):
        tick(example, context, request={**request(), **patch})
    assert context.rpc.sends == 0


def test_missing_operator_deployment_is_not_invented(example, context):
    context.config.data['financial']['defi']['deployments'] = {}
    with pytest.raises(TradingError, match='protocol_deployment_not_reviewed'):
        tick(example, context)
    assert not context.saved and context.rpc.sends == 0


def test_live_gate_error_retains_original_action_without_retry(example, context):
    context.config.data['runtime']['live_trading_enabled'] = False
    with pytest.raises(FinancialError, match='live_trading_disabled'):
        tick(example, context)
    action_id = context.saved['aave_supply_action']['action_id']
    context.config.data['runtime']['live_trading_enabled'] = True
    assert tick(example, context).metadata['financial_action']['action_id'] == action_id
    assert context.rpc.sends == 0


def test_changed_request_cannot_reuse_operation_key(example, context):
    tick(example, context)
    result = tick(example, context, request={**request(), 'amount': '11'})
    assert result.error_kind == 'resource_binding'
    assert len(FinancialStore(context.config).list_actions(context.identity)) == 1


def test_allowance_prerequisite_is_retained_without_an_approval_write(example, context):
    context.rpc.allowance = 0
    result = tick(example, context)
    assert result.status.value == 'hold'
    receipt = result.metadata['financial_action']
    assert receipt['status'] == 'prerequisite_required'
    assert receipt['required_action']['amount'] == '10'
    assert tick(example, context).metadata['financial_action']['state'] == 'awaiting_prerequisite'
    assert len(FinancialStore(context.config).list_actions(context.identity)) == 1
    assert context.rpc.sends == 0


def test_interrupted_prepare_cannot_mint_a_new_operation(example, context, monkeypatch):
    original = context.rpc._rpc

    def interrupted_quote(method, args):
        if method == 'eth_estimateGas':
            raise TimeoutError('offline RPC quote timeout')
        return original(method, args)

    monkeypatch.setattr(context.rpc, '_rpc', interrupted_quote)
    with pytest.raises(TimeoutError):
        tick(example, context)
    assert context.saved['aave_supply_action']['action_id'] is None
    monkeypatch.setattr(context.rpc, '_rpc', original)
    assert tick(example, context, operation_key='new-key').error_kind == 'execution_unknown'
    assert FinancialStore(context.config).list_actions(context.identity) == []
    assert context.rpc.sends == 0


def test_original_approval_is_never_reexecuted_by_a_later_tick(example, context):
    result = tick(example, context)
    action_id = result.metadata['financial_action']['action_id']
    ApprovalGate(context.config).approve(result.approval_id)
    result = tick(example, context, operation_key='new-key')
    assert result.status.value == 'pending_approval'
    assert result.metadata['financial_action']['action_id'] == action_id
    assert len(FinancialStore(context.config).list_actions(context.identity)) == 1
    assert context.rpc.sends == 0


def save_prepared(ctx):
    action = ctx.financial.prepare(request(), client_request_id='supply-cycle-1')
    ctx.state.set('aave_supply_action', dict(operation_key='supply-cycle-1',
                                           request=request(), action_id=action['action_id']))
    return action


def test_prepared_resume_executes_original_action_through_actual_approval_gate(example, context):
    action = save_prepared(context)
    result = tick(example, context)
    assert result.status.value == 'pending_approval'
    assert result.metadata['financial_action']['status'] == 'approval_required'
    assert result.metadata['financial_action']['action_id'] == action['action_id']
    assert result.approval_id
    assert len(FinancialStore(context.config).list_actions(context.identity)) == 1
    assert context.rpc.sends == 0


def test_prepared_resume_cannot_borrow_another_runs_authority(example, context):
    action = save_prepared(context)
    config, identity = bind_run(context.config, strategy_id='fixture-aave',
                                run_id='different-run', mode='live')
    context.financial = FinancialAPI(config, context_provider=lambda write: identity)
    with pytest.raises(FinancialError, match='financial_strategy_run_binding_mismatch'):
        tick(example, context)
    assert context.financial.get(action['action_id'])['state'] == 'prepared'
    assert len(FinancialStore(context.config).list_actions(context.identity)) == 1
    assert context.rpc.sends == 0


def test_prepared_resume_keeps_runtime_permission_gate(example, context):
    save_prepared(context)
    context.config.data['runtime']['live_trading_enabled'] = False
    with pytest.raises(FinancialError, match='live_trading_disabled'):
        tick(example, context)
    assert len(FinancialStore(context.config).list_actions(context.identity)) == 1
    assert context.rpc.sends == 0


def test_unknown_persisted_receipt_blocks_same_and_new_operations(example, context):
    action = save_prepared(context)
    store = FinancialStore(context.config)
    store.mark(action['action_id'], state='future_unknown_state')
    for operation_key in ('supply-cycle-1', 'new-key'):
        result = tick(example, context, operation_key=operation_key)
        assert result.error_kind == 'execution_unknown'
        assert result.metadata['financial_action']['action_id'] == action['action_id']
    assert len(store.list_actions(context.identity)) == 1
    assert context.rpc.sends == 0

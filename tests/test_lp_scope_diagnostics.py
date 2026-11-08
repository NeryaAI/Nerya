"""Offline LP producer diagnostics; all admission/storage use tmp_path."""
from copy import deepcopy
import json

import pytest

from nerya.strategies import request_scope as scope
from nerya.tools.native import strategy_runtime
from nerya.tools.types import ToolCall
import test_request_scope as request_scope_fixtures
from test_request_scope import FakeGateway, admit, env as env, spans_output, install_fake

fake_execution = request_scope_fixtures.fake_execution

pytestmark = pytest.mark.smoke
TEXT = '帮我创建一个 Uniswap V3 ETH/USDC LP 策略，回测最近30天，不启用交易。'


@pytest.fixture
def lp_scope(env):
    admit(env, cid='lp-original', sid='lp-session', text=TEXT, created=1793176633.991647)
    gateway = FakeGateway(lambda docs: spans_output(
        docs[0], protocol='Uniswap V3', base='ETH', quote='USDC', window_days='30天'))
    contract = scope.ensure_contract(env, gateway=gateway)
    return env, contract


def provider_text(failure):
    from nerya.agent.loop import WorkspaceNativeAgentLoop
    from nerya.llm.messages import _openai_render_messages
    block = WorkspaceNativeAgentLoop._render_tool_result(
        object.__new__(WorkspaceNativeAgentLoop), failure)
    messages = _openai_render_messages(system='', messages=[{'role': 'user', 'content': [block]}])
    return next(row['content'] for row in messages if row['role'] == 'tool')


def test_lp_pair_only_draft_blocks_before_scope_extraction(lp_scope):
    config, contract = lp_scope
    original = deepcopy(contract)
    call = ToolCall(name='strategy_draft_proposal', arguments={
        'strategy_id': 'lp_fixture', 'markets': ['UNISWAP_V3:ETH/USDC'],
        'strategy_class': 'trend', 'timeframe': '1h', 'mode': 'paper',
        'execution_mode': 'script',
        'description': 'Uniswap V3 mainnet pool 0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640',
        'prompt': TEXT,
    })
    failure = strategy_runtime.strategy_draft_proposal_handler(call, config=config)
    assert failure.is_error
    assert failure.error.detail['code'] == 'lp_pool_identity_required'
    assert failure.error.retryable is False
    assert failure.error.recovery_hint['safe_to_retry'] is False
    assert failure.error.recovery_hint['preserve_requested_scope'] is True
    assert failure.error.recovery_hint['allow_proxy_substitution'] is False
    assert 'canonical pool' in failure.error.message
    assert scope.current_contract(config) == original
    assert not config.paths.proposals.exists()


@pytest.mark.parametrize('market,fields', [
    ('ETHEREUM:WETH/USDC', {'protocol', 'base'}),
    ('ETHEREUM:USDC', {'markets[0]'}),
    ('UNISWAP_V3:WETH/USDC', {'base'}),
    ('UNISWAP:ETH/USDC', {'protocol'}),
    ('UNISWAP_V2:ETH/USDC', {'markets[0]'}),
    ('UNISWAP_V3:ETH/USDT', {'quote'}),
])
def test_lp_draft_preserves_exact_scope_diagnostics(lp_scope, market, fields):
    config, contract = lp_scope
    original = deepcopy(contract)
    call = ToolCall(name='strategy_draft_proposal', arguments={
        'strategy_id': 'lp_scope_fixture', 'markets': [market],
        'strategy_class': 'trend', 'timeframe': '1h', 'mode': 'paper',
        'execution_mode': 'script', 'prompt': TEXT,
        'description': 'Uniswap V3 mainnet pool 0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640',
    })
    failure = strategy_runtime.strategy_draft_proposal_handler(call, config=config)
    assert failure.is_error
    differences = failure.error.detail['differences']
    assert fields <= {item['field'] for item in differences}
    hint = failure.error.recovery_hint
    assert hint['action'] == 'repair_candidate_resource'
    repairable = failure.content[0].data['reason'] == 'request_scope_mismatch'
    assert hint['preserve_candidate'] is True and hint['safe_to_retry'] is repairable
    assert failure.error.retryable is repairable
    visible = provider_text(failure)
    for field in fields:
        assert field in visible
    assert 'Do not change request scope' in visible or 'preserve the admitted resource and window' in visible
    assert scope.current_contract(config) == original
    assert not config.paths.proposals.exists()


@pytest.mark.parametrize('identity', ['fresh', 'cached', 'inline_wrong', 'top_level_wrong'])
def test_draft_admits_once_defaults_chain_before_compare_and_preserves_explicit_identity(
    env, fake_execution, monkeypatch, identity,
):
    from nerya.core import yaml_io
    from nerya.tools.result_contracts import tool_json_data

    admit(env, cid='solana-draft', sid='solana-draft',
          text='Jupiter Solana SOL/USDC strategy; no trading.')
    gateway = FakeGateway(lambda docs: spans_output(docs[0], venue='Jupiter',
        chain='Solana', base='SOL', quote='USDC'))
    install_fake(monkeypatch, gateway)
    if identity != 'fresh':
        scope.ensure_contract(env, gateway=gateway)
    args = {'strategy_id': 'solana_draft', 'markets': ['JUPITER:SOL/USDC'],
            'accounts': ['paper_main'], 'create_tuning': False}
    if identity == 'inline_wrong':
        args.update(chain='solana', files={'strategy.yml': yaml_io.dumps({'chain': 'ethereum'})})
    elif identity == 'top_level_wrong':
        args['chain'] = 'ethereum'
    before = deepcopy(args)
    result = strategy_runtime.strategy_draft_proposal_handler(
        ToolCall(name='strategy_draft_proposal', arguments=args), config=env)
    assert args == before
    if identity.endswith('wrong'):
        data = tool_json_data(result)
        assert result.is_error and data['reason'] == 'request_scope_mismatch'
        assert {'field': 'chain', 'expected': 'solana', 'actual': 'ethereum'} in data['differences']
        assert result.error.retryable is True
        assert result.error.recovery_hint['safe_to_retry'] is True
        assert result.error.recovery_hint['action'] == 'repair_candidate_resource'
        assert 'Do not retry this call or switch tools' not in provider_text(result)
        assert not env.paths.proposals.exists()
        args.pop('files', None)
        args.pop('chain', None)
        result = strategy_runtime.strategy_draft_proposal_handler(
            ToolCall(name='strategy_draft_proposal', arguments=args), config=env)
    data = tool_json_data(result)
    assert not result.is_error, data
    saved = yaml_io.load(env.paths.root / data['proposal_paths']['strategy_root'] / 'strategy.yml')
    assert saved['chain'] == 'solana'
    assert len(gateway.calls) == 1
    assert 'window_days' not in scope.current_contract(env)['constraints']


def test_lp_scope_diagnostic_is_bounded_redacted_and_exception_safe(lp_scope, monkeypatch):
    config, _ = lp_scope
    secret = 'sk-' + 's' * 48
    differences = [{'field': 'base', 'expected': 'ETH', 'actual': secret + 'x' * 10000,
                    'private': secret}] * 20
    def reject(*args, **kwargs):
        raise scope.ScopeError('request_scope_mismatch', differences)
    monkeypatch.setattr(scope, 'compare', reject)
    failure, pinned = scope.guard(config, ToolCall(name='strategy_draft_proposal'), manifest={})
    assert pinned == {}
    assert secret not in json.dumps(failure.asdict())
    assert len(failure.error.detail['differences']) == 4
    assert 'private' not in failure.error.detail['differences'][0]
    assert len(failure.error.message) < 2400
    def unknown(*args, **kwargs):
        raise RuntimeError(secret)
    monkeypatch.setattr(scope, 'compare', unknown)
    failure, _ = scope.guard(config, ToolCall(name='strategy_draft_proposal'), manifest={})
    assert secret not in json.dumps(failure.asdict())
    assert failure.error.detail['differences'] == []


def test_lp_invalid_binding_is_not_repaired_as_candidate_scope(lp_scope):
    config, _ = lp_scope
    config.data['runtime']['financial_actor_id'] = 'foreign-owner'
    failure, pinned = scope.guard(config, ToolCall(name='strategy_draft_proposal'),
                                  manifest={'markets': ['UNISWAP_V3:ETH/USDC']})
    assert failure.is_error and pinned == {}
    assert failure.content[0].data['reason'] == 'request_scope_unverified'
    assert not config.paths.proposals.exists()

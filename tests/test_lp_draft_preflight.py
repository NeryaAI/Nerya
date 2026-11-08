"""Offline LP admission: no pool means no model call or staged mutation."""
import pytest

from nerya.core import yaml_io
from nerya.strategies import request_scope as scope
from nerya.tools.native import strategy_runtime
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall
from nerya.strategies.execution_contract import _lp_configuration_gaps
from test_request_scope import FakeGateway, admit, spans_output, install_fake
from test_request_scope import env as env, fake_execution as fake_execution
from test_lp_scope_diagnostics import TEXT

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize('saved_scope', [False, True])
@pytest.mark.parametrize('source', [None, {}, {'pool_id': 'ETH/USDC'}, {
    'chain': 'ethereum', 'protocol': 'uniswap_v3', 'pool_id': 'ETH/USDC',
    'token0': 'ETH', 'token1': 'USDC', 'decimals': [18, 6], 'fee': 500, 'tick_spacing': 10}])
def test_minimal_lp_draft_blocks_before_scope_extraction(env, saved_scope, source):
    admit(env, cid='lp-minimal', sid='lp-session', text=TEXT)
    if saved_scope:
        scope.ensure_contract(env, gateway=FakeGateway(lambda docs: spans_output(docs[0],
            protocol='Uniswap V3', base='ETH', quote='USDC', window_days='30天')))
    before = scope._snapshot(env)
    files = {} if source is None else {'strategy.yml': yaml_io.dumps({
        'markets': ['uniswap_v3:ETH/USDC'], 'params': {'lp_source': source}})}
    result = strategy_runtime.strategy_draft_proposal_handler(ToolCall(
        name='strategy_draft_proposal', arguments=dict(strategy_id='lp_minimal',
        markets=['uniswap_v3:ETH/USDC'], mode='paper', execution_mode='script',
        prompt=TEXT, files=files)), config=env)
    data = tool_json_data(result)
    assert data['reason'] == 'capability_gap', data
    assert data['capability_gap']['code'] == 'lp_pool_identity_required'
    assert data['status'] == 'blocked'
    assert data['financial_execution_allowed'] is False
    assert data['historical_data_verified'] is False
    assert result.is_error and result.error.retryable is False
    assert result.error.recovery_hint['safe_to_retry'] is False
    hint = data['next_required_action']
    assert hint['action'] == 'inspect_or_bind_manifest'
    assert hint['tool'] == 'Skill'
    assert hint['arguments']['file'] == 'references/mainstream-strategies.md'
    assert 'params.lp_source' in hint['field']
    assert 'Do not repeat discovery' in hint['message']
    assert hint['required_binding_fields'] == [
        'chain', 'protocol', 'pool_id', 'token0', 'token1',
        'decimals', 'fee', 'tick_spacing',
    ]
    assert scope._snapshot(env) == before
    assert not env.paths.proposals.exists()


def test_sampled_pool_preflight_has_no_wallet_requirement_but_lifecycle_does(env):
    source = {
        'chain': 'ethereum', 'protocol': 'uniswap_v3',
        'pool_id': '0x' + '33' * 20,
        'token0': '0x' + '11' * 20, 'token1': '0x' + '22' * 20,
        'decimals': [6, 18], 'fee': 500, 'tick_spacing': 10,
    }
    sampled_args = {
        'strategy_id': 'lp_sampled_fixture',
        'markets': ['UNISWAP_V3:WETH/USDC'],
        'mode': 'paper', 'execution_mode': 'script',
        'files': {'strategy.yml': yaml_io.dumps({
            'params': {'lp_source': source, 'lp_sampled_policy': {
                'enter_when_empty': True,
                'exit_below_principal_token1': '0',
                'collect_min_fees_token1': '1',
            }, 'lp_replay': {'dataset_id': 'fixture'}},
        })},
    }
    request = strategy_runtime._request_from_args(sampled_args)
    assert strategy_runtime._lp_draft_preflight(
        ToolCall(name='strategy_draft_proposal', arguments=sampled_args), request
    ) is None

    incomplete = dict(source)
    incomplete.pop('tick_spacing')
    blocked_args = {**sampled_args, 'files': {'strategy.yml': yaml_io.dumps({
        'params': {'lp_source': incomplete},
    })}}
    blocked_request = strategy_runtime._request_from_args(blocked_args)
    failure = strategy_runtime._lp_draft_preflight(
        ToolCall(name='strategy_draft_proposal', arguments=blocked_args), blocked_request
    )
    assert failure is not None
    assert 'wallet_id' not in failure.error.recovery_hint['required_binding_fields']
    assert 'component_binding' not in failure.error.recovery_hint['required_binding_fields']

    lifecycle = {
        'lp_policy': {'max_age_seconds': 60, 'persistence_seconds': 10,
                      'cooldown_seconds': 100, 'cost_multiple': 2,
                      'enter_when_empty': True, 'exit_below_principal_usd': 0,
                      'collect_min_fees_usd': 1},
        'lp_source': {**source, 'source_id': 'declared', 'token_id': None,
                      'component_binding': {'id': 'builtin:uniswap',
                                            'revision': 'r', 'abi_version': 1},
                      'account_request': {'kind': 'lp_add',
                                          'component_id': 'builtin:uniswap',
                                          'chain': 'ethereum', 'protocol': 'uniswap_v3',
                                          'parameters': {'pool_id': source['pool_id']}}},
        'lp_execution': {'operation_key': 'cycle', 'requests': {}},
    }
    gaps = _lp_configuration_gaps(lifecycle)
    assert 'params.lp_source.wallet_id' in gaps
    assert 'params.lp_source.account_request.wallet_id' in gaps
    assert 'params.lp_execution.requests (nonempty mapping required)' in gaps


def test_protocol_rejection_names_exact_branch_without_echoing_span(env):
    admit(env, cid='lp-protocol', sid='lp-session', text=TEXT)
    def choose(docs):
        return spans_output(docs[0], protocol='Uniswap V', base='ETH',
                            quote='USDC', window_days='30天')
    gateway = FakeGateway(choose)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env, gateway=gateway)
    assert raised.value.diagnostic_code == 'invalid_semantics'
    assert raised.value.diagnostic_metadata == {
        'field': 'protocol', 'reason': 'unsupported_protocol_identity'}
    assert len(gateway.calls) == 1


def test_supported_protocol_span_preserves_native_asset_and_window(env):
    admit(env, cid='lp-protocol', sid='lp-session', text=TEXT)
    contract = scope.ensure_contract(env, gateway=FakeGateway(lambda docs:
        spans_output(docs[0], protocol='Uniswap V3', base='ETH',
                     quote='USDC', window_days='30天')))
    assert contract['constraints']['protocol']['id'] == 'uniswap_v3'
    assert contract['constraints']['base'] == 'ETH'
    assert contract['constraints']['window_days'] == 30


@pytest.mark.parametrize('valid_scope', [False, True])
def test_draft_scope_guard_owns_extraction_and_preserves_identity_defaults(
    env, fake_execution, monkeypatch, valid_scope,
):
    admit(env, cid='draft-identity', sid='draft-identity-session',
          text='Aave V3 Ethereum USDC strategy; last 30 days; no trading.')
    gateway = FakeGateway(lambda docs: spans_output(docs[0],
        protocol='Aave V3' if valid_scope else 'Aave V', chain='Ethereum',
        asset='USDC', window_days='30 days'))
    install_fake(monkeypatch, gateway)
    result = strategy_runtime.strategy_draft_proposal_handler(ToolCall(
        name='strategy_draft_proposal', arguments={
            'strategy_id': 'draft_identity', 'markets': ['AAVE_V3:ETH-USDC'],
            'accounts': ['paper_main'], 'create_tuning': False,
        }), config=env)
    data = tool_json_data(result)
    assert len(gateway.calls) == 1
    if valid_scope:
        assert not result.is_error, data
        root = env.paths.root / data['proposal_paths']['strategy_root']
        saved = yaml_io.load(root / 'strategy.yml')
        assert saved['chain'] == 'ethereum'
        assert saved['protocol'] == 'aave_v3'
    else:
        assert result.is_error
        assert data['reason'] == 'request_scope_unverified'
        assert data['diagnostic_code'] == 'invalid_semantics'
        assert data['diagnostic_metadata'] == {
            'field': 'protocol', 'reason': 'unsupported_protocol_identity'}
        assert result.error.recovery_hint == data['next_required_action']
        assert result.error.retryable is False
        assert not env.paths.proposals.exists()


def test_pool_declaration_stays_in_same_task_draft_without_history_claim(env, fake_execution, monkeypatch):
    import re
    from pathlib import Path
    from nerya.tools.native.skill import SkillIndex
    from nerya.tools.native.skill_tool import skill_tool_handler

    guide = skill_tool_handler(ToolCall(name='Skill', arguments={
        'skill': 'strategy_author', 'file': 'references/lp-sampled-research.md',
    }), skill_index=SkillIndex([Path(__file__).parents[1] / 'nerya/skills/builtin']))
    assert not guide.is_error, guide.text()
    config_example = re.search(r'```yaml\n(tuning:.*?)\n```', guide.text(), re.S)
    assert config_example, 'LP scaffold must provide schema-valid tuning objectives'
    tuning = yaml_io.loads(config_example.group(1))['tuning']
    assert tuning['objectives'] == ['execution_quality']
    admit(env, cid='lp-bound', sid='lp-session', text=TEXT)
    gateway = FakeGateway(lambda docs: spans_output(docs[0], protocol='Uniswap V3',
        base='ETH', quote='USDC', window_days='30天'))
    install_fake(monkeypatch, gateway)
    source = dict(chain='ethereum', protocol='uniswap_v3', pool_id='0x' + '33' * 20,
        token0='0x' + '11' * 20, token1='0x' + '22' * 20,
        decimals=[6, 18], fee=500, tick_spacing=10)
    manifest = dict(markets=['UNISWAP_V3:ETH/USDC'], mode='paper', execution_mode='script',
        schedule={'type': 'none', 'enabled': False}, params={'lp_source': source}, tuning=tuning)
    result = strategy_runtime.strategy_draft_proposal_handler(ToolCall(
        name='strategy_draft_proposal', arguments=dict(strategy_id='lp_bound',
        markets=manifest['markets'], accounts=['paper_main'], execution_mode='script',
        files={'strategy.yml': yaml_io.dumps(manifest),
               'main.py': 'def run(ctx): return ctx.result.hold(reason="fixture")\n',
               'strategy.md': 'ETH/WETH mapping requires an explicit admitted task amendment.'})), config=env)
    data = tool_json_data(result)
    assert not result.is_error, data
    root = env.paths.root / data['proposal_paths']['strategy_root']
    saved = yaml_io.load(root / 'strategy.yml')
    assert saved['params']['lp_source'] == source
    assert saved['markets'] == ['UNISWAP_V3:ETH/USDC']
    assert saved['tuning']['objectives'] == ['execution_quality']
    checks = {row['check_id']: row['status'] for row in result.metadata['verification']['checks']}
    assert checks['strategy_contract'] == 'passed'
    binding = data['lp_binding']
    assert binding['proposal_id'] == data['proposal_id']
    assert binding['historical_data_verified'] is False
    assert binding['replay_status'] == 'blocked'
    assert scope.current_contract(env)['constraints']['base'] == 'ETH'
    assert len(gateway.calls) == 1

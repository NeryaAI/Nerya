"""Read-only discovery failures retain bounded recovery across model/UI projection."""
from types import SimpleNamespace

import pytest

from nerya.agent.loop import WorkspaceNativeAgentLoop
from nerya.agent.tool_projection import project_tool_results
from nerya.data_api import DataApiError, build_data_api_registry
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize('stage', ['deadline', 'transport', 'request_budget', 'reorg'])
def test_discovery_diagnostic_survives_model_and_event_without_raw_provider_data(stage, monkeypatch):
    registry = build_data_api_registry()

    def fail(*args, **kwargs):
        raise DataApiError('Discovery failed', kind='provider_error', retryable=False, detail={
            'provider': 'uniswap', 'action': 'pool_discover', 'stage': stage,
            'request_count': 27, 'rpc_method': 'eth_call',
            'raw_body': 'PRIVATE', 'input': {'secret': 'PRIVATE'},
        })

    monkeypatch.setattr(registry, 'call', fail)
    result = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'call', 'provider': 'uniswap', 'action': 'pool_discover',
        'args': {'base': 'WETH', 'quote': 'USDC'}}), registry=registry)
    loop = object.__new__(WorkspaceNativeAgentLoop)
    loop.config = SimpleNamespace(workspace_root='/tmp/discovery-test')
    rendered = loop._render_tool_result(result)
    event = project_tool_results([result], render_tool_result=loop._render_tool_result,
        rendered_tool_result_text=lambda value: None).event_blocks[0]
    assert result.error.retryable is False
    assert result.error.recovery_hint['safe_to_retry'] is False
    assert result.error.recovery_hint['request_count'] == 27
    assert result.error.recovery_hint['stage'] == stage
    assert result.error.recovery_hint['rpc_method'] == 'eth_call'
    assert 'Do not retry unchanged' in str(rendered)
    assert stage in str(rendered) and '27' in str(rendered)
    assert event['recovery']['safe_to_retry'] is False
    assert 'PRIVATE' not in str(rendered) + str(event)


def test_catalog_identity_error_is_not_relabelled_as_deadline(monkeypatch):
    registry = build_data_api_registry()

    def fail(*args, **kwargs):
        raise DataApiError('Unknown catalog token', kind='schema_validation', retryable=False,
            detail={'reason': 'unknown_catalog_token', 'input_field': 'base'})

    monkeypatch.setattr(registry, 'call', fail)
    result = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'call', 'provider': 'uniswap', 'action': 'pool_discover',
        'args': {'base': 'ETH', 'quote': 'USDC'}}), registry=registry)
    assert result.error.recovery_hint['action'] == 'inspect_schema'
    assert 'deadline' not in result.error.message


@pytest.mark.parametrize('count,method', [(True, ['eth_call']), ('27', {'private': 'PRIVATE'}),
                                        (-1, 'PRIVATE'), (65, 'eth_call')])
def test_untrusted_diagnostic_fields_are_not_mirrored(count, method, monkeypatch):
    registry = build_data_api_registry()

    def fail(*args, **kwargs):
        raise DataApiError('Discovery failed', kind='provider_error', retryable=False, detail={
            'stage': 'deadline', 'request_count': count, 'rpc_method': method})

    monkeypatch.setattr(registry, 'call', fail)
    result = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'call', 'provider': 'uniswap', 'action': 'pool_discover',
        'args': {'base': 'WETH', 'quote': 'USDC'}}), registry=registry)
    assert result.is_error
    assert 'request_count' not in result.error.recovery_hint
    assert 'PRIVATE' not in result.error.message


def test_history_access_failure_is_not_transient_or_rpc_switch_permission(monkeypatch):
    registry = build_data_api_registry()

    def fail(*args, **kwargs):
        raise DataApiError('History access denied', kind='configuration_required', retryable=False,
            detail={'reason': 'historical_rpc_authorization_required'})

    monkeypatch.setattr(registry, 'call', fail)
    result = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'call', 'provider': 'uniswap', 'action': 'pool_history',
        'args': {'pool_address': '0x' + '11' * 20, 'start_block': 1, 'end_block': 2,
                 'step_blocks': 1, 'max_samples': 2, 'tick_lower': 0, 'tick_upper': 60}}), registry=registry)
    assert result.error.recovery_hint['safe_to_retry'] is False
    assert 'not a transient' in result.error.message
    assert 'Do not switch rpc_provider' in result.error.message
    assert 'explicit authorized configuration change' in result.error.message


def test_history_evidence_failure_is_not_retried_unchanged(monkeypatch):
    registry = build_data_api_registry()

    def fail(*args, **kwargs):
        raise DataApiError('Historical RPC evidence failure: deadline', kind='provider_error',
            retryable=False, detail={'reason': 'historical_rpc_evidence_failure',
                                    'stage': 'deadline', 'request_count': 33})

    monkeypatch.setattr(registry, 'call', fail)
    result = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'call', 'provider': 'uniswap', 'action': 'pool_history',
        'args': {'pool_address': '0x' + '11' * 20, 'start_block': 1, 'end_block': 2,
                 'step_blocks': 1, 'max_samples': 2, 'tick_lower': 0, 'tick_upper': 60}}), registry=registry)
    assert result.error.recovery_hint['safe_to_retry'] is False
    assert 'Do not retry unchanged' in result.error.message
    assert 'no verified historical state' in result.error.message

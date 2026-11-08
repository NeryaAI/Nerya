"""Offline provider-failure diagnostics for request-scope extraction."""
# ruff: noqa: F811 -- imported pytest fixture
import pytest

from nerya.core.errors import LLMError, LLMStructuredOutputError, LLMTaskNotAllowed
from nerya.harness.cancellation import CancelledError
from nerya.llm.gateway import LLMGateway
from nerya.strategies import request_scope as scope
from nerya.tools.types import ToolCall
from test_request_scope import env  # noqa: F401

pytestmark = pytest.mark.smoke
PRIVATE = 'PRIVATE_PROVIDER_BODY token=credential /private/provider/path'


def _raise(monkeypatch, error):
    calls = []

    def provider_failure(self, **kwargs):
        calls.append(kwargs)
        raise error

    monkeypatch.setattr(LLMGateway, 'call_messages', provider_failure)
    return calls


def test_typed_transport_metadata_preserves_status_without_provider_details(env, monkeypatch):
    error = LLMError(PRIVATE)
    error.status_code = 503
    error.retryable = True
    calls = _raise(monkeypatch, error)

    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env)

    assert raised.value.diagnostic_code == 'transport_error'
    assert raised.value.diagnostic_metadata == {
        'status_code': 503, 'error_type': 'LLMError', 'error_category': 'transport'}
    assert PRIVATE not in repr(raised.value.diagnostic_metadata)
    assert len(calls) == 1


def test_structured_tool_and_configuration_types_are_distinguishable(env, monkeypatch):
    structured = LLMStructuredOutputError(PRIVATE)
    structured.status_code = 400
    calls = _raise(monkeypatch, structured)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env)
    assert raised.value.diagnostic_code == 'transport_error'
    assert raised.value.diagnostic_metadata == {
        'status_code': 400, 'error_type': 'LLMStructuredOutputError',
        'error_category': 'structured_tool'}
    assert len(calls) == 1

    config_error = LLMTaskNotAllowed(PRIVATE)
    calls = _raise(monkeypatch, config_error)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env)
    assert raised.value.diagnostic_code == 'transport_error'
    assert raised.value.diagnostic_metadata == {
        'error_type': 'LLMTaskNotAllowed', 'error_category': 'configuration'}
    assert len(calls) == 1


def test_rate_limit_status_and_unknown_exception_remain_bounded(env, monkeypatch):
    rate_limit = LLMError(PRIVATE)
    rate_limit.status_code = 429
    calls = _raise(monkeypatch, rate_limit)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env)
    assert raised.value.diagnostic_code == 'provider_rate_limited'
    assert raised.value.diagnostic_metadata == {
        'status_code': 429, 'error_type': 'LLMError', 'error_category': 'rate_limit'}
    assert len(calls) == 1

    quota = LLMError(PRIVATE)
    quota.code = 'model_quota_exhausted'
    quota.quota_exhausted = True
    calls = _raise(monkeypatch, quota)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env)
    assert raised.value.diagnostic_code == 'model_quota_exhausted'
    assert raised.value.diagnostic_metadata == {
        'error_type': 'LLMError', 'error_category': 'quota'}
    assert len(calls) == 1

    unknown = RuntimeError(PRIVATE)
    calls = _raise(monkeypatch, unknown)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env)
    assert raised.value.diagnostic_code == 'transport_error'
    assert raised.value.diagnostic_metadata == {}
    assert len(calls) == 1


@pytest.mark.parametrize('value', [True, False, 99, 600, 503.0, '503', PRIVATE])
def test_status_metadata_accepts_only_bounded_http_integers(value):
    error = scope.ScopeError(diagnostic_metadata={'status_code': value, 'raw': PRIVATE})
    assert error.diagnostic_metadata == {}


def test_configuration_failure_names_route_policy_not_candidate_repair(env, monkeypatch):
    def fail(*args, **kwargs):
        raise scope.ScopeError(diagnostic_code='transport_error', diagnostic_metadata={
            'error_type': 'LLMTaskNotAllowed', 'error_category': 'configuration'})

    monkeypatch.setattr(scope, 'ensure_contract', fail)
    result, _ = scope.guard(env, ToolCall(name='strategy_draft_proposal', arguments={}))
    assert result.error.recovery_hint['action'] == 'inspect_scope_model_policy'
    assert 'Changing candidate files cannot repair this' in result.error.message
    assert result.error.retryable is False


def test_freeform_guard_preserves_quota_blocker_as_route_recovery(env, monkeypatch):
    env.data.setdefault('runtime', {})['command_id'] = 'quota-command'
    error = scope.ScopeError(diagnostic_code='model_quota_exhausted',
                             diagnostic_metadata={'error_type': 'LLMError', 'error_category': 'quota'})
    monkeypatch.setattr(scope, 'current_contract', lambda *args, **kwargs: (_ for _ in ()).throw(error))
    failure, callback = scope.freeform_guard(env, ToolCall(name='strategy_backtest'))
    assert callback is None
    assert failure.content[0].data['reason'] == 'model_quota_exhausted'
    assert failure.error.recovery_hint['action'] == 'restore_scope_model_quota'
    assert failure.error.recovery_hint['diagnostics'] == {
        'error_type': 'LLMError', 'error_category': 'quota'}
    assert failure.error.retryable is False


def test_cancellation_is_preserved_without_scope_wrapping(env, monkeypatch):
    calls = _raise(monkeypatch, CancelledError('private cancellation detail'))
    snapshot = scope._snapshot(env, active=True)
    with pytest.raises(CancelledError):
        scope._extract(env, snapshot, gateway=scope.LLMGateway(env), token=None)
    assert len(calls) == 1

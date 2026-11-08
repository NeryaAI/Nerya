"""Equivalent source quotations retain the original admission and time anchor."""
import pytest

from nerya.strategies import request_scope as scope
from test_request_scope import FakeGateway, assistant_chain, output
from test_request_scope import env  # noqa: F401


def test_followup_may_quote_same_original_duration_with_past_qualifier(env):  # noqa: F811
    original = assistant_chain(env, 'Continue without changing the request.')
    contract = scope.ensure_contract(env, gateway=FakeGateway(
        lambda docs: output(docs[0], window='last 30 days')))
    assert contract['constraints'] == original['constraints']
    assert contract['revision'] == original['revision']
    assert contract['binding']['original_command_id'] == 'child-create'
    assert scope.current_contract(env) == contract


def test_followup_cannot_change_original_duration_without_amendment(env):  # noqa: F811
    assistant_chain(env, 'Continue without changing the request.')
    def change(docs):
        extracted = output(docs[0])
        span = extracted['fields']['window_days']
        span['text'] = '3 days'
        return extracted
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=FakeGateway(change))

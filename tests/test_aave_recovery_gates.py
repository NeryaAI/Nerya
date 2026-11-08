"""A closed decision gate must not erase an already submitted action."""

import pytest

from test_strategy_author_aave_reference import (
    tick,
    submit,
    chain_receipt,
    save_prepared,
)
from nerya.financial.store import FinancialStore

pytestmark = pytest.mark.smoke
pytest_plugins = ("test_strategy_author_aave_reference",)


@pytest.mark.parametrize("gate", [{"data_ready": False}, {"decision": False}])
def test_closed_gate_keeps_pending_original_action(example, context, gate):
    action = submit(context, tick(example, context))
    result = tick(example, context, **gate)
    assert result.metadata["financial_action"]["action_id"] == action["action_id"]
    assert result.status.value == "submitted"
    assert "funds_sent" not in result.metadata
    assert context.rpc.sends == 1


def test_closed_gate_observes_settled_action_without_new_deposit(example, context):
    action = submit(context, tick(example, context))
    chain_receipt(context)
    result = tick(example, context, operation_key="new-cycle", decision=False)
    assert result.metadata["financial_action"]["action_id"] == action["action_id"]
    assert result.status.value == "filled"
    assert context.rpc.sends == 1


def test_closed_gate_cannot_execute_prepared_action(example, context):
    action = save_prepared(context)
    result = tick(example, context, decision=False)
    assert result.metadata["financial_action"]["action_id"] == action["action_id"]
    assert result.status.value == "hold"
    assert context.financial.get(action["action_id"])["state"] == "prepared"
    assert context.rpc.sends == 0


def test_stale_data_preserves_pending_approval_without_new_prepare(example, context):
    original = tick(example, context)
    action_id = original.metadata["financial_action"]["action_id"]
    result = tick(example, context, data_ready=False, operation_key="later-cycle")
    assert result.status.value == "pending_approval"
    assert result.metadata["financial_action"]["action_id"] == action_id
    assert result.approval_id == original.approval_id
    assert context.rpc.sends == 0


def test_closed_gate_unknown_action_retains_identity_without_reprepare(
    example, context
):
    action = save_prepared(context)
    store = FinancialStore(context.config)
    store.mark(action["action_id"], state="future_unknown_state")
    result = tick(example, context, decision=False, operation_key="new-cycle")
    assert result.error_kind == "execution_unknown"
    assert result.metadata["financial_action"]["action_id"] == action["action_id"]
    assert len(store.list_actions(context.identity)) == 1
    assert context.rpc.sends == 0

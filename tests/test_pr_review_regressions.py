"""Offline regression coverage for the combined PR 20/21/22 safety review."""
from dataclasses import replace
import json
import time

import pytest

from nerya.agent.task_runs import business_result
from nerya.agent.loop import _build_deterministic_final_summary
from nerya.core.errors import LLMError
from nerya.financial.contracts import FinancialContext
from nerya.financial.store import FinancialStore
from nerya.llm.messages import MessagesRequest, OpenAIMessagesBackend, _openai_stream_event_error
from nerya.trading.portfolio_risk import RiskMetrics, evaluate, reduces_risk
from test_financial_authorizations import setup, request
from test_task_reliability import Disconnect
from test_task_run_admission import runs, admit

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize("payload", [
    {"code": "rate_limit_exceeded", "retryable": False},
    {"code": "overloaded_error", "retryable": False},
    {"code": "insufficient_quota"},
    {"message": "Insufficient balance or no resource package"},
])
def test_statusless_sse_errors_respect_provider_retry_veto(payload):
    error = _openai_stream_event_error("fixture", payload)
    assert error.retryable is False
    if "quota" in payload.get("code", "") or "balance" in payload.get("message", ""):
        assert error.code == "model_quota_exhausted"


def test_upstream_native_capabilities_are_not_replayed_on_empty_disconnect():
    transport = Disconnect()
    backend = OpenAIMessagesBackend(api_key="fixture", model="gpt-4o", transport=transport)
    with pytest.raises(LLMError) as caught:
        backend(MessagesRequest(system="fixture", messages=[], stream=True,
            metadata={"provider_native_web_search": {"enabled": True}}))
    assert caught.value.stream_output_committed is False
    assert caught.value.retryable is False
    assert transport.calls == 1


@pytest.mark.parametrize("reason, expected", [
    ("cancelled:command_control_unavailable", "并非用户主动取消"),
    ("cancelled:command_lease_lost", "执行租约已丢失"),
    ("cancelled:event_persistence_failed", "执行记录未能可靠保存"),
])
def test_system_cancellation_preserves_the_actionable_cause(reason, expected):
    result = _build_deterministic_final_summary(iterations=1, tool_calls=0, error_count=0,
        abort_reason=reason, had_model_text=False)
    assert expected in result


@pytest.mark.parametrize("state", [
    "prepared", "awaiting_approval", "reserved", "submitting", "confirmed",
    "failed_before_submission", "rejected",
])
def test_reconcile_always_returns_redacted_public_records(tmp_path, state):
    store, gateway, context, adapter, _ = setup(tmp_path)
    original_quote = adapter.quote
    secret = "review-fixture-not-a-real-secret"
    adapter.quote = lambda raw: {**original_quote(raw), "api_key": secret}
    action = gateway.prepare(context, request(), action_key="redacted")
    if state != "prepared":
        store.mark(action["action_id"], state=state)
    internal = store.get_action(action["action_id"], context, internal=True)
    assert internal["quote"]["api_key"] == secret
    result = gateway.reconcile(context, action["action_id"])
    assert result["state"] == state
    assert secret not in json.dumps(result)
    assert adapter.calls == 0


def test_submission_receipt_cannot_release_reservation_as_never_submitted(tmp_path):
    store, gateway, context, adapter, _ = setup(tmp_path)

    def inconsistent_execute(raw, quote, submitted):
        adapter.calls += 1
        submitted({"transaction_hash": "0x" + "2" * 64})
        return {"state": "failed_before_submission", "reason": "late_provider_error", "submission": {}}

    adapter.execute = inconsistent_execute
    action = gateway.prepare(context, request(), action_key="submission-boundary")
    result = gateway.execute(context, action["action_id"], quote_hash=action["quote_hash"])
    assert result["state"] == "unconfirmed"
    assert result["submission"]["transaction_hash"] == "0x" + "2" * 64
    with store.transaction() as con:
        usage = con.execute("SELECT state FROM financial_usage WHERE action_id=?", (action["action_id"],)).fetchone()
        capital = con.execute("SELECT state FROM capital_reservations WHERE reservation_id=?", ("financial:" + action["action_id"],)).fetchone()
    assert usage[0] == capital[0] == "reserved"
    assert gateway.execute(context, action["action_id"], quote_hash=action["quote_hash"])["duplicate"]
    assert adapter.calls == 1


@pytest.mark.parametrize("states, expected", [
    (["confirmed", "rejected"], "partial"),
    (["filled", "failed"], "partial"),
    (["unconfirmed", "rejected"], "unconfirmed"),
    (["submitted", "failed"], "submitted"),
])
def test_mixed_business_results_do_not_claim_full_success(states, expected):
    result = {"actions": [{"result": {"state": state}} for state in states]}
    assert business_result(result)["status"] == expected


@pytest.mark.parametrize("uncertain", ["unconfirmed", "needs_recovery", "submitted", "confirming"])
def test_failed_sibling_does_not_hide_pending_financial_receipt(tmp_path, uncertain):
    tasks = runs(tmp_path)
    run = admit(tasks)
    context = FinancialContext("operator", run_id=run["run_id"])
    funds = FinancialStore(tasks.config)
    for index, state in enumerate((uncertain, "rejected")):
        action = funds.prepare_action(context, request(), {
            "risk_usd": "10", "spend_usd": "10", "fee_usd": "0",
            "asset_amounts": {"USDC": "10"}, "available_asset_amounts": {"USDC": "1000"},
            "expires_at": time.time() + 60,
        }, action_key=str(index))
        funds.mark(action["action_id"], state=state)
    result = tasks.get(run["run_id"])
    expected = "submitted" if uncertain in {"submitted", "confirming"} else uncertain
    assert result["business_status"] == expected


def test_collateral_loss_is_not_exempted_by_small_notional_reduction():
    before = RiskMetrics(1000, gross_exposure_usd=1000, initial_margin_usd=500, maintenance_margin_usd=300)
    after = replace(before, equity_usd=200, gross_exposure_usd=999, initial_margin_usd=499)
    assert not reduces_risk(before, after)
    assert set(evaluate(before, after, {"max_margin_utilization": "0.8", "min_margin_buffer_usd": 100}, mode="reduce_only")) == {
        "trading_risk_mode_blocks_action", "margin_utilization_exceeded", "margin_buffer_insufficient",
    }


def test_genuine_margin_reduction_still_works_with_small_fees():
    before = RiskMetrics(1000, gross_exposure_usd=1000, initial_margin_usd=500, maintenance_margin_usd=300)
    after = replace(before, equity_usd=999, gross_exposure_usd=500, initial_margin_usd=250, maintenance_margin_usd=150)
    assert reduces_risk(before, after)
    assert evaluate(before, after, {}, mode="reduce_only") == []

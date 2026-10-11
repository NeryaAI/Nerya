import time
from dataclasses import replace

import pytest

from nerya.core import yaml_io
from nerya.financial.contracts import FinancialError, context_from_config
from nerya.financial.strategy_runtime import bind_run, finish_run, validate_context
from nerya.sdk.financial_api import FinancialAPI
from test_trading_kernel_safety import cfg

pytestmark = pytest.mark.smoke


def test_script_identity_is_read_from_producer_lease_not_caller_provenance(cfg):
    bound, context = bind_run(cfg, strategy_id="alpha", run_id="run-owned", mode="live")
    bound.data["runtime"]["financial_actor_id"] = "someone-else"
    derived = context_from_config(bound, actor_id="forged", scopes={"api:all"})
    assert derived.actor_id == context.actor_id == "strategy:alpha"
    assert derived.task_kind == "strategy_script"
    assert "api:all" not in derived.scopes
    with pytest.raises(FinancialError):
        validate_context(bound, replace(derived, actor_id="forged"))


def test_script_code_or_limits_change_invalidates_existing_financial_context(cfg):
    bound, context = bind_run(cfg, strategy_id="alpha", run_id="run-revision", mode="live")
    limits = cfg.paths.strategy("alpha") / "limits.yml"
    data = yaml_io.load(limits)
    data["max_single_order_usd"] = 1
    yaml_io.dump(limits, data)
    with pytest.raises(FinancialError, match="revision_changed"):
        validate_context(bound, context)


def test_finished_or_expired_tick_cannot_execute_new_financial_actions(cfg):
    bound, context = bind_run(cfg, strategy_id="alpha", run_id="run-finished", mode="live")
    finish_run(bound, context.strategy_run_id)
    with pytest.raises(FinancialError, match="not_active"):
        validate_context(bound, context)


def test_paper_financial_facade_cannot_prepare_or_execute_funds(cfg):
    facade = FinancialAPI(cfg, read_only=True)
    with pytest.raises(FinancialError, match="paper_run"):
        facade.prepare({"kind": "trade"}, client_request_id="paper")
    with pytest.raises(FinancialError, match="paper_run"):
        facade.execute("action", quote_hash="hash")


def test_script_action_can_create_a_manual_approval_without_an_agent_run_row(cfg):
    from nerya.financial.store import FinancialStore
    from nerya.financial.gateway import FinancialGateway
    bound, context = bind_run(cfg, strategy_id="alpha", run_id="script-needs-approval", mode="live")
    quote = {"risk_usd": "1", "spend_usd": "1", "fee_usd": "0", "asset_amounts": {"USDT": "1"},
             "available_asset_amounts": {"USDT": "10"}, "expires_at": time.time()+60}
    action = FinancialStore(bound).prepare_action(context, {"kind": "trade", "account_id": "acct_1", "market": "MOCK:SOLUSDT"},
                                                 quote, action_key="manual-script")
    result = FinancialGateway(bound)._approval(action)
    assert result["status"] == "approval_required" and result["run_id"] == context.run_id

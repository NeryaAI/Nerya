from dataclasses import replace
import time

import pytest

from nerya.financial.contracts import FinancialContext,FinancialError
from nerya.financial.gateway import FinancialGateway
from nerya.financial.trading_adapter import TradingFunds
from nerya.trading.capital import BudgetChecker,CapitalReservationStore
from nerya.trading.order_intents import SizingPolicy,TradePlan
from nerya.trading.position_book import PositionBook
from test_trading_kernel_safety import snapshot,order_args
from test_risk_gate import _build_workspace

pytestmark=pytest.mark.smoke


def setup(tmp_path):
    cfg=_build_workspace(tmp_path,venue="mock",market="MOCK:SOLUSDT",accounts=["acct_1"])
    cfg.data["financial"]={"enabled":True};cfg.data["runtime"]["live_trading_enabled"]=True
    from nerya.trading.accounts import get_account_profile
    profile=get_account_profile(cfg.paths,"acct_1")
    return cfg,BudgetChecker(profile=profile,snapshot=snapshot(),store=CapitalReservationStore(cfg.paths)),FinancialContext("operator",frozenset({"api:all"}))


def frozen(checker,candidate):
    return {"risk_usd":str(candidate.notional_usd),"spend_usd":"0" if candidate.reduce_only else str(candidate.required_collateral["USDT"]),
            "fee_usd":str(candidate.estimated_fee_usd),"asset_amounts":{},"available_asset_amounts":{},"expires_at":time.time()+30,
            "budget_decision":{"candidate":candidate.asdict()},"budget_free_usd":"1000","minimum_free_usd":"200"}


def reserve(gateway,ctx,candidate):
    quote=frozen(None,candidate)
    request={"kind":"trade","account_id":"acct_1","market":"MOCK:SOLUSDT","plan":{"strategy_id":"alpha"}}
    action=gateway.store.prepare_action(ctx,request,quote,action_key=str(candidate.size_base))
    approval=gateway._approval(gateway.store.get_action(action["action_id"],ctx,internal=True))
    with gateway.store.transaction() as con:con.execute("UPDATE approvals SET state='approved' WHERE id=?",(approval["approval_id"],))
    return gateway.store.reserve(action["action_id"],ctx,quote_hash=action["quote_hash"],approved=True)


def test_financial_and_legacy_gateway_share_owned_exit_reservations(tmp_path):
    cfg,checker,ctx=setup(tmp_path)
    book=PositionBook(cfg.paths)
    book.apply_fill(account_id="acct_1",strategy_id="alpha",market="MOCK:SOLUSDT",side="buy",price=100,size_base=10)
    first=checker.evaluate(**order_args(side="sell",reduce_only=True,sizing=SizingPolicy(method="fixed_base",fixed_base=6)))
    second=checker.evaluate(**order_args(side="sell",reduce_only=True,sizing=SizingPolicy(method="fixed_base",fixed_base=7)))
    gateway=FinancialGateway(cfg)
    reserve(gateway,ctx,first.candidate)
    with pytest.raises(FinancialError,match="close_quantity_already_reserved"):reserve(gateway,ctx,second.candidate)
    from nerya.core.errors import TradingError
    with pytest.raises(TradingError,match="close_quantity_already_reserved"):
        checker.store.reserve_checked(candidate=second.candidate,profile=checker.profile,snapshot=checker.snapshot)
    book.close()


def test_financial_gateway_keeps_minimum_free_cash_after_concurrent_quotes(tmp_path):
    cfg,checker,ctx=setup(tmp_path)
    first=checker.evaluate(**order_args(sizing=SizingPolicy(method="fixed_usd",fixed_usd=400)))
    second=checker.evaluate(**order_args(sizing=SizingPolicy(method="fixed_usd",fixed_usd=450)))
    gateway=FinancialGateway(cfg)
    reserve(gateway,ctx,first.candidate)
    with pytest.raises(FinancialError,match="free_balance_floor_changed"):reserve(gateway,ctx,second.candidate)


def test_real_typed_quote_uses_collateral_not_leveraged_notional(tmp_path,monkeypatch):
    cfg,checker,ctx=setup(tmp_path)
    monkeypatch.setattr("nerya.trading.submit._resolve_market_snapshot",lambda *a,**k:{"price":100,"age_s":0})
    monkeypatch.setattr("nerya.trading.account_snapshots.fresh_snapshot",lambda *a,**k:snapshot())
    plan=TradePlan(strategy_id="alpha",account_id="acct_1",market="MOCK:SOLUSDT",sizing=SizingPolicy(method="fixed_usd",fixed_usd=400),meta={"leverage":4})
    quote=TradingFunds(cfg,{}).quote({"kind":"trade","account_id":"acct_1","market":plan.market,"plan":plan.asdict()})
    assert quote["risk_usd"]=="400.0" and quote["spend_usd"]=="100.0"


def test_strategy_cannot_quote_another_strategy_position_via_generic_tools(tmp_path):
    cfg,_,ctx=setup(tmp_path)
    bound=replace(ctx,task_kind="strategy_agent",task_id="alpha")
    request={"kind":"trade","account_id":"acct_1","market":"MOCK:SOLUSDT","plan":{"strategy_id":"beta"}}
    with pytest.raises(FinancialError,match="strategy_financial_owner_mismatch"):
        FinancialGateway(cfg).prepare(bound,request,action_key="foreign-owner")

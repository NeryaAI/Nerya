from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from nerya.strategies.result import ResultBuilder

pytestmark=pytest.mark.smoke
ROOT=Path(__file__).parents[1]/'nerya/skills/builtin/strategy_author/templates'
RULES=runpy.run_path(str(ROOT/'risk_rules.py.j2'))


def exit_rule(**kwargs):
    return RULES['spot_exit'](**{'price':100,'entry_price':100,'high_water':110,'price_ts':1000,'now':1001,
        'opened_at':500,'max_age_seconds':30,'stop_loss_pct':.1,'take_profit_pct':.3,
        'trailing_stop_pct':.2,'max_holding_seconds':3600,**kwargs})


def test_deterministic_exits_have_explicit_reasons_and_stale_data_is_not_a_hold_signal():
    assert exit_rule()['action']=='hold'
    assert exit_rule(price=85)['reason']=='stop_loss'
    assert exit_rule(price=140)['reason']=='take_profit'
    assert exit_rule(price=95,high_water=125)['reason']=='trailing_stop'
    assert exit_rule(now=4200,price_ts=4200)['reason']=='holding_deadline'
    assert exit_rule(price_ts=900)['action']=='recover_data'
    with pytest.raises(ValueError): exit_rule(stop_loss_pct=10)


def rebalance_rule(**kwargs):
    return RULES['rebalance_signal'](**{'tick':120,'lower':-100,'upper':100,'outside_since':900,
        'last_rebalance':None,'now':1000,'observed_at':1000,'max_age_seconds':30,
        'persistence_seconds':60,'cooldown_seconds':300,'expected_incremental_fees':10,
        'total_execution_cost':1,'cost_multiple':2,**kwargs})


def test_lp_signal_uses_persistence_cooldown_and_cost_gate():
    assert rebalance_rule()['action']=='prepare_rebalance'
    assert rebalance_rule(tick=0)['reason']=='inside_range'
    assert rebalance_rule(outside_since=None)['reason']=='range_exit_first_observed'
    assert rebalance_rule(outside_since=999)['reason']=='range_exit_not_persistent'
    assert rebalance_rule(last_rebalance=900)['reason']=='rebalance_cooldown'
    assert rebalance_rule(expected_incremental_fees=None)['reason']=='fee_economics_unknown'
    assert rebalance_rule(total_execution_cost=10)['reason']=='fees_do_not_cover_execution'
    assert rebalance_rule(observed_at=900)['action']=='recover_data'


class State(dict):
    def set(self,key,value): self[key]=value


def test_lp_template_remembers_original_workflow_and_never_sends_in_paper():
    helper=runpy.run_path(str(ROOT/'lp_rebalance.py.j2'))['rebalance_tick']
    calls=[]
    row={'rebalance_id':'r','revision':1,'state':'confirming','cursor':0,'action_ids':['a']}
    def prepare(*a,**kw): calls.append('prepare');return row
    def advance(*a,**kw): calls.append('advance');return row
    api=SimpleNamespace(prepare_rebalance=prepare,advance_rebalance=advance,get_rebalance=lambda _:row)
    ctx=SimpleNamespace(mode='paper',state=State(),financial=api,result=ResultBuilder())
    assert helper(ctx,plan={},plan_key='owned-nft').metadata['funds_sent'] is False and calls==[]
    ctx.mode='live'
    helper(ctx,plan={},plan_key='owned-nft')
    helper(ctx,plan={},plan_key='owned-nft')
    assert calls==['prepare','advance','advance']
    row['state']='completed'
    result=helper(ctx,plan={},plan_key='owned-nft')
    assert result.reason=='LP range adjustment confirmed' and len(calls)==3


def test_financial_native_tools_are_registered_as_real_runtime_capabilities(tmp_path):
    from nerya.core.config import Config,DEFAULT_CONFIG
    from nerya.core.paths import WorkspacePaths
    from nerya.tools.registry import ToolRegistry
    from nerya.tools.native.financial import register_financial_tools
    from nerya.tools.types import RiskLevel
    registry=ToolRegistry()
    register_financial_tools(registry,SimpleNamespace(config=Config(WorkspacePaths(tmp_path),DEFAULT_CONFIG)))
    assert registry.get('financial_readiness').read_only
    assert registry.get('financial_rebalance_advance').risk==RiskLevel.DANGEROUS
    assert registry.get('financial_rebalance_prepare').risk==RiskLevel.WRITE
    assert not registry.has('financial_grant_approve')

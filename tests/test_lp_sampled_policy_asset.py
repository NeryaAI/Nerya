"""Synthetic asset decisions, never historical or runtime acceptance."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import runpy
import sys

from nerya.strategies.result import ResultBuilder

ASSETS=Path(__file__).resolve().parents[1]/'nerya/skills/builtin/strategy_author/templates'


def values():
    policy=dict(enter_when_empty=True,exit_below_principal_token1='10',collect_min_fees_token1='2')
    source=dict(chain='ethereum',protocol='uniswap_v3',pool_id='fixture',tick_lower=-100,tick_upper=100)
    snapshot=dict(schema='lp.snapshot.v1',counterfactual=True,accounting_currency='token1',
        chain='ethereum',protocol='uniswap_v3',pool_id='fixture',
        account={'positions':[]},pool={'tick_lower':-100,'tick_upper':100},
        economics={'total_execution_cost_token1':'1'})
    return policy,source,snapshot


def test_quote_policy_entry_hold_collect_exit_no_swaps_or_usd():
    fn=runpy.run_path(str(ASSETS/'lp_sampled_policy.py.j2'))['decide_sampled_lp']
    policy,source,snapshot=values()
    assert fn(policy=policy,source=source,snapshot=snapshot)['action']=='lp_add'
    position={'principal_value_token1':'20','owed_value_token1':'0'}
    snapshot['account']['positions']=[position]
    assert fn(policy=policy,source=source,snapshot=snapshot)['action']=='hold'
    position['owed_value_token1']='3'
    assert fn(policy=policy,source=source,snapshot=snapshot)['action']=='lp_collect'
    position['principal_value_token1']='9'
    assert fn(policy=policy,source=source,snapshot=snapshot)['action']=='lp_remove'


def test_saved_asset_live_closed_and_backtest_uses_actual_policy(monkeypatch):
    module=SimpleNamespace(**runpy.run_path(str(ASSETS/'lp_sampled_policy.py.j2')))
    monkeypatch.setitem(sys.modules,'lp_sampled_policy',module)
    run=runpy.run_path(str(ASSETS/'lp_sampled_strategy.py.j2'))['run']
    policy,source,snapshot=values()
    source['source_id']='pool'
    params={'lp_source':source,'lp_sampled_policy':policy}
    ctx=SimpleNamespace(mode='live',result=ResultBuilder(),config=SimpleNamespace(params=params),
        inputs=SimpleNamespace(source=lambda key:deepcopy(snapshot)))
    assert run(ctx).status.value=='error'
    ctx.mode='backtest'
    result=run(ctx)
    assert result.status.value=='hold'
    assert result.metadata['lp_decision']['action']=='lp_add'
    assert result.metadata['financial_execution_allowed'] is False


def test_research_facade_cannot_execute_outside_isolated_replay(monkeypatch):
    from nerya.strategies.research import replay_sampled_lp
    import pytest
    monkeypatch.delenv('NERYA_FREEFORM_BACKTEST',raising=False)
    with pytest.raises(RuntimeError,match='only inside'):
        replay_sampled_lp()


def test_replay_asset_is_statically_valid_at_recognized_replay_path(tmp_path):
    from nerya.strategies.validator import static_scan_blockers
    from nerya.strategies.package import load_package_from_dir
    # The ordinary scanner must own the import allowance; no boundary expansion.
    (tmp_path/'scripts').mkdir()
    (tmp_path/'scripts/custom_replay.py').write_text((ASSETS/'lp_sampled_replay.py.j2').read_text())
    (tmp_path/'main.py').write_text((ASSETS/'lp_sampled_strategy.py.j2').read_text())
    (tmp_path/'lp_sampled_policy.py').write_text((ASSETS/'lp_sampled_policy.py.j2').read_text())
    (tmp_path/'strategy.yml').write_text('id: fixture\nmode: paper\nentrypoint: main.py:run\nmarkets: ["UNISWAP_V3:ETH/USDC"]\nschedule: {type: none, enabled: false}\n')
    assert static_scan_blockers(load_package_from_dir(tmp_path))==[]

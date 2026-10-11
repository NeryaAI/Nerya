"""Immutable parent/allowance lineage; only fake funds transports."""
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from test_financial_authorizations import setup,request,DEST
from nerya.financial.gateway import FinancialGateway
from nerya.financial.contracts import FinancialError

pytestmark=pytest.mark.smoke


def test_finite_allowance_child_refresh_and_source_submission_are_linked(tmp_path):
    store,_,ctx,_,old=setup(tmp_path)
    policy=old['policy'];policy['actions']=['bridge_swap','contract_approval']
    policy['resources'].update(assets=['USDC','WETH'],chains=['bsc','base'],spenders=[DEST],routers=[DEST])
    policy['limits']['slippage_bps']=50
    grant=store.create_grant(ctx,task_kind=ctx.task_kind,task_id=ctx.task_id,security_revision=ctx.security_revision,policy=policy)
    store.approve_grant(grant['grant_id'],ctx,expected_revision=1)
    allowed=SimpleNamespace(ready=False)
    class Allowance:
        calls=0
        def quote(self,r):return {'risk_usd':r['amount'],'spend_usd':'0','fee_usd':'0','asset_amounts':{},'available_asset_amounts':{},'expires_at':time.time()+60}
        def validate(self,*_):pass
        def execute(self,r,q,submitted):self.calls+=1;allowed.ready=True;return {'state':'confirmed','finite_allowance':r['amount']}
        def status(self,*_):return {'state':'confirmed'}
    class Bridge:
        calls=0
        def quote(self,r):return {'risk_usd':r['amount'],'spend_usd':r['amount'],'fee_usd':'0','asset_amounts':{'USDC':r['amount']},
            'available_asset_amounts':{'USDC':'1000'},'expires_at':time.time()+60,
            'steps':[{'kind':'contract_approval','spender':DEST,'router':DEST}],'minimum_received_base':'10'}
        def validate(self,*_):
            if not allowed.ready:raise FinancialError('finite_bridge_allowance_required')
        def execute(self,r,q,submitted):self.calls+=1;submitted({'transaction_hash':'0x'+'2'*64});return {'state':'submitted','source_confirmed':False}
        def status(self,*_):return {'state':'confirming','source_confirmed':True,'destination_confirmed':False}
    allowance=Allowance();bridge=Bridge();gateway=FinancialGateway(store.config,adapters={'contract_approval':allowance,'bridge_swap':bridge})
    raw={**request(),'kind':'bridge_swap','to_chain':'base','to_asset':'WETH'}
    parent=gateway.prepare(ctx,raw,action_key='bridge')
    missing=gateway.execute(ctx,parent['action_id'],quote_hash=parent['quote_hash'])
    assert missing['status']=='prerequisite_required' and bridge.calls==0
    child=gateway.prepare(ctx,missing['required_action'],action_key='allowance',parent_action_id=parent['action_id'])
    with pytest.raises(FinancialError,match='prerequisite_plan_mismatch'):
        gateway.prepare(ctx,{**missing['required_action'],'spender':'0x'+'3'*40},action_key='forged',parent_action_id=parent['action_id'])
    assert gateway.execute(ctx,child['action_id'],quote_hash=child['quote_hash'])['state']=='confirmed'
    before=store.get_action(parent['action_id'],ctx)
    assert before['steps'][0]['submission']['child_action_id']==child['action_id'] and before['steps'][0]['state']=='confirmed'
    fresh=gateway.refresh(ctx,parent['action_id'],expected_revision=before['revision'])
    assert fresh['quote_hash']!=parent['quote_hash']
    sent=gateway.execute(ctx,parent['action_id'],quote_hash=fresh['quote_hash'])
    assert sent['state']=='submitted' and bridge.calls==allowance.calls==1
    assert gateway.execute(ctx,parent['action_id'],quote_hash=fresh['quote_hash'])['duplicate']
    checked=gateway.reconcile(ctx,parent['action_id'])
    assert checked['receipt']['source_confirmed'] and not checked['receipt']['destination_confirmed']
    assert checked['state']=='confirming'
    with pytest.raises(FinancialError,match='immutable'):
        gateway.refresh(ctx,parent['action_id'],expected_revision=checked['revision'])
    with store.transaction() as con:
        quantities=con.execute("SELECT kind,amount_usd FROM financial_usage WHERE state!='released'").fetchall()
    assert {r['kind']:r['amount_usd'] for r in quantities}=={'contract_approval':'60','bridge_swap':'60'}
    assert bridge.calls==1

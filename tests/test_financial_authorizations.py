from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import time

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.financial.contracts import FinancialContext,FinancialError,normalize_policy
from nerya.financial.gateway import FinancialGateway
from nerya.financial.store import FinancialStore
from nerya.approval_service import ApprovalService

pytestmark=pytest.mark.smoke
DEST="0x"+"1"*40


class Adapter:
    calls=0
    timeout=False
    def quote(self,r):
        return {"risk_usd":r["amount"],"spend_usd":r["amount"],"fee_usd":"0",
                "asset_amounts":{"USDC":r["amount"]},"available_asset_amounts":{"USDC":"1000"},
                "expires_at":time.time()+60}
    def validate(self,*_):pass
    def execute(self,r,q,submitted):
        self.calls+=1
        submitted({"transaction_hash":"0x"+"2"*64})
        if self.timeout:raise TimeoutError()
        return {"state":"confirmed","transferred":r["amount"]}
    def status(self,*_):return {"state":"confirmed","observed":True}


def setup(tmp_path,*,total="100",daily="100"):
    data=deepcopy(DEFAULT_CONFIG);data["financial"]={"enabled":True}
    data['runtime']['live_trading_enabled']=True
    data['wallet']={'providers':{'wallet-a':{'provider':'self_custody','config':{'address':DEST,'signer_ref':'vault://fake-test-key'}}}}
    cfg=Config(WorkspacePaths(tmp_path),data)
    store=FinancialStore(cfg)
    issuer=FinancialContext("operator",frozenset({"api:all"}))
    ctx=replace(issuer,task_kind="scheduled_agent",task_id="transfer",security_revision="v1")
    policy={"actions":["wallet_transfer"],"resources":{"wallets":["wallet-a"],"assets":["USDC"],
            "chains":["bsc"],"recipients":[DEST]},"limits":{"single_usd":"100","rolling_24h_usd":daily,
            "total_usd":total,"fee_usd":"10","asset_amounts":{"USDC":"100"}}}
    grant=store.create_grant(issuer,task_kind="scheduled_agent",task_id="transfer",security_revision="v1",policy=policy)
    store.approve_grant(grant["grant_id"],issuer,expected_revision=1)
    adapter=Adapter();gateway=FinancialGateway(cfg,adapters={"wallet_transfer":adapter})
    return store,gateway,ctx,adapter,grant


def request(value="60"):
    return {"kind":"wallet_transfer","wallet_id":"wallet-a","asset":"USDC","chain":"bsc","amount":value,"recipient":DEST}


def test_atomic_quota_and_concurrent_submission(tmp_path):
    store,gw,ctx,adapter,grant=setup(tmp_path)
    actions=[gw.prepare(ctx,request(),action_key=str(i)) for i in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda a:gw.execute(ctx,a["action_id"],quote_hash=a["quote_hash"]),actions))
    assert adapter.calls==1
    assert sum(o.get("state")=="confirmed" for o in outcomes)==1
    assert sum(o.get("status")=="approval_required" for o in outcomes)==1


def test_duplicate_never_resubmits_even_after_timeout(tmp_path):
    store,gw,ctx,adapter,_=setup(tmp_path);adapter.timeout=True
    action=gw.prepare(ctx,request(),action_key="once")
    first=gw.execute(ctx,action["action_id"],quote_hash=action["quote_hash"])
    assert first["state"]=="unconfirmed"
    second=gw.execute(ctx,action["action_id"],quote_hash=action["quote_hash"])
    assert second["duplicate"] and adapter.calls==1
    resolved=gw.reconcile(ctx,action["action_id"])
    assert resolved["state"]=="confirmed" and adapter.calls==1


def test_revocation_and_plan_mode(tmp_path):
    store,gw,ctx,adapter,grant=setup(tmp_path)
    action=gw.prepare(ctx,request(),action_key="p")
    with pytest.raises(FinancialError,match="plan_mode"):
        gw.execute(replace(ctx,plan_only=True),action["action_id"],quote_hash=action["quote_hash"])
    store.revoke_grant(grant["grant_id"],ctx,expected_revision=2)
    result=gw.execute(ctx,action["action_id"],quote_hash=action["quote_hash"])
    assert result["status"]=="approval_required" and adapter.calls==0


def test_wrong_recipient_quote_or_owner(tmp_path):
    store,gw,ctx,adapter,_=setup(tmp_path)
    action=gw.prepare(ctx,{**request(),"recipient":"0x"+"3"*40},action_key="wrong-dest")
    assert gw.execute(ctx,action["action_id"],quote_hash=action["quote_hash"])["status"]=="approval_required"
    with pytest.raises(FinancialError,match="quote_changed"):
        gw.execute(ctx,action["action_id"],quote_hash="forged")
    with pytest.raises(FinancialError,match="owner_mismatch"):
        store.get_action(action["action_id"],replace(ctx,actor_id="other"))
    assert adapter.calls==0


def test_claimed_approval_boolean_is_not_authorization(tmp_path):
    store,gw,ctx,_,_=setup(tmp_path)
    foreign=replace(ctx,task_id="other")
    action=gw.prepare(foreign,request(),action_key="forged-approval")
    result=store.reserve(action["action_id"],foreign,quote_hash=action["quote_hash"],approved=True)
    assert result["needs_approval"]


def test_grant_revision_does_not_reset_spending(tmp_path):
    store,gw,ctx,adapter,grant=setup(tmp_path)
    action=gw.prepare(ctx,request(),action_key="first")
    gw.execute(ctx,action["action_id"],quote_hash=action["quote_hash"])
    current=store.get_grant(grant["grant_id"],ctx)
    store.revoke_grant(grant["grant_id"],ctx,expected_revision=current["revision"])
    next_grant=store.create_grant(ctx,task_kind="scheduled_agent",task_id="transfer",security_revision="v1",policy=current["policy"])
    store.approve_grant(next_grant["grant_id"],ctx,expected_revision=1)
    next_action=gw.prepare(ctx,request(),action_key="next")
    assert gw.execute(ctx,next_action["action_id"],quote_hash=next_action["quote_hash"])["status"]=="approval_required"
    assert adapter.calls==1


def test_nonfinite_and_wildcard_policies_fail_closed(tmp_path):
    store,_,ctx,_,grant=setup(tmp_path)
    policy=grant["policy"]
    with pytest.raises(FinancialError,match="invalid_amount"):
        normalize_policy({**policy,"limits":{**policy["limits"],"total_usd":"Infinity"}})
    with pytest.raises(FinancialError,match="explicit_grant_resources"):
        normalize_policy({**policy,"resources":{**policy["resources"],"recipients":["*"]}})
    assert ApprovalService.required_scope({"kind":"financial_action"})=="approve:funds"
    with pytest.raises(ValueError,match="unknown financial"):
        ApprovalService.required_scope({"kind":"financial_unknown"})


def test_read_only_actor_cannot_activate_grant(tmp_path):
    store,_,ctx,_,grant=setup(tmp_path)
    policy=grant["policy"]
    pending=store.create_grant(ctx,task_kind="scheduled_agent",task_id="transfer",security_revision="v1",policy=policy)
    with pytest.raises(FinancialError,match="scope_denied"):
        store.approve_grant(pending["grant_id"],replace(ctx,scopes=frozenset({"read:funds"})),expected_revision=1)


def test_issuer_can_review_script_grant_without_becoming_execution_actor(tmp_path):
    store,_,ctx,_,grant=setup(tmp_path)
    issuer=FinancialContext("owner",frozenset({"write:config","approve:funds"}))
    pending=store.create_grant(issuer,task_kind="strategy_script",task_id="s",security_revision="v1",
        policy=grant["policy"],subject_actor_id="strategy:s")
    assert store.get_grant(pending["grant_id"],issuer)["actor_id"]=="strategy:s"
    assert any(row["grant_id"]==pending["grant_id"] for row in store.list_grants(issuer))
    assert store.approve_grant(pending["grant_id"],issuer,expected_revision=1)["state"]=="active"
    foreign=replace(issuer,actor_id="other")
    with pytest.raises(FinancialError,match="grant_owner_mismatch"):
        store.revoke_grant(pending["grant_id"],foreign,expected_revision=2)


def test_changed_wallet_identity_invalidates_existing_grant(tmp_path):
    store,gw,ctx,adapter,grant=setup(tmp_path)
    store.config.data['wallet']['providers']['wallet-a']['config']['address']='0x'+'4'*40
    action=gw.prepare(ctx,request(),action_key='new-binding')
    assert gw.execute(ctx,action['action_id'],quote_hash=action['quote_hash'])['status']=='approval_required'
    assert adapter.calls==0


def test_normal_chat_financial_approval_binds_original_command_and_stop(tmp_path):
    from nerya.agent.command_runtime import runtime
    store,gw,ctx,adapter,_=setup(tmp_path)
    manager=runtime(store.config);manager.accepted_model=lambda _:None
    command=manager.submit({'command_id':'normal-finance','session_id':'finance-chat','_auth_actor_id':'operator',
        'request':{'payload':{'text':'Transfer within explicit approval'}}},start=False)['command']
    manager.store.claim('finance-chat','worker')
    normal=replace(ctx,task_kind=None,task_id=None,security_revision=None,command_id=command['command_id'],
        session_id=command['session_id'],turn_id=command['turn_id'])
    action=gw.prepare(normal,request(),action_key='normal-action')
    pending=gw.execute(normal,action['action_id'],quote_hash=action['quote_hash'])
    record=ApprovalService(store.config).find(pending['approval_id'])
    assert record['session_id']=='finance-chat' and record['turn_id']==command['turn_id']
    current=manager.store.snapshot('finance-chat',command['command_id'])['command']
    manager.store.control('finance-chat','stop',cid=command['command_id'],revision=current['revision'])
    manager.store.finish(command['command_id'],'finance-chat','worker','interrupted',{'stopped_reason':'cancelled'})
    approved=ApprovalService(store.config).move(pending['approval_id'],state='approved',resolver_actor_id='operator',operator_authorized=True)
    assert approved
    with pytest.raises(FinancialError,match='command_not_active'):
        gw.execute(normal,action['action_id'],quote_hash=action['quote_hash'])
    assert adapter.calls==0


def test_mixed_grant_requires_both_approval_domains(tmp_path):
    store,_,ctx,_,grant=setup(tmp_path)
    policy=grant['policy'];policy['actions']=['wallet_transfer','swap']
    draft=store.create_grant(ctx,task_kind='scheduled_agent',task_id='transfer',security_revision='v1',policy=policy)
    with pytest.raises(FinancialError,match='scope_denied'):
        store.approve_grant(draft['grant_id'],replace(ctx,scopes=frozenset({'approve:funds'})),expected_revision=1)
    assert store.approve_grant(draft['grant_id'],replace(ctx,scopes=frozenset({'approve:funds','approve:trade'})),expected_revision=1)['state']=='active'

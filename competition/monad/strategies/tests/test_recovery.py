import sys,tempfile,time,json
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from recovery_operator import recover
from nerya.core.config import Config,DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.financial.store import FinancialStore
from nerya.financial.gateway import FinancialGateway
from nerya.financial.contracts import FinancialContext,FinancialError
from nerya.financial.strategy_runtime import bind_run,finish_run
from nerya.financial.task_binding import strategy_security_revision
from nerya.workspace.state_store import StateStore


class Recovery(unittest.TestCase):
 def test_new_producer_preserves_receipts_old_finished_lease_and_denies_duplicate(self):
  with tempfile.TemporaryDirectory() as tmp:
   paths=WorkspacePaths(Path(tmp));data=deepcopy(DEFAULT_CONFIG);data['financial']={'enabled':True,'account_limits':{'acct':{'single_usd':'10','rolling_24h_usd':'30'}}};data['runtime']['live_trading_enabled']=True
   cfg=Config(paths,data);sid='monad_lp_range';root=paths.strategy(sid);root.mkdir(parents=True)
   yaml_io.dump(root/'strategy.yml',{'strategy_id':sid,'accounts':['acct'],'markets':['PERPL:MON/AUSD']})
   paths.accounts.mkdir(parents=True);yaml_io.dump(paths.accounts_file,{'accounts':[{'id':'acct','venue':'mock','mode':'paper','permissions':{'read_balances':True,'place_order':True}}]})
   issuer=FinancialContext('operator',frozenset({'api:all'}));store=FinancialStore(cfg)
   bound,ctx=bind_run(cfg,strategy_id=sid,run_id='old_finished',mode='live')
   request={'kind':'trade','account_id':'acct','chain':'monad-testnet','market':'PERPL:MON/AUSD','plan':{'strategy_id':sid,'action':'close_position','meta':{'leverage':1}}}
   class Adapter:
    calls=0
    def quote(self,r):
     if getattr(self,'fail_quote',False):self.fail_quote=False;raise FinancialError('invalid_protocol_quote_window')
     return {'risk_usd':'1','spend_usd':'1','fee_usd':'0','asset_amounts':{'AUSD':'1'},'available_asset_amounts':{'AUSD':'100'},'expires_at':time.time()+60}
    def validate(self,*a):pass
    def execute(self,r,q,submitted):self.calls+=1;return {'state':'confirmed','position_id':'4'}
   adapter=Adapter()
   class Gateway(FinancialGateway):
    def __init__(self,c):super().__init__(c,adapters={'trade':adapter})
   old=Gateway(bound).prepare(ctx,request,action_key='old');finish_run(bound,'old_finished')
   cycle={'phase':'watch','range':[276120,276520],'receipts':{'mint':{'receipt':{'position_id':'4'}}},'binding':'same','cycle_id':'demo',
    'pending':{'action_id':old['action_id'],'producer_run_id':'old_finished','request':old['request'],'label':'collect','next_phase':'exit'}}
   StateStore(root/'state/state.json').set('cycle',cycle)
   policy={'actions':['trade'],'resources':{'accounts':['acct'],'markets':['PERPL:MON/AUSD'],'chains':['monad-testnet']},'limits':{'single_usd':'10','rolling_24h_usd':'30','total_usd':'30','fee_usd':'1','trade_actions':['close_position'],'max_leverage':'1','asset_amounts':{'AUSD':'30'}}}
   grant=store.create_grant(issuer,task_kind='strategy_script',task_id=sid,subject_actor_id='strategy:'+sid,security_revision=strategy_security_revision(cfg,sid),policy=policy);store.approve_grant(grant['grant_id'],issuer,expected_revision=1)
   with patch('recovery_operator.FinancialGateway',Gateway),patch('nerya.sdk.financial_api.FinancialGateway',Gateway):
    original_execute=Gateway.execute
    def await_approval(self,context,aid,*,quote_hash):
     self.store.mark(aid,state='awaiting_approval')
     return {'status':'approval_required'}
    with patch.object(Gateway,'execute',await_approval):
     waiting=recover(cfg,sid,old['action_id'],issuer=issuer,execute=True)
    self.assertEqual(waiting['state'],'awaiting_approval')
    replacement_id=waiting['new_action_id']
    with store.transaction() as con:con.execute('UPDATE strategy_financial_runs SET lease_until=? WHERE run_id=?',(time.time()-1,waiting['producer_run_id']))
    old_id=old['action_id']
    old['action_id']=replacement_id
    adapter.fail_quote=True
    with self.assertRaises(FinancialError):recover(cfg,sid,old['action_id'],issuer=issuer,execute=True)
    interrupted=StateStore(root/'state/state.json').get('cycle')
    checkpoint_rid=interrupted['pending']['recovery']['new_producer_run_id']
    self.assertEqual(interrupted['pending']['recovery']['stage'],'intent_checkpoint')
    result=recover(cfg,sid,old['action_id'],issuer=issuer,execute=True)
    self.assertEqual(result['producer_run_id'],checkpoint_rid)
    self.assertEqual(result['state'],'confirmed');self.assertNotEqual(result['producer_run_id'],'old_finished')
    updated=StateStore(root/'state/state.json').get('cycle')
    self.assertEqual(updated['phase'],'exit');self.assertEqual(updated['receipts']['mint'],cycle['receipts']['mint']);self.assertEqual(updated['range'],cycle['range']);self.assertNotIn('pending',updated)
    self.assertEqual(store.get_action(old_id,issuer,operator=True)['state'],'failed_before_submission')
    self.assertEqual(store.get_action(replacement_id,issuer,operator=True)['state'],'failed_before_submission')
    with store.transaction() as con:self.assertEqual(con.execute('SELECT state FROM strategy_financial_runs WHERE run_id=?',('old_finished',)).fetchone()['state'],'finished')
    self.assertEqual(adapter.calls,1)
    with self.assertRaises(FinancialError):recover(cfg,sid,old['action_id'],issuer=issuer,execute=True)
    self.assertEqual(adapter.calls,1)


if __name__=='__main__':unittest.main(verbosity=2)

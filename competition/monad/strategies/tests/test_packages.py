"""Offline branch + installed native-runner identity/grant/state contract checks."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
sys.path.insert(0,str(REPO))
sys.path.insert(0,str(ROOT))
from configure import IDS, candidates, stage
from nerya.core import yaml_io
from nerya.strategies.package import load_package_from_dir
from nerya.strategies.runner import StrategyRunner
from nerya.strategies.validator import static_scan_blockers
from nerya.strategies.context import StrategyClock
from nerya.core.paths import WorkspacePaths
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.financial.contracts import FinancialContext, FinancialError
from nerya.financial.gateway import FinancialGateway
from nerya.financial.store import FinancialStore
from nerya.financial.task_binding import strategy_security_revision

DEPLOYMENT = Path('/Users/rick/Documents/Project/Nerya/pitch-video/monad-metropolis-20261013/v3/private/testnet.deployment.json')


def module(sid):
    # Mirrors the native runner's package module isolation.
    sys.modules.pop('monad_runtime',None)
    sys.path.insert(0,str(ROOT/sid))
    try:
        spec = importlib.util.spec_from_file_location(sid,ROOT/sid/'main.py')
        result=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
        return result
    finally:
        sys.path.pop(0)


def bars(now, values):
    end=now//3600000*3600000
    return [{'time':end-(len(values)-i)*3600000,'close':str(v)} for i,v in enumerate(values)]


class State:
    def __init__(self):self.data={}
    def get(self,k,default=None):return deepcopy(self.data.get(k,default))
    def set(self,k,v):self.data[k]=deepcopy(v)


class SDK:
    def __init__(self):self.actions={};self.requests=[];self.executions=[];self.reads=[];self.uncertain=False;self.approval=False;self.prereq=False;self.account={}
    def prepare(self,req,*,client_request_id,parent_action_id=None):
        self.requests.append(deepcopy(req)); aid='action_'+str(len(self.actions))
        self.actions[aid]={'action_id':aid,'state':'prepared','quote_hash':'quote','receipt':{},'strategy_run_id':'r1'}
        return deepcopy(self.actions[aid])
    def execute(self,aid,*,quote_hash):
        self.executions.append(aid)
        if self.approval:self.actions[aid].update(state='awaiting_approval',approval_id='approval_1');return {'status':'approval_required'}
        if self.prereq:self.actions[aid]['state']='awaiting_prerequisite';return {'status':'prerequisite_required','required_actions':[{'kind':'contract_approval'}]}
        receipt={'position_id':'17','filled_size':'100','average_price':'1','amount_out':'2','amount_in':'2'}
        self.actions[aid].update(state='unconfirmed' if self.uncertain else 'confirmed',receipt=receipt)
        return deepcopy(self.actions[aid])
    def get(self,aid):return deepcopy(self.actions[aid])
    def reconcile(self,aid):
        self.reads.append(aid)
        if not self.uncertain:self.actions[aid]['state']='confirmed'
        return self.get(aid)
    def account_state(self,req):return deepcopy(self.account)


def context(sid,params=None):
    manifest=yaml_io.load(ROOT/sid/'strategy.yml')
    p=deepcopy(params or manifest['params']); p['execution_enabled']=True
    now=int(time.time()*1000)
    ctx=SimpleNamespace(strategy_id=sid,run_id='r1',mode='live',state=State(),financial=SDK(),
        config=SimpleNamespace(params=p,accounts=(p['account_id'],)),clock=SimpleNamespace(now_ms=lambda:now),
        audit=SimpleNamespace(log=lambda *a,**kw:None))
    ctx.rows=[]
    ctx.news=SimpleNamespace(fetch=lambda **kw:deepcopy(ctx.rows))
    ctx.financial.account={'health':'ok','chain_id':10143,'positions':[]}
    return ctx


class Packages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packages=candidates(DEPLOYMENT,wallet_id='demo_wallet',account_id='demo_account',perpl_account_id='perpl_testnet')

    def manifest(self,sid):return yaml_io.loads(self.packages[sid]['strategy.yml'])

    def pool(self,ctx,*,price='1',tick=0,block=10):
        p=ctx.config.params
        return {'kind':'pool','chain_id':10143,'as_of_ms':ctx.clock.now_ms(), 'pool_id':p['pool_id'],
            'deployment_sha256':p['deployment_sha256'],**{k:p['pool'][k] for k in ('address','token0','token1','fee','tick_spacing')},
            'quote_per_token':price,'tick':tick,'block':block,'range_ticks':p.get('range_ticks'),
            'tick_lower':-p.get('range_ticks',1),'tick_upper':p.get('range_ticks',1),
            'mint_amounts':['0.009','0.009'],'decimals':[6,18]}

    def perp(self,ctx,values):
        return {'kind':'perpl','chain_id':10143,'market':'PERPL:MON/AUSD','as_of_ms':ctx.clock.now_ms(),
            'mark':'1','candles':bars(ctx.clock.now_ms(),values)}

    def test_installation_review_has_scoped_finite_wallet_permissions(self):
        from integration import plan
        deployed=json.loads(DEPLOYMENT.read_text())
        review=plan([self.manifest(sid) for sid in IDS],deployed)
        self.assertTrue(review['does_not_activate'])
        permissions=review['config_merge_review']['financial']['wallet_permissions']['demo_wallet']
        self.assertEqual(set(permissions),{'lp_add','lp_remove','lp_collect','swap','contract_approval'})
        self.assertEqual(len(review['accounts_merge_by_id']),2)
        self.assertTrue(all(row['subject_actor_id']=='strategy:'+row['strategy_id'] for row in review['grant_drafts']))

    def test_native_manifests_and_static_scan(self):
        for sid in IDS:
            package=load_package_from_dir(ROOT/sid)
            self.assertEqual(static_scan_blockers(package),[])
            self.assertFalse(package.manifest.schedule.enabled)
            self.assertEqual(package.manifest.mode,'paper')
            self.assertFalse(package.manifest.extras['params']['execution_enabled'])

    def test_lp_mint_watch_collect_exit_and_minimum_precision(self):
        sid=IDS[0]; ctx=context(sid,self.manifest(sid)['params']); entry=module(sid)
        ctx.rows=[self.pool(ctx)]
        entry.run(ctx)
        self.assertEqual(ctx.state.get('cycle')['phase'],'watch')
        self.assertEqual(ctx.financial.requests[0]['kind'],'lp_add')
        self.assertEqual(ctx.financial.requests[0]['parameters']['tick_lower'],-200)
        self.assertEqual(ctx.financial.requests[0]['parameters']['amount0_min'],'0.008910')
        self.assertTrue(all(isinstance(ctx.financial.requests[0]['parameters'][key],str) for key in ('amount0_max','amount1_max','amount0_min','amount1_min')))
        position={'token_id':17,'pool':{'pool_id':ctx.config.params['pool_id']},'liquidity':'123', 'tokens_owed_base':['0','0']}
        ctx.financial.account={'health':'ok','positions':[position]}
        result=entry.run(ctx)
        self.assertIn('Watching',result['reason']);self.assertEqual(len(ctx.financial.requests),1)
        position['tokens_owed_base']=['1','0']; entry.run(ctx)
        self.assertEqual(ctx.financial.requests[-1]['kind'],'lp_collect')
        self.assertTrue(all(isinstance(ctx.financial.requests[-1]['parameters'][key],str) for key in ('collect0_max','collect1_max')))
        entry.run(ctx)
        self.assertEqual(ctx.financial.requests[-1]['parameters']['token_id'],17)
        self.assertEqual(ctx.financial.requests[-1]['parameters']['liquidity'],123)
        self.assertEqual(ctx.state.get('cycle')['phase'],'done')
        entry.run(ctx);self.assertEqual(len(ctx.financial.requests),3)

    def test_perp_closed_cross_and_reduce_only_close(self):
        sid=IDS[1];ctx=context(sid);entry=module(sid)
        ctx.rows=[self.perp(ctx,[1]*18+[2])]
        # forming candle cannot erase the qualifying last closed crossing
        ctx.rows[0]['candles'].append({'time':ctx.clock.now_ms()//3600000*3600000,'close':'0.1'})
        entry.run(ctx)
        self.assertEqual(ctx.financial.requests[0]['plan']['side'],'long')
        self.assertEqual(ctx.financial.requests[0]['parameters']['max_negative_pnl_bps'],100)
        self.assertEqual(ctx.state.get('cycle')['phase'],'watch')
        ctx.financial.account={'health':'ok','chain_id':10143,'market':{'id':10,'config':{'size_decimals':0}},
            'positions':[{'mkt':10,'sd':1,'s':100,'pid':8}]}
        ctx.clock.now_ms=lambda:int(time.time()*1000)+181000
        ctx.rows[0]['as_of_ms']=ctx.clock.now_ms()
        entry.run(ctx)
        self.assertTrue(ctx.financial.requests[-1]['parameters']['reduce_only'])
        self.assertEqual(ctx.financial.requests[-1]['parameters']['position_id'],8)
        self.assertEqual(ctx.financial.requests[-1]['plan']['sizing']['fixed_base'],100.0)
        from nerya.sdk.trading_api import _plan_from_dict
        from nerya.trading.submit import _plan_to_intent
        _plan_to_intent(_plan_from_dict(ctx.financial.requests[-1]['plan']))
        self.assertEqual(ctx.state.get('cycle')['phase'],'done')

    def test_no_cross_warmup_and_gap_do_not_submit(self):
        entry=module(IDS[1])
        for values in ([1]*25,[1]*18):
            ctx=context(IDS[1]);ctx.rows=[self.perp(ctx,values)];entry.run(ctx)
            self.assertEqual(ctx.financial.requests,[])
        ctx=context(IDS[1]);ctx.rows=[self.perp(ctx,[1]*18+[2])]
        ctx.rows[0]['candles'][3]['time']+=1
        with self.assertRaises(ValueError):entry.run(ctx)
        self.assertEqual(ctx.financial.requests,[])

    def test_meme_actual_momentum_bounded_roundtrip(self):
        sid=IDS[2];ctx=context(sid,self.manifest(sid)['params']);entry=module(sid)
        ctx.rows=[self.pool(ctx)];entry.run(ctx);self.assertEqual(ctx.financial.requests,[])
        start=ctx.clock.now_ms();ctx.clock.now_ms=lambda:start+15000
        ctx.rows=[self.pool(ctx,price='1.002',block=11)];entry.run(ctx)
        self.assertEqual(ctx.financial.requests[0]['asset'],ctx.config.params['quote_token'])
        self.assertEqual(ctx.state.get('cycle')['phase'],'watch')
        entry.run(ctx);self.assertEqual(len(ctx.financial.requests),1)
        ctx.rows=[self.pool(ctx,price='0.97',block=12)];entry.run(ctx)
        self.assertEqual(ctx.financial.requests[-1]['amount'],'2')
        self.assertEqual(ctx.financial.requests[-1]['parameters']['token_out'],ctx.config.params['quote_token'])
        self.assertEqual(ctx.state.get('cycle')['phase'],'done')

    def test_approval_prerequisite_uncertainty_and_binding_do_not_duplicate(self):
        entry=module(IDS[1])
        for fence in ('approval','prereq','uncertain'):
            ctx=context(IDS[1]);ctx.rows=[self.perp(ctx,[1]*18+[2])];setattr(ctx.financial,fence,True)
            entry.run(ctx);ctx.run_id='r2';entry.run(ctx)
            self.assertEqual(len(ctx.financial.executions),1)
            self.assertEqual(len(ctx.financial.requests),1)
            self.assertEqual(ctx.state.get('cycle')['phase'],'idle')
        ctx.config.params['fixed_base']='101'
        with self.assertRaises(ValueError):entry.run(ctx)

    def test_stale_wrong_chain_and_paper_never_execute(self):
        entry=module(IDS[1])
        for field,value in [('chain_id',143),('as_of_ms',0)]:
            ctx=context(IDS[1]);ctx.rows=[self.perp(ctx,[1]*18+[2])];ctx.rows[0][field]=value
            with self.assertRaises(ValueError):entry.run(ctx)
            self.assertEqual(ctx.financial.executions,[])
        ctx=context(IDS[1]);ctx.mode='paper';ctx.rows=[self.perp(ctx,[1]*18+[2])]
        entry.run(ctx);self.assertEqual(ctx.financial.executions,[])
        self.assertIsNone(ctx.state.get('cycle'))

    def test_default_public_provider_testnet_workspace_and_disabled_gate(self):
        from unittest.mock import patch
        from nerya.strategies.news_fetchers import default_fetchers_for
        manifest=load_package_from_dir(ROOT/IDS[0]).manifest
        cfg=Config(WorkspacePaths(Path('/tmp/nerya-native-monad-test')), {})
        self.assertEqual(default_fetchers_for(manifest.news_sources,config=cfg,manifest=manifest),{})
        cfg.data['plugins']={'monad_strategy_data':{'enabled':True,'workspace':str(cfg.paths.root),'deployment_path':str(DEPLOYMENT)}}
        source='monad_lp_public';fetch=lambda **kw:[{'kind':'pool'}]
        with patch('competition.monad.strategies.readers.fetchers',return_value={source:fetch}):
            self.assertIs(default_fetchers_for(manifest.news_sources,config=cfg,manifest=manifest)[source],fetch)
            cfg.data['plugins']['monad_strategy_data']['workspace']='/tmp/other'
            with self.assertRaises(ValueError):default_fetchers_for(manifest.news_sources,config=cfg,manifest=manifest)

    def test_proposals_are_native_and_do_not_install_or_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=WorkspacePaths(Path(directory));rows=stage(paths,self.packages)
            self.assertEqual(len(rows),3)
            for row in rows:
                self.assertFalse(paths.strategy(row['strategy_id']).exists())
                staged=paths.proposals/row['proposal_id']/'after'/'strategies'/row['strategy_id']
                self.assertEqual(load_package_from_dir(staged).strategy_id,row['strategy_id'])


    def test_new_perpl_id_uses_public_feed_and_rejects_wrong_bindings(self):
        from unittest.mock import patch
        from competition.monad.strategies.readers import fetchers
        manifest = self.manifest(IDS[1])
        manifest['strategy_id'] = 'mon_perpl_trend60'
        observed = {'kind': 'perpl', 'chain_id': 10143, 'market': 'PERPL:MON/AUSD', 'candles': []}
        with patch('competition.monad.strategies.readers.perpl_snapshot', return_value=observed) as public, patch('competition.monad.strategies.readers.pool_snapshot') as pool:
            self.assertEqual(fetchers([manifest], DEPLOYMENT)['monad_perp_public'](limit=1), [observed])
            public.assert_called_once_with()
            pool.assert_not_called()
            for section, key, value in [('params', 'chain_id', 143), ('params', 'chain', 'monad'),
                    ('params', 'account_id', ''), ('root', 'accounts', ['other']),
                    ('root', 'markets', ['PERPL:MON/USD']), ('root', 'news_sources', [])]:
                wrong = deepcopy(manifest)
                (wrong if section == 'root' else wrong[section])[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    fetchers([wrong], DEPLOYMENT)
            self.assertEqual(public.call_count, 1)
            public.return_value = {**observed, 'chain_id': 143}
            with self.assertRaises(ValueError):
                fetchers([manifest], DEPLOYMENT)['monad_perp_public']()
        with patch('competition.monad.strategies.readers.pool_snapshot', return_value={'kind': 'pool'}) as pool:
            self.assertEqual(fetchers([self.manifest(IDS[0])], DEPLOYMENT)['monad_lp_public'](), [{'kind': 'pool'}])
            pool.assert_called_once()


class NativeRunner(unittest.TestCase):
    def test_installed_native_strategy_actor_grant_recovery_and_restart(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            paths=WorkspacePaths(Path(directory));data=deepcopy(DEFAULT_CONFIG)
            data['runtime']['live_trading_enabled']=True
            data['financial']={'enabled':True,'account_limits':{'perpl_testnet':{'single_usd':'10','rolling_24h_usd':'30'}}}
            cfg=Config(paths,data);paths.accounts.mkdir(parents=True,exist_ok=True)
            yaml_io.dump(paths.accounts_file,{'accounts':[{'id':'perpl_testnet','venue':'mock','exchange':'mock','mode':'paper','status':'active',
                'permissions':{'read_balances':True,'place_order':True,'cancel_order':True}}]})
            package=candidates(DEPLOYMENT,wallet_id='w',account_id='a',perpl_account_id='perpl_testnet')[IDS[1]]
            manifest=yaml_io.loads(package['strategy.yml']);manifest['mode']='live';manifest['params']['execution_enabled']=True;manifest['params']['component_id']='builtin:trading'
            root=paths.strategy(IDS[1]);root.mkdir(parents=True)
            for name,text in package.items():(root/name).write_text(text)
            yaml_io.dump(root/'strategy.yml',manifest)
            store=FinancialStore(cfg);issuer=FinancialContext('operator',frozenset({'api:all'}))
            now=int(time.time()*1000)
            public={'kind':'perpl','chain_id':10143,'market':'PERPL:MON/AUSD','as_of_ms':now,'mark':'1','candles':bars(now,[1]*18+[2])}
            class Adapter:
                calls=0; uncertain=True
                def quote(self,req):return {'risk_usd':'1','spend_usd':'1','fee_usd':'0','asset_amounts':{'AUSD':'1'},'available_asset_amounts':{'AUSD':'100'},'expires_at':time.time()+60}
                def validate(self,*args):pass
                def execute(self,req,q,submitted):
                    self.calls+=1;submitted({'transaction_hash':'0x'+'1'*64})
                    return {'state':'unconfirmed'}
                def status(self,*args):return {'state':'unconfirmed' if self.uncertain else 'confirmed','filled_size':'100','average_price':'1'}
            adapter=Adapter()
            class Gateway(FinancialGateway):
                def __init__(self,config):super().__init__(config,adapters={'trade':adapter})
                def account_state(self,context,req):return {'health':'ok','chain_id':10143,'positions':[]}
            grant=store.create_grant(issuer,task_kind='strategy_script',task_id=IDS[1],
                security_revision=strategy_security_revision(cfg,IDS[1]),subject_actor_id='strategy:'+IDS[1],
                policy={'actions':['trade'],'resources':{'accounts':['perpl_testnet'],'markets':['PERPL:MON/AUSD'],'chains':['monad-testnet'],'components':['builtin:trading']},
                    'limits':{'trade_actions':['open_position','close_position'],'max_leverage':'1','slippage_bps':50,'single_usd':'10','rolling_24h_usd':'30','total_usd':'30','fee_usd':'1','asset_amounts':{'AUSD':'30'}}})
            store.approve_grant(grant['grant_id'],issuer,expected_revision=1)
            cfg.data['plugins']['monad_strategy_data']={'enabled':True,'workspace':str(paths.root),'deployment_path':str(DEPLOYMENT)}
            with patch('nerya.sdk.financial_api.FinancialGateway',Gateway), patch('competition.monad.strategies.readers.fetchers',return_value={'monad_perp_public':lambda **kw:[public]}):
                runner=StrategyRunner(cfg)
                first=runner.run_tick(IDS[1],run_id='native_tick_1')
                self.assertNotEqual(first.status,'error',first.asdict())
                state=json.loads((root/'state'/'state.json').read_text())
                aid=state['cycle']['pending']['action_id']
                action=store.get_action(aid,issuer,operator=True)
                self.assertEqual(action['actor_id'],'strategy:'+IDS[1])
                self.assertEqual(action['strategy_run_id'],'native_tick_1')
                self.assertEqual(action['state'],'unconfirmed', {'reason':action.get('receipt'), 'context':action.get('context'), 'grant':store.get_grant(grant['grant_id'],issuer)})
                self.assertEqual(adapter.calls,1)
                StrategyRunner(cfg,news_fetchers=runner.news_fetchers).run_tick(IDS[1],run_id='native_tick_2')
                self.assertEqual(adapter.calls,1)
                adapter.uncertain=False
                third=runner.run_tick(IDS[1],run_id='native_tick_3')
                self.assertNotEqual(third.status,'error',third.asdict())
                state=json.loads((root/'state'/'state.json').read_text())
                self.assertEqual(state['cycle']['phase'],'watch')
                self.assertEqual(adapter.calls,1)
                # Wrong actor cannot read or adopt; later producer cannot execute prior action.
                with self.assertRaises(FinancialError):store.get_action(aid,FinancialContext('other',frozenset({'read:funds'})))
                changed=deepcopy(manifest);changed['params']['fixed_base']='101';yaml_io.dump(root/'strategy.yml',changed)
                fourth=runner.run_tick(IDS[1],run_id='native_tick_4')
                self.assertEqual(fourth.status,'error')
                self.assertEqual(adapter.calls,1)


if __name__=='__main__':unittest.main(verbosity=2)

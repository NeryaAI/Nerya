"""Nonsecret installation/account/permission and finite grant review plan.

Produces data only. Operator promotes proposals and approves grants separately.
No config overwrite, credential lookup, signer or remote write.
"""
from copy import deepcopy
from decimal import Decimal
from nerya.financial.contracts import normalize_policy
try:
    from .configure import IDS
except ImportError:
    from configure import IDS


def plan(manifests, deployed):
    tokens=deployed['tokens'];contracts=deployed['contracts']
    account_rows=[];wallets={};limits={};grants=[]
    for manifest in manifests:
        sid=manifest['strategy_id'];p=manifest['params'];aid=p['account_id']
        limits[aid]={'single_usd':'10','rolling_24h_usd':'30'}
        if sid==IDS[1]:
            account_rows.append({'id':aid,'kind':'exchange','venue':'perpl','exchange':'perpl','market_type':'linear','mode':'live',
                'status':'active','permissions':{'read_balances':True,'place_order':True,'cancel_order':True}})
            policy={'actions':['trade'],'resources':{'accounts':[aid],'markets':['PERPL:MON/AUSD'],
                'chains':['monad-testnet'],'components':[p['component_id']]},
                'limits':{'single_usd':'10','rolling_24h_usd':'30','total_usd':'30','fee_usd':'1',
                    'slippage_bps':p['slippage_bps'],'trade_actions':['open_position','close_position'],'max_leverage':'1','asset_amounts':{'AUSD':'30'}},
                'admission_budget_note':'100 bps negative PnL budget is included in quoted collateral/spend; no extra free grant, total remains 30 test units'}
        else:
            wid=p['wallet_id'];pool=p['pool'];assets=[pool['token0'],pool['token1'],'NATIVE']
            account_rows.append({'id':aid,'kind':'chain','venue':'evm','exchange':'evm','chain':'monad-testnet','mode':'live',
                'wallet_id':wid,'status':'active','permissions':{'read_balances':True,'place_order':True,'cancel_order':False}})
            actions=['lp_add','lp_collect','lp_remove'] if sid==IDS[0] else ['swap']
            wallets.setdefault(wid,{}).update({kind:True for kind in [*actions,'contract_approval']})
            limits['wallet:'+wid]={'single_usd':'10','rolling_24h_usd':'30'}
            spender=contracts['position_manager'] if sid==IDS[0] else contracts['router']
            caps={token:('1' if tokens[token.lower()]['symbol']!='nMEME' else '20') for token in assets if token!='NATIVE'}
            caps['NATIVE']='0.05'
            policy={'actions':[*actions,'contract_approval'],'resources':{'accounts':[aid],'wallets':[wid],
                'assets':assets,'chains':['monad-testnet'],'protocols':['uniswap_v3'],
                'components':[p['component_id'],'builtin:evm_funds'],'spenders':[spender],'routers':[spender]},
                'limits':{'single_usd':'10','rolling_24h_usd':'30','total_usd':'30','fee_usd':'1',
                    'slippage_bps':p['slippage_bps'],'asset_amounts':caps}}
        grants.append({'strategy_id':sid,'subject_actor_id':'strategy:'+sid,'task_kind':'strategy_script','task_id':sid,
            'security_revision':'COMPUTE_FROM_INSTALLED_PACKAGE_AFTER_FINAL_CONFIG','expires_after_seconds':3600,
            'state':'draft_requires_operator_approval','policy':normalize_policy(policy)})
    unique={}
    for row in account_rows:
        if row['id'] in unique and unique[row['id']]!=row:raise ValueError('Account identity collision')
        unique[row['id']]=row
    return {'scope':'Monad TESTNET 10143 only; USD fields are fixed TEST UNITS with no monetary value',
        'accounts_merge_by_id':list(unique.values()),
        'config_merge_review':{'financial':{'wallet_permissions':wallets,'account_limits':limits,
            'defi':{'deployments':{'monad-testnet':{'uniswap_v3':deployed}},'max_gas_limit':1000000,'max_pool_deviation_bps':100}},},
        'grant_drafts':grants,'activation_requires':
            ['Reviewed native proposal promotion','Existing wallet binding to isolated testnet address; signer ref remains operator-managed',
             'Perpl plugin + enrolled testnet account 1227 (parent-reported; revalidate), chain_id=10143, perpl:read/perpl:trade permissions',
             'Final manifest mode=live and params.execution_enabled=true BEFORE computing grant security revision',
             'financial.enabled and runtime.live_trading_enabled in isolated runtime only','Operator grant approval for exact strategy subject/revision',
             'Read-only StrategyRunner.news_fetchers registration from readers.py; scheduler registration after validation'],
        'operator_strategy_limits':{row['strategy_id']:{'enabled':True,'max_single_order_usd':10,'max_daily_notional_usd':30,'max_open_positions':1} for row in manifests},
        'does_not_activate':True}

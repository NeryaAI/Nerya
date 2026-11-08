"""Synthetic producer identity checks; no ETH/WETH alias or live claim."""
from copy import deepcopy

import pytest

from nerya.data_api.uniswap_history import FACTORY
from nerya.strategies.request_scope import ScopeError, _uniswap_prepared_resource


def fixture():
    pool=dict(chain_id=1,factory=FACTORY,pool_address='0x'+'33'*20,
        token0='0x'+'11'*20,token1='0x'+'22'*20,decimals=[6,18],fee=500,tick_spacing=10)
    envelope=dict(row_contract='uniswap_v3_pool_history.v1',observation_kind='historical_lp_pool_state',
        pool_identity_scope='historical_onchain_pool_with_current_token_catalog',chain_id=1,pool=pool,
        sources=[{'url':'https://tokens.uniswap.org','sha256':'a'*64}],
        token_identities=[dict(chain_id=1,address=pool['token'+str(i)],symbol=symbol,decimals=pool['decimals'][i],
            catalog_source_sha256='a'*64,identity_scope='current_official_catalog')
            for i,symbol in enumerate(('USDC','WETH'))])
    return envelope,[deepcopy(pool)],{'pool_address':pool['pool_address']},{'markets':['UNISWAP_V3:WETH/USDC']}


def test_current_catalog_and_historical_contracts_match_exact_requested_assets():
    resource=_uniswap_prepared_resource(*fixture())
    assert resource['base']=='WETH' and resource['quote']=='USDC'
    assert resource['chain']=='ethereum'
    assert resource['protocol']['id']=='uniswap_v3'


def test_wrapped_asset_is_not_silently_equivalent_to_native_request():
    envelope,rows,args,manifest=fixture()
    manifest['markets']=['UNISWAP_V3:ETH/USDC']
    with pytest.raises(ScopeError,match='request_scope_mismatch'):
        _uniswap_prepared_resource(envelope,rows,args,manifest)


@pytest.mark.parametrize('damage', ['factory','pool','chain','row_token','catalog_origin',
    'catalog_hash','token_address','decimals','symbol_duplicate','symbol_prose','catalog_missing','hash_missing'])
def test_candidate_labels_or_incomplete_identity_cannot_replace_producer_binding(damage):
    envelope,rows,args,manifest=fixture()
    if damage=='factory':envelope['pool']['factory']='0x'+'44'*20
    if damage=='pool':args['pool_address']='0x'+'44'*20
    if damage=='chain':envelope['chain_id']=True
    if damage=='row_token':rows[0]['token1']='0x'+'55'*20
    if damage=='catalog_origin':envelope['sources'][0]['url']='https://untrusted.invalid'
    if damage=='catalog_hash':envelope['token_identities'][0]['catalog_source_sha256']='b'*64
    if damage=='token_address':envelope['token_identities'][0]['address']='0x'+'55'*20
    if damage=='decimals':envelope['token_identities'][0]['decimals']=18
    if damage=='symbol_duplicate':envelope['token_identities'][0]['symbol']='WETH'
    if damage=='symbol_prose':envelope['token_identities'][0]['symbol']='USDC / anything'
    if damage=='catalog_missing':envelope.pop('token_identities')
    if damage=='hash_missing':
        envelope['sources'][0].pop('sha256')
        envelope['token_identities'][0].pop('catalog_source_sha256')
    with pytest.raises(ScopeError):
        _uniswap_prepared_resource(envelope,rows,args,manifest)


@pytest.mark.parametrize('cadence', [False, True])
def test_prepared_bytes_and_window_are_checked_before_resource_identity(cadence):
    import hashlib,json
    from nerya.strategies import request_scope as scope
    envelope,rows,args,manifest=fixture()
    envelope.update(mock=False,requested_window={'start':'2026-09-28T00:00:00Z','end':'2026-10-28T00:00:00Z'})
    declaration={'version':1,'datasets':[{'id':'pool','provider':'uniswap','action':'pool_history',
        'args':{**args,**envelope['requested_window']}}]}
    payload={'rows':rows,'_envelope':envelope}
    if cadence:
        manifest.update(schedule={'type':'cron','cron':'*/5 * * * *','enabled':False},
            params={'lp_source':{'tick_lower':-120,'tick_upper':120}})
        envelope.update(provider='uniswap',action='pool_history',row_contract='nerya.lp_cadence.v1',
            observation_kind='historical_lp_cadence')
        envelope['sources'].insert(0,{'sha256':'b'*64})
        payload={'protocol':'nerya.lp_cadence.v1','_envelope':envelope,
            'pool':{k:v for k,v in envelope['pool'].items() if k!='factory'},
            'schedule':deepcopy(manifest['schedule']),'tick_lower':-120,'tick_upper':120,
            'requested_window':deepcopy(envelope['requested_window']),
            'prepared_sha256':'b'*64,'observation_count':8641}
    blob=json.dumps(payload).encode()
    receipt={'id':'pool','provider':'uniswap','action':'pool_history','sources':envelope['sources'],
        'sha256':hashlib.sha256(blob).hexdigest(),'snapshot_file':'prepared_inputs/pool.json',
        'requested_window':envelope['requested_window'], 'rows':8641 if cadence else len(rows)}
    prepared={'manifest':declaration,'files':{'pool.json':blob},'receipts':[receipt]}
    contract={'constraints':{'resources':[scope._market('UNISWAP_V3:WETH/USDC')],
        'start_utc':1790553600,'end_utc':1793145600}}
    assert scope.validate_prepared(contract,manifest,declaration,prepared)=={'prepared_inputs/pool.json':receipt['sha256']}
    if cadence:
        for key,value in [('protocol','other'),('pool',{}),('schedule',{}),
                ('tick_lower',-60),('tick_upper',180),('tick_lower',True),
                ('requested_window',{}),('prepared_sha256','c'*64),('observation_count',1)]:
            damaged=json.dumps({**payload,key:value}).encode()
            altered={**prepared,'files':{'pool.json':damaged},
                'receipts':[{**receipt,'sha256':hashlib.sha256(damaged).hexdigest()}]}
            with pytest.raises(ScopeError):scope.validate_prepared(contract,manifest,declaration,altered)
        for key,value in [('row_contract','uniswap_v3_pool_history.v1'),
                ('observation_kind','historical_lp_pool_state'),('provider','other'),
                ('token_identities',[]),('chain_id',True),('mock',True)]:
            damaged=json.dumps({**payload,'_envelope':{**envelope,key:value}}).encode()
            altered={**prepared,'files':{'pool.json':damaged},
                'receipts':[{**receipt,'sha256':hashlib.sha256(damaged).hexdigest()}]}
            with pytest.raises(ScopeError):scope.validate_prepared(contract,manifest,declaration,altered)
    prepared['files']['pool.json']=blob+b' '
    with pytest.raises(ScopeError):scope.validate_prepared(contract,manifest,declaration,prepared)
    prepared['files']['pool.json']=blob
    declaration['datasets'][0]['args']['pool_address']='0x'+'44'*20
    with pytest.raises(ScopeError):scope.validate_prepared(contract,manifest,declaration,prepared)


def test_real_public_producer_preparation_validates_exact_scope(tmp_path):
    from nerya.data_api.registry import DataApiRegistry
    from nerya.data_api.uniswap_history import uniswap_specs
    from nerya.skills.builtin.backtest.scripts.replay_inputs import prepare_inputs
    from nerya.strategies import request_scope as scope
    from test_uniswap_history_data import Fake,CatalogFake,POOL
    import json
    start,end='2020-09-13T12:46:40Z','2020-09-13T13:06:40Z'
    declaration={'version':1,'datasets':[{'id':'pool','provider':'uniswap','action':'pool_history',
        'args':{'pool_address':POOL,'start':start,'end':end,'step_blocks':6,
                'max_samples':64,'tick_lower':-120,'tick_upper':120}}]}
    (tmp_path/'replay_inputs.json').write_text(json.dumps(declaration))
    registry=DataApiRegistry()
    for spec in uniswap_specs(Fake(),catalog_http=CatalogFake()):registry.register_action(spec)
    prepared=prepare_inputs(tmp_path,registry=registry)
    manifest={'markets':['UNISWAP_V3:WETH/USDC']}
    contract={'constraints':{'resources':[scope._market(manifest['markets'][0])],
        'start_utc':scope._timestamp(start),'end_utc':scope._timestamp(end)}}
    assert scope.validate_prepared(contract,manifest,declaration,prepared)=={
        'prepared_inputs/pool.json':prepared['receipts'][0]['sha256']}
    assert prepared['receipts'][0]['requested_window_complete'] is False
    contract['constraints']['resources']=[scope._market('UNISWAP_V3:ETH/USDC')]
    manifest['markets']=['UNISWAP_V3:ETH/USDC']
    with pytest.raises(ScopeError):scope.validate_prepared(contract,manifest,declaration,prepared)

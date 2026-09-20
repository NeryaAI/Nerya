"""Independent QA of actual Prompt-authored observer candidates.

Controlled candles/state isolate branches; this is NOT historical/model evidence.
The supplied main.py files are loaded unchanged. No orders or model calls run.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from nerya.core import yaml_io
from nerya.strategies.result import ResultBuilder


def load(root):
    content=(root/'main.py').read_bytes()
    digest=hashlib.sha256(content).hexdigest()
    spec=importlib.util.spec_from_file_location('_candidate_qa_'+uuid.uuid4().hex,root/'main.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module,yaml_io.load(root/'strategy.yml'),digest


def status(result):
    value=result.status
    return value.value if hasattr(value,'value') else value


def context(manifest,closes,*,forming=False,state=None,fetch_failure=False):
    raw=deepcopy(manifest)
    start=1700000000
    rows=[{'ts':start+i*900,'open':v,'high':v+1,'low':v-1,'close':v,'volume':0} for i,v in enumerate(closes)]
    now=rows[-1]['ts']+900
    if forming:rows.append({'ts':now,'open':1,'high':10000,'low':1,'close':10000,'volume':0})
    calls=[];forbidden=[];values={} if state is None else state;published={}
    def candles(market,**kwargs):
        calls.append({'market':market,**kwargs})
        if fetch_failure:raise RuntimeError('controlled unavailable reader')
        return deepcopy(rows[-int(kwargs.get('limit',len(rows))):])
    def cas(key,*,expect,new_value):
        if values.get(key)!=expect:return False
        values[key]=new_value;return True
    class Forbidden:
        def __getattr__(self,name):
            def blocked(*args,**kwargs):
                forbidden.append(name);raise AssertionError('unexpected side effect: '+name)
            return blocked
    ctx=SimpleNamespace(config=SimpleNamespace(markets=tuple(raw['markets']),accounts=tuple(raw['accounts']),extras=raw),market=SimpleNamespace(candles=candles),clock=SimpleNamespace(now_ms=lambda:now*1000),state=SimpleNamespace(get=values.get,compare_and_set=cas),result=ResultBuilder(),llm=Forbidden(),trading=Forbidden(),subagents=Forbidden(),team=Forbidden(),inputs=SimpleNamespace(publish=lambda name,value,**kw:published.update({name:value})))
    return ctx,calls,forbidden,values


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--script',type=Path,required=True)
    parser.add_argument('--signal',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();checks=[];fingerprints={}
    def check(name,fn):
        fn();checks.append(name)
    for label,root in [('script',args.script),('signal',args.signal)]:
        module,manifest,before=load(root);fingerprints[label]={'main_py_sha256':before,'strategy_id':manifest['strategy_id']}
        def assert_case(title,closes,expected,**kw):
            ctx,calls,side,state=context(manifest,closes,**kw)
            result=module.run(ctx)
            assert status(result)==expected,(title,status(result),result)
            assert side==[],(title,side)
            assert calls and calls[0]['timeframe']=='15m'
            checks.append(label+': '+title)
            return result,state
        positive=[100.0]*159+[110.0]
        expected='ok' if label=='script' else 'dispatch'
        stopped='hold' if label=='script' else 'skip'  # Installed ResultBuilder.skip is an alias for HOLD.
        result,state=assert_case('qualifying closed candle takes positive path',positive,expected)
        assert_case('same candle with retained state does not repeat',positive,stopped,state=state)
        assert_case('nonqualifying flat candles stop',[100.0]*160,stopped)
        assert_case('forming last candle does not hide prior closed signal',positive,expected,forming=True)
        assert_case('reader failure is an error not no-signal',positive,'error',fetch_failure=True)
        assert_case('insufficient history stops',[100.0]*8,stopped)
        assert manifest['evaluation']['mode']=='observation'
        assert manifest['schedule']['enabled'] is False
        assert manifest['policy']['allow_direct_order'] is False
        checks.append(label+': observation mode and inactive schedule retained')
        if label=='signal':
            assert manifest['agent_task']['enabled'] is True
            assert manifest['agent_profile']['role']
            evidence=json.dumps(result.metadata,ensure_ascii=False)+str(result.prompt)
            assert '110' in evidence
            checks.append('signal: actual triggering price is carried in task evidence')
        else:
            assert manifest['agent_task']['enabled'] is False
            ctx,calls,side,_=context(manifest,positive)
            ctx.config.extras['data_sources'][0]['parameters']['sma_window']=1
            assert status(module.run(ctx))=='hold'
            checks.append('script: editing source SMA parameter changes the decision')
        assert hashlib.sha256((root/'main.py').read_bytes()).hexdigest()==before,'candidate changed during audit'
    output={'scope':'Actual generated files, controlled branch inputs only. No strategy edits, real model calls or orders in this independent audit. Historical and main-Agent receipts are separate.','passed':len(checks),'checks':checks,'fingerprints':fingerprints}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(output,ensure_ascii=False))


if __name__=='__main__':main()

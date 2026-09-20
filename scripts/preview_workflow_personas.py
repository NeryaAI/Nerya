"""Isolated persona UI acceptance. Controlled history, actual backtest engine.
No real model calls, account orders, schedules or production candidate changes.
"""
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import json
import math
import os
import runpy
import time

from nerya.api.local_server import build_server
from nerya.core import yaml_io
from nerya.strategies.package import load_package
from nerya.strategies.verification import check_workflow
from nerya.strategies.workflow_service import view_workflow, propose_workflow
from nerya.skills.builtin.backtest.scripts import backtest_run

BASE=Path(__file__).resolve().parents[1]
name=os.environ.get('NERYA_PERSONA_REVIEW','workflow-personas-release')
assert name.startswith('workflow-personas-') and '/' not in name and '..' not in name
OUT=BASE/'dashboard/test-results'/name
ROOT=OUT/'workspace'
assert not ROOT.exists(), 'Use a fresh isolated review directory'
ROOT.mkdir(parents=True)
seed=runpy.run_path(str(BASE/'tests/test_strategy_dispatch_selection.py'))['seed_selected_branch']
cfg,pkg=seed(ROOT)
raw=yaml_io.load(pkg.root/'strategy.yml')
raw.update(title='MACD 条件观察 · 验收',description='无交叉结束，上穿与下穿分别分析。受控样例，不交易。',evaluation={'mode':'observation'})
yaml_io.dump(pkg.root/'strategy.yml',raw)
pkg=load_package(cfg.paths,pkg.strategy_id)
# The indicator needs 105 closed bars; explicitly warm up 160 in this fixture.
yaml_io.dump(OUT/'backtest.yml', {'warmup_bars':160})


def load_history(config,**kwargs):
    config.tf='15m'; config.timeframes=['15m']
    end=int(time.time()//900)*900
    rows=[]
    for i in range(720):
        p=100+5*math.sin(i/10)
        rows.append({'ts':end-(720-i)*900,'open':p,'high':p+1,'low':p-1,'close':p,'volume':0,'fixture':'PERSONA_CONTROLLED_HISTORY'})
    return {m:{'15m':deepcopy(rows)} for m in config.markets},['15m'],{}

with patch.object(backtest_run,'load_workspace_config',return_value=cfg), patch.object(backtest_run,'_load_candles_with_timeframe_fallback',load_history):
    replay=backtest_run.run_strategy_backtest(strategy_id=pkg.strategy_id,workspace=ROOT,allow_mock=True,config_path=str(OUT/'backtest.yml'))
(OUT/'actual-replay.json').write_text(json.dumps(replay,ensure_ascii=False,indent=2,default=str))
base=view_workflow(cfg.paths,pkg.strategy_id)
# A real candidate made through the existing proposal API: no copied result.
saved=propose_workflow(cfg.paths,{'strategy_id':pkg.strategy_id,'base_revision':base['revision'],
    'changes':[{'node_id':'script:main.py','content':(pkg.root/'main.py').read_text()+'\n# 此候选尚未做历史回放\n'}],
    'metadata':base['metadata']})
assert saved['ok'],saved
cases=[{'case':'sample','strategy_id':pkg.strategy_id,'proposal_id':None},
       {'case':'candidate','strategy_id':pkg.strategy_id,'proposal_id':saved['proposal_id']}]
# Independent copies retain actual source but have no copied runtime ledger.
for kind,title in [('stale','参数已更新 · 旧结果'),('invalid','待修复 · 语法检查')]:
    sid='review_'+kind; target=cfg.paths.strategy(sid); target.mkdir(parents=True,exist_ok=True)
    for rel in pkg.files:
        p=target/rel; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes((pkg.root/rel).read_bytes())
    manifest=yaml_io.load(target/'strategy.yml'); manifest.update(strategy_id=sid,title=title)
    yaml_io.dump(target/'strategy.yml',manifest)
    if kind=='stale':
        with patch.object(backtest_run,'load_workspace_config',return_value=cfg), patch.object(backtest_run,'_load_candles_with_timeframe_fallback',load_history):
            out=backtest_run.run_strategy_backtest(strategy_id=sid,workspace=ROOT,allow_mock=True,config_path=str(OUT/'backtest.yml'))
        p=target/'main.py';p.write_text(p.read_text()+'\n# 代码在回放完成后变更，必须重新验证\n')
    else:
        (target/'main.py').write_text('"""有意保留的语法错误，用于验证定位修复入口。"""\ndef run(ctx)\n    return None\n')
    cases.append({'case':kind,'strategy_id':sid,'proposal_id':None})
results={item['case']:check_workflow(cfg.paths,item['strategy_id'],item['proposal_id'],schedules=[]) for item in cases}
assert results['candidate']['replay']['status']=='missing'
assert results['stale']['replay']['status']=='stale'
assert not results['invalid']['validation']['ok']
assert results['sample']['replay']['provenance']['data_kind']=='sample'
assert results['sample']['replay']['status']=='sample',results['sample']['replay']
(OUT/'verification-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
server=build_server(cfg,host='127.0.0.1',port=0)
info={'api':f'http://127.0.0.1:{server.server_address[1]}','workspace':str(ROOT),'cases':cases,
      'scope':'Real UI, validation, proposal API and backtest engine. Controlled historical candles; no real model or account execution.'}
(OUT/'fixture-index.json').write_text(json.dumps(info,ensure_ascii=False,indent=2))
print(json.dumps(info,ensure_ascii=False),flush=True)
try:server.serve_forever()
finally:server.server_close()

"""Bounded public-history and artifact facade for strategy-local replay only."""
from __future__ import annotations
import csv
import json
import os
from pathlib import Path

def _roots():
    if os.environ.get('NERYA_FREEFORM_BACKTEST') != '1':
        raise RuntimeError('Research SDK is available only inside strategy-local replay')
    roots = [Path(os.environ[name]).resolve() for name in
        ('NERYA_STRATEGY_ROOT','NERYA_BACKTEST_OUT_DIR','NERYA_WORKSPACE')]
    if not roots[1].is_relative_to(roots[0]):
        raise ValueError('Replay output must stay inside the saved strategy package')
    return roots

def manifest():
    from ..core.yaml_io import load
    return load(_roots()[0] / 'strategy.yml')

def fetch_candles(market, *, interval, start, end, count):
    from ..core.config import load_config
    from ..data.candles import fetch_candles as fetch
    from ..data.history_store import timeframe_seconds
    root,out,workspace = _roots()
    saved = manifest()
    if market not in saved.get('markets',[]):
        raise ValueError('Historical market must belong to the saved candidate')
    if not all(type(x) is int for x in (start,end,count)) or not 0 < count <= 200000:
        raise ValueError('Integer bounded history window/count required')
    step = timeframe_seconds(interval)
    if start < 0 or end <= start or end-start > 366*86400 or count*step < end-start:
        raise ValueError('History window exceeds declared bounds')
    return fetch(market,interval=interval,start=start,end=end,count=count,
        config_like=load_config(workspace),allow_mock=False)

def _output(name):
    root,out,workspace = _roots()
    if not isinstance(name,str) or not name or Path(name).is_absolute():
        raise ValueError('Relative replay artifact name required')
    path = (out / name).resolve()
    if not path.is_relative_to(out):
        raise ValueError('Replay artifact path escapes output directory')
    path.parent.mkdir(parents=True,exist_ok=True)
    return path

def write_json(name, value):
    _output(name).write_text(json.dumps(value,indent=2,default=str)+chr(10),encoding='utf-8')

def write_text(name, value):
    _output(name).write_text(str(value),encoding='utf-8')

def write_csv(name, rows, *, fields):
    with _output(name).open('w',newline='',encoding='utf-8') as stream:
        writer = csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

def entrypoint():
    import importlib.util
    root,out,workspace = _roots()
    saved=manifest()
    file,sep,name=str(saved.get('entrypoint','main.py:run')).partition(':')
    path=(root/file).resolve()
    if not sep or not path.is_relative_to(root):
        raise ValueError('Saved replay entrypoint must belong to its package')
    spec=importlib.util.spec_from_file_location('nerya_saved_research_entrypoint',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn=getattr(module,name)
    if not callable(fn):raise ValueError('Saved entrypoint must be callable')
    return fn

def validate_financial_request(request):
    """Check the same request envelope without resolving funds or credentials."""
    _roots()
    from ..financial.contracts import normalize_request
    return normalize_request(request)

def validate_financial_call(method, *args, **kwargs):
    """Bind a simulated financial call to the installed keyword-only contract."""
    _roots()
    from inspect import signature
    from ..sdk.financial_api import FinancialAPI
    if method not in {'prepare','execute','get','reconcile','account_state'}:
        raise ValueError('Unsupported research financial method')
    signature(getattr(FinancialAPI,method)).bind(None,*args,**kwargs)

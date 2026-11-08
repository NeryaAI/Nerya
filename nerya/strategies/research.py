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

def dataset(name):
    """Read host-prepared immutable rows plus their provenance envelope."""
    _roots()
    import re
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('Use a declared replay dataset id')
    directory = os.environ.get('NERYA_REPLAY_INPUTS')
    if not directory:
        raise ValueError('No host-prepared replay inputs are available')
    path = Path(directory) / (name + '.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
        raise ValueError('Prepared replay input is missing or unsafe')
    return json.loads(path.read_text(encoding='utf-8'))

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
    if not callable(fn):
        raise ValueError('Saved entrypoint must be callable')
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

def replay_sampled_lp():
    """Run the saved LP policy against its immutable host-prepared dataset.

    Returns quote-token counterfactual results only. Dataset preparation stays
    outside the sandbox; callers cannot supply a replacement function or rows.
    """
    _roots()
    saved = manifest()
    policy = (saved.get('params') or {}).get('lp_replay')
    if not isinstance(policy, dict) or not isinstance(policy.get('dataset_id'), str):
        raise ValueError('Saved lp_replay dataset contract required')
    from ..skills.builtin.backtest.scripts.lp_replay import simulate_lp_policy
    return simulate_lp_policy(entrypoint(), saved, dataset(policy['dataset_id']))

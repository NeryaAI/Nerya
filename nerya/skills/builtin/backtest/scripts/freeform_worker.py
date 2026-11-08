"""Trusted bootstrap; launched only inside the freeform Seatbelt profile."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import resource
import runpy
import sys
import zoneinfo


def lp_entrypoint_bridge(entrypoint):
    """Trusted adapter for the existing isolated-strategy protocol, not a receipt.

    The host supplies one current LP snapshot. Only candidate decisions cross
    back; all ledger operations and execution evidence remain in the host.
    """
    return '''from types import SimpleNamespace
import importlib.util
from pathlib import Path
_file, _name = ENTRYPOINT.split(':')
_spec = importlib.util.spec_from_file_location('_saved_lp', Path(__file__).parent / _file)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
_run = getattr(_module, _name)
def run(ctx):
    source = ctx.config.params['lp_source']['source_id']
    def current(key):
        if key != source:
            raise ValueError('LP source unavailable')
        return ctx.trigger['lp_snapshot']
    return _run(SimpleNamespace(strategy_id=ctx.strategy_id, mode='backtest', runmode='backtest',
        run_id='lp-replay', config=ctx.config, clock=ctx.clock, state=ctx.state,
        result=ctx.result, inputs=SimpleNamespace(source=current)))
'''.replace('ENTRYPOINT', repr(entrypoint))


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    for name, value in ((resource.RLIMIT_CPU, request['cpu_seconds']),
                        (resource.RLIMIT_FSIZE, request['file_bytes']),
                        (resource.RLIMIT_NOFILE, 128), (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(name, (value, value))
    with open(os.devnull, 'rb') as stdin:
        os.dup2(stdin.fileno(), 0)
    # -I -B -S prevents host PYTHONPATH, bytecode and site/.pth injection.
    sys.path[:0] = request['dependencies']
    zoneinfo.reset_tzpath(request['timezone_roots'])
    source = Path(request['source']) / 'nerya'
    spec = importlib.util.spec_from_file_location(
        'nerya', source / '__init__.py', submodule_search_locations=[str(source)])
    module = importlib.util.module_from_spec(spec)
    sys.modules['nerya'] = module
    spec.loader.exec_module(module)
    from nerya.strategies import research

    def prepared_history_only(*args, **kwargs):
        raise RuntimeError('Freeform sandbox requires prepared package history; fetch_candles is unavailable')

    # Explain the contract before a facade can try configuration/provider IO.
    # Seatbelt still denies networking even if authored code bypasses this guard.
    research.fetch_candles = prepared_history_only
    candidate = Path(request['base']) / 'candidate'
    script = candidate / request['script']
    sys.path.extend([str(script.parent), str(candidate)])
    sys.argv = [str(script)]
    runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    main()

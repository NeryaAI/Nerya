"""Persistent real-Seatbelt replay bridge tests, synthetic temporary packages only."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time

import pytest

from nerya.harness.cancellation import CancelToken, CancelledError
from nerya.strategies.package import load_package_from_dir
from nerya.skills.builtin.backtest.scripts.isolated_strategy import IsolatedStrategy, IsolatedStrategyError, ALLOWED_METHODS

pytestmark = [pytest.mark.smoke, pytest.mark.skipif(sys.platform != "darwin", reason="real Seatbelt")]


def package(tmp_path, code, extra=None):
    root = tmp_path / "candidate"
    root.mkdir()
    for name, text in {"strategy.yml": "id: isolated_test\nmode: paper\nmarkets: [BTC/USDT]\nschedule: {type: cron, cron: '0 * * * *'}\n", "main.py": code, **(extra or {})}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return load_package_from_dir(root)


def snapshot(ts=60):
    return {"strategy_id": "isolated_test", "run_id": "historical:1", "session_id": None,
            "mode": "backtest", "runmode": "backtest", "symbol": "BTC/USDT", "timeframe": "1m",
            "clock": {"ts": ts, "now_ms": ts * 1000, "now_iso": "1970-01-01T00:01:00Z"},
            "config": {"strategy_id": "isolated_test", "markets": ["BTC/USDT"], "accounts": [],
                       "extras": {"params": {"period": 2}, "timeframe": "1m"}},
            "trigger": {"kind": "market.candle_closed", "payload": {"historical": True}}, "policy": {}}


def no_dispatch(*args):
    pytest.fail("unexpected host callback: " + str(args))


def reaped(worker):
    assert worker.closed
    assert worker.safety["process_exited"] and worker.safety["process_group_stopped"]
    assert worker.process.poll() is not None
    assert not worker.base.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(worker.process.pid, 0)


def test_frozen_source_executes_captured_bytes_despite_replaced_and_restored_path(tmp_path, monkeypatch):
    from nerya.strategies import candidate_validation as boundary
    from nerya.strategies.verification import source_revision
    code = "from helper import REASON\ndef run(ctx): return ctx.result.hold(reason=REASON)\n"
    pkg = package(tmp_path, code, {'helper.py': "REASON = 'captured original'\n",
                                  'subagents/confirm.agent.md': 'Captured role prompt.'})
    files = {name: (pkg.root / name).read_text() for name in pkg.files}
    revision = source_revision(files)
    worker = IsolatedStrategy(pkg, frozen_files=files)
    # Caller mutation after construction cannot change the captured source.
    files['helper.py'] = "REASON = 'caller mutation'\n"
    pkg.main_path.write_text("def run(ctx): return ctx.result.hold(reason='foreign candidate')\n")
    monkeypatch.setattr(boundary, '_snapshot', lambda *a: pytest.fail('mutable package path read'))
    with worker:
        # Restoring the original would hide the swap from before/after checks.
        pkg.main_path.write_text(code)
        assert worker.run(snapshot(), no_dispatch)['reason'] == 'captured original'
        assert worker.safety['source_revision'] == revision
        assert not (worker.base / 'candidate/subagents/confirm.agent.md').exists()
    reaped(worker)


def test_frozen_manifest_owns_entrypoint_and_does_not_need_original_directory(tmp_path):
    pkg = package(tmp_path, "def run(ctx): raise AssertionError('stale entrypoint')\n")
    files = {'strategy.yml': (pkg.root / 'strategy.yml').read_text() + 'entrypoint: selected.py:chosen\n',
             'selected.py': "def chosen(ctx): return ctx.result.ok(reason='frozen manifest')\n"}
    worker = IsolatedStrategy(pkg, frozen_files=files)
    pkg.root.rename(tmp_path / 'original-hidden')
    with worker:
        assert worker.run(snapshot(), no_dispatch)['reason'] == 'frozen manifest'
    reaped(worker)


@pytest.mark.parametrize('name', ['../escape.py', '/absolute.py', 'x/../escape.py', 'x//bad.py', 'x\\bad.py'])
def test_frozen_source_rejects_unsafe_paths(tmp_path, name):
    pkg = package(tmp_path, "def run(ctx): return ctx.result.ok()\n")
    files = {key: (pkg.root / key).read_text() for key in pkg.files}
    files[name] = 'pass'
    with pytest.raises((ValueError, IsolatedStrategyError), match='path'):
        IsolatedStrategy(pkg, frozen_files=files)


def test_frozen_source_rejects_modified_copy_before_worker_spawn(tmp_path, monkeypatch):
    from nerya.skills.builtin.backtest.scripts import isolated_strategy as module
    pkg = package(tmp_path, "def run(ctx): return ctx.result.ok()\n")
    files = {name: (pkg.root / name).read_text() for name in pkg.files}
    original = Path.write_bytes
    def tampered(path, data):
        if path.name == 'main.py' and 'nerya-isolated-strategy-' in str(path):
            data = b"def run(ctx): return ctx.result.hold(reason='tampered copy')\n"
        return original(path, data)
    monkeypatch.setattr(Path, 'write_bytes', tampered)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('spawned changed source'))
    with pytest.raises(IsolatedStrategyError, match='snapshot.*mismatch'):
        with IsolatedStrategy(pkg, frozen_files=files):
            pytest.fail('entered changed source')


def test_persistent_candidate_current_prefix_rpc_and_real_result_builder(tmp_path):
    pkg = package(tmp_path, '''
from nerya.strategies import StrategyResultStatus
ticks = 0
def run(ctx):
    global ticks
    ticks += 1
    assert ctx.config.params['period'] == 2
    assert ctx.config.timeframe == ctx.timeframe == '1m'
    assert ctx.clock.now_ms() == ctx.clock.ts * 1000
    assert ctx.trigger.payload.historical
    rows = ctx.market.candles(ctx.symbol, timeframe='1m', limit=10)
    position = ctx.portfolio.position(ctx.symbol)
    assert position.size == 2 and position['size'] == 2
    count = ctx.state.get('calls', 0)
    ctx.state.set('calls', count + 1)
    answer = ctx.subagents.run('analyst', context={'latest': rows[-1]['close']})
    assert answer.decision == 'hold'
    return ctx.result.hold(reason='historical', metadata={'ticks': ticks, 'rows': len(rows), 'status': StrategyResultStatus.HOLD})
''')
    state, current, calls = {}, [], []
    def dispatch(method, args, kwargs):
        calls.append(method)
        if method == 'market.candles':
            return list(current)
        if method == 'portfolio.position':
            return {'size': 2}
        if method == 'state.get':
            return state.get(args[0], args[1])
        if method == 'state.set':
            state[args[0]] = args[1]
            return None
        if method == 'subagents.run':
            return {'decision': 'hold'}
        pytest.fail(method)
    with IsolatedStrategy(pkg) as worker:
        pid = worker.process.pid
        for n in (1, 2):
            current.append({'ts': n * 60, 'close': 100 + n})
            result = worker.run(snapshot(n * 60), dispatch)
            assert result['status'] == 'hold'
            assert result['metadata'] == {'ticks': n, 'rows': n, 'status': 'hold'}
            assert worker.process.pid == pid
    assert state == {'calls': 2} and len(calls) == 10
    reaped(worker)


def test_real_boundary_denies_host_files_network_fork_and_candidate_writes(tmp_path, monkeypatch):
    sentinel = tmp_path / 'host-secret'
    sentinel.write_text('never readable')
    monkeypatch.setenv('ISOLATED_HOST_SECRET', 'never forwarded')
    pkg = package(tmp_path, f'''
import os, socket, subprocess
from pathlib import Path
def denied(fn):
    try: fn()
    except PermissionError: return
    raise AssertionError('boundary allowed operation')
def run(ctx):
    assert 'ISOLATED_HOST_SECRET' not in os.environ
    denied(lambda: Path({str(sentinel)!r}).read_text())
    denied(lambda: Path(__file__).write_text('overwrite'))
    denied(lambda: socket.socket().connect(('127.0.0.1', 9)))
    denied(lambda: subprocess.run(['/usr/bin/true']))
    denied(lambda: os.fork())
    (Path(os.environ['TMPDIR']) / 'allowed').write_text('scratch')
    return ctx.result.ok()
''')
    with IsolatedStrategy(pkg) as worker:
        assert worker.run(snapshot(), no_dispatch)['status'] == 'ok'
    assert sentinel.read_text() == 'never readable'
    reaped(worker)


def test_dispatch_exception_is_original_and_cannot_be_caught_by_candidate(tmp_path):
    pkg = package(tmp_path, '''
def run(ctx):
    try: ctx.subagents.run('analyst')
    except BaseException: return ctx.result.hold(reason='forged')
''')
    class MissingDecision(Exception):
        pass
    missing = MissingDecision('host needs decision')
    def dispatch(*_):
        raise missing
    with pytest.raises(MissingDecision) as caught:
        with IsolatedStrategy(pkg) as worker:
            worker.run(snapshot(), dispatch)
    assert caught.value is missing
    reaped(worker)


@pytest.mark.parametrize('limit,options', [('calls', {'max_calls': 2}), ('total_calls', {'max_total_calls': 2})])
def test_rpc_call_limits(tmp_path, limit, options):
    pkg = package(tmp_path, "def run(ctx):\n    for _ in range(3): ctx.state.get('safe')\n    return ctx.result.hold()\n")
    calls = []
    with pytest.raises(IsolatedStrategyError, match='call limit'):
        with IsolatedStrategy(pkg, **options) as worker:
            worker.run(snapshot(), lambda *args: calls.append(args))
    assert len(calls) == 2
    reaped(worker)


@pytest.mark.parametrize('key', ['position:BTC/USDT', '__portfolio_nav__'])
def test_reserved_state_never_reaches_host_dispatch(tmp_path, key):
    pkg = package(tmp_path, f"def run(ctx):\n    ctx.state.set({key!r}, {{'cash': 1e9}})\n")
    with pytest.raises(IsolatedStrategyError, match='accounting state'):
        with IsolatedStrategy(pkg) as worker:
            worker.run(snapshot(), no_dispatch)
    reaped(worker)


def test_configuration_is_read_only_and_private_or_unknown_facades_are_rejected(tmp_path):
    pkg = package(tmp_path, '''
def run(ctx):
    params = ctx.config.params
    assert isinstance(params, dict)
    params['period'] = 99
    assert ctx.config.params['period'] == 2
    for fn in [lambda: setattr(ctx.config, 'mode', 'live'),
               lambda: getattr(ctx, '__dict__'), lambda: getattr(ctx.trading, '_connector'),
               lambda: getattr(ctx.trading, 'submit_intent'), lambda: ctx.clock.freeze(iso='2100-01-01')]:
        try: fn()
        except (AttributeError, TypeError): pass
        else: raise AssertionError('mutable/private/unknown API')
    return ctx.result.ok()
''')
    with IsolatedStrategy(pkg) as worker:
        assert worker.run(snapshot(), no_dispatch)['status'] == 'ok'
    reaped(worker)


@pytest.mark.parametrize('attack', ['unknown', 'frame'])
def test_forged_rpc_is_validated_by_host_not_worker_facade(tmp_path, attack):
    pkg = package(tmp_path, f'''
import sys
def run(ctx):
    bootstrap = sys.modules['__main__']
    bootstrap._write({{'type': 'rpc', 'frame': 'wrong' if {attack!r} == 'frame' else bootstrap._frame,
                      'method': 'state.__dict__', 'args': [], 'kwargs': {{}}}})
    while True: pass
''')
    with pytest.raises(IsolatedStrategyError, match='denied|frame mismatch'):
        with IsolatedStrategy(pkg) as worker:
            worker.run(snapshot(), no_dispatch)
    reaped(worker)


@pytest.mark.parametrize('cancel', [False, True])
def test_timeout_or_cancel_closes_worker_group(tmp_path, cancel):
    pkg = package(tmp_path, "def run(ctx):\n    while True: pass\n")
    token = CancelToken()
    with IsolatedStrategy(pkg, cancel_token=token, request_timeout=0.3) as worker:
        timer = threading.Timer(0.05, token.cancel) if cancel else None
        if timer:
            timer.start()
        try:
            with pytest.raises(CancelledError if cancel else IsolatedStrategyError, match='cancelled|timeout'):
                worker.run(snapshot(), no_dispatch)
        finally:
            if timer:
                timer.join()
    reaped(worker)


def test_oversized_result_is_rejected_and_reaped(tmp_path):
    pkg = package(tmp_path, "def run(ctx):\n    return {'blob': 'x' * 300000}\n")
    with pytest.raises(IsolatedStrategyError, match='byte limit'):
        with IsolatedStrategy(pkg) as worker:
            worker.run(snapshot(), no_dispatch)
    reaped(worker)


def test_host_dispatch_allowlist_stays_in_sync():
    from nerya.skills.builtin.backtest.scripts.isolated_context import _METHODS
    assert ALLOWED_METHODS == _METHODS


def test_watchdog_kills_worker_during_slow_callback_without_background_host_writes(tmp_path):
    pkg = package(tmp_path, "def run(ctx):\n    ctx.state.get('safe')\n    return ctx.result.hold()\n")
    with IsolatedStrategy(pkg, request_timeout=.1) as worker:
        def dispatch(*_):
            time.sleep(.25)
            assert worker.process.poll() is not None
            return None
        with pytest.raises(IsolatedStrategyError, match='timeout'):
            worker.run(snapshot(), dispatch)
    reaped(worker)


def test_lifecycle_expiry_stops_idle_worker(tmp_path):
    pkg = package(tmp_path, "def run(ctx): return ctx.result.hold()\n")
    with IsolatedStrategy(pkg, lifecycle_timeout=1) as worker:
        assert worker.run(snapshot(), no_dispatch)['status'] == 'hold'
        worker.process.wait(timeout=2)
        with pytest.raises(IsolatedStrategyError, match='lifecycle timeout'):
            worker.run(snapshot(), no_dispatch)
    reaped(worker)


@pytest.mark.parametrize('attack', ['oversize', 'duplicate', 'array', 'nonfinite'])
def test_malformed_wire_frames_never_reach_dispatch(tmp_path, attack):
    frames = {'oversize': b'x' * 270000, 'duplicate': b'{"type":"rpc","type":"result"}\n',
              'array': b'[]\n', 'nonfinite': b'{"value":NaN}\n'}
    pkg = package(tmp_path, f'''
import sys
def run(ctx):
    wire = sys.modules['__main__']._wire
    wire.write({frames[attack]!r})
    wire.flush()
    while True: pass
''')
    with pytest.raises(IsolatedStrategyError, match='byte limit|duplicate|object|invalid worker JSON'):
        with IsolatedStrategy(pkg) as worker:
            worker.run(snapshot(), no_dispatch)
    reaped(worker)


def test_total_bytes_and_private_snapshot_fail_closed(tmp_path):
    pkg = package(tmp_path, "def run(ctx): return ctx.result.hold()\n")
    with pytest.raises(IsolatedStrategyError, match='total byte limit'):
        with IsolatedStrategy(pkg, max_total_bytes=100) as worker:
            worker.run(snapshot(), no_dispatch)
    reaped(worker)
    for key in ['host_workspace', 'tape', 'future_candles']:
        with pytest.raises(IsolatedStrategyError, match='snapshot'):
            with IsolatedStrategy(pkg) as worker:
                worker.run({**snapshot(), key: []}, no_dispatch)
        reaped(worker)


def test_host_json_normalization_does_not_execute_object_hooks(tmp_path):
    pkg = package(tmp_path, "def run(ctx): return ctx.state.get('safe')\n")
    class Unsafe:
        def asdict(self):
            pytest.fail('host object hook executed')
    with pytest.raises(IsolatedStrategyError, match='finite JSON'):
        with IsolatedStrategy(pkg) as worker:
            worker.run(snapshot(), lambda *_: Unsafe())
    reaped(worker)


def test_async_callback_selection_and_normal_prints(tmp_path):
    pkg = package(tmp_path, '''
def run(ctx): raise AssertionError('wrong callback')
async def selected(ctx):
    print('candidate diagnostic')
    return ctx.result.ok(metadata={'now': ctx.clock.now().isoformat()})
''')
    with IsolatedStrategy(pkg, callback='selected') as worker:
        result = worker.run(snapshot(), no_dispatch)
        assert result['status'] == 'ok'
        assert result['metadata']['now'].startswith('1970-01-01T00:01:00')
    reaped(worker)


def test_actual_ema_confirmation_template_runs_with_host_engine_and_current_mockctx(tmp_path):
    from nerya.data.history_store import timeframe_seconds
    from nerya.skills.builtin.backtest.scripts.config import BacktestConfig
    from nerya.skills.builtin.backtest.scripts.engine import run_backtest
    from nerya.skills.builtin.backtest.scripts.isolated_context import context_snapshot, dispatch_context

    templates = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/templates'
    pkg = package(tmp_path, (templates / 'ema_cross_confirm.py.j2').read_text(), {
        'ema_cross.py': (templates / 'ema_cross.py.j2').read_text(),
        'strategy_execution.py': (templates / 'strategy_execution.py.j2').read_text(),
    })
    prices = [100] * 5 + [110, 115, 90, 85]
    # Explicit synthetic OHLCV fixtures, not observed historical market data.
    rows = [{'ts': 1700000040 + 60 * i, 'open': price, 'high': price, 'low': price, 'close': price, 'volume': 100}
            for i, price in enumerate(prices)]
    config = BacktestConfig(markets=['BTC/USDT'], tf='1m', warmup_bars=0, initial_capital_usd=1000)
    strategy_config = {'strategy_id': 'isolated_test', 'mode': 'paper', 'markets': ['BTC/USDT'], 'timeframe': '1m',
        'params': {'indicator': {'fast_period': 2, 'slow_period': 5}, 'lookback': 100,
                   'confidence': .8, 'confirmation_agent': 'confirm', 'confirmation_min_confidence': .6,
                   'sizing': {'method': 'fixed_usd', 'fixed_usd': 100}, 'protection': None}}
    decisions, seen = [], []
    def decision(ctx, args, kwargs):
        decisions.append(kwargs['payload'])
        assert args == ['confirm']
        payload = kwargs['payload']
        assert payload['timeframe'] == ctx.config.timeframe == '1m'
        duration = timeframe_seconds(payload['timeframe'])
        assert all(set(row) == {'ts', 'open', 'high', 'low', 'close', 'volume'}
                   for row in payload['candles'])
        assert all((row['ts'] + duration) * 1000 <= ctx.clock.now_ms()
                   for row in payload['candles'])
        return {'ok': True, 'output': {'decision': 'ENTRY', 'confidence': .8}}
    with IsolatedStrategy(pkg) as worker:
        def invoke(ctx):
            snap = context_snapshot(ctx)
            seen.append(snap)
            return worker.run(snap, lambda method, args, kwargs: dispatch_context(ctx, method, args, kwargs, decision=decision))
        result = run_backtest(pkg.root, config, candles_by_market={'BTC/USDT': rows},
                              strategy_config=strategy_config, run_fn=invoke)
    assert decisions and result.order_attempts >= 2
    assert result.trades, result.decisions
    assert len(seen) == len(rows)
    assert not any('bars_by_market' in item or 'tape' in item for item in seen)
    reaped(worker)

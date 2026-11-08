"""Offline confirmation payload contract; synthetic verdicts are behavior evidence only."""
import ast
import copy
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS

import pytest

from nerya.strategies import ResultBuilder
from nerya.strategies.candle_view import candle_snapshots

pytestmark = pytest.mark.smoke
TEMPLATES = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/templates'
FIELDS = ('ts', 'open', 'high', 'low', 'close', 'volume')
ASK = 'Return decision ENTRY or HOLD and numeric confidence from 0 to 1.'


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('payload tests must not access network or DB')
    monkeypatch.setattr('socket.socket.connect', blocked)
    monkeypatch.setattr('socket.socket.connect_ex', blocked)
    monkeypatch.setattr('socket.create_connection', blocked)
    monkeypatch.setattr('sqlite3.connect', blocked)


@pytest.fixture
def reference(monkeypatch):
    for name in ('strategy_execution', 'ema_cross', 'ema_cross_confirm'):
        module = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        asset = TEMPLATES / (name + '.py.j2')
        exec(compile(asset.read_text(), str(asset), 'exec'), module.__dict__)
    return module


def rows(count=80):
    return [dict(ts=1700006400 + i * 3600, open=100., high=125., low=75.,
                 close=120. if i == count - 1 else 100., volume=10.)
            for i in range(count)]


def context(candles):
    calls, trades, saved = [], [], {}
    params = dict(indicator={'fast_period': 12, 'slow_period': 48}, lookback=240,
                  confidence=.7, confirmation_agent='fixture_analyst',
                  confirmation_min_confidence=.6, sizing={'method': 'pct_nav', 'pct_nav': .1})
    ctx = NS(config=NS(params=params, timeframe='1h', markets=('MOCK:PAIR',)),
             trigger={'market': 'MOCK:PAIR'}, result=ResultBuilder(),
             clock=NS(now_ms=lambda: (candles[-1]['ts'] + 3600) * 1000),
             market=NS(candles=lambda market, **kwargs: candles[-kwargs['limit']:]),
             portfolio=NS(positions=lambda market: []),
             state=NS(get=saved.get, set=saved.__setitem__), saved=saved,
             calls=calls, trades=trades,
             envelope={'ok': True, 'output': {'decision': 'ENTRY', 'confidence': .8}})
    def confirm(name, *, payload):
        assert name == params['confirmation_agent']
        calls.append(payload)
        return ctx.envelope
    def trade(**kwargs):
        trades.append(kwargs)
        return {'status': 'pending_approval', 'approval_id': 'fixture-approval'}
    ctx.subagents = NS(run=confirm)
    ctx.trading = NS(open_position=trade, close_position=trade)
    return ctx


@pytest.mark.parametrize('bars,expected', [(None, 60), (1, 1), (20, 20), (60, 60), (61, 60), (1000, 60)])
def test_exact_payload_bound_and_full_signal_history(reference, bars, expected):
    candles = candle_snapshots(rows(), '1h')
    original = copy.deepcopy(candles)
    ctx = context(candles)
    if bars is not None:
        ctx.config.params['confirmation_context_bars'] = bars
    signals = reference.ema_cross(candles, **ctx.config.params['indicator'])
    original_signals = copy.deepcopy(signals)
    assert reference.confirmation_step(ctx, 'MOCK:PAIR', candles, signals) == {'confidence': .8}
    assert ctx.calls == [dict(market='MOCK:PAIR', timeframe='1h', signals=signals, ask=ASK,
                              candles=[{key: row[key] for key in FIELDS}
                                       for row in candles[-expected:]])]
    assert all(tuple(row) == FIELDS for row in ctx.calls[0]['candles'])
    assert ctx.calls[0]['signals'] is signals
    assert len(signals['buy']) == len(candles) > expected
    assert candles == original and signals == original_signals
    assert ctx.config.params['indicator'] == {'fast_period': 12, 'slow_period': 48}


@pytest.mark.parametrize('bars', [True, False, 0, -1, 20., '20', None, float('nan'), float('inf')])
def test_invalid_context_is_not_silently_coerced(reference, bars):
    candles = rows()
    ctx = context(candles)
    ctx.config.params['confirmation_context_bars'] = bars
    result = reference.confirmation_step(ctx, 'MOCK:PAIR', candles, {})
    assert result.status.value == 'error' and result.error_kind == 'config_error'
    assert not ctx.calls and not ctx.trades and not ctx.saved


@pytest.mark.parametrize('alias', ['ts', 'ts_ms', 'timestamp_ms', 'open_time_ms', 'timestamp'])
@pytest.mark.parametrize('milliseconds', [False, True])
def test_real_sdk_timestamp_aliases_and_numeric_strings(reference, alias, milliseconds):
    row = rows(1)[0]
    expected = dict(row)
    stamp = row.pop('ts')
    row = {key: str(value) for key, value in row.items()}
    row[alias] = str(stamp * (1000 if milliseconds else 1))
    assert reference.confirmation_candles([row], 60, '1h') == [expected]


@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('bad', [True, False, float('nan'), float('inf'), -float('inf'),
                                'NaN', 'Infinity', '', 'not-a-number', None, {}, []])
def test_malformed_values_fail_before_agent_call(reference, field, bad):
    candles = rows()
    ctx = context(candles)
    candles[-1][field] = bad
    result = reference.confirmation_step(ctx, 'MOCK:PAIR', candles, {})
    assert result.status.value == 'error' and result.error_kind == 'confirmation_error'
    assert not ctx.calls and not ctx.trades and not ctx.saved


@pytest.mark.parametrize('field', FIELDS)
def test_missing_required_field_never_fabricates_a_value(reference, field):
    candles = rows()
    ctx = context(candles)
    del candles[-1][field]
    result = reference.confirmation_step(ctx, 'MOCK:PAIR', candles, {})
    assert result.error_kind == 'confirmation_error' and not ctx.calls


@pytest.mark.parametrize('stamp', [0, -1, .5, '2026-10-29T00:00:00Z'])
def test_invalid_timestamp_does_not_fall_back_to_another_alias(reference, stamp):
    candles = rows()
    ctx = context(candles)
    candles[-1].update(ts=stamp, timestamp_ms=1700006400000)
    result = reference.confirmation_step(ctx, 'MOCK:PAIR', candles, {})
    assert result.error_kind == 'confirmation_error' and not ctx.calls


@pytest.mark.parametrize('alias', ['ts_ms', 'timestamp_ms', 'open_time_ms', 'timestamp'])
@pytest.mark.parametrize('bad', [True, float('nan'), float('inf'), 'broken'])
def test_malformed_timestamp_alias_is_rejected(reference, alias, bad):
    candles = rows()
    ctx = context(candles)
    del candles[-1]['ts']
    candles[-1][alias] = bad
    result = reference.confirmation_step(ctx, 'MOCK:PAIR', candles, {})
    assert result.error_kind == 'confirmation_error' and not ctx.calls


@pytest.mark.parametrize('bad', [None, [], 'bad row'])
def test_malformed_candle_mapping_fails_confirmation(reference, bad):
    ctx = context(rows())
    result = reference.confirmation_step(ctx, 'MOCK:PAIR', [bad], {})
    assert result.error_kind == 'confirmation_error' and not ctx.calls


@pytest.mark.parametrize('count', [0, 1, 59, 60, 61])
def test_available_context_is_not_padded(reference, count):
    candles = rows(count)
    ctx = context(rows())
    result = reference.confirmation_step(ctx, 'MOCK:PAIR', candles, {})
    if count == 0:
        assert result.error_kind == 'confirmation_error' and not ctx.calls
    else:
        assert result == {'confidence': .8}
        assert ctx.calls[0]['candles'] == candles[-60:]


def test_heavy_metadata_is_dropped_and_serialized_payload_shrinks(reference):
    candles = candle_snapshots(rows(), '1h')
    for row in candles:
        row.update(_envelope={'source': 'fixture', 'detail': 'large metadata ' * 1000},
                   unused={'nested': ['extra' * 1000]}, source='synthetic')
    original = copy.deepcopy(candles)
    ctx = context(candles)
    signals = reference.ema_cross(candles, **ctx.config.params['indicator'])
    before = dict(market='MOCK:PAIR', timeframe='1h', candles=candles[-60:], signals=signals, ask=ASK)
    reference.confirmation_step(ctx, 'MOCK:PAIR', candles, signals)
    after = ctx.calls[0]
    def serialized_size(payload):
        return len(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                              ensure_ascii=False, allow_nan=False).encode('utf-8'))
    before_bytes, after_bytes = serialized_size(before), serialized_size(after)
    assert after_bytes < before_bytes / 10
    assert after['candles'] == [{key: row[key] for key in FIELDS} for row in candles[-60:]]
    assert after['signals'] is signals and candles == original
    print(f'confirmation payload fixture: {before_bytes} -> {after_bytes} UTF-8 bytes')


def test_run_uses_only_current_closed_prefix_and_preserves_signals(reference):
    candles = rows()
    closed = copy.deepcopy(candles)
    future = rows(1)[0]
    future.update(ts=candles[-1]['ts'] + 3600, close=80.)
    candles.append(future)
    ctx = context(candles)
    ctx.clock = NS(now_ms=lambda: future['ts'] * 1000)
    ctx.config.params['confirmation_context_bars'] = 20
    signals = reference.ema_cross(closed, **ctx.config.params['indicator'])
    result = reference.run(ctx)
    assert result.status.value == 'pending_approval' and result.approval_id == 'fixture-approval'
    assert len(ctx.calls) == len(ctx.trades) == 1
    assert ctx.calls[0]['candles'] == closed[-20:]
    assert ctx.calls[0]['signals'] == signals and signals['buy'][-1] is True
    assert all(row['ts'] < future['ts'] for row in ctx.calls[0]['candles'])
    assert ctx.trades[0]['reasoning_ref'] == f"ema_cross:buy:{closed[-1]['ts']}"


@pytest.mark.parametrize('decision,confidence,expected', [('HOLD', .8, 'hold'),
    ('ENTRY', .59, 'hold'), ('ENTRY', True, 'error'), ('ENTRY', float('nan'), 'error')])
def test_verdict_validation_and_veto_stay_in_force(reference, decision, confidence, expected):
    candles = rows()
    ctx = context(candles)
    ctx.envelope = {'ok': True, 'output': {'decision': decision, 'confidence': confidence}}
    result = reference.run(ctx)
    assert result.status.value == expected and len(ctx.calls) == 1 and not ctx.trades


def test_no_signal_and_deterministic_exit_do_not_confirm(reference):
    candles = rows()
    candles[-1]['close'] = 100.
    ctx = context(candles)
    assert reference.run(ctx).reason == 'no_new_trade_signal' and not ctx.calls and not ctx.trades
    candles[-1]['close'] = 80.
    ctx.saved.clear()
    ctx.portfolio.positions = lambda market: [NS(size=2.)]
    assert reference.run(ctx).status.value == 'pending_approval'
    assert not ctx.calls and len(ctx.trades) == 1


def test_confirmation_failure_is_retryable_and_typed_runtime_errors_propagate(reference):
    candles = rows()
    ctx = context(candles)
    def unavailable(*args, **kwargs):
        raise RuntimeError('fixture unavailable')
    ctx.subagents.run = unavailable
    assert reference.run(ctx).error_kind == 'confirmation_error'
    assert not ctx.saved and not ctx.trades
    class Cancelled(RuntimeError):
        pass
    def cancelled(*args, **kwargs):
        raise Cancelled('fixture cancellation')
    ctx.subagents.run = cancelled
    with pytest.raises(Cancelled):
        reference.run(ctx)
    assert not ctx.saved and not ctx.trades


def test_generated_package_accepts_saved_context_option_and_allowed_imports(tmp_path):
    from nerya.core import yaml_io
    from nerya.core.paths import WorkspacePaths
    from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
    from nerya.strategies.package import load_package_from_dir
    from nerya.strategies.validator import _validate_loaded, static_scan_blockers

    request = StrategyGenerationRequest(
        strategy_id='fixture_bounds', strategy_class='trend', execution_mode='script',
        markets=('MOCK:PAIR',), accounts=('paper_fixture',), timeframe='1h',
        subagents=('fixture_analyst',), create_tuning=False,
        files={'strategy.yml': yaml_io.dumps({'params': {'confirmation_context_bars': 20}})})
    package = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        request, validate=False, create_proposal_record=False)
    manifest = yaml_io.loads(package.files['strategy.yml'])
    assert manifest['params']['confirmation_context_bars'] == 20
    # Generation rewrites documentation comments; executable code must match.
    assert ast.dump(ast.parse(package.files['main.py'])) == ast.dump(
        ast.parse((TEMPLATES / 'ema_cross_confirm.py.j2').read_text()))
    assert {'ema_cross.py', 'strategy_execution.py', 'tests/test_contract.py'} <= package.files.keys()
    candidate = tmp_path / 'candidate'
    for name, source in package.files.items():
        destination = candidate / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding='utf-8')
    loaded = load_package_from_dir(candidate)
    assert not static_scan_blockers(loaded)
    checked = _validate_loaded(loaded, smoke_test=False, run_tests=False)
    assert checked.ok, checked.asdict()

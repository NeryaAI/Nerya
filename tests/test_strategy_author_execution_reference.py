"""Behavioral checks of reviewed authoring assets, never live candidate code."""
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
import sys

import pytest
from nerya.strategies import ResultBuilder

pytestmark = pytest.mark.smoke
ROOT = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author'


def synthetic_candle(ts, close):
    """Explicit flat OHLCV fixture, not observed historical market data."""
    return dict(ts=ts, open=close, high=close, low=close, close=close, volume=100.)


@pytest.fixture(params=['plain', 'confirmed'])
def example(request, monkeypatch):
    shared = ROOT / 'templates/strategy_execution.py.j2'
    if shared.exists():
        execution = ModuleType('strategy_execution')
        exec(compile(shared.read_text(), str(shared), 'exec'), execution.__dict__)
        monkeypatch.setitem(sys.modules, 'strategy_execution', execution)
    module = ModuleType('main')
    asset = ROOT / 'templates/ema_cross.py.j2'
    exec(compile(asset.read_text(), str(asset), 'exec'), module.__dict__)
    if request.param == 'confirmed':
        monkeypatch.setitem(sys.modules, 'ema_cross', module)
        asset = ROOT / 'templates/ema_cross_confirm.py.j2'
        exec(compile(asset.read_text(), str(asset), 'exec'), module.__dict__)
    return module


def context(*, last=120., quantity=0., receipt=None):
    market = 'MOCK:PAIR'
    prices = [100.] * 60 + [last]
    rows = [synthetic_candle(1700006400 + i * 3600, value) for i, value in enumerate(prices)]
    state, calls = {}, []
    receipt = receipt if receipt is not None else {'status': 'filled', 'order_id': 'order-1'}
    params = dict(indicator=dict(fast_period=12, slow_period=48), lookback=len(rows),
                  sizing={'method': 'pct_nav', 'pct_nav': .17}, confidence=.8,
                  protection={'stop_loss': {'type': 'pct', 'value': .03}},
                  confirmation_min_confidence=.6, confirmation_agent='market_analyst')
    ctx = NS(config=NS(markets=(market,), timeframe='1h', params=params),
             trigger={'market': market}, result=ResultBuilder(),
             state=NS(get=state.get, set=state.__setitem__),
             clock=NS(now_ms=lambda: (rows[-1]['ts'] + 3600) * 1000),
             policy=NS(default_order_usd=1), calls=calls, saved=state, rows=rows,
             verdict={'ok': True, 'output': {'decision': 'ENTRY', 'confidence': .8}},
             execution={'status': 'unknown', 'terminal': False, 'reconciled': False,
                        'has_open_orders': None, 'recovery': 'verify_execution'})
    def candles(requested_market, *, timeframe, limit):
        calls.append(('data', requested_market))
        assert timeframe == ctx.config.timeframe and limit == params['lookback']
        return ctx.rows[-limit:]
    def positions(requested_market):
        calls.append(('positions', requested_market))
        return [NS(size=quantity)] if quantity else []
    def agent(name, *, payload):
        calls.append(('agent', name))
        return ctx.verdict
    def enter(*, market, side, sizing, protection, confidence, reasoning_ref):
        calls.append(('open', market))
        assert sizing == params['sizing'] and protection == params['protection']
        return receipt
    def close(*, market, side, confidence, reasoning_ref):
        calls.append(('close', market))
        return receipt
    ctx.market = NS(candles=candles)
    ctx.portfolio = NS(positions=positions, position=lambda market: next(iter(positions(market)), None))
    ctx.subagents = NS(run=agent)
    def execution_status(original_receipt):
        calls.append(('execution_status', original_receipt))
        assert original_receipt == receipt
        return ctx.execution
    ctx.trading = NS(open_position=enter, close_position=close, execution_status=execution_status)
    return ctx


def test_failed_sdk_receipt_is_error_with_full_reconciliation_ids(example):
    receipt = {'status': 'failed', 'plan_id': 'plan-1', 'session_id': 'session-1',
               'executor_id': 'executor-1', 'executor': {'order_ids': ['order-1']}}
    ctx = context(receipt=receipt)
    result = example.run(ctx)
    assert result.status.value == 'error'
    assert result.metadata['execution'] == receipt


def test_authoritative_filled_query_releases_pending_for_signal_exit(example):
    receipt = {'status': 'submitted', 'intent_id': 'intent-1', 'order_id': None}
    ctx = context(receipt=receipt)
    assert example.run(ctx).status.value == 'submitted'
    ctx.execution = {'status': 'filled', 'terminal': True, 'reconciled': True,
                     'has_open_orders': False, 'recovery': None}
    ctx.portfolio.positions = lambda market: [NS(size=2.)]
    ctx.rows.append(synthetic_candle(ctx.rows[-1]['ts'] + 3600, 80.))
    example.run(ctx)
    assert [c[0] for c in ctx.calls if c[0] in {'open', 'close'}] == ['open', 'close']
    saved = next(iter(ctx.saved.values()))
    assert saved['last_submission']['receipt'] == receipt
    assert saved['last_submission']['execution_status'] == ctx.execution


@pytest.mark.parametrize('override', [
    {'terminal': False}, {'reconciled': False}, {'has_open_orders': True},
    {'has_open_orders': None}, {'status': 'unknown'}, {'status': 'submitted'},
])
def test_incomplete_authority_query_keeps_pending_despite_changed_holdings(example, override):
    ctx = context(receipt={'status': 'filled', 'order_id': 'o1'})
    example.run(ctx)
    ctx.execution = dict(status='filled', terminal=True, reconciled=True,
                         has_open_orders=False, recovery=None)
    ctx.execution.update(override)
    ctx.portfolio.positions = lambda market: [NS(size=2.)]
    ctx.rows.append(synthetic_candle(ctx.rows[-1]['ts'] + 3600, 80.))
    example.run(ctx)
    assert [c[0] for c in ctx.calls if c[0] in {'open', 'close'}] == ['open']
    assert next(iter(ctx.saved.values()))['pending']['receipt']['order_id'] == 'o1'


@pytest.mark.parametrize('status', ['filled', 'rejected', 'canceled'])
def test_confirmed_terminal_consumes_candle_and_preserves_ids_without_retry(example, status):
    receipt = {'status': 'submitted', 'intent_id': 'i1'}
    ctx = context(receipt=receipt)
    example.run(ctx)
    ctx.execution = dict(status=status, terminal=True, reconciled=True,
                         has_open_orders=False, recovery=None)
    assert example.run(ctx).reason == 'duplicate_closed_candles'
    assert [c[0] for c in ctx.calls].count('open') == 1
    saved = next(iter(ctx.saved.values()))
    assert 'pending' not in saved and saved['last_submission']['receipt'] == receipt
    assert saved['last_submission']['execution_status'] == ctx.execution


@pytest.mark.parametrize('receipt_status,expected', [
    ('rejected', 'rejected'), ('pending_approval', 'pending_approval'),
    ('submitted', 'submitted'), ('partial', 'partial'), ('filled', 'filled'),
    ('error', 'error'), ('canceled', 'error'), ('working', 'submitted'), ('unknown', 'error'),
])
@pytest.mark.parametrize('last,quantity,action', [(120., 0., 'open'), (80., 2., 'close')])
def test_receipts_and_ids_survive_without_automatic_resubmission(example, receipt_status, expected, last, quantity, action):
    receipt = dict(status=receipt_status, plan_id='p1', session_id='s1', approval_id='a1',
                   executor_id='e1', order_id='o1', executor={'order_ids': ['o1', 'o2']})
    ctx = context(last=last, quantity=quantity, receipt=receipt)
    result = example.run(ctx)
    assert result.status.value == expected and result.metadata['execution'] == receipt
    assert (result.approval_id, result.order_id, result.session_id) == ('a1', 'o1', 's1')
    example.run(ctx)
    ctx.rows.append(synthetic_candle(ctx.rows[-1]['ts'] + 3600, last))
    example.run(ctx)
    assert [c[0] for c in ctx.calls].count(action) == 1
    assert next(iter(ctx.saved.values()))['pending']['receipt'] == receipt


def test_unknown_submission_exception_blocks_retries_even_on_new_candles(example):
    ctx = context()
    def timeout(**kwargs):
        ctx.calls.append(('open', kwargs['market']))
        raise TimeoutError('ack lost after possible acceptance')
    ctx.trading.open_position = timeout
    with pytest.raises(TimeoutError):
        example.run(ctx)
    assert example.run(ctx).status.value == 'error'
    ctx.rows.append(synthetic_candle(ctx.rows[-1]['ts'] + 3600, 130.))
    assert example.run(ctx).status.value == 'error'
    assert [c[0] for c in ctx.calls].count('open') == 1


def test_pre_submission_read_failure_allows_same_candle_retry(example):
    ctx = context()
    original = ctx.portfolio.positions
    def failed(market):
        raise RuntimeError('read failure before any submission')
    ctx.portfolio.positions = failed
    with pytest.raises(RuntimeError):
        example.run(ctx)
    assert not ctx.saved
    ctx.portfolio.positions = original
    assert example.run(ctx).status.value == 'filled'
    assert [c[0] for c in ctx.calls].count('open') == 1


@pytest.mark.parametrize('foreign', ['MOCK:OTHER', 'OTHER:PAIR'])
def test_foreign_market_stops_before_reads_or_side_effects(example, foreign):
    ctx = context()
    ctx.trigger['market'] = foreign
    assert example.run(ctx).status.value == 'error'
    assert not ctx.calls and not ctx.saved


def test_open_candle_cannot_enter_but_same_candle_can_enter_after_close(example):
    ctx = context()
    ctx.clock.now_ms = lambda: ctx.rows[-1]['ts'] * 1000 + 60000
    assert example.run(ctx).status.value == 'hold'
    assert not any(c[0] in {'agent', 'open', 'close'} for c in ctx.calls)
    ctx.clock.now_ms = lambda: (ctx.rows[-1]['ts'] + 3600) * 1000
    assert example.run(ctx).status.value == 'filled'


def test_ema_crossover_requires_slow_period_plus_one_closed_bars(example):
    """A 48-bar request cannot manufacture a 12/48 EMA crossover."""
    ctx = context()
    ctx.rows = [synthetic_candle(1700006400 + i * 3600, 100.) for i in range(48)]
    ctx.config.params['lookback'] = len(ctx.rows)
    ctx.clock.now_ms = lambda: (ctx.rows[-1]['ts'] + 3600) * 1000
    result = example.run(ctx)
    assert result.status.value == 'hold'
    assert result.reason == 'insufficient_closed_history'
    assert not any(c[0] in {'open', 'close'} for c in ctx.calls)

    ctx = context()
    ctx.rows = [synthetic_candle(1700006400 + i * 3600, 100.) for i in range(48)]
    ctx.rows.append(synthetic_candle(ctx.rows[-1]['ts'] + 3600, 110.))
    ctx.config.params['lookback'] = len(ctx.rows)
    ctx.clock.now_ms = lambda: (ctx.rows[-1]['ts'] + 3600) * 1000
    assert example.run(ctx).status.value == 'filled'


def test_ema_crossover_drops_forming_bar_before_history_check(example):
    ctx = context()
    ctx.rows = [synthetic_candle(1700006400 + i * 3600, 100.) for i in range(48)]
    ctx.rows.append(synthetic_candle(ctx.rows[-1]['ts'] + 3600, 110.))
    ctx.config.params['lookback'] = len(ctx.rows)
    ctx.clock.now_ms = lambda: ctx.rows[-1]['ts'] * 1000 + 60_000
    result = example.run(ctx)
    assert result.status.value == 'hold'
    assert result.reason == 'insufficient_closed_history'
    assert not any(c[0] in {'open', 'close'} for c in ctx.calls)


def test_read_only_confirmation_failure_then_verdict_and_veto(example):
    if not hasattr(example, 'confirmation_step'):
        return
    ctx = context()
    ctx.verdict = {'ok': False, 'error': 'timeout'}
    assert example.run(ctx).status.value == 'error' and not ctx.saved
    ctx.verdict = {'ok': True, 'output': {'decision': 'ENTRY', 'confidence': .8}}
    assert example.run(ctx).status.value == 'filled'
    assert [c[0] for c in ctx.calls].count('agent') == 2
    for decision, confidence in [('HOLD', 1.), ('ENTRY', .59)]:
        ctx = context()
        ctx.verdict = {'ok': True, 'output': {'decision': decision, 'confidence': confidence}}
        assert example.run(ctx).status.value == 'hold'
        assert not any(c[0] in {'open', 'close'} for c in ctx.calls)
    ctx = context(last=80., quantity=2.)
    assert example.run(ctx).status.value == 'filled'
    assert not any(c[0] == 'agent' for c in ctx.calls)


def test_confirmation_runtime_failure_is_error_without_consuming_candle(example):
    if not hasattr(example, 'confirmation_step'):
        return
    ctx = context()
    original = ctx.subagents.run
    def failed(name, *, payload):
        raise RuntimeError('model down')
    ctx.subagents.run = failed
    result = example.run(ctx)
    assert result.status.value == 'error' and result.error_kind == 'confirmation_error'
    assert ctx.saved == {} and not any(c[0] in {'open', 'close'} for c in ctx.calls)
    ctx.subagents.run = original
    assert example.run(ctx).status.value == 'filled'
    assert [c[0] for c in ctx.calls].count('open') == 1


def test_confirmation_cancellation_and_typed_failures_propagate(example):
    if not hasattr(example, 'confirmation_step'):
        return
    from asyncio import CancelledError as AsyncCancelledError
    from concurrent.futures import CancelledError as FutureCancelledError
    from nerya.harness.cancellation import CancelledError
    class ControlSignal(RuntimeError):
        pass
    for error in (CancelledError('cancel'), AsyncCancelledError(), FutureCancelledError(),
                  ControlSignal('stop'), ValueError('invalid input')):
        ctx = context()
        def stopped(name, *, payload):
            raise error
        ctx.subagents.run = stopped
        with pytest.raises(type(error)) as caught:
            example.run(ctx)
        assert caught.value is error
        assert ctx.saved == {} and not any(c[0] in {'open', 'close'} for c in ctx.calls)

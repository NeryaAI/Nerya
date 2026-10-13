"""Test-token pool momentum, bounded buy/hold/stop/sell roundtrip."""
from decimal import Decimal
from monad_runtime import binding, observation, advance, submit, funds_request, outcome, number


def run(ctx):
    p = ctx.config.params
    if not 1 <= p['stop_bps'] <= 1000 or not 1 <= p['slippage_bps'] <= 300 or number(p['entry_amount']) > Decimal('0.01'):
        raise ValueError('Bounded meme size/stop/slippage required')
    if not 1 <= p['momentum_bps'] <= p['max_momentum_bps'] <= 1000:
        raise ValueError('Bounded momentum threshold required')
    state = binding(ctx, p)
    wait = advance(ctx, state)
    if wait is not None:
        return wait
    if state['phase'] in {'done', 'blocked'}:
        return outcome('Bounded cycle finished or blocked', phase=state['phase'])
    pool = observation(ctx, p, 'pool')
    if p['test_token_symbol'] != 'nMEME' or p['test_token'].lower() not in {pool['token0'].lower(), pool['token1'].lower()}:
        raise ValueError('Explicit nMEME test token required')
    price = number(pool['quote_per_token'])
    request = funds_request(p, 'swap')
    request['parameters'] = {'pool_id': p['pool_id'], 'token_out': p['test_token']}
    request['slippage_bps'] = p['slippage_bps']
    if state['phase'] == 'idle':
        previous = state.get('sample') if ctx.mode == 'live' else ctx.state.get('paper_sample')
        sample = {'price': str(price), 'block': pool['block'], 'as_of_ms': pool['as_of_ms']}
        if not previous:
            state['sample'] = sample
            ctx.state.set('cycle' if ctx.mode == 'live' else 'paper_sample', state if ctx.mode == 'live' else sample)
            return outcome('First actual pool observation saved; waiting for momentum')
        if pool['block'] <= previous['block']:
            return outcome('Duplicate/out-of-order pool block')
        state['sample'] = sample
        ctx.state.set('cycle' if ctx.mode == 'live' else 'paper_sample', state if ctx.mode == 'live' else sample)
        elapsed = pool['as_of_ms']-previous['as_of_ms']
        momentum = (price/number(previous['price'])-1)*10000
        if not p['min_sample_seconds']*1000 <= elapsed <= p['max_sample_seconds']*1000 or not p['momentum_bps'] <= momentum <= p['max_momentum_bps']:
            return outcome('Pool momentum gate did not qualify', momentum_bps=str(momentum))
        request.update(asset=p['quote_token'], amount=str(number(p['entry_amount'])))
        state['entry_price'] = str(price)
        return submit(ctx, p, state, 'buy', request, 'watch')
    bought = state['receipts']['buy']['receipt']
    state['entry_price'] = str(number(bought['amount_in'])/number(bought['amount_out']))
    held = ctx.clock.now_ms()-state['confirmed_at_ms']
    drawdown = (1-price/number(state['entry_price']))*10000
    if held < p['max_hold_seconds']*1000 and drawdown < p['stop_bps']:
        return outcome('Holding test token; bounded software stop', drawdown_bps=str(drawdown))
    # Spend only confirmed credits from this strategy, never quoted output or all-wallet balance.
    amount = number(state['receipts']['buy']['receipt']['amount_out'])
    request.update(asset=p['test_token'], amount=str(amount))
    request['parameters']['token_out'] = p['quote_token']
    return submit(ctx, p, state, 'sell', request, 'done')

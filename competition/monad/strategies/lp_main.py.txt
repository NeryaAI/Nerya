"""Bounded testnet LP mint, fee watch, collect and full exit."""
from decimal import Decimal, ROUND_DOWN
from monad_runtime import binding, observation, advance, submit, funds_request, outcome, number


def run(ctx):
    p = ctx.config.params
    state = binding(ctx, p)
    wait = advance(ctx, state)
    if wait is not None:
        return wait
    if state['phase'] in {'done', 'blocked'}:
        return outcome('Bounded cycle finished or blocked', phase=state['phase'])
    pool = observation(ctx, p, 'pool')
    request = funds_request(p, 'lp_add')
    if state['phase'] == 'idle':
        spacing, tick = p['pool']['tick_spacing'], int(pool['tick'])
        width = p['range_ticks']
        if type(width) is not int or width <= spacing or width > 6000 or width % spacing:
            raise ValueError('Bounded aligned range required')
        center = tick // spacing * spacing
        lower, upper = center - width, center + width
        if not -887272 < lower < tick < upper < 887272:
            raise ValueError('Invalid range')
        # token_amounts yields actual two-sided amounts at the observed sqrt price.
        amounts = pool['mint_amounts']
        if pool.get('range_ticks') != width or pool.get('tick_lower') != lower or pool.get('tick_upper') != upper:
            raise ValueError('Reader mint sizing range mismatch')
        maxima = [number(p['amount0_max']), number(p['amount1_max'])]
        for index in (0, 1):
            if not 0 < number(amounts[index]) <= maxima[index]:
                raise ValueError('Computed mint exceeds fixed input cap')
        slip = p['slippage_bps']
        if type(slip) is not int or not 1 <= slip <= 300:
            raise ValueError('Invalid LP slippage')
        request['parameters'] = {'pool_id': p['pool_id'], 'tick_lower': lower, 'tick_upper': upper,
            'amount0_max': str(maxima[0]), 'amount1_max': str(maxima[1]),
            'amount0_min': str((number(amounts[0]) * (10000-slip)/10000).quantize(Decimal(10)**-pool['decimals'][0], rounding=ROUND_DOWN)),
            'amount1_min': str((number(amounts[1]) * (10000-slip)/10000).quantize(Decimal(10)**-pool['decimals'][1], rounding=ROUND_DOWN)), 'minimum_liquidity': p['minimum_liquidity']}
        state['range'] = [lower, upper]
        return submit(ctx, p, state, 'mint', request, 'watch')
    minted = state['receipts']['mint']['receipt']
    token_id = int(minted['position_id'])
    request['parameters'] = {'pool_id': p['pool_id']}
    observation_state = ctx.financial.account_state(request)
    if observation_state.get('health') != 'ok':
        return outcome('LP inventory unavailable')
    position = next((row for row in observation_state['positions'] if row['token_id'] == token_id), None)
    if position is None or position['pool']['pool_id'] != p['pool_id']:
        raise ValueError('Confirmed strategy NFT absent or mismatched')
    ctx.audit.log('monad.lp.watch', {'token_id': token_id, 'tick': pool['tick'],
        'tokens_owed_base': position['tokens_owed_base'], 'liquidity': position['liquidity']})
    if state['phase'] == 'watch':
        held = ctx.clock.now_ms() - state['confirmed_at_ms']
        outside = not state['range'][0] <= int(pool['tick']) < state['range'][1]
        fees = any(int(value) > 0 for value in position['tokens_owed_base'])
        if not outside and held < p['max_hold_seconds'] * 1000 and not fees:
            return outcome('Watching actual fee growth and range', token_id=token_id, tokens_owed_base=position['tokens_owed_base'])
        request['kind'] = 'lp_collect'
        request['parameters'] = {'pool_id': p['pool_id'], 'token_id': token_id,
            'collect0_max': p['collect0_max'], 'collect1_max': p['collect1_max']}
        return submit(ctx, p, state, 'collect', request, 'exit')
    request['kind'] = 'lp_remove'
    request['parameters'] = {'pool_id': p['pool_id'], 'token_id': token_id, 'liquidity': int(position['liquidity']),
        'collect0_max': p['collect0_max'], 'collect1_max': p['collect1_max']}
    if int(position['liquidity']) <= 0:
        raise ValueError('Unexpected empty LP; reconcile before exit')
    return submit(ctx, p, state, 'exit', request, 'done')

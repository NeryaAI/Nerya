"""Actual closed hourly SMA6/18 signals, gated one-position testnet cycle."""
from decimal import Decimal
from monad_runtime import binding, observation, advance, submit, outcome, number


def signal(candles, now_ms):
    rows = [row for row in candles if int(row['time']) + 3600000 <= now_ms]
    rows.sort(key=lambda row: int(row['time']))
    times = [int(row['time']) for row in rows]
    if len(set(times)) != len(times) or any(b-a != 3600000 for a, b in zip(times, times[1:])):
        raise ValueError('Duplicate/gapped hourly candles')
    if len(rows) < 19:
        return {'signal': 'warmup', 'bars': len(rows)}
    values = [number(row['close']) for row in rows]
    fast0, slow0 = sum(values[-7:-1])/6, sum(values[-19:-1])/18
    fast1, slow1 = sum(values[-6:])/6, sum(values[-18:])/18
    direction = 'long' if fast0 <= slow0 and fast1 > slow1 else 'short' if fast0 >= slow0 and fast1 < slow1 else 'none'
    return {'signal': direction, 'time': times[-1], 'fast': str(fast1), 'slow': str(slow1), 'bars': len(rows)}


def run(ctx):
    p = ctx.config.params
    if not 1 <= p['stop_bps'] <= 1000 or not 1 <= p['slippage_bps'] <= 300 or number(p['fixed_base']) > 1000 or p['max_negative_pnl_bps'] != 100:
        raise ValueError('Bounded Perpl size/stop/slippage required')
    state = binding(ctx, p)
    wait = advance(ctx, state)
    if wait is not None:
        return wait
    if state['phase'] in {'done', 'blocked'}:
        return outcome('Bounded cycle finished or blocked', phase=state['phase'])
    public = observation(ctx, p, 'perpl')
    if public.get('market') != 'PERPL:MON/AUSD':
        raise ValueError('MON public market required')
    computed = signal(public['candles'], ctx.clock.now_ms())
    if computed.get('time') and ctx.clock.now_ms()-computed['time'] > 7200000:
        return outcome('Closed hourly data is stale', signal=computed)
    ctx.audit.log('monad.perp.signal', computed)
    request = {'kind': 'trade', 'chain': p['chain'], 'account_id': p['account_id'],
        'market': 'PERPL:MON/AUSD', 'component_id': p['component_id'], 'parameters': {'slippage_bps': p['slippage_bps'], 'max_negative_pnl_bps': p['max_negative_pnl_bps']}, 'plan': {'strategy_id': ctx.strategy_id}}
    if state['phase'] == 'idle':
        if computed['signal'] not in {'long', 'short'}:
            return outcome('No actual SMA6/18 crossing', signal=computed)
        if computed['time'] <= state.get('last_signal_ms', 0):
            return outcome('Signal already consumed', signal=computed)
        observed = ctx.financial.account_state(request) if ctx.mode == 'live' and p['execution_enabled'] else None
        if observed and (observed.get('health') != 'ok' or observed.get('chain_id') != 10143 or observed['positions']):
            return outcome('Entry requires healthy empty isolated Perpl account')
        size = number(p['fixed_base'])
        state.update(side=computed['signal'], signal=computed, last_signal_ms=computed['time'])
        action, phase = 'open_position', 'watch'
    else:
        held = ctx.clock.now_ms() - state['confirmed_at_ms']
        entry = state['receipts']['open']['receipt']
        price = number(public['mark'])
        entry_price = number(entry['average_price'])
        adverse = (entry_price-price)/entry_price if state['side'] == 'long' else (price-entry_price)/entry_price
        opposite = computed['signal'] in {'long', 'short'} and computed['signal'] != state['side']
        if held < p['max_hold_seconds']*1000 and adverse*10000 < p['stop_bps'] and not opposite:
            return outcome('Watching position; software stop needs active ticks', signal=computed, adverse_bps=str(adverse*10000))
        observed = ctx.financial.account_state(request)
        if observed.get('health') != 'ok' or observed.get('chain_id') != 10143:
            return outcome('Fresh position inventory required for close')
        market = observed['market']
        selected = [row for row in observed['positions'] if row['mkt'] == market['id'] and row['sd'] == (1 if state['side']=='long' else 2)]
        if len(selected) != 1:
            raise ValueError('Expected isolated confirmed Perpl position')
        position = selected[0]
        size = Decimal(position['s']) / 10**market['config']['size_decimals']
        if size != number(entry['filled_size']):
            raise ValueError('Position changed outside strategy; no guessed close size')
        request['parameters'].update(reduce_only=True, position_id=position['pid'])
        action, phase = 'close_position', 'done'
    request['plan'] = {'plan_id': ctx.strategy_id+':'+state['cycle_id']+':'+action, 'strategy_id': ctx.strategy_id, 'account_id': p['account_id'], 'market': request['market'],
        'action': action, 'source': 'strategy_runtime', 'side': state['side'], 'sizing': {'method': 'fixed_base', 'fixed_base': float(size)},
        'entry': {'order_type': 'market', 'time_in_force': 'ioc'}, 'meta': {'leverage': 1}, 'confidence': 1}
    return submit(ctx, p, state, 'open' if action=='open_position' else 'close', request, phase)

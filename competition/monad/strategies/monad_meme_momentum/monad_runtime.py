"""Shared, copied SDK-only action journal. No wallet/network access here."""
from decimal import Decimal
import hashlib
import json


def number(value, *, zero=False):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0 or (not zero and value == 0):
        raise ValueError('Expected a finite positive quantity')
    return value


def outcome(reason, **metadata):
    return {'status': 'hold', 'reason': reason, 'metadata': metadata}


def binding(ctx, params):
    if params.get('chain_id') != 10143 or params.get('chain') != 'monad-testnet':
        raise ValueError('Monad testnet 10143 required')
    if len(ctx.config.accounts) != 1 or ctx.config.accounts[0] != params['account_id']:
        raise ValueError('Declared strategy account mismatch')
    if type(params['max_hold_seconds']) is not int or not 1 <= params['max_hold_seconds'] <= 3600:
        raise ValueError('Hold cap must be 1..3600 seconds')
    if type(params['max_data_age_seconds']) is not int or not 1 <= params['max_data_age_seconds'] <= 60:
        raise ValueError('Observation freshness cap must be 1..60 seconds')
    fingerprint = hashlib.sha256(json.dumps({'strategy': ctx.strategy_id, 'params': params}, sort_keys=True).encode()).hexdigest()
    state = ctx.state.get('cycle')
    if state and state['binding'] != fingerprint:
        raise ValueError('State/config binding changed; reconcile original cycle first')
    if not state and ctx.mode != 'live':
        return {'binding': fingerprint, 'phase': 'idle', 'receipts': {}, 'cycle_id': params['cycle_id']}
    if not state:
        state = {'binding': fingerprint, 'phase': 'idle', 'receipts': {}, 'cycle_id': params['cycle_id']}
        ctx.state.set('cycle', state)
    return state


def observation(ctx, params, kind):
    rows = ctx.news.fetch(sources=[params['observation_source']], limit=1)
    matches = [row for row in rows if row.get('kind') == kind]
    if len(matches) != 1:
        raise ValueError('Fresh configured observation unavailable')
    row = matches[0]
    age = ctx.clock.now_ms() - int(row['as_of_ms'])
    if row.get('chain_id') != 10143 or not 0 <= age <= params['max_data_age_seconds'] * 1000:
        raise ValueError('Wrong network or stale observation')
    if kind == 'pool':
        if row.get('deployment_sha256') != params['deployment_sha256'] or row.get('pool_id') != params['pool_id']:
            raise ValueError('Pool deployment binding mismatch')
        for field in ('address', 'token0', 'token1'):
            if str(row[field]).lower() != params['pool'][field].lower():
                raise ValueError('Pool identity mismatch')
        if row['tick_spacing'] != params['pool']['tick_spacing'] or row['fee'] != params['pool']['fee']:
            raise ValueError('Pool parameter mismatch')
    return row


def advance(ctx, state):
    pending = state.get('pending')
    if not pending:
        return None
    if not pending.get('action_id'):
        return outcome('Interrupted before action checkpoint; operator recovery required', pending=pending)
    action = ctx.financial.get(pending['action_id'])
    if action['state'] in {'submitted', 'confirming', 'unconfirmed', 'needs_recovery'}:
        action = ctx.financial.reconcile(action['action_id'])
    if action['state'] == 'confirmed':
        receipt = action.get('receipt') or {}
        state['receipts'][pending['label']] = {'action_id': action['action_id'], 'receipt': receipt}
        state['phase'] = pending['next_phase']
        state['confirmed_at_ms'] = ctx.clock.now_ms()
        state.pop('pending')
        ctx.state.set('cycle', state)
        return outcome('Confirmed financial action; next phase on next tick', phase=state['phase'], action_id=action['action_id'], receipt=receipt)
    if action['state'] in {'rejected', 'failed_before_submission'}:
        state['phase'] = 'blocked'
        ctx.state.set('cycle', state)
        return outcome('Action failed; no automatic retry', action_id=action['action_id'], state=action['state'])
    # Prepared/approval/prerequisite actions belong to their original producer tick.
    # Never execute them from a later tick, replace their identity, or spoof a lease.
    return outcome('Awaiting original approval/prerequisite or receipt', action_id=action['action_id'], state=action['state'], approval_id=action.get('approval_id'))


def submit(ctx, params, state, label, request, next_phase):
    if ctx.mode != 'live' or params['execution_enabled'] is not True:
        return outcome('Observation only; live testnet gate is disabled', candidate=request)
    if state.get('pending'):
        return advance(ctx, state)
    pending = {'label': label, 'request': request, 'next_phase': next_phase, 'producer_run_id': ctx.run_id}
    state['pending'] = pending
    ctx.state.set('cycle', state)  # intent checkpoint precedes any preparation/submission
    action = ctx.financial.prepare(request, client_request_id=f"{ctx.strategy_id}:{state['cycle_id']}:{label}")
    pending['action_id'] = action['action_id']
    ctx.state.set('cycle', state)
    result = ctx.financial.execute(action['action_id'], quote_hash=action['quote_hash'])
    current = ctx.financial.get(action['action_id'])
    if result.get('status') == 'prerequisite_required':
        return outcome('Finite approvals required under original action', action_id=action['action_id'], required_actions=result.get('required_actions', []))
    if current['state'] == 'awaiting_approval':
        return {'status': 'pending_approval', 'reason': 'Explicit financial authorization required',
                'approval_id': current.get('approval_id'), 'metadata': {'action_id': action['action_id']}}
    return advance(ctx, state)


def funds_request(params, kind):
    return {'kind': kind, 'account_id': params['account_id'], 'wallet_id': params['wallet_id'],
            'chain': params['chain'], 'protocol': params['protocol'], 'component_id': params['component_id']}

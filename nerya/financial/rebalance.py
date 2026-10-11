"""Durable LP exit -> optional ratio swap -> new-range entry.

This is a coordinator, not a privileged transport. Each child action enters
FinancialGateway with the current producer and its own finite authorization.
Only confirmed proceeds of preceding steps may fund the next step. A new
strategy tick may reconcile old actions, never impersonate their producer.
"""
from decimal import Decimal
import sqlite3
import time
import uuid

from .contracts import FinancialError, amount, digest, normalize_request
from .gateway import FinancialGateway
from .resource_binding import canonical_request
from .store import FinancialStore, encode

LP_PROTOCOLS = frozenset({'uniswap_v3', 'uniswap_v4', 'pancakeswap_v3'})
UNCERTAIN = frozenset({'submitting', 'submitted', 'confirming', 'unconfirmed', 'needs_recovery'})


class RebalanceService:
    def __init__(self, config, *, gateway=None):
        self.config = config
        self.gateway = gateway or FinancialGateway(config)
        self.store = FinancialStore(config)

    def _plan(self, context, raw):
        if not isinstance(raw, dict) or set(raw) - {'exit', 'swap', 'entry', 'expires_at', 'max_total_fee_usd'}:
            raise FinancialError('invalid_rebalance_plan', 400)
        deadline = float(amount(raw.get('expires_at')))
        if not time.time() < deadline <= time.time() + 86400:
            raise FinancialError('rebalance_finite_deadline_required', 400)
        fees = str(amount(raw.get('max_total_fee_usd'), zero=True))
        requests = [raw.get('exit'), *([raw['swap']] if raw.get('swap') is not None else []), raw.get('entry')]
        requests = [canonical_request(self.config, normalize_request(request)) for request in requests]
        first, last = requests[0], requests[-1]
        if first['kind'] != 'lp_remove' or last['kind'] != 'lp_add' or (len(requests) == 3 and requests[1]['kind'] != 'swap'):
            raise FinancialError('invalid_rebalance_steps', 400)
        if not first.get('parameters', {}).get('token_id') or last.get('parameters', {}).get('token_id') or last.get('position_id'):
            raise FinancialError('rebalance_requires_owned_exit_and_new_position', 400)
        resource = (first['wallet_id'], first['chain'])
        if any((r.get('wallet_id'), r.get('chain')) != resource for r in requests):
            raise FinancialError('rebalance_resource_mismatch', 403)
        pools = []
        for request in (first, last):
            if request.get('protocol') not in LP_PROTOCOLS:
                raise FinancialError('rebalance_protocol_unsupported', 422)
            deployment = self.config.get('financial.defi.deployments', {}).get(request['chain'], {}).get(request['protocol'], {})
            pool = deployment.get('pools', {}).get(request.get('parameters', {}).get('pool_id'), {})
            if not deployment.get('reviewed') or not pool.get('reviewed'):
                raise FinancialError('rebalance_pool_not_reviewed', 422)
            pools.append(tuple(str(pool.get(key, '')).lower() for key in ('token0', 'token1')))
        if not all(pools[0]) or pools[0] != pools[1]:
            raise FinancialError('rebalance_requires_same_underlying_pair', 422)
        if len(requests) == 3:
            swap = requests[1]
            if swap.get('recipient') or {str(swap.get(k, '')).lower() for k in ('asset', 'to_asset')} != set(pools[0]):
                raise FinancialError('rebalance_swap_must_keep_pair_and_owner', 403)
        from ..trading.components import trading_components
        bindings = []
        for request in requests:
            self.gateway._strategy_resources(context, request)
            component = trading_components(self.config).resolve(request)
            component.spec.validate_request(request)
            bindings.append(component.binding())
        return {'requests': requests, 'tokens': list(pools[0]), 'components': bindings,
                'expires_at': deadline, 'max_total_fee_usd': fees}

    def create(self, context, raw, *, client_key):
        context.require('read:funds')
        if not isinstance(client_key, str) or not 0 < len(client_key) <= 160:
            raise FinancialError('rebalance_client_key_required', 400)
        plan = self._plan(context, raw)
        plan_hash = digest(plan)
        first = plan['requests'][0]
        resource = digest([first['wallet_id'], first['chain'], first['protocol'], first['parameters']['token_id']])
        now = time.time()
        rid = 'rebalance_' + uuid.uuid4().hex
        with self.store.transaction() as con:
            previous = con.execute('SELECT * FROM lp_rebalances WHERE actor_id=? AND client_key=?', (context.actor_id, client_key)).fetchone()
            if previous:
                if previous['plan_hash'] != plan_hash:
                    raise FinancialError('rebalance_idempotency_conflict')
                rid = previous['rebalance_id']
            else:
                try:
                    con.execute('''INSERT INTO lp_rebalances(rebalance_id,actor_id,client_key,task_kind,task_id,
                        security_revision,resource_key,plan_hash,plan_json,action_ids_json,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (rid, context.actor_id, client_key, context.task_kind,
                        context.task_id, context.security_revision, resource, plan_hash, encode(plan), '[]', now, now))
                except sqlite3.IntegrityError as exc:
                    raise FinancialError('lp_position_already_rebalancing') from exc
        return self.get(context, rid)

    def _read(self, context, rid, *, advancing=False):
        context.require('read:funds')
        with self.store.transaction() as con:
            row = con.execute('SELECT * FROM lp_rebalances WHERE rebalance_id=?', (rid,)).fetchone()
        if not row:
            raise FinancialError('rebalance_not_found', 404)
        if row['actor_id'] != context.actor_id:
            raise FinancialError('rebalance_owner_mismatch', 403)
        if advancing and (row['task_kind'], row['task_id'], row['security_revision']) != (context.task_kind, context.task_id, context.security_revision):
            raise FinancialError('rebalance_strategy_revision_changed', 403)
        return FinancialStore.public(row, internal=True)

    def get(self, context, rid):
        row = self._read(context, rid)
        row['actions'] = [self.gateway.store.get_action(aid, context) for aid in row['action_ids']]
        return row

    def _save(self, row, *, state=None, reason=None, cursor=None, action_ids=None):
        with self.store.transaction() as con:
            changed = con.execute('''UPDATE lp_rebalances SET state=?,reason=?,cursor=?,action_ids_json=?,
                revision=revision+1,updated_at=? WHERE rebalance_id=? AND revision=?''',
                (state or row['state'], reason, row['cursor'] if cursor is None else cursor,
                 encode(row['action_ids'] if action_ids is None else action_ids), time.time(), row['rebalance_id'], row['revision']))
            if changed.rowcount != 1:
                raise FinancialError('revision_conflict')

    def _proceeds(self, context, row):
        balances = {token: Decimal(0) for token in row['plan']['tokens']}
        fees = Decimal(0)
        for aid in row['action_ids'][:row['cursor']]:
            action = self.gateway.store.get_action(aid, context, internal=True)
            if action['state'] != 'confirmed':
                raise FinancialError('rebalance_prior_step_not_confirmed')
            receipt, quote, request = action['receipt'], action['quote'], action['request']
            # Unknown realized costs retain their full quoted budget. They are
            # not presented as a verified zero cost or net realized profit.
            fees += amount(receipt.get('settled_fee_usd', quote['fee_usd']), zero=True)
            if request['kind'] == 'lp_remove':
                flows, pool = receipt.get('cashflows'), quote.get('pool')
                if not isinstance(flows, list) or len(flows) != 2 or not pool:
                    raise FinancialError('rebalance_exit_cashflows_unavailable')
                for index, flow in enumerate(flows):
                    token = str(pool['token' + str(index)]).lower()
                    if token not in balances or amount(flow['debit_base'], zero=True) != 0:
                        raise FinancialError('rebalance_exit_cashflows_mismatch')
                    balances[token] += amount(flow['credit_base'], zero=True) / Decimal(10)**pool['decimals'][index]
            elif request['kind'] == 'swap':
                result = receipt.get('result', {})
                for _ in range(3):
                    if 'amount_out' in result:
                        break
                    result = result.get('result', {})
                if (result.get('extra') or {}).get('confirmed') is not True or (result.get('extra') or {}).get('amount_out_source') not in {'receipt','transaction_meta','transaction_trace'}:
                    raise FinancialError('rebalance_swap_receipt_unavailable')
                spent = amount(result.get('amount_in'))
                if spent != amount(request['amount']):
                    raise FinancialError('rebalance_swap_spend_mismatch')
                balances[request['asset'].lower()] -= spent
                balances[request['to_asset'].lower()] += amount(result.get('amount_out'))
        if any(value < 0 for value in balances.values()):
            raise FinancialError('rebalance_spent_unowned_proceeds', 403)
        return balances, fees

    def advance(self, context, rid, *, expected_revision):
        if 'approve:funds' not in context.scopes:
            context.require('write:funds')
        if context.plan_only:
            raise FinancialError('plan_mode_denies_financial_execution', 403)
        from ..trading.locks import trading_lock
        with trading_lock(self.config.paths, 'rebalance:' + rid) as acquired:
            if not acquired:
                raise FinancialError('rebalance_in_progress')
            row = self._read(context, rid, advancing=True)
            if row['revision'] != expected_revision:
                raise FinancialError('revision_conflict')
            if row['state'] in {'completed', 'stopped'}:
                return self.get(context, rid)
            index = row['cursor']
            plan = row['plan']
            request = plan['requests'][index]
            aid = row['action_ids'][index] if len(row['action_ids']) > index else None
            if aid is None:
                if time.time() >= plan['expires_at']:
                    self._save(row, state='needs_recovery', reason='rebalance_plan_expired')
                    return self.get(context, rid)
                from ..trading.components import trading_components
                trading_components(self.config).resolve(request, binding=plan['components'][index])
                proceeds, spent_fees = self._proceeds(context, row)
                if index:
                    needs = ({request['asset'].lower(): amount(request['amount'])} if request['kind'] == 'swap' else
                             {token: amount(request['parameters'].get(f'amount{n}_max', '0'), zero=True) for n, token in enumerate(plan['tokens'])})
                    if any(value > proceeds.get(token, Decimal(0)) for token, value in needs.items()):
                        self._save(row, state='needs_recovery', reason='rebalance_proceeds_below_entry_bounds')
                        return self.get(context, rid)
                key = f'{rid}:{index}'
                # Recover a crash between preparing a child and linking it.
                # An unlinked child has never been executed by this coordinator.
                with self.store.transaction() as con:
                    old = con.execute('SELECT action_id FROM financial_actions WHERE actor_id=? AND action_key=?', (context.actor_id, key)).fetchone()
                action = self.gateway.store.get_action(old[0], context, internal=True) if old else self.gateway.prepare(context, request, action_key=key)
                if spent_fees + amount(action['quote']['fee_usd'], zero=True) > amount(plan['max_total_fee_usd'], zero=True):
                    self._save(row, state='needs_recovery', reason='rebalance_total_fee_limit', action_ids=[*row['action_ids'], action['action_id']])
                    return self.get(context, rid)
                aid = action['action_id']
                self._save(row, state='running', action_ids=[*row['action_ids'], aid])
                row = self._read(context, rid, advancing=True)
            action = self.gateway.store.get_action(aid, context, internal=True)
            if action['state'] in UNCERTAIN:
                self.gateway.reconcile(context, aid)
                action = self.gateway.store.get_action(aid, context, internal=True)
            if action['state'] in {'prepared', 'awaiting_approval', 'awaiting_prerequisite'}:
                old_context = action['context']
                if (old_context.get('run_id'), old_context.get('strategy_run_id'), old_context.get('command_id')) != (context.run_id, context.strategy_run_id, context.command_id):
                    self._save(row, state='needs_recovery', reason='rebalance_requires_original_producer_approval')
                    return self.get(context, rid)
                _, spent_fees = self._proceeds(context, row)
                if time.time() >= plan['expires_at'] or spent_fees + amount(action['quote']['fee_usd'], zero=True) > amount(plan['max_total_fee_usd'], zero=True):
                    self._save(row, state='needs_recovery', reason='rebalance_deadline_or_fee_limit')
                    return self.get(context, rid)
                self.gateway.execute(context, aid, quote_hash=action['quote_hash'])
                action = self.gateway.store.get_action(aid, context, internal=True)
            if action['state'] == 'confirmed':
                index += 1
                self._save(row, cursor=index, state='completed' if index == len(plan['requests']) else 'running')
            else:
                state = 'awaiting_approval' if action['state'] in {'awaiting_approval','awaiting_prerequisite'} else 'confirming' if action['state'] in {'submitted','confirming'} else 'needs_recovery'
                self._save(row, state=state, reason=action['state'])
            return self.get(context, rid)

    def stop(self, context, rid, *, expected_revision):
        if 'approve:funds' not in context.scopes:
            context.require('write:funds')
        from ..trading.locks import trading_lock
        with trading_lock(self.config.paths, 'rebalance:' + rid) as acquired:
            if not acquired:
                raise FinancialError('rebalance_in_progress')
            row = self._read(context, rid, advancing=True)
            if row['revision'] != expected_revision:
                raise FinancialError('revision_conflict')
            actions = [self.gateway.store.get_action(aid, context, internal=True) for aid in row['action_ids']]
            if any(a['state'] not in {'confirmed','rejected','failed_before_submission','discarded'} for a in actions):
                raise FinancialError('rebalance_resolve_or_discard_child_before_stop')
            self._save(row, state='stopped', reason='no_automatic_unwind')
            return self.get(context, rid)

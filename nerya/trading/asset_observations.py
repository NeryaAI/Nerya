"""UI observations from SDK adapters, deliberately separate from trading NAV."""
from decimal import Decimal, InvalidOperation, localcontext
import re
import time


class ObservationError(ValueError):
    pass


def decimal(value, field, *, nonnegative=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ObservationError(f'invalid_decimal:{field}')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ObservationError(f'invalid_decimal:{field}') from exc
    if not result.is_finite() or abs(result) > Decimal('1e60') or (nonnegative and result < 0):
        raise ObservationError(f'invalid_decimal:{field}')
    return format(result, 'f')


def text(value, field, *, required=False):
    if value is None and not required:
        return ''
    if not isinstance(value, str) or len(value) > 256 or (required and not value.strip()):
        raise ObservationError(f'invalid_identity:{field}')
    return value.strip()


def rows(value, field):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 500 or any(not isinstance(row, dict) for row in value):
        raise ObservationError(f'invalid_collection:{field}')
    return value


def normalize_observation(raw, *, connector_id, source, now=None):
    if not isinstance(raw, dict) or type(raw.get('schema_version')) is not int or raw.get('schema_version') != 1:
        raise ObservationError('unsupported_observation_schema')
    instant = time.time() if now is None else now
    stamp = decimal(raw.get('as_of'), 'as_of', nonnegative=True)
    if stamp is None or not 0 < Decimal(stamp) <= Decimal(str(instant + 30)):
        raise ObservationError('observation_timestamp_required')
    if 'balances' not in raw:
        raise ObservationError('balances_collection_required')
    balances = []
    identities = set()
    with localcontext() as context:
        context.prec = 96
        for row in rows(raw['balances'], 'balances'):
            chain = text(row.get('chain'), 'chain')
            token = text(row.get('token'), 'token', required=True)
            if token.startswith('0x'):
                if not re.fullmatch(r'0x[0-9a-fA-F]{40}', token):
                    raise ObservationError('invalid_evm_token_address')
                token = token.lower()
            identity = (chain.lower(), token)
            if identity in identities:
                raise ObservationError('duplicate_asset_identity')
            identities.add(identity)
            amount = decimal(row.get('amount'), 'amount', nonnegative=True)
            raw_amount = row.get('raw_amount')
            decimals = row.get('decimals')
            if raw_amount is not None:
                if not isinstance(raw_amount, str) or not re.fullmatch(r'\d{1,90}', raw_amount):
                    raise ObservationError('raw_amount_must_be_integer_string')
                if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 36:
                    raise ObservationError('token_decimals_required')
                scaled = Decimal(raw_amount) / (Decimal(10) ** decimals)
                if amount is not None and Decimal(amount) != scaled:
                    raise ObservationError('token_amount_unit_mismatch')
                amount = format(scaled, 'f')
            if amount is None:
                raise ObservationError('token_amount_required')
            available = decimal(row.get('available'), 'available', nonnegative=True)
            locked = decimal(row.get('locked'), 'locked', nonnegative=True)
            if any(value is not None and Decimal(value) > Decimal(amount) for value in (available, locked)):
                raise ObservationError('token_balance_partition_invalid')
            if available is not None and locked is not None and Decimal(available) + Decimal(locked) > Decimal(amount):
                raise ObservationError('token_balance_partition_invalid')
            price = decimal(row.get('price_usd'), 'price_usd', nonnegative=True)
            value = format(Decimal(amount) * Decimal(price), 'f') if price is not None else None
            balances.append({'asset_id': f'{chain.lower()}:{token}', 'chain': chain.lower(), 'token': token,
                             'symbol': text(row.get('symbol'), 'symbol') or token,
                             'amount': amount, 'raw_amount': raw_amount, 'decimals': decimals,
                             'available': available, 'locked': locked, 'price_usd': price, 'value_usd': value})
        positions = []
        position_ids = set()
        for row in rows(raw.get('positions'), 'positions'):
            identity = text(row.get('id'), 'position_id', required=True)
            if identity in position_ids:
                raise ObservationError('duplicate_position_identity')
            position_ids.add(identity)
            positions.append({'id': identity, 'kind': text(row.get('kind'), 'position_kind', required=True),
                              'chain': text(row.get('chain'), 'position_chain'),
                              'protocol': text(row.get('protocol'), 'protocol'),
                              'label': text(row.get('label'), 'position_label') or identity,
                              'value_usd': decimal(row.get('value_usd'), 'position_value', nonnegative=True),
                              'unrealized_pnl_usd': decimal(row.get('unrealized_pnl_usd'), 'position_pnl'),
                              'claimable_rewards_usd': decimal(row.get('claimable_rewards_usd'), 'rewards', nonnegative=True)})
        perf = raw.get('performance')
        if perf is not None and not isinstance(perf, dict):
            raise ObservationError('invalid_performance')
        perf = perf or {}
        performance = {key: decimal(perf.get(key), key, nonnegative=key == 'fees_usd') for key in
                       ('realized_pnl_usd', 'unrealized_pnl_usd', 'income_usd', 'fees_usd')}
        performance['period_start'] = decimal(perf.get('period_start'), 'period_start', nonnegative=True)
        performance['period_end'] = decimal(perf.get('period_end'), 'period_end', nonnegative=True)
        if any(performance[key] is not None for key in ('realized_pnl_usd', 'income_usd', 'fees_usd')):
            start, end = performance['period_start'], performance['period_end']
            if start is None or end is None or not 0 <= Decimal(start) <= Decimal(end) <= Decimal(stamp):
                raise ObservationError('performance_period_required')
        priced = [Decimal(row['value_usd']) for row in balances if row['value_usd'] is not None]
        return {'schema_version': 1, 'connector_id': connector_id, 'as_of': float(stamp),
                'collected_at': instant, 'source': source, 'balances': balances, 'positions': positions,
                'performance': performance, 'coverage': {'balances': 'observed',
                'positions': 'observed' if 'positions' in raw else 'unknown',
                'performance': 'observed' if raw.get('performance') else 'unknown'},
                'summary': {'priced_balance_value_usd': format(sum(priced, Decimal(0)), 'f') if priced or not balances else None,
                            'unpriced_balances': sum(row['value_usd'] is None for row in balances),
                            'balance_count': len(balances), 'position_count': len(positions)},
                'usage': 'display_only'}

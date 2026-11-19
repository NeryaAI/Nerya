"""Normalize Gamma identities before an outcome reaches data or signing APIs."""
import json

from ..core.errors import TradingError


def _array(value, field):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise TradingError(f'invalid prediction {field}') from exc
    if not isinstance(value, list):
        raise TradingError(f'prediction {field} unavailable')
    return value


def market_outcomes(row, venue):
    if not isinstance(row, dict):
        raise TradingError('invalid prediction market metadata')
    version = row.get('version')
    if version not in ('v1', 'v2'):
        raise TradingError('prediction protocol version missing or unsupported')
    ids = _array(row.get('positionIds' if version == 'v2' else 'clobTokenIds'), 'outcome ids')
    outcomes = _array(row.get('outcomes'), 'outcomes')
    if not ids or len(ids) != len(outcomes) or len(set(str(x).casefold() for x in outcomes)) != len(outcomes):
        raise TradingError('prediction outcome identities are incomplete or ambiguous')
    if any(not isinstance(x, str) or not x.isdecimal() or not x.isascii() for x in ids) or len(set(ids)) != len(ids):
        raise TradingError('invalid prediction outcome asset id')
    if any(not isinstance(x, str) or not x.strip() for x in outcomes):
        raise TradingError('invalid prediction outcome label')
    resolution = row.get('resolutionStatus' if version == 'v2' else 'umaResolutionStatus')
    status = ('resolved' if resolution == 'resolved' else 'closed' if row.get('closed') is True
              else 'open' if row.get('acceptingOrders') is True else 'unknown')
    return [{
        'market': f'{venue}:{asset_id}', 'asset_id': asset_id, 'token_id': asset_id,
        'protocol_version': version, 'instrument_type': 'prediction_outcome',
        'slug': row.get('slug'), 'question': row.get('question'), 'outcome': outcome,
        'condition_id': row.get('conditionId'), 'end_date': row.get('endDate'),
        'outcomes': outcomes, 'asset_ids': ids,
        'closed': row.get('closed') is True, 'accepting_orders': row.get('acceptingOrders') is True,
        'settlement_status': status, 'quantity_unit': 'shares',
    } for asset_id, outcome in zip(ids, outcomes)]


def instrument_metadata(meta):
    return {
        'type': meta['instrument_type'], 'label': meta.get('question') or meta['market'],
        'outcome': meta['outcome'], 'quantity_unit': meta['quantity_unit'],
        'price_unit': 'PUSD',
        'asset_id': meta['asset_id'], 'protocol_version': meta['protocol_version'],
        'settlement_status': meta['settlement_status'], 'data_status': 'available',
    }

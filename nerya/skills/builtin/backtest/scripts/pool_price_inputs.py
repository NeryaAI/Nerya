"""Prepare an admitted pool-price simulation without relabelling its source."""
from __future__ import annotations

import json

from .....core.errors import TradingError
from .....data.history_store import normalize_row, timeframe_seconds
from .replay_inputs import prepare_inputs

SCOPE = 'historical_pool_price_simulation'


def prepare_pool_prices(config, package, cfg, out_dir, *, cancel_token=None):
    declared = package.manifest.extras.get('params', {}).get('price_simulation')
    if not declared:
        return None
    if not isinstance(declared, dict) or set(declared) != {'dataset_id'}:
        raise TradingError('params.price_simulation requires only dataset_id')
    if len(cfg.markets) != 1 or cfg.timeframes != [cfg.tf] or cfg.agent_replay:
        raise TradingError('Pool-price simulation requires one market/timeframe and a script entrypoint')
    from .....strategies.request_scope import ensure_contract, validate_prepared
    contract = ensure_contract(config, token=cancel_token)
    if contract is None:
        raise TradingError('Pool-price simulation requires an admitted request scope')
    prepared = prepare_inputs(package.root, cancel_token=cancel_token, config=config)
    if len(prepared['receipts']) != 1 or prepared['receipts'][0]['id'] != declared['dataset_id']:
        raise TradingError('Pool-price simulation requires its single declared dataset')
    manifest = {**package.manifest.asdict(), **package.manifest.extras}
    validate_prepared(contract, manifest, prepared['manifest'], prepared, config=config)
    receipt = prepared['receipts'][0]
    payload = json.loads(prepared['files'][receipt['id'] + '.json'])
    envelope = payload['_envelope']
    from datetime import datetime, timezone
    window = envelope.get('requested_window') or {}
    for key, stamp in (('start', cfg._window_start), ('end', cfg._window_end)):
        actual = datetime.fromisoformat(str(window.get(key, '')).replace('Z', '+00:00'))
        if actual.tzinfo is None or actual.astimezone(timezone.utc).timestamp() != stamp:
            raise TradingError('Pool-price input must preserve the admitted UTC window')
    if envelope.get('timeframe') != cfg.tf:
        raise TradingError('Pool candle timeframe differs from the saved strategy')
    step = timeframe_seconds(cfg.tf)
    first = ((cfg._window_start + step - 1) // step - cfg.warmup_bars) * step
    rows = [normalize_row(row) for row in payload['rows'] if first <= row['ts'] < cfg._window_end]
    root = out_dir / 'prepared_inputs'
    root.mkdir()
    for name, blob in prepared['files'].items():
        (root / name).write_bytes(blob)
    return {'series': {cfg.markets[0]: {cfg.tf: rows}}, 'prepared': prepared,
            'proof': {'scope': SCOPE, 'execution_target': cfg.markets[0],
                'pool': envelope['pool'], 'quote_units': envelope['pool']['quote']['symbol'],
                'price_units': envelope['quote_units'], 'volume_units': envelope.get('volume_units'),
                'dataset_id': receipt['id'], 'prepared_input_sha256': receipt['sha256'],
                'sources': receipt['sources'], 'requested_window': receipt['requested_window'],
                'limitations': ['Pool prices are not historical aggregator routes or quotes.',
                    'Orders, fills, fees and slippage use the saved simulation assumptions.',
                    'Legacy *_usd engine fields are quote-token accounting units; no USD conversion is attested.']}}


def bind_pool_price_evidence(metrics, provenance, inputs):
    proof = inputs['proof']
    provenance['price_simulation'] = proof
    provenance['prepared_datasets'] = inputs['prepared']['receipts']
    provenance['scope'] = 'Saved strategy replay using attributed pool prices and simulated fills, not historical aggregator execution.'
    metrics['performance_evidence'] = False
    metrics['accounting_currency'] = proof['quote_units']
    metrics['replay'].update(scope=SCOPE, strategy_execution_verified=True,
        financial_execution_allowed=False, funds_sent=False)
    metrics.setdefault('flags', []).append('pool_price_simulation_not_historical_route_execution')

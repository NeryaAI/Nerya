"""Offline regression for sampled LP snapshot preparation, not live evidence."""
from copy import deepcopy
import json

import pytest

from nerya.skills.builtin.backtest.scripts.replay_inputs import prepare_inputs
from nerya.strategies import request_scope as scope
from test_uniswap_prepared_scope import fixture


def test_sampled_snapshot_retains_verified_pool_in_prepared_scope(tmp_path, monkeypatch):
    envelope, rows, args, manifest = fixture()
    args.update(start='2026-10-07', end='2026-11-06', step_blocks=4000,
                max_samples=64, tick_lower=-120, tick_upper=120)
    envelope.update(mock=False, requested_window=dict(start=args['start'], end=args['end']),
                    coverage={'requested_window_complete': False})
    ref = 'replay://' + 'b' * 64
    declaration = {'version': 1, 'datasets': [dict(
        id='pool', provider='uniswap', action='pool_history', args=args, producer_ref=ref)]}
    (tmp_path / 'replay_inputs.json').write_text(json.dumps(declaration))
    manifest.update(chain='ethereum', protocol='uniswap_v3',
                    params={'lp_source': {'pool_id': args['pool_address']},
                            'lp_replay': {'mode': 'sampled'}})
    contract = {'constraints': {
        'resources': [{**scope._market(manifest['markets'][0]), 'chain': 'ethereum',
                       'pool': args['pool_address']}],
        'start_utc': scope._timestamp(args['start']), 'end_utc': scope._timestamp(args['end'])}}
    context = object()

    def read(config, producer_ref, provider, action, original_args):
        assert (config, producer_ref, provider, action, original_args) == (
            context, ref, 'uniswap', 'pool_history', args)
        return {'rows': rows, '_envelope': envelope}

    monkeypatch.setattr('nerya.data_api.replay_snapshot.read', read)
    prepared = prepare_inputs(tmp_path, config=context)
    assert scope.validate_prepared(contract, manifest, declaration, prepared) == {
        'prepared_inputs/pool.json': prepared['receipts'][0]['sha256']}
    assert prepared['receipts'][0]['requested_window_complete'] is False

    # A different admitted/candidate pool still cannot reuse these same bytes.
    other = '0x' + '44' * 20
    changed_contract, changed_manifest = deepcopy(contract), deepcopy(manifest)
    changed_contract['constraints']['resources'][0]['pool'] = other
    changed_manifest['params']['lp_source']['pool_id'] = other
    with pytest.raises(scope.ScopeError) as rejected:
        scope.validate_prepared(changed_contract, changed_manifest, declaration, prepared)
    assert rejected.value.differences == [{
        'field': 'prepared.pool', 'expected': other, 'actual': args['pool_address']}]

"""Optional null pool evidence is equivalent; established pools cannot disappear."""
from copy import deepcopy

import pytest

from nerya.strategies import request_scope as scope
from test_request_scope import spans_output


@pytest.mark.smoke
def test_optional_pool_omission_matches_null_but_preserves_existing_pool(monkeypatch):
    documents = [
        {'command_id': 'original', 'text': 'ETH/USDC pool-address', 'created_at': 1792900000, 'request_hash': 'original'},
        {'command_id': 'followup', 'text': 'Continue the same candidate.', 'created_at': 1792900001, 'request_hash': 'followup'},
    ]
    snapshot = {'binding': {'command_id': 'followup', 'original_command_id': 'original',
        'session_id': 'task', 'actor_id': 'owner', 'documents': documents}}
    snapshot['input_hash'] = scope._hash(snapshot['binding'])
    original = spans_output(documents[0], base='ETH', quote='USDC')
    omitted = deepcopy(original)
    omitted['fields'].pop('pool')
    explicit = scope._chain([original, original], snapshot)
    actual = scope._chain([original, omitted], snapshot)
    assert actual['constraints'] == explicit['constraints']
    assert actual['normalized_evidence']['fields'].get('pool') is None

    # Exercise the same comparison with an already verified non-null pool.
    # Keep pool parsing outside this regression's chain-comparison seam.
    canonical = scope._canonical
    pool = spans_output(documents[0], pool='pool-address')['fields']['pool']
    def with_verified_pool(*args, **kwargs):
        result = canonical(*args, **kwargs)
        if kwargs.get('previous') is None:
            result['normalized_evidence']['fields']['pool'] = pool
            result['constraints']['pool'] = pool['text']
        return result
    monkeypatch.setattr(scope, '_canonical', with_verified_pool)
    with pytest.raises(scope.ScopeError):
        scope._chain([original, omitted], snapshot)
    with pytest.raises(scope.ScopeError):
        scope._chain([original, original], snapshot)

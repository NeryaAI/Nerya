"""Offline producer-to-model compaction contract for current pool discovery."""
import json

import pytest

from nerya.data_api.registry import compact_data_result
from nerya.data_api.types import DataApiContext
from nerya.data_api.uniswap_discovery import UniswapPoolDiscoveryProvider
from nerya.llm.tool_compaction import compact_tool_result
from test_uniswap_pool_discovery import (
    CatalogFake, POOL100, POOL10000, POOL3000, POOL500, RpcFake,
)

pytestmark = pytest.mark.smoke


def _four_candidate_output():
    raw = UniswapPoolDiscoveryProvider(
        RpcFake(pools={100: POOL100, 500: POOL500, 3000: POOL3000, 10000: POOL10000}),
        catalog_http=CatalogFake(),
    ).pool_discover(DataApiContext(), {'base': 'USDC', 'quote': 'WETH'})
    return compact_data_result('uniswap', 'pool_discover', raw, limit=50)


def test_pool_discovery_keeps_four_identities_and_bounded_proof():
    output = _four_candidate_output()
    envelope = output['_envelope']
    envelope.update({
        'raw_transcript': [{'authorization': 'PRIVATE', 'body': 'x' * 100_000}] * 100,
        'provider_body': 'PRIVATE_PROVIDER_BODY',
        'padding': 'x' * 100_000,
    })
    result = compact_tool_result('data_api', output, size_threshold=0)
    kept = result.kept

    assert result.rule_id == 'data_api.table'
    assert len(kept['rows_sample']) == kept['row_count'] == 4
    assert {row['pool_address'] for row in kept['rows_sample']} == {
        POOL100.lower(), POOL500.lower(), POOL3000.lower(), POOL10000.lower()}
    assert all(row['block_number'] == 100 and row['block_hash'] for row in kept['rows_sample'])
    compacted_envelope = kept['_envelope']
    assert compacted_envelope['pool_identity_scope'] == 'current_only'
    assert compacted_envelope['candidate_count'] == 4
    assert compacted_envelope['chain_id'] == 1 and compacted_envelope['chain'] == 'Ethereum'
    assert compacted_envelope['request_args'] == {
        'base': 'USDC', 'quote': 'WETH', 'rpc_provider': 'publicnode'}
    assert compacted_envelope['block'] == {
        'number': 100, 'hash': '0x' + '65'.zfill(64), 'timestamp': 1700000000}
    assert compacted_envelope['fee_tier_policy'] == 'standard_documented_non_exhaustive'
    assert compacted_envelope['fee_tiers_scanned'] == [100, 500, 3000, 10000]
    assert compacted_envelope['historical_data_verified'] is False
    assert compacted_envelope['financial_execution_allowed'] is False
    assert compacted_envelope['rows_sha256'] == envelope['rows_sha256']
    assert compacted_envelope['sources'] == [
        {'url': envelope['sources'][0]['url'], 'sha256': envelope['sources'][0]['sha256'],
         'request_count': envelope['sources'][0]['request_count']},
        {'url': envelope['sources'][1]['url'], 'sha256': envelope['sources'][1]['sha256'],
         'request_count': envelope['sources'][1]['request_count']},
    ]
    text = json.dumps(kept, ensure_ascii=False)
    assert 'PRIVATE' not in text and 'raw_transcript' not in text and 'provider_body' not in text
    assert 'next_required_action' not in kept


def test_unknown_metadata_is_ignored_when_complete_identity_is_present():
    output = _four_candidate_output()
    output['_envelope']['padding'] = 'x' * 100_000
    result = compact_tool_result('data_api', output, size_threshold=0)
    kept = result.kept

    assert kept['sample_truncated'] is False
    assert kept['metadata_truncated'] is False
    assert len(kept['rows_sample']) == 4
    assert 'next_required_action' not in kept
    assert kept['_envelope']['historical_data_verified'] is False
    assert kept['_envelope']['financial_execution_allowed'] is False
    assert 'padding' not in json.dumps(kept)


def test_pool_discovery_false_safety_flags_and_hashes_survive_adversarial_metadata():
    output = _four_candidate_output()
    output['_envelope'].update({
        'historical_data_verified': False,
        'financial_execution_allowed': False,
        'raw_transcript': [{'body': 'PRIVATE_BODY', 'authorization': 'PRIVATE_TOKEN'}],
        'provider_body': 'PRIVATE_PROVIDER_BODY',
        'source_secret': 'PRIVATE_SECRET',
    })
    result = compact_tool_result('data_api', output, size_threshold=0)
    kept = result.kept

    assert kept['_envelope']['historical_data_verified'] is False
    assert kept['_envelope']['financial_execution_allowed'] is False
    assert kept['_envelope']['rows_sha256'] == output['_envelope']['rows_sha256']
    assert all(source['sha256'] for source in kept['_envelope']['sources'])
    text = json.dumps(kept, ensure_ascii=False)
    assert all(secret not in text for secret in ('PRIVATE_BODY', 'PRIVATE_TOKEN',
                                                  'PRIVATE_PROVIDER_BODY', 'PRIVATE_SECRET'))


def test_malformed_oversized_proof_cannot_bypass_bounds_or_leak_source_credentials():
    output = _four_candidate_output()
    output['_envelope'].update({
        'request_args': {'base': 'x' * 100_000, 'quote': ['unexpected'], 'rpc_provider': True},
        'block': {'number': '100', 'hash': 'x' * 100_000, 'timestamp': '1700000000'},
        'sources': [{'url': 'https://user:secret@example.invalid/rpc?token=PRIVATE_TOKEN',
                     'sha256': 'h' * 100_000, 'request_count': '40'}] * 100,
    })
    kept = compact_tool_result('data_api', output, size_threshold=0).kept
    envelope = kept['_envelope']
    assert len(json.dumps(envelope, ensure_ascii=False).encode()) <= 8192
    assert envelope['pool_identity_scope'] == 'current_only'
    assert envelope['historical_data_verified'] is False
    assert envelope['financial_execution_allowed'] is False
    assert kept['metadata_truncated'] is True
    text = json.dumps(kept, ensure_ascii=False)
    assert 'PRIVATE_TOKEN' not in text and 'user:secret' not in text

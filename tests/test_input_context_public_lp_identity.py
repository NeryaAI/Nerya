"""Publishing LP data preserves public addresses without publishing secrets."""
import pytest

from nerya.strategies.input_context import StrategyInputContext, safe_data


@pytest.mark.smoke
def test_public_lp_identity_survives_publish_while_keys_are_redacted():
    identities = {'pool_id': '0x' + '1' * 40,
                  'token0': '0x' + '2' * 40, 'token1': '0x' + '3' * 40}
    value = {**identities, 'pool': {**identities,
        'underlying_tokens': [identities['token0'], identities['token1']]},
        'api_key': 'test-api-secret', 'private_key': '0x' + '4' * 64}
    inputs = StrategyInputContext(sources=[], market=None, news=None, markets=(), run_id='offline')
    inputs.publish('lp', value)
    for cleaned in (safe_data(value), inputs.read('lp')):
        assert {key: cleaned[key] for key in identities} == identities
        assert cleaned['pool'] == value['pool']
        assert cleaned['api_key']['__redacted__'] is True
        assert cleaned['private_key']['__redacted__'] is True
    assert value['api_key'] == 'test-api-secret'

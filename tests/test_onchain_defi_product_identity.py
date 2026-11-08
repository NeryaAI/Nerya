"""Read-only DeFi discovery must not confuse product IDs with pool addresses."""
from copy import deepcopy

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.types import ToolCall


@pytest.mark.parametrize(
    'action',
    ['defi_detail', 'defi_rate_chart', 'defi_tvl_chart', 'defi_depth_price_chart'],
)
def test_defi_schema_explains_product_identity_before_calling(tmp_path, action):
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    result = data_api_handler(
        ToolCall(name='data_api', arguments={
            'op': 'schema', 'provider': 'onchainos', 'action': action,
        }),
        config_like=config,
    )
    assert not result.is_error
    schema = result.content[0].data['input_schema']
    description = schema['properties']['investment_id'].get('description', '')
    assert 'investmentId' in description
    assert 'defi_search' in description
    assert 'pool address' in description
    assert 'chain' in description
    assert not config.paths.db.exists()

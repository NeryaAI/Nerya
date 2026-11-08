"""LP discovery must name an actionable read, not a circular inventory."""
import pytest

from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall
from test_dex_exact_scope_routing import producer as producer, read_skill

pytestmark = pytest.mark.smoke


def test_lp_inventory_points_to_pool_schema_without_aliasing_requested_assets(producer):
    config, descriptor = producer
    result = tool_json_data(descriptor.handler(ToolCall(name='financial_components', arguments={
        'mode': 'backtest', 'protocol': 'uniswap_v3', 'market': 'ETH/USDC', 'kind': 'lp_add'})))
    discovery = result['pool_discovery']
    assert discovery['next_required_action'] == {'tool': 'data_api', 'arguments': {
        'op': 'schema', 'provider': 'uniswap', 'action': 'pool_discover'}}
    assert discovery['historical_data_verified'] is False
    assert result['financial_execution_allowed'] is False
    assert result['requested']['market'] == 'ETH/USDC'
    assert 'not an ETH/WETH alias' in discovery['message']
    assert not config.paths.db.exists()


def test_loaded_lp_workflow_uses_pool_discovery_before_history():
    reference = read_skill(skill='strategy_author', file='references/mainstream-strategies.md')
    section = ' '.join(reference.split('## LP work', 1)[1].split('## ', 1)[0].split())
    assert 'pool_discover' in section
    assert 'current pool identity' in section
    assert 'multiple fee tiers' in section
    assert 'explicit task amendment' in section
    assert 'pool_history' in section
    assert 'does not select dynamic rebalance' in section
    assert 'fixed-range model may be presented as an explicit research assumption' in section
    assert 'do not add lifecycle wallet/NFT/USD-economics requirements' in section


def test_loaded_lp_history_recovery_preserves_evidence_type_and_nonretryable_receipt():
    reference = read_skill(skill='strategy_author', file='references/mainstream-strategies.md')
    section = ' '.join(reference.split('## LP work', 1)[1].split('## ', 1)[0].split())
    for boundary in ('historical_rpc_authorization_required',
                     'DefiLlama APY observations are research attachments',
                     'not substitutes for pool tick, liquidity and fee-growth history',
                     'Preserve the non-retryable receipt',
                     'continue only after the access precondition changes',
                     'Public summaries name the verified current identity and remaining history gap'):
        assert boundary in section

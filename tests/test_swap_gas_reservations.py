from decimal import Decimal
from types import SimpleNamespace

import pytest

from nerya.financial.trading_adapter import TradingFunds

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize('native_input', [False, True])
def test_swap_reserves_native_fee_and_rent_without_double_counting_input(monkeypatch, native_input):
    asset = 'SOL' if native_input else 'USDC'
    request = {'kind': 'swap', 'wallet_id': 'wallet', 'chain': 'solana',
               'asset': asset, 'to_asset': 'TOKEN', 'amount': '1', 'slippage_bps': 50}
    quote = {'gas_cost_usd': .4, 'extra': {'gas_budget': {'amount': '.012', 'native_input': native_input}}}
    monkeypatch.setattr('nerya.wallet.swap_approval.prepare_swap', lambda *args: ({}, quote))
    provider = SimpleNamespace(get_balance=lambda **kw: SimpleNamespace(amount=5))
    monkeypatch.setattr('nerya.financial.adapters.owned_wallet',
        lambda *args: ('wallet', 'self_custody', {'address': 'owner', 'token_symbols': {asset: asset}}, provider))
    monkeypatch.setattr('nerya.financial.adapters.usd_price', lambda symbol: Decimal(200 if symbol == 'SOL' else 1))
    result = TradingFunds(None, request).quote(request)
    assert result['fee_usd'] == '0.4'
    if native_input:
        assert result['asset_amounts'] == {'SOL': '1.012'}
    else:
        assert result['asset_amounts'] == {'USDC': '1', 'NATIVE': '0.012'}
    assert all(value == '5' for value in result['available_asset_amounts'].values())

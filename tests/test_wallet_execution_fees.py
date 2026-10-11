from contextlib import closing

import pytest

from nerya.wallet.fees import evm_network_fee, price_network_fee

pytestmark = pytest.mark.smoke


def test_evm_network_fee_includes_actual_l1_fee():
    result = evm_network_fee({'gasUsed': '0x5208', 'effectiveGasPrice': hex(10**9), 'l1Fee': hex(10**12)}, 'tx', 'ETH')
    assert result['amount_base'] == str(21000 * 10**9 + 10**12)
    priced = price_network_fee(result, price_provider=lambda asset: 2000)
    assert priced['fee_usd'] == pytest.approx(.044) and priced['status'] == 'verified'


@pytest.mark.parametrize('evidence', [None, {}, {'status': 'observed'},
    {'status': 'observed', 'amount_base': '-1', 'asset': 'ETH', 'decimals': 18}])
def test_missing_cost_is_never_verified_zero(evidence):
    assert price_network_fee(evidence, price_provider=lambda _: 2000)['status'] == 'unpriced_gas'


def test_zero_network_cost_needs_observed_evidence_and_no_price_lookup():
    evidence = {'status': 'observed', 'amount_base': '0', 'asset': 'SOL', 'decimals': 9}
    result = price_network_fee(evidence, price_provider=lambda _: 1/0)
    assert result['fee_usd'] == 0 and result['status'] == 'verified'


def test_strategy_books_gas_once_without_double_subtracting_net_swap_fees(tmp_path, monkeypatch):
    from test_wallet_strategy_execution import setup
    from nerya.wallet.strategy_execution import record_fill
    from nerya.trading.position_book import PositionBook
    from nerya.trading.order_tracker import OrderTracker
    cfg, market, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr('nerya.financial.adapters.usd_price', lambda _: 200)
    payload = {'strategy_id': 's1', 'account_id': 'meme', 'market': market, 'side': 'buy', 'amount_in': 100}
    result = {'amount_out': 49, 'tx_hash': 'tx-fees', 'extra': {'amount_out_source': 'transaction_meta',
        'network_fee': {'status': 'observed', 'asset': 'SOL', 'amount_base': '1000000', 'decimals': 9}}}
    record_fill(cfg, payload, result, 'gas-test')
    record_fill(cfg, payload, result, 'gas-test')
    with closing(OrderTracker(cfg.paths)) as tracker:
        order = tracker.get_by_client_order_id('wallet_gas-test')
        fills = tracker.fills_for_order(order.order_id)
    assert len(fills) == 1 and fills[0].fee_usd == pytest.approx(.2)
    assert fills[0].price == pytest.approx(100/49)
    with closing(PositionBook(cfg.paths)) as book:
        position = book.get_open(account_id='meme', strategy_id='s1', market=market)
    assert position.fees_usd == pytest.approx(.2)

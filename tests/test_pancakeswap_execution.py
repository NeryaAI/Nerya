import pytest
from eth_abi import encode
from eth_utils import keccak

from nerya.financial.defi.pancakeswap import PancakeSwapFunds
from nerya.financial.contracts import FinancialError
from nerya.financial.gateway import FinancialGateway
from nerya.trading.components import trading_components
from test_uniswap_execution import setup as uni_setup, request, receipt, LpRpc, NFT_ID
from test_aave_execution import SENDER, DATA, TX_HASH

pytestmark = pytest.mark.smoke


class PancakeRpc(LpRpc):
    def _rpc(self, method, args):
        if method == 'eth_call' and args[0]['data'][:10] == '0x' + keccak(text='slot0()')[:4].hex():
            return '0x' + encode(['uint160','int24','uint16','uint16','uint16','uint32','bool'],
                                [2**96, 0, 0, 0, 0, 65535, True]).hex()
        return super()._rpc(method, args)


def setup(tmp_path, monkeypatch):
    cfg, _, ctx, _ = uni_setup(tmp_path, monkeypatch)
    deployments = cfg.data['financial']['defi']['deployments']['base']
    deployments['pancakeswap_v3'] = deployments.pop('uniswap_v3')
    rpc = PancakeRpc()
    from types import SimpleNamespace
    raw = cfg.data['wallet']['providers']['wallet']['config']
    monkeypatch.setattr('nerya.financial.defi.pancakeswap.PancakeSwapFunds',
        lambda c, r: PancakeSwapFunds(c, r, connector=rpc,
            provider=SimpleNamespace(id='self_custody', _resolve_signer_key=lambda: '11'*32), wallet_config=raw))
    return cfg, rpc, ctx


def test_pancake_slot0_uint32_and_protocol_specific_routing(tmp_path, monkeypatch):
    cfg, rpc, ctx = setup(tmp_path, monkeypatch)
    payload = request('pancakeswap_v3')
    assert trading_components(cfg).resolve(payload).id == 'builtin:pancakeswap'
    action = FinancialGateway(cfg).prepare(ctx, payload, action_key='pancake')
    assert action['quote']['pool']['tick'] == 0 and action['quote']['risk_usd'] == '20'
    assert action['quote']['component_binding']['id'] == 'builtin:pancakeswap'


def test_pancake_confirmed_mint_records_owned_nft_once(tmp_path, monkeypatch):
    cfg, rpc, ctx = setup(tmp_path, monkeypatch)
    gateway = FinancialGateway(cfg)
    action = gateway.prepare(ctx, request('pancakeswap_v3'), action_key='pancake')
    quote = gateway.store.get_action(action['action_id'], ctx, internal=True)['quote']
    rpc.minted = True
    rpc.liquidity = 2000000000
    rpc.receipt = receipt(quote)
    rpc.transaction = {'hash': TX_HASH, 'from': SENDER, 'to': DATA, 'input': quote['transaction']['data'], 'value': '0x0'}
    gateway.store.mark(action['action_id'], state='submitted', submission={'transaction_hash': TX_HASH, 'action_id': action['action_id']})
    result = gateway.reconcile(ctx, action['action_id'])
    assert result['state'] == 'confirmed' and result['receipt']['position_id'] == str(NFT_ID)
    assert 'pancakeswap_v3' in result['receipt']['position_key']
    assert gateway.reconcile(ctx, action['action_id'])['state'] == 'confirmed'


def test_pancake_adapter_does_not_claim_infinity_or_uniswap_support():
    for protocol in ('pancakeswap_infinity', 'uniswap_v3', 'byreal'):
        with pytest.raises(FinancialError, match='unsupported_lp_operation'):
            PancakeSwapFunds(None, {'kind': 'lp_add', 'protocol': protocol})

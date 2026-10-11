"""PancakeSwap v3 owned NFT liquidity, using the shared verified lifecycle.

Pancake's slot0 protocol fee is uint32, not Uniswap's uint8. Position-manager
mint/increase/decrease/collect tuples and liquidity events are compatible.
MasterChef staking, Infinity hooks and rewards are separate capabilities.
https://developer.pancakeswap.finance/contracts/v3/pancakev3pool
https://developer.pancakeswap.finance/contracts/v3/nonfungiblepositionmanager
"""
from .uniswap import UniswapFunds


class PancakeSwapFunds(UniswapFunds):
    supported_protocols = frozenset({'pancakeswap_v3'})
    v3_protocol_fee_type = 'uint32'

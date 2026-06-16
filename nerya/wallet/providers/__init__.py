"""Concrete wallet providers. All imports inside these modules are lazy
so the parent package keeps loading even when none of the optional SDKs
are installed."""

from .self_custody import SelfCustodyWallet
from .okx_os import OkxOsWallet
from .bitget import BitgetWalletSkill
from .binance_agentic import BinanceAgenticWallet
from .coinbase import CoinbaseWallet
from .byreal import ByrealWallet

__all__ = [
    "SelfCustodyWallet",
    "OkxOsWallet",
    "BitgetWalletSkill",
    "BinanceAgenticWallet",
    "CoinbaseWallet",
    "ByrealWallet",
]

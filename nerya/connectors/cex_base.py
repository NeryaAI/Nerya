"""Shared CEX connector types.

The heavy-weight :class:`NativeCEXConnector` base plus the hand-rolled
Binance/Bybit/OKX/Hyperliquid subclasses lived here until
:class:`~nerya.connectors.ccxt_adapter.CcxtConnector` replaced them.
Only the credential dataclass remains — it's the vault-facing contract
that the registry + wallet providers (e.g. ``okx_os``) still read.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CEXCredentials:
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""  # OKX / KuCoin / Bitget etc.


__all__ = ["CEXCredentials"]

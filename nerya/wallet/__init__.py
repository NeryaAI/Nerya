"""Nerya on-chain wallet layer.

Provides a pluggable :class:`WalletProvider` abstraction so the agent can
execute chain-side swaps / transfers through the wallet backend an
operator chose without Nerya auto-installing any of their dependencies.

Supported backends (all optional, all lazy-loaded):

* ``self_custody`` — goat-sdk / eth_account / solders based local signing.
* ``okx_os``       — OKX On-Chain OS (https://web3.okx.com) REST API.
* ``bitget``       — bitget-wallet-skill (Node/TS skill invoked via subprocess).
* ``binance_agentic`` — binance-web3/binance-agentic-wallet skill (Node/TS).
* ``xagt_agent_plugin`` — @xagt/agent-plugin login + OKX OnchainOS skill setup.

Nothing in this package performs ``pip install`` or ``npm install`` on its
own — each provider raises a :class:`WalletDependencyError` containing the
exact install command(s) the operator should run.
"""

from .errors import (
    WalletError,
    WalletDependencyError,
    WalletPolicyDenied,
    WalletProviderNotFound,
)
from .protocol import (
    CAPABILITY_STATUS,
    EXECUTION_PROFILE,
    WalletBalance,
    WalletCapabilities,
    WalletCapability,
    WalletProvider,
    WalletQuote,
    WalletReadiness,
    WalletSwapResult,
)
from .registry import (
    PROVIDERS,
    WalletRegistry,
    build_provider,
    list_configured_providers,
    list_providers,
    list_wallet_market_data_sources,
    market_data_sources_for_provider,
    readiness_report,
    resolve_active,
    resolve_for_account,
    resolve_for_strategy,
)

__all__ = [
    "CAPABILITY_STATUS",
    "EXECUTION_PROFILE",
    "WalletError",
    "WalletDependencyError",
    "WalletPolicyDenied",
    "WalletProviderNotFound",
    "WalletProvider",
    "WalletReadiness",
    "WalletCapability",
    "WalletCapabilities",
    "WalletQuote",
    "WalletSwapResult",
    "WalletBalance",
    "PROVIDERS",
    "WalletRegistry",
    "build_provider",
    "list_configured_providers",
    "list_wallet_market_data_sources",
    "list_providers",
    "market_data_sources_for_provider",
    "readiness_report",
    "resolve_active",
    "resolve_for_account",
    "resolve_for_strategy",
]

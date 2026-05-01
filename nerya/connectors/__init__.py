"""Exchange and chain connectors.

CEX venues are fulfilled by :class:`CcxtConnector` (unified ccxt
adapter) — Nerya no longer ships hand-rolled Binance/Bybit/OKX/
Hyperliquid clients. DEX chains (``EVMNative``, ``BSCNative``,
``SolanaNative``) remain in-tree because they need bespoke
router/swap logic that ccxt doesn't cover. Paper mode uses
``MockExchange`` / ``MockChain`` for deterministic tests.

See :mod:`nerya.connectors.provider_spec` for how venues map to
concrete connector classes, and :func:`build_connector` for the
single entry point used by the workspace-aware registry.
"""

from .base import Balance, CEXConnectorBase, Connector, DEXConnectorBase, OrderAck, Ticker
from .bsc_native import BSCNative
from .ccxt_adapter import CcxtConnector
from .cex_base import CEXCredentials
from .evm_native import EVMNative
from .mock_chain import MockChain
from .mock_exchange import MockExchange
from .polymarket import PolymarketConnector
from .yahoo import YahooFinanceConnector
from .provider_spec import ExchangeProviderSpec, get_registry
from .registry import ConnectorRegistry, build_connector, list_providers
from .solana_native import SolanaNative

__all__ = [
    "Balance", "Connector", "CEXConnectorBase", "DEXConnectorBase",
    "OrderAck", "Ticker",
    "BSCNative", "EVMNative", "SolanaNative",
    "CcxtConnector", "PolymarketConnector", "YahooFinanceConnector",
    "CEXCredentials",
    "MockExchange", "MockChain",
    "ConnectorRegistry", "build_connector", "list_providers",
    "ExchangeProviderSpec", "get_registry",
]


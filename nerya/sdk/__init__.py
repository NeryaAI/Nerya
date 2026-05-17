"""Internal Nerya SDK — used by Skills and in-process callers.

The user-facing Python SDK is `nerya_sdk` in the `sdk/python/` workspace.
That client talks to this internal SDK either in-process or over file/HTTP.

Re-export the typed trade-plan schemas so SDK
callers (agent runtimes, scripts, tools) can build :class:`TradePlan` /
:class:`SizingPolicy` / :class:`ProtectionRule` without reaching into
``nerya.trading.order_intents`` directly.
"""

from .internal_client import InternalClient, boot
from .trading_api import TradingAPI
from ..trading.order_intents import (
    PartialExitSpec,
    ProtectionRule,
    SizingPolicy,
    StopLossSpec,
    TakeProfitSpec,
    TradeEntry,
    TradePlan,
    TrailingStopSpec,
)

__all__ = [
    "InternalClient", "boot",
    "TradingAPI",
    "TradePlan", "TradeEntry", "SizingPolicy",
    "ProtectionRule", "StopLossSpec", "TakeProfitSpec",
    "TrailingStopSpec", "PartialExitSpec",
]

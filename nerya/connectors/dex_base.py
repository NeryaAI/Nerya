"""Shared DEX/chain connector base. Real signing lives under signer policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import DEXConnectorBase
from .http import HttpTransport, UrllibHttp


@dataclass
class DEXCredentials:
    rpc_url: str = ""
    # signer/custody references live in workspace/accounts/signer_policy.yml;
    # the connector only ever receives a resolved signer handle from the
    # ExecutionEngine, never raw keys.
    signer_ref: str = ""


@dataclass
class NativeDEXConnector(DEXConnectorBase):
    venue: str = "NATIVE_DEX"
    chain: str = "generic"  # ethereum | bsc | arbitrum | solana | ...
    rpc_url: str = ""
    live: bool = False
    transport: HttpTransport = field(default_factory=UrllibHttp)
    credentials: DEXCredentials = field(default_factory=DEXCredentials)

    def _rpc(self, method: str, params: list[Any]) -> Any:
        if not self.rpc_url:
            return None
        status, doc = self.transport.request(
            "POST", self.rpc_url, body={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
            }, timeout=15.0,
        )
        if status >= 400:
            return None
        return doc.get("result")


__all__ = ["DEXCredentials", "NativeDEXConnector"]

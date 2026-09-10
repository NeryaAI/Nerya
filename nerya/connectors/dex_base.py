"""Shared DEX/chain connector base. Real signing lives under signer policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.errors import TradingError
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
        """JSON-RPC call that fails LOUDLY.

        A silent ``None`` here previously made nonce/gas reads return 0
        and signed transactions broadcast with garbage. Failure modes:
        - no rpc_url configured -> TradingError
        - transport exception / HTTP >= 400 -> TradingError
        - JSON-RPC ``error`` object -> TradingError
        - legitimate ``result: null`` (e.g. pending receipt) -> None
        """
        if not self.rpc_url:
            raise TradingError(
                f"{self.venue} rpc_url is not configured for chain="
                f"{self.chain!r}; set it in the connector/provider config"
            )
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            status, doc = self.transport.request(
                "POST", self.rpc_url, body=body, timeout=15.0,
            )
        except TradingError:
            raise
        except Exception as exc:
            raise TradingError(
                f"{self.venue} rpc {method} transport failed: {exc}"
            ) from exc
        if status >= 400:
            raise TradingError(
                f"{self.venue} rpc {method} returned http {status} "
                f"(rate limit? wrong rpc_url for chain {self.chain!r}?)"
            )
        if not isinstance(doc, dict):
            raise TradingError(
                f"{self.venue} rpc {method} returned non-object payload: {doc!r}"
            )
        if doc.get("error") is not None:
            raise TradingError(
                f"{self.venue} rpc {method} json-rpc error: {doc.get('error')}"
            )
        return doc.get("result")


__all__ = ["DEXCredentials", "NativeDEXConnector"]

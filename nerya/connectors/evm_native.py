"""EVM chain connector (Ethereum / Arbitrum / Polygon / Base / ...).

Read-only RPC methods work against any EVM JSON-RPC endpoint. Write
methods go through :meth:`send_raw_transaction`, which signs a legacy
(type-0) transaction with EIP-155 and broadcasts it. BSC-specific
DEX routing (PancakeSwap v2) lives in :class:`BSCNative`; this class
supports raw contract calls + ETH transfers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..core.errors import TradingError
from .base import OrderAck, Ticker
from .dex_base import NativeDEXConnector


@dataclass
class EVMNative(NativeDEXConnector):
    venue: str = "EVM"
    chain: str = "ethereum"
    chain_id: int = 1

    def get_balance(self, address: str) -> float:
        """Native-asset balance in ETH units (or chain's native unit)."""
        res = self._rpc("eth_getBalance", [address, "latest"])
        if not res:
            return 0.0
        wei = int(res, 16) if isinstance(res, str) else int(res)
        return wei / 1e18

    def get_erc20_balance(self, token: str, address: str) -> float:
        """balanceOf(address) via eth_call, assumes 18 decimals unless known."""
        addr = address[2:].rjust(64, "0")
        data = "0x70a08231" + addr  # selector for balanceOf(address)
        res = self._rpc("eth_call", [{"to": token, "data": data}, "latest"])
        if not res or res == "0x":
            return 0.0
        return int(res, 16) / 1e18

    def get_gas_price_gwei(self) -> float:
        res = self._rpc("eth_gasPrice", [])
        if not res:
            return 0.0
        return int(res, 16) / 1e9

    def get_block_number(self) -> int:
        res = self._rpc("eth_blockNumber", [])
        return int(res, 16) if isinstance(res, str) else 0

    def get_ticker(self, market: str) -> Ticker:
        # DEX spot tickers must come from an AMM quote / aggregator — returning
        # a synthesised price here would be indistinguishable from a silent
        # mock, which is banned on runtime paths (see nerya.core.truth).
        raise TradingError(
            "EVM connector does not provide spot ticker by itself; "
            "use market_data_skill + dex_aggregator quote"
        )

    def simulate_swap(self, *, token_in: str, token_out: str,
                       amount_in: float, slippage_bps: int = 50) -> dict:
        """Very small simulator — real aggregator integration deferred."""
        return {
            "ok": True,
            "chain": self.chain,
            "token_in": token_in, "token_out": token_out,
            "amount_in": amount_in,
            "expected_out": amount_in * (1 - slippage_bps / 10_000),
            "slippage_bps": slippage_bps,
            "gas_price_gwei": self.get_gas_price_gwei(),
        }

    def place_order(self, *args, **kw) -> OrderAck:
        raise NotImplementedError(
            "EVM is a raw-chain venue; route swaps via BSCNative or an "
            "aggregator skill and use trading_skill for intent flow"
        )

    # ---------------------------------------------------------- signed writes
    def get_nonce(self, address: str) -> int:
        res = self._rpc("eth_getTransactionCount", [address, "pending"])
        return int(res, 16) if isinstance(res, str) else 0

    def send_raw_transaction(
        self,
        *,
        to: str,
        data: str = "0x",
        value: int = 0,
        signer_private_key: str,
        gas_price_gwei: float | None = None,
        gas_limit: int = 250_000,
    ) -> dict[str, Any]:
        """Generic signed contract call / ETH transfer.

        Callers should build ``data`` (ABI-encoded selector + args) upstream;
        this method only takes care of nonce, gas, signing, broadcast.
        """
        if not self.live:
            raise TradingError("evm writes disabled (accounts.live=false)")
        try:
            from eth_account import Account  # type: ignore
        except Exception as exc:
            raise TradingError(
                f"evm signed tx requires eth_account: {exc}"
            ) from exc
        from_addr = Account.from_key(signer_private_key).address
        nonce = self.get_nonce(from_addr)
        gp_gwei = gas_price_gwei if gas_price_gwei is not None else (
            self.get_gas_price_gwei() or 5.0
        )
        tx = {
            "to": to, "value": int(value),
            "gas": int(gas_limit),
            "gasPrice": int(gp_gwei * 1e9),
            "nonce": nonce,
            "chainId": self.chain_id,
            "data": data if data.startswith("0x") else "0x" + data,
        }
        signed = Account.sign_transaction(tx, signer_private_key)
        raw_hex = signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") \
            else signed.rawTransaction.hex()
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        tx_hash = self._rpc("eth_sendRawTransaction", [raw_hex])
        if not tx_hash:
            raise TradingError("evm eth_sendRawTransaction returned empty result")
        return {"tx_hash": tx_hash, "from": from_addr, "to": to,
                "value": value, "nonce": nonce, "gas_price_gwei": gp_gwei,
                "chain": self.chain, "chain_id": self.chain_id}

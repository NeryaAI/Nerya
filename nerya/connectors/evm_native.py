"""EVM chain connector (Ethereum / Arbitrum / Polygon / Base / ...).

Read-only RPC methods work against any EVM JSON-RPC endpoint. Write
methods go through :meth:`send_raw_transaction`, which signs a legacy
(type-0) transaction with EIP-155 and broadcasts it. BSC-specific
DEX routing (PancakeSwap v2) lives in :class:`BSCNative`; this class
supports raw contract calls + ETH transfers.

Write-path safety: every signed send verifies ``eth_chainId`` against
the configured ``chain_id`` *before* signing (an EIP-155 signature for
the wrong chain is unreplayable and loses its fees) and, by default,
waits for the transaction receipt before reporting success.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..core.errors import TradingError
from .base import OrderAck, Ticker
from .dex_base import NativeDEXConnector

#: Canonical chain-name -> EIP-155 chain-id map. Single source of truth
#: for the connectors layer (self_custody re-uses it; provider_spec
#: defaults ``chain_id`` from it when the operator row omits it).
EVM_CHAIN_IDS: dict[str, int] = {
    "ethereum": 1,
    "bsc": 56,
    "polygon": 137,
    "arbitrum": 42161,
    "optimism": 10,
    "avalanche": 43114,
    "base": 8453,
    "linea": 59144,
    "zksync": 324,
    "blast": 81457,
    "scroll": 534352,
    "mantle": 5000,
    "fantom": 250,
    "celo": 42220,
    "gnosis": 100,
    "sepolia": 11155111,
    "base-sepolia": 84532,
}

#: Sanity floor used only when the RPC cannot be asked for a gas price.
#: A stale fallback below a chain's real floor produces a tx that never
#: mines, so the per-chain values err high.
_GAS_PRICE_FALLBACK_GWEI: dict[str, float] = {
    "ethereum": 15.0,
    "bsc": 3.0,
    "polygon": 50.0,
    "arbitrum": 0.1,
    "optimism": 0.01,
    "base": 0.01,
    "avalanche": 30.0,
    "fantom": 200.0,
    "gnosis": 1.0,
    "celo": 5.0,
}
_GAS_PRICE_FALLBACK_DEFAULT_GWEI = 5.0

_SEL_DECIMALS = "0x313ce567"  # decimals()


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

    def get_erc20_decimals(self, token: str) -> int:
        res = self._rpc("eth_call", [{"to": token, "data": _SEL_DECIMALS}, "latest"])
        if not res or res == "0x":
            raise TradingError(
                f"decimals() call failed for token {token} on {self.chain} — "
                "cannot scale balances safely; pass decimals explicitly"
            )
        try:
            return int(res, 16)
        except ValueError as exc:
            raise TradingError(
                f"decimals() returned malformed data for token {token}: {res!r}"
            ) from exc

    def get_erc20_balance(self, token: str, address: str,
                          *, decimals: int | None = None) -> float:
        """balanceOf(address) via eth_call.

        ``decimals=None`` resolves the token's decimals on-chain — the
        previous hardcoded ``/ 1e18`` mis-reported USDC/USDT (6 dp) by
        12 orders of magnitude.
        """
        addr = address.lower().removeprefix("0x").rjust(64, "0")
        data = "0x70a08231" + addr  # selector for balanceOf(address)
        res = self._rpc("eth_call", [{"to": token, "data": data}, "latest"])
        if not res or res == "0x":
            return 0.0
        if decimals is None:
            decimals = self.get_erc20_decimals(token)
        return int(res, 16) / (10 ** int(decimals))

    def get_gas_price_gwei(self) -> float:
        res = self._rpc("eth_gasPrice", [])
        if not res:
            return 0.0
        return int(res, 16) / 1e9

    def get_block_number(self) -> int:
        res = self._rpc("eth_blockNumber", [])
        return int(res, 16) if isinstance(res, str) else 0

    def get_chain_id(self) -> int:
        res = self._rpc("eth_chainId", [])
        return int(res, 16) if isinstance(res, str) else 0

    def _verify_chain_id(self) -> None:
        """Refuse to sign when the RPC speaks for a different chain."""
        actual = self.get_chain_id()
        if actual and int(self.chain_id) and actual != int(self.chain_id):
            raise TradingError(
                f"chain-id mismatch: rpc_url serves chain {actual} but the "
                f"connector is configured for chain_id={self.chain_id} "
                f"(chain={self.chain!r}). Signing for the wrong chain loses "
                "the fees — fix rpc_url or chain_id."
            )

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

    def gas_price_or_fallback(self, gas_price_gwei: float | None = None) -> float:
        """Resolved gas price: explicit override > RPC > per-chain floor."""
        if gas_price_gwei is not None:
            return float(gas_price_gwei)
        try:
            live = self.get_gas_price_gwei()
        except TradingError:
            live = 0.0
        if live > 0:
            return live
        return _GAS_PRICE_FALLBACK_GWEI.get(
            self.chain, _GAS_PRICE_FALLBACK_DEFAULT_GWEI,
        )

    def wait_for_receipt(
        self,
        tx_hash: str,
        *,
        timeout_s: float = 90.0,
        poll_s: float = 2.0,
    ) -> dict[str, Any]:
        """Poll for the tx receipt; raise on timeout or on-chain revert."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if isinstance(receipt, dict) and receipt.get("blockHash") is not None:
                status = receipt.get("status")
                if isinstance(status, str) and status.lower() in ("0x0", "0"):
                    raise TradingError(
                        f"tx {tx_hash} reverted on-chain (status=0x0); "
                        f"gas_used={receipt.get('gasUsed')}"
                    )
                return receipt
            time.sleep(poll_s)
        raise TradingError(
            f"tx {tx_hash} not mined after {timeout_s:.0f}s — it may still "
            "land; check the explorer before re-sending (duplicate sends "
            "double-spend the swap)"
        )

    def send_raw_transaction(
        self,
        *,
        to: str,
        data: str = "0x",
        value: int = 0,
        signer_private_key: str,
        gas_price_gwei: float | None = None,
        gas_limit: int = 250_000,
        confirm: bool = True,
    ) -> dict[str, Any]:
        """Generic signed contract call / ETH transfer.

        Callers should build ``data`` (ABI-encoded selector + args) upstream;
        this method takes care of chain-id verification, nonce, gas,
        signing, broadcast and (by default) receipt confirmation.
        """
        if not self.live:
            raise TradingError("evm writes disabled (accounts.live=false)")
        self._verify_chain_id()
        try:
            from eth_account import Account  # type: ignore
        except Exception as exc:
            raise TradingError(
                f"evm signed tx requires eth_account: {exc}"
            ) from exc
        from_addr = Account.from_key(signer_private_key).address
        nonce = self.get_nonce(from_addr)
        gp_gwei = self.gas_price_or_fallback(gas_price_gwei)
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
        out = {"tx_hash": tx_hash, "from": from_addr, "to": to,
               "value": value, "nonce": nonce, "gas_price_gwei": gp_gwei,
               "chain": self.chain, "chain_id": self.chain_id,
               "confirmed": False}
        if confirm:
            receipt = self.wait_for_receipt(tx_hash)
            out["confirmed"] = True
            out["block_number"] = _as_int(receipt.get("blockNumber"))
            out["gas_used"] = _as_int(receipt.get("gasUsed"))
        return out


def _as_int(hexish: Any) -> int:
    if isinstance(hexish, str):
        try:
            return int(hexish, 16)
        except ValueError:
            return 0
    try:
        return int(hexish or 0)
    except (TypeError, ValueError):
        return 0

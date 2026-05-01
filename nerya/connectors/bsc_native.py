"""BSC (Binance Smart Chain) native connector with PancakeSwap v2 routing.

This connector is connector-oriented:

* Reads: native + ERC-20 balances, allowances, gas, block, token metadata, and
  PancakeSwap v2 router quotes (``getAmountsOut``) — all via JSON-RPC.
* Writes: ``approve`` + ``swap`` against the v2 router. Signing is gated
  behind ``live=True`` AND a resolved ``signer_ref`` (custody key from
  SecretVault). The private key is never persisted or logged.

Soft-optional dependencies: ``eth_abi`` (ABI encoding) and ``eth_account``
(secp256k1 + EIP-155 signing). If either is unavailable the signed write
path raises a clear :class:`TradingError`; the read path keeps working.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import TradingError
from .base import OrderAck
from .evm_native import EVMNative


# --------------------------------------------------------------- constants
PANCAKE_V2_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_V2_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
BUSD = "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"
USDT_BEP20 = "0x55d398326f99059fF775485246999027B3197955"
USDC_BEP20 = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"

BSC_CHAIN_ID = 56

# function selectors (keccak256(signature)[:4])
_SEL = {
    "balanceOf":       "0x70a08231",  # balanceOf(address)
    "allowance":       "0xdd62ed3e",  # allowance(address,address)
    "decimals":        "0x313ce567",  # decimals()
    "symbol":          "0x95d89b41",  # symbol()
    "name":            "0x06fdde03",  # name()
    "approve":         "0x095ea7b3",  # approve(address,uint256)
    "getAmountsOut":   "0xd06ca61f",  # getAmountsOut(uint256,address[])
    "swapExactTokensForTokens":
        "0x38ed1739",  # swapExactTokensForTokens(uint,uint,address[],address,uint)
    "swapExactETHForTokens":
        "0x7ff36ab5",  # swapExactETHForTokens(uint,address[],address,uint)
    "swapExactTokensForETH":
        "0x18cbafe5",  # swapExactTokensForETH(uint,uint,address[],address,uint)
}


# =============================================================== helpers
def _pad_addr(addr: str) -> str:
    a = addr.lower().removeprefix("0x").rjust(64, "0")
    return a


def _pad_uint(n: int) -> str:
    return f"{n:064x}"


def _encode_path(path: list[str]) -> str:
    """ABI-encode ``address[]`` parameter following the offset already placed by caller."""
    n = len(path)
    out = _pad_uint(n)
    for a in path:
        out += _pad_addr(a)
    return out


def _decode_uint_array(hex_str: str) -> list[int]:
    """Decode a dynamic ``uint256[]`` return value (single dynamic output)."""
    if not hex_str or hex_str == "0x":
        return []
    data = hex_str[2:]
    # first 32 bytes = offset (typically 0x20); next 32 bytes = length
    if len(data) < 128:
        return []
    length = int(data[64:128], 16)
    vals: list[int] = []
    for i in range(length):
        start = 128 + i * 64
        vals.append(int(data[start:start + 64], 16))
    return vals


# =============================================================== connector
@dataclass
class BSCNative(EVMNative):
    venue: str = "BSC"
    chain: str = "bsc"
    chain_id: int = BSC_CHAIN_ID
    rpc_url: str = "https://bsc-dataseed.binance.org"
    router: str = PANCAKE_V2_ROUTER
    factory: str = PANCAKE_V2_FACTORY
    wbnb: str = WBNB

    #: Gas estimate defaults (override per-call if needed)
    default_gas_limit: int = 300_000
    default_gas_price_gwei: float = 3.0

    #: Slippage tolerance in bps (50 bps = 0.5%) used when caller omits minimumOut
    default_slippage_bps: int = 50

    # ---------------------------------------------------------- reads
    def get_erc20_balance(self, token: str, address: str, *, decimals: int | None = None) -> float:
        data = _SEL["balanceOf"] + _pad_addr(address)
        res = self._rpc("eth_call", [{"to": token, "data": data}, "latest"])
        if not res or res == "0x":
            return 0.0
        raw = int(res, 16)
        if decimals is None:
            decimals = self.get_erc20_decimals(token)
        return raw / (10 ** decimals)

    def get_erc20_allowance(self, token: str, owner: str, spender: str,
                             *, decimals: int | None = None) -> float:
        data = _SEL["allowance"] + _pad_addr(owner) + _pad_addr(spender)
        res = self._rpc("eth_call", [{"to": token, "data": data}, "latest"])
        if not res or res == "0x":
            return 0.0
        raw = int(res, 16)
        if decimals is None:
            decimals = self.get_erc20_decimals(token)
        return raw / (10 ** decimals)

    def get_erc20_decimals(self, token: str) -> int:
        res = self._rpc("eth_call", [{"to": token, "data": _SEL["decimals"]}, "latest"])
        if not res or res == "0x":
            return 18
        try:
            return int(res, 16)
        except Exception:
            return 18

    def get_erc20_symbol(self, token: str) -> str:
        res = self._rpc("eth_call", [{"to": token, "data": _SEL["symbol"]}, "latest"])
        if not res or res == "0x":
            return ""
        return _decode_string(res)

    def get_nonce(self, address: str) -> int:
        res = self._rpc("eth_getTransactionCount", [address, "pending"])
        return int(res, 16) if isinstance(res, str) else 0

    # ---------------------------------------------------------- PancakeSwap v2
    def get_amounts_out(self, amount_in_wei: int, path: list[str]) -> list[int]:
        """Wrapper around router.getAmountsOut — returns list of wei amounts."""
        # offset for the address[] parameter (bytes 32+32=64 prefix: amount_in + offset)
        head = _pad_uint(amount_in_wei) + _pad_uint(0x40)
        body = _encode_path(path)
        data = _SEL["getAmountsOut"] + head + body
        res = self._rpc("eth_call", [{"to": self.router, "data": data}, "latest"])
        if not res or res == "0x":
            raise TradingError(
                f"pancake getAmountsOut returned empty — "
                f"path {path} (amount_in_wei={amount_in_wei})"
            )
        return _decode_uint_array(res)

    def quote_swap(
        self,
        *,
        token_in: str,
        token_out: str,
        amount_in: float,
        slippage_bps: int | None = None,
        path: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return a quote dict suitable for display / logging.

        ``path`` defaults to ``[token_in, WBNB, token_out]`` when ``token_in``
        and ``token_out`` are both non-WBNB ERC-20s; otherwise a direct
        two-hop path is used.
        """
        slippage_bps = int(slippage_bps if slippage_bps is not None else self.default_slippage_bps)
        dec_in = self.get_erc20_decimals(token_in)
        dec_out = self.get_erc20_decimals(token_out)
        p = list(path) if path else self._default_path(token_in, token_out)
        amount_in_wei = int(amount_in * (10 ** dec_in))
        amounts = self.get_amounts_out(amount_in_wei, p)
        amount_out_wei = amounts[-1] if amounts else 0
        amount_out_min_wei = int(amount_out_wei * (10_000 - slippage_bps) / 10_000)
        amount_out = amount_out_wei / (10 ** dec_out)
        amount_out_min = amount_out_min_wei / (10 ** dec_out)
        mid = (amount_out / amount_in) if amount_in else 0.0
        return {
            "chain": self.chain,
            "venue": self.venue,
            "router": self.router,
            "path": p,
            "amount_in": amount_in,
            "amount_in_wei": amount_in_wei,
            "amount_out": amount_out,
            "amount_out_wei": amount_out_wei,
            "amount_out_min": amount_out_min,
            "amount_out_min_wei": amount_out_min_wei,
            "price": mid,
            "slippage_bps": slippage_bps,
            "gas_price_gwei": self.get_gas_price_gwei() or self.default_gas_price_gwei,
        }

    def _default_path(self, token_in: str, token_out: str) -> list[str]:
        t0 = token_in.lower()
        t1 = token_out.lower()
        wbnb = self.wbnb.lower()
        if t0 == wbnb or t1 == wbnb:
            return [token_in, token_out]
        return [token_in, self.wbnb, token_out]

    # ---------------------------------------------------------- writes
    def approve(
        self,
        *,
        token: str,
        spender: str,
        amount: int,
        signer_private_key: str,
        gas_price_gwei: float | None = None,
        gas_limit: int | None = None,
    ) -> dict[str, Any]:
        """Submit an ERC-20 ``approve`` tx. Returns ``{tx_hash, nonce, ...}``."""
        self._check_live()
        data = _SEL["approve"] + _pad_addr(spender) + _pad_uint(amount)
        return self._sign_and_send(
            to=token,
            data=data,
            value=0,
            signer_private_key=signer_private_key,
            gas_price_gwei=gas_price_gwei,
            gas_limit=gas_limit,
        )

    def swap(
        self,
        *,
        token_in: str,
        token_out: str,
        amount_in: float,
        amount_out_min: float | None = None,
        slippage_bps: int | None = None,
        path: list[str] | None = None,
        recipient: str,
        deadline_seconds: int = 600,
        signer_private_key: str,
        gas_price_gwei: float | None = None,
        gas_limit: int | None = None,
    ) -> dict[str, Any]:
        """Execute a PancakeSwap v2 swap through the router.

        The caller resolves the signer's private key exactly once via the
        signer policy and hands it to this method; we never persist it. The
        private key is immediately overwritten after use.
        """
        self._check_live()
        # Build the canonical path and amounts.
        quote = self.quote_swap(
            token_in=token_in, token_out=token_out, amount_in=amount_in,
            slippage_bps=slippage_bps, path=path,
        )
        amount_in_wei = quote["amount_in_wei"]
        if amount_out_min is None:
            amount_out_min_wei = quote["amount_out_min_wei"]
        else:
            dec_out = self.get_erc20_decimals(token_out)
            amount_out_min_wei = int(amount_out_min * (10 ** dec_out))
        deadline = int(time.time()) + int(deadline_seconds)
        p = quote["path"]

        is_native_in = token_in.lower() == self.wbnb.lower()
        is_native_out = token_out.lower() == self.wbnb.lower()

        if is_native_in:
            # swapExactETHForTokens(uint amountOutMin, address[] path, address to, uint deadline)
            head = _pad_uint(amount_out_min_wei) + _pad_uint(0x80) \
                   + _pad_addr(recipient) + _pad_uint(deadline)
            body = _encode_path(p)
            data = _SEL["swapExactETHForTokens"] + head + body
            value = amount_in_wei
        elif is_native_out:
            head = (_pad_uint(amount_in_wei)
                    + _pad_uint(amount_out_min_wei)
                    + _pad_uint(0xa0)
                    + _pad_addr(recipient)
                    + _pad_uint(deadline))
            body = _encode_path(p)
            data = _SEL["swapExactTokensForETH"] + head + body
            value = 0
        else:
            head = (_pad_uint(amount_in_wei)
                    + _pad_uint(amount_out_min_wei)
                    + _pad_uint(0xa0)
                    + _pad_addr(recipient)
                    + _pad_uint(deadline))
            body = _encode_path(p)
            data = _SEL["swapExactTokensForTokens"] + head + body
            value = 0

        out = self._sign_and_send(
            to=self.router,
            data=data,
            value=value,
            signer_private_key=signer_private_key,
            gas_price_gwei=gas_price_gwei,
            gas_limit=gas_limit,
        )
        out["quote"] = quote
        out["recipient"] = recipient
        out["deadline"] = deadline
        return out

    def place_order(self, *, market: str, side: str, order_type: str,
                     size: float, price: float | None = None,
                     client_order_id: str | None = None,
                     time_in_force: str = "GTC") -> OrderAck:
        """CEX-style place_order adapter — not used for DEX.

        Scripts should call ``trading_skill`` which resolves to
        :meth:`swap`. We expose this to keep the ``Connector`` interface
        uniform so the execution engine can dispatch by venue.
        """
        raise NotImplementedError(
            "BSC is a DEX venue; use bsc_skill.swap / trading_skill for swaps"
        )

    # ---------------------------------------------------------- signer
    def _check_live(self) -> None:
        if not self.live:
            raise TradingError(
                "BSC writes disabled (set accounts.live=true + runtime.live_trading_enabled=true)"
            )

    def _sign_and_send(
        self,
        *,
        to: str,
        data: str,
        value: int,
        signer_private_key: str,
        gas_price_gwei: float | None,
        gas_limit: int | None,
    ) -> dict[str, Any]:
        """Sign a legacy (type-0) transaction with EIP-155 and broadcast."""
        try:
            from eth_account import Account  # type: ignore
            from eth_account._utils.legacy_transactions import encode_transaction
            from eth_account._utils.legacy_transactions import serializable_unsigned_transaction_from_dict
        except Exception as exc:  # pragma: no cover - eth_account missing
            raise TradingError(
                f"bsc swap requires `eth_account` python package: {exc}"
            ) from exc

        from_addr = Account.from_key(signer_private_key).address
        nonce = self.get_nonce(from_addr)
        gp_gwei = gas_price_gwei if gas_price_gwei is not None else (
            self.get_gas_price_gwei() or self.default_gas_price_gwei
        )
        gas_price_wei = int(gp_gwei * 1e9)
        tx = {
            "to": to,
            "value": value,
            "gas": int(gas_limit or self.default_gas_limit),
            "gasPrice": gas_price_wei,
            "nonce": nonce,
            "chainId": self.chain_id,
            "data": data if data.startswith("0x") else "0x" + data,
        }
        try:
            signed = Account.sign_transaction(tx, signer_private_key)
        finally:
            # Scrub the local reference; caller also scrubs its copy.
            signer_private_key = "0" * len(signer_private_key)  # noqa: F841
        raw_hex = signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") \
            else signed.rawTransaction.hex()
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        tx_hash = self._rpc("eth_sendRawTransaction", [raw_hex])
        if not tx_hash:
            raise TradingError("bsc eth_sendRawTransaction returned empty result")
        return {
            "tx_hash": tx_hash,
            "from": from_addr,
            "to": to,
            "value": value,
            "nonce": nonce,
            "gas_price_gwei": gp_gwei,
            "gas_limit": int(gas_limit or self.default_gas_limit),
            "chain": self.chain,
            "chain_id": self.chain_id,
        }


def _decode_string(hex_str: str) -> str:
    """Best-effort decode of an ABI-encoded ``string`` return value."""
    try:
        data = hex_str[2:] if hex_str.startswith("0x") else hex_str
        if len(data) < 128:
            return bytes.fromhex(data.rstrip("0")).decode("utf-8", errors="replace")
        length = int(data[64:128], 16)
        body = data[128:128 + length * 2]
        return bytes.fromhex(body).decode("utf-8", errors="replace")
    except Exception:
        return ""


__all__ = [
    "BSCNative",
    "PANCAKE_V2_ROUTER",
    "PANCAKE_V2_FACTORY",
    "WBNB",
    "BUSD",
    "USDT_BEP20",
    "USDC_BEP20",
    "BSC_CHAIN_ID",
]

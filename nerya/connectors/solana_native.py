"""Solana chain connector.

Reads (RPC): native balance, SPL balances, slot, block.
Writes: Jupiter-powered swaps. The Jupiter aggregator returns a serialized
v0 transaction that we sign locally (Ed25519 via PyNaCl) and broadcast via
``sendTransaction``. Private-key resolution happens through the signer
policy — the connector only ever receives a single-shot hex/base58 key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.errors import TradingError
from .base import OrderAck, Ticker
from .dex_base import NativeDEXConnector

JUPITER_BASE_URL = "https://quote-api.jup.ag/v6"


@dataclass
class SolanaNative(NativeDEXConnector):
    venue: str = "SOLANA"
    chain: str = "solana"
    jupiter_url: str = JUPITER_BASE_URL
    default_slippage_bps: int = 50

    # ------------------------------------------------------------- reads
    def get_balance(self, address: str) -> float:
        res = self._rpc("getBalance", [address])
        if not res:
            return 0.0
        lamports = res.get("value", 0) if isinstance(res, dict) else int(res)
        return lamports / 1e9

    def get_token_balance(self, owner: str, mint: str) -> float:
        res = self._rpc(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        if not res or not isinstance(res, dict):
            return 0.0
        total = 0.0
        for a in res.get("value", []):
            info = a.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            amt = info.get("tokenAmount", {}).get("uiAmount")
            if amt is not None:
                total += float(amt)
        return total

    def get_slot(self) -> int:
        res = self._rpc("getSlot", [])
        return int(res) if res is not None else 0

    def get_ticker(self, market: str) -> Ticker:
        raise TradingError(
            "Solana connector does not provide spot tickers; "
            "use market_data_skill + Jupiter quote"
        )

    # ------------------------------------------------------------- quote
    def quote_jupiter(
        self, *,
        input_mint: str,
        output_mint: str,
        amount_in_raw: int,
        slippage_bps: int | None = None,
        only_direct_routes: bool = False,
    ) -> dict[str, Any]:
        """Fetch a Jupiter v6 quote. ``amount_in_raw`` is in base units
        (lamports for SOL, token-native units otherwise)."""
        slip = int(slippage_bps if slippage_bps is not None else self.default_slippage_bps)
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_in_raw),
            "slippageBps": str(slip),
            "onlyDirectRoutes": "true" if only_direct_routes else "false",
        }
        status, doc = self.transport.request(
            "GET", f"{self.jupiter_url}/quote",
            params=params, timeout=15.0,
        )
        if status >= 400 or not isinstance(doc, dict):
            raise TradingError(f"jupiter /quote failed: {doc}")
        return doc

    def simulate_swap(self, *, input_mint: str, output_mint: str,
                       amount_in: float, slippage_bps: int = 50) -> dict:
        """Back-compat paper-mode simulator (used by tests). Real quotes
        live in :meth:`quote_jupiter`."""
        return {
            "ok": True, "chain": self.chain,
            "input_mint": input_mint, "output_mint": output_mint,
            "amount_in": amount_in,
            "expected_out": amount_in * (1 - slippage_bps / 10_000),
            "slippage_bps": slippage_bps,
        }

    # ------------------------------------------------------------- swap
    def swap(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_in_raw: int,
        signer_private_key: str,
        slippage_bps: int | None = None,
        user_public_key: str | None = None,
        wrap_and_unwrap_sol: bool = True,
        priority_fee_lamports: int | None = None,
    ) -> dict[str, Any]:
        """Execute a Jupiter swap end-to-end.

        1. quote from /quote
        2. /swap to get a base64 v0 tx
        3. sign + sendTransaction
        """
        self._check_live()
        quote = self.quote_jupiter(
            input_mint=input_mint, output_mint=output_mint,
            amount_in_raw=amount_in_raw, slippage_bps=slippage_bps,
        )
        user_pubkey = user_public_key or _pubkey_from_signer(signer_private_key)
        body: dict[str, Any] = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": bool(wrap_and_unwrap_sol),
        }
        if priority_fee_lamports is not None:
            body["prioritizationFeeLamports"] = int(priority_fee_lamports)
        status, swap_doc = self.transport.request(
            "POST", f"{self.jupiter_url}/swap",
            body=body, timeout=20.0,
        )
        if status >= 400 or not isinstance(swap_doc, dict):
            raise TradingError(f"jupiter /swap failed: {swap_doc}")
        b64_tx = swap_doc.get("swapTransaction")
        if not b64_tx:
            raise TradingError(f"jupiter /swap missing swapTransaction: {swap_doc}")
        signed_b64 = _sign_solana_v0_tx(b64_tx, signer_private_key)
        tx_sig = self._rpc("sendTransaction",
                            [signed_b64, {"encoding": "base64",
                                          "skipPreflight": False,
                                          "maxRetries": 3}])
        if not tx_sig:
            raise TradingError("solana sendTransaction returned empty signature")
        return {
            "signature": tx_sig,
            "input_mint": input_mint,
            "output_mint": output_mint,
            "amount_in_raw": amount_in_raw,
            "quote": quote,
            "user": user_pubkey,
        }

    def place_order(self, *args, **kw) -> OrderAck:
        raise NotImplementedError(
            "Solana is a DEX venue; route via trading_skill / solana swap"
        )

    def _check_live(self) -> None:
        if not self.live:
            raise TradingError(
                "solana writes disabled (set accounts.live=true + runtime.live_trading_enabled=true)"
            )


# ------------------------------------------------------------------ helpers
def _sign_solana_v0_tx(b64_tx: str, signer_private_key: str) -> str:
    """Decode a base64 v0 tx, re-sign it with PyNaCl Ed25519, return base64.

    Solana v0 layout::

        <num_signatures:shortvec_u8> <sig_1:64b> ... <sig_n:64b> <message>

    The message hash (first-message-signer = fee-payer = our key) is
    signed and written into the first signature slot.
    """
    try:
        import base64
        import base58  # type: ignore
        from nacl.signing import SigningKey  # type: ignore
    except Exception as exc:
        raise TradingError(
            f"solana swap requires pynacl + base58: {exc}"
        ) from exc

    raw = base64.b64decode(b64_tx)
    # Parse shortvec for num_signatures
    n_sigs, off = _read_shortvec_u16(raw, 0)
    sig_end = off + 64 * n_sigs
    message_bytes = raw[sig_end:]

    sk = _signing_key_from_secret(signer_private_key)
    sig = sk.sign(message_bytes).signature
    if len(sig) != 64:
        raise TradingError("unexpected ed25519 signature length")
    # Write our signature into slot 0 (fee payer).
    out = bytearray(raw)
    out[off:off + 64] = sig
    return base64.b64encode(bytes(out)).decode("ascii")


def _pubkey_from_signer(signer_private_key: str) -> str:
    try:
        import base58  # type: ignore
        from nacl.signing import SigningKey  # type: ignore
    except Exception as exc:
        raise TradingError(f"solana requires pynacl + base58: {exc}") from exc
    sk = _signing_key_from_secret(signer_private_key)
    vk = sk.verify_key
    return base58.b58encode(bytes(vk)).decode("ascii")


def _signing_key_from_secret(secret: str):
    """Accepts hex (0x-prefixed or not, 64 bytes = seed+pub) or base58 (64 bytes)."""
    from nacl.signing import SigningKey  # type: ignore
    s = secret.strip()
    # Hex (legacy ethereum-style) 32-byte seed
    if s.startswith("0x") or (len(s) in (64, 66) and all(c in "0123456789abcdefABCDEF" for c in s.removeprefix("0x"))):
        seed = bytes.fromhex(s.removeprefix("0x"))
        if len(seed) != 32:
            raise TradingError("solana hex key must be 32 bytes (no public half)")
        return SigningKey(seed)
    # Base58 Solana "secret key" = 64 bytes (seed || pubkey)
    try:
        import base58  # type: ignore
    except Exception as exc:
        raise TradingError(f"solana base58 key requires base58: {exc}") from exc
    full = base58.b58decode(s)
    if len(full) == 64:
        return SigningKey(full[:32])
    if len(full) == 32:
        return SigningKey(full)
    raise TradingError(
        f"solana signer key must be 32 or 64 bytes (got {len(full)})"
    )


def _read_shortvec_u16(buf: bytes, off: int) -> tuple[int, int]:
    """Solana's compact-u16 decoder — returns (value, new_offset)."""
    value = 0
    shift = 0
    while True:
        if off >= len(buf):
            raise TradingError("shortvec truncated")
        b = buf[off]
        off += 1
        value |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            break
        shift += 7
        if shift >= 21:
            raise TradingError("shortvec too long")
    return value, off

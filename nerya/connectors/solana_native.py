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

JUPITER_BASE_URL = "https://api.jup.ag/swap/v2"


@dataclass
class SolanaNative(NativeDEXConnector):
    venue: str = "SOLANA"
    chain: str = "solana"
    jupiter_url: str = JUPITER_BASE_URL
    jupiter_api_key: str = field(default="", repr=False)
    jupiter_exclude_dexes: tuple[str, ...] = ()
    jupiter_max_priority_fee_lamports: int = 1_000_000
    jupiter_max_total_fee_lamports: int = 2_000_000
    jupiter_max_rent_lamports: int = 10_000_000
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

    def get_mint_decimals(self, mint: str) -> int:
        """On-chain decimals for an SPL mint (getTokenSupply)."""
        res = self._rpc("getTokenSupply", [mint])
        val = (res or {}).get("value") if isinstance(res, dict) else None
        if not isinstance(val, dict) or "decimals" not in val:
            raise TradingError(
                f"getTokenSupply failed for mint {mint} — cannot scale "
                "amounts safely; pass decimals explicitly"
            )
        return int(val["decimals"])

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
        taker: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a Jupiter v6 quote. ``amount_in_raw`` is in base units
        (lamports for SOL, token-native units otherwise)."""
        slip = int(slippage_bps if slippage_bps is not None else self.default_slippage_bps)
        if self.jupiter_url.rstrip('/').endswith('/v2'):
            from .jupiter_v2 import quote
            return quote(self, input_mint=input_mint, output_mint=output_mint,
                         amount_in_raw=amount_in_raw, slippage_bps=slip,
                         only_direct_routes=only_direct_routes, taker=taker)
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
            headers={"x-api-key":self.jupiter_api_key} if self.jupiter_api_key else {},
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
    def wait_for_signature(
        self,
        signature: str,
        *,
        timeout_s: float = 60.0,
        poll_s: float = 2.0,
    ) -> dict[str, Any]:
        """Poll ``getSignatureStatuses`` until the tx confirms or fails.

        Without this, a broadcast that the node accepted but the cluster
        rejected (or dropped) looks like a success and invites duplicate
        sends.
        """
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            res = self._rpc(
                "getSignatureStatuses",
                [[signature], {"searchStatusHistory": True}],
            )
            statuses = (res or {}).get("value") if isinstance(res, dict) else None
            st = (statuses or [None])[0] if statuses else None
            if st:
                if st.get("err"):
                    raise TradingError(
                        f"solana tx {signature} failed on-chain: {st['err']}"
                    )
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return st
            time.sleep(poll_s)
        raise TradingError(
            f"solana tx {signature} not confirmed after {timeout_s:.0f}s — "
            "it may still land; check the explorer before re-sending"
        )

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
        confirm: bool = True,
        quote: dict[str, Any] | None = None,
        on_broadcast: Any = None,
    ) -> dict[str, Any]:
        """Execute a Jupiter swap end-to-end.

        1. quote from /quote (or use the caller-supplied ``quote`` so the
           exact quote that was price-checked is the one executed)
        2. /swap to get a base64 v0 tx
        3. sign + sendTransaction
        4. (default) poll until the signature confirms
        """
        self._check_live()
        user_pubkey = user_public_key or _pubkey_from_signer(signer_private_key)
        if self.jupiter_url.rstrip('/').endswith('/v2'):
            from .jupiter_v2 import execute
            slip = slippage_bps if slippage_bps is not None else self.default_slippage_bps
            if quote is None:
                quote = self.quote_jupiter(input_mint=input_mint, output_mint=output_mint,
                    amount_in_raw=amount_in_raw, slippage_bps=slip, taker=user_pubkey)
            return execute(self, quote, signer_private_key, input_mint=input_mint, output_mint=output_mint,
                amount=amount_in_raw, slippage=slip, owner=user_pubkey, confirm=confirm, on_broadcast=on_broadcast)
        if quote is None:
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
            headers={"x-api-key":self.jupiter_api_key} if self.jupiter_api_key else {},
        )
        if status >= 400 or not isinstance(swap_doc, dict):
            raise TradingError(f"jupiter /swap failed: {swap_doc}")
        b64_tx = swap_doc.get("swapTransaction")
        if not b64_tx:
            raise TradingError(f"jupiter /swap missing swapTransaction: {swap_doc}")
        return self.send_swap_transaction(b64_tx, signer_private_key, output_mint=output_mint,
            user_public_key=user_pubkey, quote=quote, confirm=confirm, on_broadcast=on_broadcast)

    def send_swap_transaction(self, b64_tx, signer_private_key, *, output_mint, user_public_key,
                              quote=None, confirm=True, on_broadcast=None):
        self._check_live()
        signed_b64 = _sign_solana_v0_tx(b64_tx, signer_private_key)
        import base64, base58
        raw = base64.b64decode(signed_b64)
        _, offset = _read_shortvec_u16(raw,0)
        expected_sig = base58.b58encode(raw[offset:offset+64]).decode()
        if on_broadcast:
            on_broadcast({"tx_hash":expected_sig,"chain":"solana","owner":user_public_key,"token_out":output_mint})
        tx_sig = self._rpc("sendTransaction",
                            [signed_b64, {"encoding": "base64",
                                          "skipPreflight": False,
                                          "maxRetries": 3}])
        if tx_sig != expected_sig:
            raise TradingError("solana sendTransaction returned missing/mismatched signature", ambiguous=True)
        out: dict[str, Any] = {
            "signature": tx_sig,
            "output_mint": output_mint,
            "quote": quote or {},
            "user": user_public_key,
            "confirmed": False,
        }
        if confirm:
            try:
                st = self.wait_for_signature(tx_sig)
                out["confirmed"] = True
                out["confirmation_status"] = st.get("confirmationStatus")
                out["slot"] = st.get("slot")
                out["amount_out"] = self.transaction_output(tx_sig, user_public_key, output_mint)
                out["network_fee"] = self.transaction_fee(tx_sig, user_public_key)
            except TradingError as exc:
                out["confirmation_error"] = str(exc)
        return out

    def transaction_fee(self, signature, owner):
        """Observed network cost, including compute-unit priority fees; not rent."""
        tx = self._rpc('getTransaction', [signature, {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 0}])
        meta = (tx or {}).get('meta') or {}
        keys = ((tx or {}).get('transaction') or {}).get('message', {}).get('accountKeys', [])
        if not keys or meta.get('fee') is None:
            return {'status': 'unavailable'}
        payer = keys[0].get('pubkey') if isinstance(keys[0], dict) else keys[0]
        from .jupiter_v2 import integer
        fee = integer(meta['fee'], 'observed network fee')
        return {'status': 'observed', 'asset': 'SOL', 'amount_base': str(fee if payer == owner else 0),
                'decimals': 9, 'payer': payer, 'transaction_hash': signature, 'source': 'transaction_meta'}

    def transaction_output(self, signature, owner, mint):
        tx = self._rpc("getTransaction",[signature,{"encoding":"jsonParsed","maxSupportedTransactionVersion":0}])
        meta = (tx or {}).get("meta") or {}
        if meta.get("err") is not None:
            raise TradingError("swap transaction failed on-chain")
        def balance(rows):
            return sum(float((row.get("uiTokenAmount") or {}).get("uiAmountString") or 0)
                for row in rows if row.get("owner")==owner and row.get("mint")==mint)
        if mint != "So11111111111111111111111111111111111111112":
            if "postTokenBalances" not in meta:
                raise TradingError("transaction token balance evidence unavailable")
            return balance(meta["postTokenBalances"])-balance(meta.get("preTokenBalances") or [])
        keys = ((tx or {}).get("transaction") or {}).get("message",{}).get("accountKeys",[])
        index = next((i for i,k in enumerate(keys) if (k.get("pubkey") if isinstance(k,dict) else k)==owner),None)
        if index is None or not meta.get("postBalances"):
            raise TradingError("transaction native balance evidence unavailable")
        # Include this owner's token accounts: funding/closing ATAs moves
        # rent between accounts but is not swap proceeds. WSOL accounts also
        # hold native value when the router leaves the output wrapped.
        indices={index}
        for row in (meta.get('preTokenBalances') or [])+(meta.get('postTokenBalances') or []):
            if row.get('owner')==owner:
                indices.add(int(row['accountIndex']))
        delta=sum(meta['postBalances'][i]-meta['preBalances'][i] for i in indices)
        return (delta+(int(meta.get('fee') or 0) if index==0 else 0))/1e9

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
    signed and written into the first signature slot. Before signing we
    verify (F12) that the message's first required signer really is our
    pubkey — overwriting slot 0 of a tx built for another keypair would
    otherwise spend *that* wallet's funds/ATAs with our signature
    rejected (or worse, slot-swap confusion on multi-sig txs).
    """
    try:
        import base64
        import base58  # type: ignore
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
    our_pubkey = base58.b58encode(bytes(sk.verify_key)).decode("ascii")
    first_signer = _first_required_signer(message_bytes)
    if first_signer != our_pubkey:
        raise TradingError(
            f"solana tx first required signer {first_signer} is not this "
            f"wallet ({our_pubkey}) — refusing to sign a transaction that "
            "was not built for our keypair (destination wallets are not "
            "supported on this path)"
        )
    sig = sk.sign(message_bytes).signature
    if len(sig) != 64:
        raise TradingError("unexpected ed25519 signature length")
    # Write our signature into slot 0 (fee payer).
    out = bytearray(raw)
    out[off:off + 64] = sig
    return base64.b64encode(bytes(out)).decode("ascii")


def _first_required_signer(message: bytes) -> str:
    """Base58 pubkey of a legacy/v0 message's first required signer.

    Both layouts open with a 3-byte header (numRequiredSignatures,
    numReadonlySignedAccounts, numReadonlyUnsignedAccounts) followed by
    the compact-u16 account list — signer 0 is the fee payer and the
    only key this connector ever signs with.
    """
    import base58  # type: ignore

    if not message:
        raise TradingError('empty Solana transaction message')
    off = 0
    if message[0] & 0x80:
        if message[0] != 0x80:
            raise TradingError('unsupported Solana message version')
        off = 1
    n_required = message[off]
    off += 1
    if n_required < 1:
        raise TradingError(
            "solana message declares no required signers — refusing to sign"
        )
    off += 2  # numReadonlySignedAccounts + numReadonlyUnsignedAccounts
    n_accounts, off = _read_shortvec_u16(message, off)
    if n_accounts < 1 or off + 32 > len(message):
        raise TradingError(
            "solana message is not a parseable legacy/v0 transaction "
            "(account list truncated) — refusing to blind-sign"
        )
    return base58.b58encode(message[off:off + 32]).decode("ascii")


def _pubkey_from_signer(signer_private_key: str) -> str:
    try:
        import base58  # type: ignore
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

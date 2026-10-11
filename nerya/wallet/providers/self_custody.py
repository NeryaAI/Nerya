"""Self-custody wallet provider.

Balances are read via the connectors layer (``evm_native`` /
``solana_native``). Live swaps are wired for:

* **BSC** — PancakeSwap v2 router via :class:`BSCNative`
  (allowance check + approve + swapExact{ETH,Tokens}{ForTokens,ForETH},
  EIP-155 signing, receipt confirmation).
* **Solana** — Jupiter aggregator via :class:`SolanaNative`
  (v0 tx re-signed locally with Ed25519, signature confirmation).

Other EVM chains have no router wired yet and refuse live swaps with a
clear message instead of silently doing nothing. The private key is
resolved from the workspace ``SecretVault`` (a ``vault://`` reference in
scope ``wallet``) exactly once per execution and scrubbed afterwards —
it is never persisted, logged, or returned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ...connectors.bsc_native import (
    BSCNative,
    BUSD,
    USDC_BEP20,
    USDT_BEP20,
    WBNB,
)
from ...connectors.evm_native import EVM_CHAIN_IDS
from ...core.errors import SecretAccessDenied, SecretNotFoundError, TradingError
from ..amounts import to_base_units, to_base_units_ceil
from ..errors import (
    WalletDependencyError,
    WalletError,
    WalletPolicyDenied,
    WalletQuoteError,
)
from ..protocol import (
    WalletBalance,
    WalletCapabilities,
    WalletCapability,
    WalletProvider,
    WalletQuote,
    WalletReadiness,
    WalletSwapResult,
)

log = logging.getLogger(__name__)


_INSTALL_HINT = (
    "pip install eth-account web3 solders pynacl base58  "
    "# or (preferred) follow https://github.com/goat-sdk/goat to install "
    "the goat SDK for your language and wire it in manually."
)

# Major EVM chains the self-custody provider knows about. Adding a chain
# here is enough to make ``get_balance`` work for it as long as the
# operator supplies an RPC URL via
# ``wallet.self_custody.rpc_urls.<chain>``. Native-symbol map is used to
# label the returned :class:`WalletBalance`.
_EVM_CHAIN_IDS: dict[str, int] = dict(EVM_CHAIN_IDS)

_EVM_NATIVE_SYMBOLS: dict[str, str] = {
    "ethereum": "ETH",
    "bsc": "BNB",
    "polygon": "MATIC",
    "arbitrum": "ETH",
    "optimism": "ETH",
    "avalanche": "AVAX",
    "base": "ETH",
    "linea": "ETH",
    "zksync": "ETH",
    "blast": "ETH",
    "scroll": "ETH",
    "mantle": "MNT",
    "fantom": "FTM",
    "celo": "CELO",
    "gnosis": "XDAI",
    "sepolia": "ETH",
    "base-sepolia": "ETH",
}

#: Live swaps are wired for these chains only. Everything else refuses
#: honestly instead of silently returning ok=False after approval.
_SWAPPABLE_CHAINS: tuple[str, ...] = ("bsc", "solana")

_SOL_MINT = "So11111111111111111111111111111111111111112"  # wrapped SOL mint

# Common BSC symbols -> PancakeSwap addresses (everything else must be a
# 0x contract address supplied by the operator).
_BSC_TOKENS: dict[str, str] = {
    "BNB": WBNB,
    "WBNB": WBNB,
    "USDT": USDT_BEP20,
    "BUSD": BUSD,
    "USDC": USDC_BEP20,
}

_SUPPORTED_CHAINS: tuple[str, ...] = tuple(_EVM_CHAIN_IDS.keys()) + ("solana",)

_CAPABILITIES = WalletCapabilities(
    balance=WalletCapability(
        supported=True, status="experimental",
        note=(
            "Reads chain state via connectors.evm_native / solana_native. "
            "Covered chains: " + ", ".join(_SUPPORTED_CHAINS) + ". "
            "Token decimals are resolved on-chain."
        ),
    ),
    quote=WalletCapability(
        supported=True, status="partial",
        note=(
            "Real router quotes for bsc (PancakeSwap v2 getAmountsOut) and "
            "solana (Jupiter v2 /order, configured API key required). Other chains require an explicit router."
        ),
    ),
    swap=WalletCapability(
        supported=True, status="partial",
        note=(
            "Live swaps wired for bsc (PancakeSwap v2, approve+swap, "
            "receipt-confirmed) and solana (Jupiter, signature-confirmed). "
            "Key resolved from wallet.self_custody.signer_ref (vault://, "
            "scope 'wallet') at execution time and scrubbed after use. "
            "Other EVM chains refuse until an aggregator is wired."
        ),
    ),
    market_data=WalletCapability(supported=False, status="stub"),
    execution_profile="partial",
    chains=_SUPPORTED_CHAINS,
    notes=(
        "Balances are production-grade via connectors. Quote + swap are "
        "real for bsc and solana; other chains are read-only."
    ),
)


@dataclass
class SelfCustodyWallet(WalletProvider):
    id: str = "self_custody"
    label: str = "Self-custody (eth_account / solders / goat-sdk)"
    chains: tuple[str, ...] = _SUPPORTED_CHAINS
    signer_ref: str = ""
    rpc_urls: dict[str, str] | None = None
    workspace: str = ""
    vault_passphrase: str = ""
    #: Optional HTTP transport injection (tests / custom routing). When
    #: None the connectors use their default UrllibHttp.
    transport: Any = None
    config: dict[str, Any] = field(default_factory=dict)

    def _connector_transport(self):
        return self.transport if self.transport is not None else None

    # ------------------------------------------------------------------
    def _probe(self) -> tuple[list[str], list[str]]:
        """Return (found, missing) dependency identifiers."""
        found: list[str] = []
        missing: list[str] = []
        for mod, label in (
            ("goat_sdk", "pip:goat-sdk"),
            ("eth_account", "pip:eth-account"),
            ("web3", "pip:web3"),
            ("solders", "pip:solders"),
            ("nacl", "pip:pynacl"),
            ("base58", "pip:base58"),
        ):
            try:
                __import__(mod)
                found.append(label)
            except Exception:
                missing.append(label)
        return found, missing

    def readiness(self) -> WalletReadiness:
        found, missing = self._probe()
        evm_ok = "pip:eth-account" in found
        sol_ok = "pip:pynacl" in found and "pip:base58" in found
        goat_ok = "pip:goat-sdk" in found
        ready = goat_ok or evm_ok or sol_ok
        reason = ""
        if not ready:
            reason = (
                "install one of: goat-sdk (preferred), eth-account/web3 "
                "(EVM), or solders/solana (Solana)."
            )
        return WalletReadiness(
            provider=self.id,
            ready=ready,
            missing=missing if not ready else [],
            install_hint=_INSTALL_HINT,
            reason=reason,
        )

    def capabilities(self) -> WalletCapabilities:
        from dataclasses import replace
        return replace(_CAPABILITIES,swap_chains=tuple(dict.fromkeys(('bsc','solana',*(self.config.get('dex_routes') or {})))),
                       minimum_output='enforced',receipt_polling=True)

    def _configured_route(self,chain):
        route=(self.config.get('dex_routes') or {}).get(chain)
        if not route:return None
        from .evm_v2 import EvmV2Wallet
        if route.get('type','evm_v2')!='evm_v2':raise WalletPolicyDenied('unknown configured DEX route type')
        adapter=EvmV2Wallet(id=self.id,config={**self.config,**route,'chain':chain},workspace=self.workspace,
            rpc_urls=self.rpc_urls,transport=self.transport,signer_ref=self.signer_ref)
        # Preserve MetaMask seed derivation and the selected vault identity.
        adapter._resolve_signer_key=self._resolve_signer_key
        return adapter

    # ------------------------------------------------------------------
    def get_balance(
        self, *, chain: str, address: str, token: str, **kw: Any,
    ) -> WalletBalance:
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)

        chain_l = chain.lower()
        if chain_l == "solana":
            return self._solana_balance(address=address, token=token)
        return self._evm_balance(chain=chain_l, address=address, token=token, **kw)

    def list_balances(
        self, *, specs: list[dict[str, Any]] | None = None, **_kw: Any,
    ) -> list[WalletBalance]:
        """Walk every operator-configured ``balances`` row in one call.

        Per-row failures are logged (never silent) and skipped so one bad
        row cannot black-hole the whole portfolio; the snapshot caller
        marks the snapshot ``degraded`` based on whether any rows failed.
        """

        out: list[WalletBalance] = []
        if not specs:
            return out
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        failed = 0
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            chain = str(spec.get("chain") or "").lower()
            address = str(spec.get("address") or "").strip()
            token = str(spec.get("token") or "")
            if not chain or not address:
                continue
            try:
                bal = self.get_balance(
                    chain=chain, address=address, token=token,
                )
            except Exception as exc:
                failed += 1
                log.warning(
                    "self_custody list_balances row failed "
                    "(chain=%s token=%s address=%s): %s",
                    chain, token, address, exc,
                )
                continue
            symbol = str(spec.get("symbol") or "").upper() or bal.symbol
            if symbol:
                bal.symbol = symbol
            out.append(bal)
        if failed and not out:
            raise WalletError(
                f"all {failed} balance rows failed (see warnings above)"
            )
        return out

    # ------------------------------------------------------------------
    def quote(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50, **kw: Any,
    ) -> WalletQuote:
        chain_l = (chain or "").lower()
        route=self._configured_route(chain_l)
        if route:
            return route.quote(chain=chain_l,token_in=token_in,token_out=token_out,amount_in=amount_in,slippage_bps=slippage_bps,**kw)
        if chain_l == "bsc":
            return self._bsc_quote(
                token_in=token_in, token_out=token_out,
                amount_in=amount_in, slippage_bps=slippage_bps, **kw,
            )
        if chain_l == "solana":
            return self._solana_quote(
                token_in=token_in, token_out=token_out,
                amount_in=amount_in, slippage_bps=slippage_bps, **kw,
            )
        raise WalletQuoteError(f'No swap router configured for {chain}; choose an aggregator wallet for this chain')

    def swap(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50,
        receiver: str | None = None, live: bool = False,
        min_out: float | None = None, **kw: Any,
    ) -> WalletSwapResult:
        if not live:
            return WalletSwapResult(
                provider=self.id, chain=chain, ok=False,
                reason="live=False; use quote() or enable runtime.live_trading_enabled",
                amount_in=float(amount_in),
            )
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        chain_l = (chain or "").lower()
        route=self._configured_route(chain_l)
        if route:
            return route.swap(chain=chain_l,token_in=token_in,token_out=token_out,amount_in=amount_in,
                slippage_bps=slippage_bps,receiver=receiver,live=live,min_out=min_out,**kw)
        if chain_l not in _SWAPPABLE_CHAINS:
            raise WalletPolicyDenied(
                f"self_custody live swap is wired for {' / '.join(_SWAPPABLE_CHAINS)} "
                f"only; chain={chain!r} has no router configured."
            )
        key = self._resolve_signer_key()
        try:
            if chain_l == "solana":
                return self._solana_swap(
                    key=key, token_in=token_in, token_out=token_out,
                    amount_in=amount_in, slippage_bps=slippage_bps,
                    receiver=receiver, min_out=min_out, **kw,
                )
            return self._bsc_swap(
                key=key, token_in=token_in, token_out=token_out,
                amount_in=amount_in, slippage_bps=slippage_bps,
                receiver=receiver, min_out=min_out, **kw,
            )
        except TradingError as exc:
            # Connector-layer failures (RPC, revert, broadcast) surface as
            # wallet errors, not silent ok=False results.
            raise WalletError(f"self_custody {chain_l} swap failed: {exc}") from exc
        finally:
            key = ""  # scrub the local key reference

    # -------------------------------------------------------------- signer
    def _resolve_vault_secret(self, ref: str) -> str:
        """Resolve one ``vault://`` reference (workspace vault, scope wallet)."""

        r = (ref or "").strip()
        if not r.startswith("vault://"):
            raise WalletPolicyDenied(
                "signer secrets must be vault:// references — plaintext keys "
                "in nerya.yml are rejected for live swaps."
            )
        if not self.workspace:
            raise WalletDependencyError(
                self.id, ["workspace"],
                "self_custody needs a workspace to open the secret vault",
            )
        from pathlib import Path as _Path

        vp = _Path(self.workspace) / "vault" / "secrets.enc"
        if not vp.exists():
            raise WalletDependencyError(
                self.id, ["vault:secrets.enc"],
                f"no vault found at {vp}; store the signing key with "
                "`nerya secrets put <name> --scope wallet` and point the "
                "wallet config at vault://<name>",
            )
        from ...security.secrets import SecretVault

        vault = SecretVault.open(vp, passphrase=self.vault_passphrase or None)
        if vault.load_error:
            raise WalletError(
                f"vault at {vp} could not be decrypted: {vault.load_error}"
            )
        try:
            return vault.resolve(
                r.split("vault://", 1)[-1], required_scope="wallet",
            )
        except SecretNotFoundError as exc:
            raise WalletPolicyDenied(
                f"signer secret not found in vault: {r}"
            ) from exc
        except SecretAccessDenied as exc:
            raise WalletPolicyDenied(
                f"signer secret lacks the 'wallet' scope: {r}"
            ) from exc

    def _resolve_signer_key(self) -> str:
        """Resolve the ``vault://`` signer ref once, at execution time."""
        ref = (self.signer_ref or "").strip()
        if not ref:
            raise WalletPolicyDenied(
                "self_custody swap requires wallet.self_custody.signer_ref "
                "(a vault:// reference to the private key)."
            )
        return self._resolve_vault_secret(ref)

    # -------------------------------------------------------------- BSC
    @staticmethod
    def _resolve_bsc_token(token: str) -> str:
        t = (token or "").strip()
        mapped = _BSC_TOKENS.get(t.upper())
        if mapped:
            return mapped
        if t.lower().startswith("0x") and len(t) == 42:
            return t
        raise WalletPolicyDenied(
            f"cannot resolve BSC token {token!r}: use a symbol in "
            f"{sorted(_BSC_TOKENS)} or a 0x contract address"
        )

    def _bsc_connector(self, *, live: bool) -> BSCNative:
        from ...connectors.dex_base import DEXCredentials

        rpc = (self.rpc_urls or {}).get("bsc", BSCNative.rpc_url)
        kwargs: dict[str, Any] = {}
        transport = self._connector_transport()
        if transport is not None:
            kwargs["transport"] = transport
        return BSCNative(
            chain="bsc", chain_id=_EVM_CHAIN_IDS.get("bsc", 56), rpc_url=rpc,
            live=live,
            credentials=DEXCredentials(rpc_url=rpc, signer_ref=self.signer_ref),
            **kwargs,
        )

    def _bsc_quote(
        self, *, token_in: str, token_out: str, amount_in: float,
        slippage_bps: int, **kw: Any,
    ) -> WalletQuote:
        conn = self._bsc_connector(live=False)
        addr_in = self._resolve_bsc_token(token_in)
        addr_out = self._resolve_bsc_token(token_out)
        try:
            q = conn.quote_swap(
                token_in=addr_in, token_out=addr_out,
                amount_in=float(amount_in), slippage_bps=int(slippage_bps),
            )
        except TradingError as exc:
            raise WalletQuoteError(f"bsc router quote failed: {exc}") from exc
        return WalletQuote(
            provider=self.id, chain=conn.chain,
            token_in=token_in, token_out=token_out,
            amount_in=float(amount_in),
            expected_out=float(q["amount_out"]),
            min_out=float(q["amount_out_min"]),
            slippage_bps=int(slippage_bps),
            gas_cost_usd=0.0,
            extra={
                "router": conn.router, "path": q["path"],
                "amount_out_min_wei": q["amount_out_min_wei"],
                "gas_price_gwei": q["gas_price_gwei"],
                "real_quote": True,
            },
        )

    def _bsc_swap(
        self, *, key: str, token_in: str, token_out: str, amount_in: float,
        slippage_bps: int, receiver: str | None,
        min_out: float | None, **kw: Any,
    ) -> WalletSwapResult:
        from eth_account import Account

        conn = self._bsc_connector(live=True)
        signer_addr = Account.from_key(key).address
        recipient = (receiver or "").strip() or signer_addr
        addr_in = self._resolve_bsc_token(token_in)
        addr_out = self._resolve_bsc_token(token_out)

        quote = conn.quote_swap(
            token_in=addr_in, token_out=addr_out,
            amount_in=float(amount_in), slippage_bps=int(slippage_bps),
        )
        # Honor the approved floor: never send a tx whose amountOutMin is
        # below what the operator approved. Wei stays integer end-to-end;
        # ceil() guarantees the on-chain floor is never below the approval.
        amount_out_min_wei = int(quote["amount_out_min_wei"])
        if min_out is not None and float(min_out) > 0:
            dec_out = conn.get_erc20_decimals(addr_out)
            approved_min_wei = to_base_units_ceil(min_out, dec_out)
            amount_out_min_wei = max(amount_out_min_wei, approved_min_wei)

        # ERC-20 inputs need a router allowance before the swap can move
        # the funds; approve the exact amount when it's short (or when the
        # allowance cannot be read — missing approval fails at broadcast).
        native_symbol=str(self.config.get('native_symbol') or 'BNB').upper()
        is_native_in = token_in.upper() in (native_symbol,'NATIVE')
        is_native_out = token_out.upper() in (native_symbol,'NATIVE')
        dec_out = conn.get_erc20_decimals(addr_out)
        if not is_native_in:
            try:
                allowed = conn.get_erc20_allowance(
                    addr_in, signer_addr, conn.router,
                )
                dec_in = conn.get_erc20_decimals(addr_in)
                allowance_wei = to_base_units(allowed, dec_in)
            except TradingError:
                allowance_wei = 0
            if allowance_wei < int(quote["amount_in_wei"]):
                approval = conn.approve(
                    token=addr_in, spender=conn.router,
                    amount=int(quote["amount_in_wei"]),
                    signer_private_key=key,
                    on_broadcast=kw.get('on_broadcast'),
                )
                if not approval.get('confirmed'):
                    return WalletSwapResult(provider=self.id,chain=conn.chain,ok=False,
                        tx_hash=str(approval.get('tx_hash') or ''),reason='allowance_confirmation_pending',
                        extra={'confirmed':False,'phase':'approval'})

        out = conn.swap(
            token_in=addr_in, token_out=addr_out,
            amount_in=float(amount_in),
            slippage_bps=int(slippage_bps),
            recipient=recipient,
            signer_private_key=key,
            amount_out_min_wei=amount_out_min_wei,
            native_in=is_native_in, native_out=is_native_out,
            on_broadcast=(lambda tx:kw['on_broadcast']({**tx,'token_out':addr_out,'decimals_out':dec_out,
                'receiver':recipient,'native_out':is_native_out})) if kw.get('on_broadcast') else None,
        )
        confirmed = bool(out.get("confirmed"))
        from ..receipts import token_received, native_received
        actual = token_received(out.get('receipt'),addr_out,recipient,dec_out) if not is_native_out else None
        from ..fees import evm_network_fee
        from ...connectors.chains import ChainRegistry
        network_fee = evm_network_fee(out.get('receipt'), str(out.get('tx_hash') or ''), ChainRegistry().get(conn.chain).native_symbol)
        if is_native_out and confirmed:
            actual = native_received(conn,out['tx_hash'],recipient)
        return WalletSwapResult(
            provider=self.id, chain=conn.chain,
            ok=confirmed and actual is not None,
            tx_hash=str(out.get("tx_hash") or ""),
            amount_in=float(amount_in),
            amount_out=float(actual or 0),
            reason="" if confirmed else "broadcast_ok_receipt_pending",
            extra={
                "recipient": recipient, "router": conn.router,
                "path": quote["path"], "nonce": out.get("nonce"),
                "confirmed": confirmed, "real_quote": True,
                'amount_out_source':('transaction_trace' if is_native_out else 'receipt') if actual is not None else 'unknown',
                'expected_out':quote['amount_out'],
                'network_fee': network_fee,
            },
        )

    # -------------------------------------------------------------- Solana
    def _solana_connector(self, *, live: bool):
        from ...connectors.dex_base import DEXCredentials
        from ...connectors.solana_native import SolanaNative

        rpc = (self.rpc_urls or {}).get(
            "solana", "https://api.mainnet-beta.solana.com",
        )
        kwargs: dict[str, Any] = {}
        transport = self._connector_transport()
        if transport is not None:
            kwargs["transport"] = transport
        return SolanaNative(
            chain="solana", rpc_url=rpc, live=live,
            credentials=DEXCredentials(rpc_url=rpc, signer_ref=self.signer_ref),
            jupiter_url=str(self.config.get('jupiter_url') or 'https://api.jup.ag/swap/v2'),
            jupiter_api_key=self._resolve_vault_secret(self.config['jupiter_api_key_ref']) if self.config.get('jupiter_api_key_ref') else '',
            jupiter_exclude_dexes=tuple(self.config.get('jupiter_exclude_dexes') or ()),
            jupiter_max_priority_fee_lamports=int(self.config.get('jupiter_max_priority_fee_lamports', 1_000_000)),
            jupiter_max_total_fee_lamports=int(self.config.get('jupiter_max_total_fee_lamports', 2_000_000)),
            jupiter_max_rent_lamports=int(self.config.get('jupiter_max_rent_lamports', 10_000_000)),
            **kwargs,
        )

    def _solana_quote(
        self, *, token_in: str, token_out: str, amount_in: float,
        slippage_bps: int, **kw: Any,
    ) -> WalletQuote:
        conn = self._solana_connector(live=False)
        mint_in = _SOL_MINT if (token_in or "").upper() in ("", "SOL", "NATIVE") \
            else token_in
        mint_out = _SOL_MINT if (token_out or "").upper() in ("", "SOL", "NATIVE") \
            else token_out
        try:
            dec_in = 9 if mint_in == _SOL_MINT else conn.get_mint_decimals(mint_in)
            # F11: Decimal conversion — float math truncated against the
            # frozen amount (e.g. 8.1 * 1e6 → 8099999).
            amount_in_raw = int(Decimal(str(amount_in)) * (10 ** Decimal(dec_in)))
            doc = conn.quote_jupiter(
                input_mint=mint_in, output_mint=mint_out,
                amount_in_raw=amount_in_raw, slippage_bps=int(slippage_bps),
            )
        except TradingError as exc:
            raise WalletQuoteError(f"solana jupiter quote failed: {exc}") from exc
        out_amount = int(doc.get("outAmount") or 0)
        dec_out = 9 if mint_out == _SOL_MINT else conn.get_mint_decimals(mint_out)
        expected_out = out_amount / (10 ** dec_out)
        if expected_out <= 0:
            raise WalletQuoteError(
                f"jupiter quote has no positive output: outAmount={doc.get('outAmount')!r}"
            )
        gas_cost_usd, gas_budget = 0.0, {}
        if conn.jupiter_url.rstrip('/').endswith('/v2'):
            from ...financial.adapters import usd_price
            try:
                # Public quotes without a taker have no authoritative fee
                # payer. Reserve the reviewed ceilings rather than claiming 0.
                sol_price = usd_price('SOL', transport=self.transport)
                network = Decimal(conn.jupiter_max_total_fee_lamports)/Decimal(10**9)
                rent = Decimal(conn.jupiter_max_rent_lamports)/Decimal(10**9)
                gas_cost_usd = float(network * sol_price)
                gas_budget = {'asset':'NATIVE','amount':str(network+rent),'network_cap_native':str(network),
                              'rent_cap_native':str(rent),'rent_is_refundable_reserve':True,
                              'native_price_usd':str(sol_price),'native_input':mint_in == _SOL_MINT}
            except Exception as exc:
                raise WalletQuoteError('Jupiter fee valuation unavailable; cannot approve a zero-cost quote') from exc
        return WalletQuote(
            provider=self.id, chain="solana",
            token_in=token_in, token_out=token_out,
            amount_in=float(amount_in),
            expected_out=expected_out,
            min_out=int(doc.get('otherAmountThreshold') or int(out_amount*(1-int(slippage_bps)/10000))) / (10**dec_out),
            slippage_bps=int(slippage_bps),
            gas_cost_usd=gas_cost_usd,
            extra={
                "in_amount_raw": doc.get("inAmount"),
                "out_amount_raw": doc.get("outAmount"),
                "price_impact_pct": doc.get("priceImpactPct"),
                "real_quote": True,
                'decimals_in':dec_in,'decimals_out':dec_out,
                'router': doc.get('router', 'jupiter_v1'),
                'route_labels': [step.get('swapInfo', {}).get('label') for step in doc.get('routePlan', [])],
                'fee_bps': doc.get('feeBps'), 'fee_mint': doc.get('feeMint'),
                'gas_budget':gas_budget,
            },
        )

    def _solana_swap(
        self, *, key: str, token_in: str, token_out: str, amount_in: float,
        slippage_bps: int, receiver: str | None,
        min_out: float | None, **kw: Any,
    ) -> WalletSwapResult:
        conn = self._solana_connector(live=True)
        mint_in = _SOL_MINT if (token_in or "").upper() in ("", "SOL", "NATIVE") \
            else token_in
        mint_out = _SOL_MINT if (token_out or "").upper() in ("", "SOL", "NATIVE") \
            else token_out
        dec_in = 9 if mint_in == _SOL_MINT else conn.get_mint_decimals(mint_in)
        # Resolve the output decimals BEFORE broadcasting: losing the
        # signature to a failed read after the fact would orphan a live tx.
        dec_out = 9 if mint_out == _SOL_MINT else conn.get_mint_decimals(mint_out)
        # F11: Decimal conversion — float math truncated against the
        # frozen amount (e.g. 8.1 * 1e6 → 8099999).
        amount_in_raw = int(Decimal(str(amount_in)) * (10 ** Decimal(dec_in)))
        from ...connectors.solana_native import _pubkey_from_signer
        wallet_pubkey = _pubkey_from_signer(key)
        if receiver and receiver.strip() != wallet_pubkey:
            raise WalletPolicyDenied('solana destination addresses are not supported: receiver differs from signing wallet')
        # Quote once, price-check the approved floor BEFORE broadcasting,
        # then execute that exact quote. Jupiter's /swap only carries
        # slippageBps — without this pre-check a quote→broadcast race
        # could fill below the approved floor with nothing on-chain to
        # revert it.
        quote = conn.quote_jupiter(
            input_mint=mint_in, output_mint=mint_out,
            amount_in_raw=amount_in_raw, slippage_bps=int(slippage_bps),
            taker=wallet_pubkey,
        )
        if min_out is not None and float(min_out) > 0:
            expected_out_wei = int(quote.get("outAmount") or 0)
            if expected_out_wei < to_base_units_ceil(min_out, dec_out):
                raise WalletPolicyDenied(
                    f"jupiter quote outAmount {quote.get('outAmount')!r} "
                    f"({expected_out_wei / (10 ** dec_out):.6f} out) is "
                    f"below the approved floor {min_out} — refusing to "
                    "broadcast; request a fresh approval."
                )
            threshold = int(quote.get('otherAmountThreshold') or 0)
            if threshold < to_base_units_ceil(min_out,dec_out):
                raise WalletPolicyDenied('Jupiter executable minimum is below approved floor; request a tighter quote')
        # F12: Jupiter's ``userPublicKey`` must be the signing wallet —
        # it owns the source ATAs and pays the fees. A distinct
        # ``receiver`` destination is not supported on this path yet
        # (the v0 signer would refuse it anyway); refuse loudly here.
        out = conn.swap(
            input_mint=mint_in, output_mint=mint_out,
            amount_in_raw=amount_in_raw,
            signer_private_key=key,
            slippage_bps=int(slippage_bps),
            user_public_key=wallet_pubkey,
            quote=quote,
            on_broadcast=kw.get('on_broadcast'),
        )
        confirmed = bool(out.get("confirmed"))
        actual_out = out.get('amount_out')
        return WalletSwapResult(
            provider=self.id, chain="solana",
            ok=confirmed and actual_out is not None,
            tx_hash=str(out.get("signature") or ""),
            amount_in=float(amount_in),
            amount_out=float(actual_out or 0),
            reason="" if confirmed else "broadcast_ok_confirmation_pending",
            extra={
                "user": out.get("user"), "confirmed": confirmed,
                "confirmation_status": out.get("confirmation_status"),
                "slot": out.get("slot"), "real_quote": True,
                'amount_out_source':'transaction_meta' if actual_out is not None else 'unknown',
                'network_fee': out.get('network_fee'),
            },
        )

    # -------------------------------------------------------------- balances
    def _evm_balance(self, *, chain: str, address: str, token: str,
                      rpc_url: str | None = None, decimals: int | None = None,
                      **_kw) -> WalletBalance:
        rpc = rpc_url or ((self.rpc_urls or {}).get(chain))
        if not rpc:
            raise WalletPolicyDenied(
                f"no rpc_url configured for chain={chain}; set "
                f"wallet.self_custody.rpc_urls.{chain}"
            )
        chain_id = _EVM_CHAIN_IDS.get(chain, 1)
        from ...connectors.dex_base import DEXCredentials
        from ...connectors.evm_native import EVMNative

        kwargs: dict[str, Any] = {}
        transport = self._connector_transport()
        if transport is not None:
            kwargs["transport"] = transport
        conn = EVMNative(chain=chain, chain_id=chain_id, rpc_url=rpc, live=False,
                          credentials=DEXCredentials(rpc_url=rpc, signer_ref=self.signer_ref),
                          **kwargs)
        # Native-token discovery: empty / ``native`` / the chain's own
        # native symbol. A generic symbol list is intentionally NOT used —
        # "ETH" on bsc or "MATIC" on base are ERC-20s there, not natives.
        native_symbol = _EVM_NATIVE_SYMBOLS.get(chain, "NATIVE")
        token_norm = (token or "").strip().upper()
        is_native = (
            token in ("", "native")
            or token_norm in {native_symbol.upper(), "NATIVE"}
        )
        if is_native:
            bal = conn.get_balance(address=address)
            return WalletBalance(provider=self.id, chain=chain, address=address,
                                  token="native", balance=bal, symbol=native_symbol,
                                  decimals=18)
        bal = conn.get_erc20_balance(token=token, address=address)
        try:
            onchain_decimals = conn.get_erc20_decimals(token)
        except (WalletError, TradingError):
            onchain_decimals = int(decimals) if decimals is not None else 18
        return WalletBalance(provider=self.id, chain=chain, address=address,
                              token=token, balance=bal,
                              symbol=token_norm,
                              decimals=onchain_decimals)

    def _solana_balance(self, *, address: str, token: str) -> WalletBalance:
        from ...connectors.dex_base import DEXCredentials
        from ...connectors.solana_native import SolanaNative

        rpc = (self.rpc_urls or {}).get("solana", "https://api.mainnet-beta.solana.com")
        kwargs: dict[str, Any] = {}
        transport = self._connector_transport()
        if transport is not None:
            kwargs["transport"] = transport
        conn = SolanaNative(chain="solana", rpc_url=rpc, live=False,
                             credentials=DEXCredentials(rpc_url=rpc, signer_ref=self.signer_ref),
                             **kwargs)
        if token in ("", "SOL", "native"):
            bal = conn.get_balance(address=address) if hasattr(conn, "get_balance") else 0.0
            return WalletBalance(provider=self.id, chain="solana", address=address,
                                  token="SOL", balance=bal, symbol="SOL", decimals=9)
        bal = conn.get_token_balance(owner=address, mint=token) if hasattr(conn, "get_token_balance") else 0.0
        try:
            dec = conn.get_mint_decimals(token)
        except TradingError:
            dec = 9
        return WalletBalance(provider=self.id, chain="solana", address=address,
                              token=token, balance=bal, decimals=dec)

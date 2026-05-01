"""Self-custody wallet provider.

Preferred backend: `goat-sdk` (https://github.com/goat-sdk/goat). Because
goat ships primarily as a TypeScript package, we lazy-import in this
order:

1. ``goat_sdk`` (hypothetical Python binding / community package).
2. ``eth_account`` + ``web3`` for EVM chains — the most widely-installed
   "just sign this tx" combo, already used elsewhere in Nerya.
3. ``solders`` / ``solana`` for Solana chains.

If none is available we raise :class:`WalletDependencyError` with the
commands the operator should run manually. Nothing in this file performs
the install itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import WalletDependencyError, WalletPolicyDenied
from ..protocol import (
    WalletBalance,
    WalletCapabilities,
    WalletCapability,
    WalletProvider,
    WalletQuote,
    WalletReadiness,
    WalletSwapResult,
)


_INSTALL_HINT = (
    "pip install eth-account web3 solders solana  "
    "# or (preferred) follow https://github.com/goat-sdk/goat to install "
    "the goat SDK for your language and wire it in manually."
)


# Major EVM chains the self-custody provider knows about. Adding a chain
# here is enough to make ``get_balance`` work for it as long as the
# operator supplies an RPC URL via
# ``wallet.self_custody.rpc_urls.<chain>``. Native-symbol map is used to
# label the returned :class:`WalletBalance`.
_EVM_CHAIN_IDS: dict[str, int] = {
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
    "gnosis": "xDAI",
    "sepolia": "ETH",
    "base-sepolia": "ETH",
}


_SUPPORTED_CHAINS: tuple[str, ...] = tuple(_EVM_CHAIN_IDS.keys()) + ("solana",)

# Static capability ceiling. ``self_custody`` routes balances through the
# connectors layer (real) but the quote/swap paths are placeholders that
# do not actually produce executable orders — that requires either a goat
# SDK integration or the agent going through the connectors directly.
_CAPABILITIES = WalletCapabilities(
    balance=WalletCapability(
        supported=True, status="experimental",
        note=(
            "Reads chain state via connectors.evm_native / solana_native. "
            "Covered chains: " + ", ".join(_SUPPORTED_CHAINS) + ". "
            "No automated token-metadata lookup — operator supplies decimals."
        ),
    ),
    quote=WalletCapability(
        supported=True, status="stub",
        note=(
            "Returns amount_in * (1 - slippage). Real quoting requires a "
            "goat-sdk integration or a 1inch/Jupiter skill — not shipped."
        ),
    ),
    swap=WalletCapability(
        supported=True, status="stub",
        note=(
            "swap(live=True) always returns ok=False and points the operator "
            "at the connectors layer; no goat-sdk or native signer is wired."
        ),
    ),
    execution_profile="experimental",
    chains=_SUPPORTED_CHAINS,
    notes=(
        "Balance is production-grade via connectors. Quote + swap are "
        "placeholders until goat-sdk (or an aggregator skill) is wired."
    ),
)


@dataclass
class SelfCustodyWallet(WalletProvider):
    id: str = "self_custody"
    label: str = "Self-custody (goat-sdk / eth_account / solders)"
    chains: tuple[str, ...] = _SUPPORTED_CHAINS
    signer_ref: str = ""
    rpc_urls: dict[str, str] | None = None
    config: dict[str, Any] | None = None

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
        sol_ok = "pip:solders" in found
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
        return _CAPABILITIES

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

        The snapshot loop calls this whenever it's available so we can
        decide *here* how to short-circuit failures (e.g. log per-row
        but keep going) and emit a clean list of :class:`WalletBalance`
        the caller can sum. Keeps ``get_balance()`` as the single-row
        primitive the agent / SDK still uses elsewhere.
        """

        out: list[WalletBalance] = []
        if not specs:
            return out
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
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
            except Exception:
                # Per-row failures shouldn't black-hole the whole
                # portfolio. The snapshot caller marks the snapshot
                # ``degraded`` based on whether any rows succeeded.
                continue
            symbol = str(spec.get("symbol") or "").upper() or bal.symbol
            if symbol:
                bal.symbol = symbol
            out.append(bal)
        return out

    def quote(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50, **kw: Any,
    ) -> WalletQuote:
        # Quoting is off-chain and read-only — we just return a best-effort
        # stub derived from the inputs. Real quoting should come via an
        # aggregator skill (1inch / Jupiter) plugged into the operator's
        # goat-sdk instance.
        expected = float(amount_in) * (1.0 - slippage_bps / 10_000)
        return WalletQuote(
            provider=self.id,
            chain=chain,
            token_in=token_in,
            token_out=token_out,
            amount_in=float(amount_in),
            expected_out=expected,
            min_out=expected * (1.0 - slippage_bps / 10_000),
            slippage_bps=slippage_bps,
            extra={"note": "self_custody quote is a stub; wire goat-sdk or "
                           "1inch/Jupiter for real prices."},
        )

    def swap(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50,
        receiver: str | None = None, live: bool = False, **kw: Any,
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
        if not self.signer_ref:
            raise WalletPolicyDenied(
                "self_custody swap requires wallet.self_custody.signer_ref "
                "(a vault:// reference to the private key)."
            )
        return WalletSwapResult(
            provider=self.id, chain=chain, ok=False,
            reason=(
                "self_custody real swap path is wired to the connectors layer "
                "(BSCNative/EVMNative/SolanaNative). Call the connector "
                "registry directly or enable wallet.self_custody.auto_swap."
            ),
            amount_in=float(amount_in),
            extra={"signer_ref": self.signer_ref},
        )

    # --------------------------------------------------------------
    def _evm_balance(self, *, chain: str, address: str, token: str,
                      rpc_url: str | None = None, decimals: int = 18, **_kw) -> WalletBalance:
        rpc = rpc_url or ((self.rpc_urls or {}).get(chain))
        if not rpc:
            raise WalletPolicyDenied(
                f"no rpc_url configured for chain={chain}; set "
                f"wallet.self_custody.rpc_urls.{chain}"
            )
        chain_id = _EVM_CHAIN_IDS.get(chain, 1)
        from ...connectors.evm_native import EVMNative
        from ...connectors.dex_base import DEXCredentials

        conn = EVMNative(chain=chain, chain_id=chain_id, rpc_url=rpc, live=False,
                          credentials=DEXCredentials(rpc_url=rpc, signer_ref=self.signer_ref))
        # Native-token discovery: empty / ``native`` / a known native
        # symbol matching the chain's native asset.
        native_symbol = _EVM_NATIVE_SYMBOLS.get(chain, "NATIVE")
        token_norm = (token or "").upper()
        is_native = (
            token in ("", "native")
            or token_norm in {native_symbol, "NATIVE"}
            or token_norm in {"ETH", "BNB", "MATIC", "ARB", "BASE",
                              "AVAX", "FTM", "CELO", "MNT", "XDAI"}
        )
        if is_native:
            bal = conn.get_balance(address=address)
            return WalletBalance(provider=self.id, chain=chain, address=address,
                                  token="native", balance=bal, symbol=native_symbol)
        bal = conn.get_erc20_balance(token=token, address=address)
        return WalletBalance(provider=self.id, chain=chain, address=address,
                              token=token, balance=bal, decimals=decimals)

    def _solana_balance(self, *, address: str, token: str) -> WalletBalance:
        from ...connectors.solana_native import SolanaNative
        from ...connectors.dex_base import DEXCredentials

        rpc = (self.rpc_urls or {}).get("solana", "https://api.mainnet-beta.solana.com")
        conn = SolanaNative(chain="solana", rpc_url=rpc, live=False,
                             credentials=DEXCredentials(rpc_url=rpc, signer_ref=self.signer_ref))
        if token in ("", "SOL", "native"):
            bal = conn.get_balance(address=address) if hasattr(conn, "get_balance") else 0.0
            return WalletBalance(provider=self.id, chain="solana", address=address,
                                  token="SOL", balance=bal, symbol="SOL", decimals=9)
        bal = conn.get_token_balance(owner=address, mint=token) if hasattr(conn, "get_token_balance") else 0.0
        return WalletBalance(provider=self.id, chain="solana", address=address,
                              token=token, balance=bal)

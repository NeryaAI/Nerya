"""MetaMask-compatible agent wallet (BIP-39 / BIP-44 self-custody).

A MetaMask account is a standard BIP-39 seed with EVM derivation path
``m/44'/60'/0'/0/<index>``. This provider lets Nerya act as the agent
for such an account:

* **create** — ``nerya wallet create metamask`` generates a fresh
  12-24 word seed, stores it in the workspace SecretVault, prints the
  derived address once, and writes the ``wallet.providers.<id>``
  binding. Importing the same words into MetaMask shows the same
  address (verified against the standard BIP-44 test vectors).
* **import** — paste an existing MetaMask seed phrase or an exported
  account private key into the dashboard wallet form; it is vaultified
  automatically and referenced by ``vault://`` from ``nerya.yml``.
* **use** — balances on every supported EVM chain; live swaps where a
  router is wired (BSC / PancakeSwap v2), signed locally with the
  derived key behind the operator approval gate.

Security model (inherited from ``self_custody``): config only ever
holds ``vault://`` references — the seed/private key is resolved at
execution time and scrubbed afterwards; it is never logged, returned,
or persisted in plaintext. ``address`` in the config is an optional
checksum the operator can pin: readiness fails loudly if the seed no
longer derives it.

Solana accounts are deliberately NOT derived from the seed here
(MetaMask's Solana support uses SLIP-0010 ed25519 paths this provider
does not implement) — use the ``self_custody`` provider with a direct
Solana key for Solana.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...connectors.evm_native import EVM_CHAIN_IDS
from ..errors import (
    WalletDependencyError,
    WalletError,
    WalletPolicyDenied,
)
from ..protocol import WalletCapabilities, WalletCapability, WalletReadiness
from .self_custody import (
    SelfCustodyWallet,
    _INSTALL_HINT,
)

#: Every EVM chain the connectors layer knows; MetaMask covers all of
#: them for reads (swaps still require a wired router).
_METAMASK_CHAINS: tuple[str, ...] = tuple(EVM_CHAIN_IDS.keys())

_DERIVATION_TEMPLATE = "m/44'/60'/0'/0/{index}"


def _ensure_hdwallet_features() -> None:
    try:
        from eth_account import Account

        Account.enable_unaudited_hdwallet_features()
    except Exception as exc:  # pragma: no cover - eth_account missing
        raise WalletDependencyError(
            "metamask", ["pip:eth-account"],
            "pip install eth-account",
        ) from exc


def generate_seed(words: int = 12) -> str:
    """Generate a fresh BIP-39 mnemonic (crypto-grade entropy)."""
    if words not in (12, 15, 18, 21, 24):
        raise ValueError("words must be one of 12/15/18/21/24")
    _ensure_hdwallet_features()
    from eth_account.hdaccount import generate_mnemonic as _gen

    try:
        from eth_account.hdaccount import Language

        return str(_gen(num_words=words, lang=Language.ENGLISH))
    except (ImportError, AttributeError):
        # Older eth_account without the Language enum.
        return str(_gen(num_words=words, lang="english"))


def derive_address(seed: str, address_index: int = 0) -> str:
    """Derive the MetaMask-visible address for a seed at ``address_index``.

    Raises :class:`WalletError` on an invalid checksum / phrase so bad
    input never silently produces a different wallet.
    """
    _ensure_hdwallet_features()
    from eth_account import Account

    try:
        acct = Account.from_mnemonic(
            str(seed).strip(),
            account_path=_DERIVATION_TEMPLATE.format(index=int(address_index)),
        )
    except Exception as exc:
        raise WalletError(
            f"invalid seed phrase or derivation path: {exc}"
        ) from exc
    return acct.address


_CAPABILITIES = WalletCapabilities(
    balance=WalletCapability(
        supported=True, status="experimental",
        note=(
            "Reads EVM chain state via connectors.evm_native for the "
            "seed-derived account. Chains: " + ", ".join(_METAMASK_CHAINS)
            + ". Token decimals resolved on-chain."
        ),
    ),
    quote=WalletCapability(
        supported=True, status="partial",
        note=(
            "Real router quotes for bsc (PancakeSwap v2 getAmountsOut); "
            "other chains return a clearly-marked synthetic placeholder."
        ),
    ),
    swap=WalletCapability(
        supported=True, status="partial",
        note=(
            "Live swaps wired for bsc (PancakeSwap v2, approve+swap, "
            "receipt-confirmed), signed with the seed-derived key behind "
            "the operator approval gate. Other EVM chains refuse until a "
            "router is wired. Solana is not derivable here (SLIP-0010)."
        ),
    ),
    market_data=WalletCapability(supported=False, status="stub"),
    execution_profile="partial",
    chains=_METAMASK_CHAINS,
    notes=(
        "The seed never leaves the vault: config carries vault:// refs "
        "only, and the derived signing key is resolved per execution."
    ),
)


@dataclass
class MetaMaskWallet(SelfCustodyWallet):
    """Agent wallet backed by a MetaMask-compatible BIP-44 account."""

    id: str = "metamask"
    label: str = "MetaMask agent wallet (BIP-44 self-custody)"
    chains: tuple[str, ...] = _METAMASK_CHAINS
    #: vault:// reference to the BIP-39 seed phrase (preferred mode).
    seed_ref: str = ""
    #: vault:// reference to a raw exported private key (alternative).
    private_key_ref: str = ""
    #: BIP-44 account index: m/44'/60'/0'/0/<address_index>.
    address_index: int = 0
    #: Optional operator-pinned checksum address; readiness fails loudly
    #: when the seed no longer derives it.
    expected_address: str = ""

    # ------------------------------------------------------------------
    def capabilities(self) -> WalletCapabilities:
        return _CAPABILITIES

    def readiness(self) -> WalletReadiness:
        base = super().readiness()
        if not base.ready:
            return base
        mismatch = self._address_mismatch()
        if mismatch:
            return WalletReadiness(
                provider=self.id, ready=False, missing=[],
                install_hint=_INSTALL_HINT, reason=mismatch,
            )
        has_ref = any(
            (getattr(self, f) or "").strip()
            for f in ("seed_ref", "private_key_ref", "signer_ref")
        )
        ready = WalletReadiness(
            provider=self.id, ready=True, missing=[],
            install_hint=_INSTALL_HINT,
        )
        if not has_ref:
            ready.reason = (
                "read-only: no seed_ref / private_key_ref configured — "
                "create one with `nerya wallet create metamask` or import "
                "an existing seed to enable swaps."
            )
        return ready

    def derived_address(self) -> str | None:
        """The address this wallet would sign with (seed mode only).

        Returns ``None`` when no seed is configured; raises
        :class:`WalletError` when the vault/seed is broken so callers can
        surface the problem instead of guessing.
        """
        seed_ref = (self.seed_ref or "").strip()
        if not seed_ref:
            return None
        seed = self._resolve_vault_secret(seed_ref)
        return derive_address(seed, self.address_index)

    # ------------------------------------------------------------------
    def _require_evm_chain(self, chain: str) -> str:
        chain_l = (chain or "").strip().lower()
        if chain_l == "solana":
            raise WalletPolicyDenied(
                "MetaMask agent wallet manages EVM accounts only "
                "(m/44'/60'/...); use the self_custody provider for a "
                "Solana key."
            )
        return chain_l

    def get_balance(self, *, chain: str, address: str, token: str, **kw: Any):
        self._require_evm_chain(chain)
        return super().get_balance(chain=chain, address=address,
                                   token=token, **kw)

    def quote(self, *, chain: str, token_in: str, token_out: str,
              amount_in: float, slippage_bps: int = 50, **kw: Any):
        self._require_evm_chain(chain)
        return super().quote(chain=chain, token_in=token_in,
                             token_out=token_out, amount_in=amount_in,
                             slippage_bps=slippage_bps, **kw)

    def swap(self, *, chain: str, token_in: str, token_out: str,
             amount_in: float, slippage_bps: int = 50,
             receiver: str | None = None, live: bool = False,
             min_out: float | None = None, **kw: Any):
        self._require_evm_chain(chain)
        return super().swap(chain=chain, token_in=token_in,
                            token_out=token_out, amount_in=amount_in,
                            slippage_bps=slippage_bps, receiver=receiver,
                            live=live, min_out=min_out, **kw)

    # -------------------------------------------------------------- signer
    def _resolve_signer_key(self) -> str:
        seed_ref = (self.seed_ref or "").strip()
        if seed_ref:
            seed = self._resolve_vault_secret(seed_ref)
            from eth_account import Account

            try:
                acct = Account.from_mnemonic(
                    seed.strip(),
                    account_path=_DERIVATION_TEMPLATE.format(
                        index=int(self.address_index),
                    ),
                )
            except Exception as exc:
                raise WalletError(
                    f"metamask seed derivation failed: {exc}"
                ) from exc
            key = "0x" + acct.key.hex()
            seed = ""  # scrub the local seed reference
            return key
        private_key_ref = (self.private_key_ref or "").strip()
        if private_key_ref:
            return self._resolve_vault_secret(private_key_ref)
        return super()._resolve_signer_key()

    # -------------------------------------------------------------- internals
    def _address_mismatch(self) -> str:
        """Reason string when the seed does not derive the pinned address."""
        expected = (self.expected_address or "").strip()
        seed_ref = (self.seed_ref or "").strip()
        if not expected or not seed_ref:
            return ""
        try:
            derived = self.derived_address()
        except (WalletError, WalletPolicyDenied, WalletDependencyError) as exc:
            return f"seed address could not be verified: {exc}"
        if derived is None:
            return ""
        if derived.lower() != expected.lower():
            return (
                f"configured address {expected!r} does not match the "
                f"address derived from the configured seed "
                f"({derived!r} at index {self.address_index}) — refusing "
                "to operate on the wrong wallet."
            )
        return ""


__all__ = [
    "MetaMaskWallet",
    "generate_seed",
    "derive_address",
]

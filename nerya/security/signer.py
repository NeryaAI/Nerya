"""Signing policy gate — *not* the actual signer.

Audit 2026-04-24 honesty fix
----------------------------
Older drafts of this module contained a placeholder ``Signer`` that
returned ``{"signed": True, "tx_hash_preview": "0x..."}``. Nothing in
``README.md``) still reference this file as "the signer", so we keep
the module name stable but make its contents match runtime truth:

- :class:`SignerPolicy` is a real dataclass; it captures the
  "allowed chain + allowed contract + max notional" gate operators
  configure today through ``runtime.live_trading_enabled`` and the
  per-account allowed-markets policy.
- :class:`Signer` is intentionally a *guard* that raises
  :class:`nerya.core.errors.PolicyDenied`. Real private-key signing
  happens deeper in the stack, in paths that already exist:

  - EVM chains: :mod:`nerya.connectors.evm_native` +
    :mod:`nerya.connectors.bsc_native` (eth_account + web3)
  - Solana:    :mod:`nerya.connectors.solana_native` (solders)
  - CEX REST:  :mod:`nerya.connectors.signing` (HMAC)
  - Operator policies (kill-switch, live flags):
    :mod:`nerya.security.policy_signer` (HMAC)

  Wallet providers (``nerya.wallet.providers.*``) resolve signer
  material through ``vault://`` refs at call time; the agent context
  never sees a raw key.

If a caller really does want a centralised tx-signing facade, they
should build it on top of the per-provider capabilities above and
then register it here. Until that happens, :meth:`Signer.sign_payload`
refuses to return a fabricated signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.errors import PolicyDenied


@dataclass
class SignerPolicy:
    """Policy envelope for any would-be signer.

    ``allowed_chains`` + ``allowed_contracts`` define the safe-list an
    operator is willing to let the agent touch. ``max_value_usd`` caps
    individual tx notional. Today these fields are consulted by the
    trading layer's Risk Gate rather than by a central signer.
    """

    allowed_chains: list[str] = field(default_factory=list)
    allowed_contracts: dict[str, list[str]] = field(default_factory=dict)
    max_value_usd: float = 0.0

    @classmethod
    def default(cls) -> "SignerPolicy":
        return cls(allowed_chains=[], allowed_contracts={}, max_value_usd=0.0)

    def allows(self, *, chain: str, contract: str) -> bool:
        if chain not in self.allowed_chains:
            return False
        contracts = self.allowed_contracts.get(chain) or []
        return contract in contracts


class Signer:
    """Policy gate. Never forges a signature.

    This class exists so the doc promise "the agent calls the signer"
    still points at a real symbol. The method :meth:`sign_payload`
    validates policy and then refuses to fabricate a signature,
    directing the caller at the real per-venue signing paths.
    """

    def __init__(self, vault: Any, policy: SignerPolicy | None = None):
        self.vault = vault
        self.policy = policy or SignerPolicy.default()

    def policy_check(
        self,
        *,
        chain: str,
        contract: str,
        intent_id: str | None = None,
    ) -> None:
        """Raise :class:`PolicyDenied` if the call violates policy.

        Used by higher layers (e.g. a future centralised signing facade
        or a test harness) that want to reuse the same gate without
        forging a signature.
        """

        if not intent_id:
            raise PolicyDenied("sign requires a referenced intent_id")
        if chain not in self.policy.allowed_chains:
            raise PolicyDenied(f"chain {chain} not allowed by signer policy")
        contracts = self.policy.allowed_contracts.get(chain) or []
        if contract not in contracts:
            raise PolicyDenied(f"contract {contract} not allowed on {chain}")

    def sign_payload(
        self,
        *,
        chain: str,
        contract: str,
        payload: dict[str, Any],
        intent_id: str | None = None,
    ) -> dict[str, Any]:
        """Refuse to forge a signature. Points at the real signing path.

        Callers who need an actual signature must route through the
        chain-specific connector (``evm_native`` / ``bsc_native`` /
        ``solana_native``) or the CEX signing helper
        (``connectors.signing``), both of which read the private key
        from a ``vault://`` ref at call time and never return it.
        """

        self.policy_check(chain=chain, contract=contract, intent_id=intent_id)
        raise PolicyDenied(
            "security.signer.Signer is a policy gate only; use the "
            "chain-native path (nerya.connectors.evm_native / bsc_native "
            "/ solana_native) or nerya.connectors.signing.okx_sign for "
            "the actual signature. payload kind is intentionally "
            "unexamined here."
        )


__all__ = ["Signer", "SignerPolicy"]

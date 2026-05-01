"""Bitget wallet skill provider.

Wraps the bitget-wallet-skill Node/TS project. The operator must clone
and install it themselves; Nerya only speaks JSON over stdin/stdout.

Repository: https://github.com/bitget-wallet/bitget-wallet-skill (the
ecosystem ships multiple variants — any skill that exposes the
``balance``, ``quote`` and ``swap`` commands over stdin/stdout is
compatible).
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from ._node_skill import NodeSkillRef


# Every method delegates to the operator-installed Node skill over the
# stdin/stdout protocol. Status "real" reflects Nerya's side of the wire —
# the skill itself is a third-party dependency whose maturity the
# operator owns.
_CAPABILITIES = WalletCapabilities(
    balance=WalletCapability(
        supported=True, status="real",
        note="Delegated to the Bitget wallet skill over stdin/stdout.",
    ),
    quote=WalletCapability(
        supported=True, status="real",
        note="Delegated to the Bitget wallet skill.",
    ),
    swap=WalletCapability(
        supported=True, status="real",
        note=(
            "Delegated to the Bitget wallet skill. Requires "
            "runtime.live_trading_enabled=true on Nerya's side."
        ),
    ),
    execution_profile="production",
    chains=(),
    notes=(
        "Production-ready once the operator has `npm install`ed "
        "bitget-wallet-skill and pointed wallet.bitget.skill_path at it."
    ),
)


@dataclass
class BitgetWalletSkill(WalletProvider):
    id: str = "bitget"
    label: str = "Bitget Wallet Skill (Node subprocess)"
    skill_path: str = ""
    entry: str = "dist/nerya.js"
    repo: str = "https://github.com/bitget-wallet/bitget-wallet-skill"
    config: dict[str, Any] = field(default_factory=dict)

    def _ref(self) -> NodeSkillRef:
        return NodeSkillRef(
            id=self.id, label=self.label, repo=self.repo, entry=self.entry,
            package="@bitget/wallet-skill", skill_path=self.skill_path,
        )

    def readiness(self) -> WalletReadiness:
        ref = self._ref()
        ok, missing = ref.skill_ready()
        return WalletReadiness(
            provider=self.id, ready=ok, missing=missing,
            install_hint=ref.install_hint() if not ok else "",
            reason="" if ok else "Bitget wallet skill not installed or not on PATH.",
        )

    def capabilities(self) -> WalletCapabilities:
        return _CAPABILITIES

    # ------------------------------------------------------------------
    def get_balance(
        self, *, chain: str, address: str, token: str, **kw: Any,
    ) -> WalletBalance:
        doc = self._ref().invoke("balance", {
            "chain": chain, "address": address, "token": token, **kw,
        })
        return WalletBalance(
            provider=self.id, chain=chain, address=address, token=token,
            balance=float(doc.get("balance") or 0.0),
            symbol=str(doc.get("symbol") or ""),
            decimals=int(doc.get("decimals") or 18),
        )

    def quote(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50, **kw: Any,
    ) -> WalletQuote:
        doc = self._ref().invoke("quote", {
            "chain": chain, "token_in": token_in, "token_out": token_out,
            "amount_in": float(amount_in), "slippage_bps": slippage_bps, **kw,
        })
        expected = float(doc.get("expected_out") or 0.0)
        return WalletQuote(
            provider=self.id, chain=chain,
            token_in=token_in, token_out=token_out,
            amount_in=float(amount_in),
            expected_out=expected,
            min_out=float(doc.get("min_out") or expected * (1 - slippage_bps / 10_000)),
            slippage_bps=slippage_bps,
            price_impact_bps=int(doc.get("price_impact_bps") or 0),
            gas_cost_usd=float(doc.get("gas_cost_usd") or 0.0),
            extra={"raw": doc},
        )

    def swap(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50,
        receiver: str | None = None, live: bool = False, **kw: Any,
    ) -> WalletSwapResult:
        if not live:
            return WalletSwapResult(
                provider=self.id, chain=chain, ok=False,
                reason="live=False; Bitget skill swap requires runtime.live_trading_enabled",
                amount_in=float(amount_in),
            )
        doc = self._ref().invoke("swap", {
            "chain": chain, "token_in": token_in, "token_out": token_out,
            "amount_in": float(amount_in), "slippage_bps": slippage_bps,
            "receiver": receiver or "", **kw,
        })
        return WalletSwapResult(
            provider=self.id, chain=chain,
            ok=bool(doc.get("ok", True)),
            tx_hash=str(doc.get("tx_hash") or ""),
            amount_in=float(amount_in),
            amount_out=float(doc.get("amount_out") or 0.0),
            reason=str(doc.get("reason") or ""),
            extra={"raw": doc},
        )

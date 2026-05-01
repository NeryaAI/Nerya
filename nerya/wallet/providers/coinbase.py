"""Coinbase wallet provider.

Supports two installation paths, whichever the operator has on their box.
Neither is auto-installed by Nerya:

1. **Python CDP SDK** (preferred) — ``pip install cdp-sdk`` or the newer
   ``pip install coinbase-agentkit``. When present we use it for balance
   lookups + quote/swap via ``Wallet``.  Docs:
   https://docs.cdp.coinbase.com/
2. **Node CDP skill** (fallback) — point ``wallet.coinbase.skill_path`` at
   a checkout of ``@coinbase/cdp-sdk`` /
   ``@coinbase/coinbase-sdk`` wrapped in a Nerya skill entry (see
   :mod:`nerya.wallet.providers._node_skill` for the wire protocol). This
   mirrors how we invoke other TS wallet libs.

Credentials (API key name + private key) are resolved the same way
everywhere: through ``nerya.yml`` → ``wallet.coinbase.{api_key_ref,
api_private_key_ref}`` → ``SecretVault``. Operators should NOT paste the
raw secret into the config.
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


_PY_PREFERRED = ("cdp", "cdp_sdk", "coinbase_agentkit")


# Static capability ceiling. The provider has three realistic backend
# shapes and this summary reflects the best-case mix:
#
# - ``cdp-sdk``: balance + swap are real (``Wallet.trade``); quote is a
#   synthetic placeholder because CDP does not expose a standalone quote.
# - ``coinbase-agentkit``: balance is real; swap is not wired on our side.
# - Node skill (``@coinbase/cdp-sdk``): balance + quote + swap all real,
#   same stdin/stdout protocol as the other Node-backed providers.
_CAPABILITIES = WalletCapabilities(
    balance=WalletCapability(
        supported=True, status="real",
        note=(
            "Real via cdp-sdk / coinbase-agentkit, or the Node "
            "@coinbase/cdp-sdk skill when configured."
        ),
    ),
    quote=WalletCapability(
        supported=True, status="partial",
        note=(
            "Python cdp-sdk has no standalone quote — quote() returns a "
            "synthetic amount_in*(1-slippage) placeholder. The Node skill "
            "path is real; prefer it for accurate pricing."
        ),
    ),
    swap=WalletCapability(
        supported=True, status="partial",
        note=(
            "Real via Python cdp-sdk (Wallet.trade) and via the Node skill. "
            "The coinbase-agentkit-only path is not wired yet and raises "
            "WalletPolicyDenied."
        ),
    ),
    execution_profile="partial",
    chains=("base", "base-sepolia", "ethereum", "ethereum-sepolia"),
    notes=(
        "Use wallet.coinbase with cdp-sdk for real swaps, or the Node "
        "skill for full quote+swap parity. agentkit-only installs are "
        "feature-incomplete on the swap path."
    ),
)


@dataclass
class CoinbaseWallet(WalletProvider):
    id: str = "coinbase"
    label: str = "Coinbase CDP Wallet (cdp-sdk / coinbase-agentkit or Node skill)"

    api_key_name: str = ""
    api_private_key: str = ""
    network_id: str = "base-mainnet"
    # For Node/TS fallback, same shape as bitget/binance_agentic.
    skill_path: str = ""
    entry: str = "dist/index.js"
    repo: str = "https://github.com/coinbase/cdp-sdk  # or coinbase-agentkit"
    config: dict[str, Any] = field(default_factory=dict)

    def _have_creds(self) -> bool:
        return bool(self.api_key_name and self.api_private_key)

    def _probe_py(self) -> str | None:
        for mod in _PY_PREFERRED:
            try:
                __import__(mod)
                return mod
            except Exception:
                continue
        return None

    def _ref(self) -> NodeSkillRef:
        return NodeSkillRef(
            id=self.id,
            label=self.label,
            repo=self.repo,
            entry=self.entry,
            package="@coinbase/cdp-sdk",
            skill_path=self.skill_path,
        )

    # --------------------------------------------------------------
    def readiness(self) -> WalletReadiness:
        py_mod = self._probe_py()
        node_ok, node_missing = (False, [])
        if self.skill_path:
            node_ok, node_missing = self._ref().skill_ready()

        missing: list[str] = []
        if not py_mod:
            missing.append("pip:cdp-sdk (or coinbase-agentkit)")
        if self.skill_path and not node_ok:
            missing.extend(node_missing)
        if not self._have_creds():
            missing.append("cred:api_key_name/api_private_key")

        ready = bool((py_mod or node_ok) and self._have_creds())
        install_hint = (
            "pip install cdp-sdk  # or: pip install coinbase-agentkit. "
            "Then create an API key at https://portal.cdp.coinbase.com/ and "
            "store it via `nerya vault create-secret`, setting "
            "wallet.coinbase.{api_key_name_ref, api_private_key_ref}."
        )
        reason = ""
        if not ready:
            if not (py_mod or node_ok):
                reason = "no python cdp-sdk and no coinbase node skill_path."
            elif not self._have_creds():
                reason = "Coinbase requires api_key_name + api_private_key."
        return WalletReadiness(
            provider=self.id, ready=ready, missing=missing,
            install_hint=install_hint, reason=reason,
        )

    def capabilities(self) -> WalletCapabilities:
        return _CAPABILITIES

    # --------------------------------------------------------------
    def _py_wallet(self):
        """Return a live CDP wallet handle using whichever SDK is installed."""
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        mod = self._probe_py()
        if not mod:
            raise WalletDependencyError(
                self.id, ["pip:cdp-sdk"], r.install_hint,
            )
        if mod in ("cdp", "cdp_sdk"):
            from cdp import Cdp, Wallet  # type: ignore
            Cdp.configure(
                api_key_name=self.api_key_name,
                private_key=self.api_private_key,
            )
            wallet = Wallet.create(network_id=self.network_id)
            return wallet, "cdp"
        if mod == "coinbase_agentkit":
            from coinbase_agentkit import CdpWalletProvider  # type: ignore
            wp = CdpWalletProvider(
                api_key_name=self.api_key_name,
                api_key_private_key=self.api_private_key,
                network_id=self.network_id,
            )
            return wp, "agentkit"
        raise WalletDependencyError(self.id, ["pip:cdp-sdk"], r.install_hint)

    def get_balance(
        self, *, chain: str, address: str, token: str, **kw: Any,
    ) -> WalletBalance:
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)

        if self.skill_path and not self._probe_py():
            doc = self._ref().invoke("balance", {
                "chain": chain, "address": address, "token": token,
            })
            bal = float(doc.get("balance") or 0.0)
            return WalletBalance(
                provider=self.id, chain=chain, address=address, token=token,
                balance=bal, symbol=str(doc.get("symbol") or ""),
                decimals=int(doc.get("decimals") or 18),
            )
        try:
            wallet, kind = self._py_wallet()
            if kind == "cdp":
                ba = wallet.balance(token or "eth")
                return WalletBalance(
                    provider=self.id, chain=chain, address=address,
                    token=token, balance=float(ba), symbol=str(token or "ETH"),
                )
            ba = wallet.get_balance(token or "eth")  # agentkit
            return WalletBalance(
                provider=self.id, chain=chain, address=address,
                token=token, balance=float(ba), symbol=str(token or "ETH"),
            )
        except WalletDependencyError:
            raise
        except Exception as exc:
            raise WalletPolicyDenied(f"coinbase get_balance failed: {exc}")

    def quote(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50, **kw: Any,
    ) -> WalletQuote:
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        if self.skill_path and not self._probe_py():
            doc = self._ref().invoke("quote", {
                "chain": chain, "token_in": token_in, "token_out": token_out,
                "amount_in": amount_in, "slippage_bps": slippage_bps,
            })
            expected = float(doc.get("expected_out") or 0.0)
            return WalletQuote(
                provider=self.id, chain=chain,
                token_in=token_in, token_out=token_out,
                amount_in=float(amount_in),
                expected_out=expected,
                min_out=float(doc.get("min_out") or expected * 0.99),
                slippage_bps=slippage_bps,
                extra={"raw": doc},
            )
        expected = float(amount_in) * (1.0 - slippage_bps / 10_000)
        return WalletQuote(
            provider=self.id, chain=chain,
            token_in=token_in, token_out=token_out,
            amount_in=float(amount_in),
            expected_out=expected,
            min_out=expected * (1.0 - slippage_bps / 10_000),
            slippage_bps=slippage_bps,
            extra={"note": "CDP Python SDK does not expose a standalone "
                            "quote; use Wallet.trade for real pricing."},
        )

    def swap(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50,
        receiver: str | None = None, live: bool = False, **kw: Any,
    ) -> WalletSwapResult:
        if not live:
            return WalletSwapResult(
                provider=self.id, chain=chain, ok=False,
                reason="live=False; enable runtime.live_trading_enabled",
                amount_in=float(amount_in),
            )
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)

        if self.skill_path and not self._probe_py():
            doc = self._ref().invoke("swap", {
                "chain": chain, "token_in": token_in, "token_out": token_out,
                "amount_in": amount_in, "slippage_bps": slippage_bps,
                "receiver": receiver,
            })
            return WalletSwapResult(
                provider=self.id, chain=chain,
                ok=bool(doc.get("ok")),
                tx_hash=str(doc.get("tx_hash") or ""),
                amount_in=float(amount_in),
                amount_out=float(doc.get("amount_out") or 0.0),
                reason=str(doc.get("reason") or ""),
                extra={"raw": doc},
            )
        try:
            wallet, kind = self._py_wallet()
            if kind == "cdp":
                trade = wallet.trade(
                    amount=amount_in, from_asset_id=token_in,
                    to_asset_id=token_out,
                )
                trade.wait()
                return WalletSwapResult(
                    provider=self.id, chain=chain, ok=True,
                    tx_hash=str(getattr(trade, "transaction_hash", "") or ""),
                    amount_in=float(amount_in),
                    amount_out=float(getattr(trade, "to_amount", 0.0) or 0.0),
                    extra={"sdk": "cdp"},
                )
            raise WalletPolicyDenied(
                "coinbase_agentkit swap requires wiring through AgentKit "
                "action provider; use wallet.coinbase with cdp-sdk or the "
                "Node skill for now."
            )
        except WalletDependencyError:
            raise
        except Exception as exc:
            raise WalletPolicyDenied(f"coinbase swap failed: {exc}")

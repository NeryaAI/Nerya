"""OKX On-Chain OS (a.k.a. OKX Web3 DEX API) wallet provider.

Docs: https://www.okx.com/web3/build/docs/waas/introduction

No Python SDK required — we talk to the REST endpoint using the stdlib
transport Nerya already uses (``UrllibHttp``). This provider therefore has
zero hard dependencies; its only ``missing`` signals are credentials.
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


_CAPABILITIES = WalletCapabilities(
    balance=WalletCapability(
        supported=True, status="real",
        note="GET /api/v5/wallet/asset/total-value-by-address.",
    ),
    quote=WalletCapability(
        supported=True, status="real",
        note="GET /api/v5/dex/aggregator/quote.",
    ),
    swap=WalletCapability(
        supported=True, status="partial",
        note=(
            "Returns an unsigned transaction from /api/v5/dex/aggregator/swap. "
            "An operator must broadcast the raw tx via "
            "connectors.evm_native.send_raw_transaction; Nerya does not sign "
            "or broadcast automatically."
        ),
    ),
    execution_profile="partial",
    chains=("ethereum", "bsc", "polygon", "arbitrum", "base", "solana"),
    notes=(
        "Full quote path is production-grade. Swap is quote+unsigned-tx only "
        "until an operator-approved signer pipeline is wired in."
    ),
)


_BASE_URL = "https://www.okx.com"
_QUOTE_PATH = "/api/v5/dex/aggregator/quote"
_SWAP_PATH = "/api/v5/dex/aggregator/swap"
_BALANCE_PATH = "/api/v5/wallet/asset/total-value-by-address"


_CHAIN_IDS = {
    "ethereum": 1, "eth": 1,
    "bsc": 56, "bnb": 56,
    "polygon": 137,
    "arbitrum": 42161,
    "base": 8453,
    "solana": 501,
}


@dataclass
class OkxOsWallet(WalletProvider):
    id: str = "okx_os"
    label: str = "OKX On-Chain OS (Web3 DEX API)"
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    api_project_id: str = ""
    base_url: str = _BASE_URL
    config: dict[str, Any] = field(default_factory=dict)

    def _have_creds(self) -> bool:
        return all([self.api_key, self.api_secret, self.api_passphrase])

    def readiness(self) -> WalletReadiness:
        if self._have_creds():
            return WalletReadiness(provider=self.id, ready=True)
        missing = [
            m for m in [
                "cred:api_key" if not self.api_key else "",
                "cred:api_secret" if not self.api_secret else "",
                "cred:api_passphrase" if not self.api_passphrase else "",
            ] if m
        ]
        return WalletReadiness(
            provider=self.id,
            ready=False,
            missing=missing,
            install_hint=(
                "configure wallet.okx_os.{api_key_ref,api_secret_ref,"
                "api_passphrase_ref} in nerya.yml (pointing at vault:// refs) "
                "and store the real values via `nerya vault create-secret`."
            ),
            reason="OKX OS requires API key + secret + passphrase.",
        )

    def capabilities(self) -> WalletCapabilities:
        return _CAPABILITIES

    # ------------------------------------------------------------------
    def _signed_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        from ...connectors.http import UrllibHttp
        from ...connectors.signing import okx_sign
        from urllib.parse import urlencode

        qs = urlencode(params, doseq=True)
        full_path = f"{path}?{qs}" if qs else path
        headers, _ = okx_sign(self.api_key, self.api_secret, self.api_passphrase,
                               method="GET", path=full_path, body=None)
        if self.api_project_id:
            headers["OK-ACCESS-PROJECT"] = self.api_project_id
        transport = UrllibHttp()
        status, doc = transport.request(
            "GET", f"{self.base_url}{full_path}",
            headers=headers, timeout=20.0,
        )
        if status >= 400:
            raise WalletPolicyDenied(
                f"OKX OS {path} returned {status}: {doc}"
            )
        return doc if isinstance(doc, dict) else {"raw": doc}

    def _chain_index(self, chain: str) -> int:
        idx = _CHAIN_IDS.get((chain or "").lower())
        if not idx:
            raise WalletPolicyDenied(f"OKX OS: unsupported chain {chain!r}")
        return idx

    # ------------------------------------------------------------------
    def get_balance(
        self, *, chain: str, address: str, token: str, **kw: Any,
    ) -> WalletBalance:
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        doc = self._signed_get(_BALANCE_PATH, {
            "address": address, "chains": str(self._chain_index(chain)),
        })
        total = 0.0
        try:
            total = float((doc.get("data") or [{}])[0].get("totalValue") or 0.0)
        except Exception:
            total = 0.0
        return WalletBalance(
            provider=self.id, chain=chain, address=address, token=token,
            balance=total, symbol="USD", decimals=2,
        )

    def quote(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50, **kw: Any,
    ) -> WalletQuote:
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        doc = self._signed_get(_QUOTE_PATH, {
            "chainId": self._chain_index(chain),
            "fromTokenAddress": token_in,
            "toTokenAddress": token_out,
            "amount": str(int(float(amount_in) * 10 ** int(kw.get("decimals_in") or 18))),
            "slippage": str(slippage_bps / 10_000),
        })
        data = (doc.get("data") or [{}])[0]
        expected = float(data.get("toTokenAmount") or 0) / 10 ** int(kw.get("decimals_out") or 18)
        return WalletQuote(
            provider=self.id, chain=chain,
            token_in=token_in, token_out=token_out,
            amount_in=float(amount_in),
            expected_out=expected,
            min_out=expected * (1.0 - slippage_bps / 10_000),
            slippage_bps=slippage_bps,
            extra={"raw": data},
        )

    def swap(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50,
        receiver: str | None = None, live: bool = False, **kw: Any,
    ) -> WalletSwapResult:
        if not live:
            return WalletSwapResult(
                provider=self.id, chain=chain, ok=False,
                reason="live=False; OKX OS swap requires runtime.live_trading_enabled",
                amount_in=float(amount_in),
            )
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        if not receiver:
            raise WalletPolicyDenied("OKX OS swap requires a receiver address")
        doc = self._signed_get(_SWAP_PATH, {
            "chainId": self._chain_index(chain),
            "fromTokenAddress": token_in,
            "toTokenAddress": token_out,
            "amount": str(int(float(amount_in) * 10 ** int(kw.get("decimals_in") or 18))),
            "slippage": str(slippage_bps / 10_000),
            "userWalletAddress": receiver,
        })
        data = (doc.get("data") or [{}])[0]
        tx = data.get("tx") or {}
        return WalletSwapResult(
            provider=self.id, chain=chain,
            ok=bool(data),
            tx_hash=tx.get("hash") or "",
            amount_in=float(amount_in),
            amount_out=float(data.get("toTokenAmount") or 0)
                       / 10 ** int(kw.get("decimals_out") or 18),
            extra={"tx_unsigned": tx, "raw": data,
                    "note": "OKX OS returns an unsigned tx; broadcast via "
                            "connectors.evm_native.send_raw_transaction once "
                            "the signer policy approves it."},
        )

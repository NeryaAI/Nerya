"""Binance-Web3 agentic wallet provider.

Wraps the upstream ``@binance/agentic-wallet`` CLI package. Source-checkout
installs from ``binance-skills-hub`` are still accepted when operators point
``wallet.binance_agentic.skill_path`` at that directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import WalletPolicyDenied
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


_CAPABILITIES = WalletCapabilities(
    balance=WalletCapability(
        supported=True, status="real",
        note="Delegated to the binance-agentic-wallet skill over stdin/stdout.",
    ),
    quote=WalletCapability(
        supported=True, status="real",
        note="Delegated to the binance-agentic-wallet skill.",
    ),
    swap=WalletCapability(
        supported=True, status="real",
        note=(
            "Delegated to the binance-agentic-wallet skill. Requires "
            "runtime.live_trading_enabled=true on Nerya's side."
        ),
    ),
    market_data=WalletCapability(
        supported=True,
        status="real",
        note="GET /bapi/defi/v1/public/alpha-trade/klines for Binance Alpha tokens.",
    ),
    execution_profile="production",
    chains=(),
    notes=(
        "Production-ready once the operator has `npm install`ed the "
        "binance-agentic-wallet skill from binance-skills-hub."
    ),
)


_ALPHA_KLINES_URL = "https://www.binance.com/bapi/defi/v1/public/alpha-trade/klines"
_ALPHA_INTERVALS = {
    "1s", "15s", "30s", "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M",
}


@dataclass
class BinanceAgenticWallet(WalletProvider):
    id: str = "binance_agentic"
    label: str = "Binance Agentic Wallet (binance-web3 skill)"
    skill_path: str = ""
    entry: str = "dist/index.js"
    repo: str = (
        "https://github.com/binance/binance-skills-hub "
        "# subdir: skills/binance-web3/binance-agentic-wallet"
    )
    config: dict[str, Any] = field(default_factory=dict)

    def _ref(self) -> NodeSkillRef:
        return NodeSkillRef(
            id=self.id, label=self.label, repo=self.repo, entry=self.entry,
            package="@binance/agentic-wallet", skill_path=self.skill_path,
        )

    def readiness(self) -> WalletReadiness:
        ref = self._ref()
        ok, missing = ref.skill_ready()
        return WalletReadiness(
            provider=self.id, ready=ok, missing=missing,
            install_hint=ref.install_hint() if not ok else "",
            reason="" if ok else "binance-agentic-wallet skill not installed.",
        )

    def capabilities(self) -> WalletCapabilities:
        return _CAPABILITIES

    # ------------------------------------------------------------------
    def get_market_klines(
        self,
        *,
        market: str,
        interval: str = "1h",
        limit: int = 100,
        **_kw: Any,
    ) -> list[dict[str, Any]]:
        """Fetch Binance Alpha token candlesticks.

        ``market`` is the Binance Alpha symbol such as ``ALPHA_175USDT``.
        """

        symbol = str(market or "").strip().upper()
        if not symbol:
            raise WalletPolicyDenied("Binance Alpha market data requires symbol")
        interval_s = str(interval or "1h")
        if interval_s not in _ALPHA_INTERVALS:
            interval_s = "1h"
        from urllib.parse import urlencode

        from ...connectors.http import UrllibHttp

        url = _ALPHA_KLINES_URL + "?" + urlencode({
            "symbol": symbol,
            "interval": interval_s,
            "limit": str(max(1, min(int(limit or 100), 1500))),
        })
        status, doc = UrllibHttp().request("GET", url, timeout=20.0)
        if status >= 400:
            raise WalletPolicyDenied(f"Binance Alpha klines returned {status}: {doc}")
        rows = doc.get("data") if isinstance(doc, dict) else []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            try:
                if isinstance(row, dict):
                    ts = row.get("openTime") or row.get("time") or row.get("t")
                    o = row.get("open") or row.get("o")
                    h = row.get("high") or row.get("h")
                    lo = row.get("low") or row.get("l")
                    c = row.get("close") or row.get("c")
                    v = row.get("volume") or row.get("v")
                elif isinstance(row, (list, tuple)) and len(row) >= 6:
                    ts, o, h, lo, c, v = row[:6]
                else:
                    continue
                ts_i = int(float(ts))
                if ts_i > 1_000_000_000_000:
                    ts_i //= 1000
                out.append({
                    "ts": ts_i,
                    "open": float(o),
                    "high": float(h),
                    "low": float(lo),
                    "close": float(c),
                    "volume": float(v or 0),
                })
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda r: r["ts"])
        return out

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
                reason="live=False; Binance agentic swap requires runtime.live_trading_enabled",
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

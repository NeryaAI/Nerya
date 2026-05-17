"""OKX Agentic Wallet / Onchain OS wallet provider.

Docs:
https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet

The official Agentic Wallet quick-start login is email + verification
code through the OnchainOS CLI. Nerya's direct quote/swap/K-line methods
still call OKX Web3 Open API endpoints, so those API-backed methods need
API key + secret + passphrase configured as vault refs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
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
    market_data=WalletCapability(
        supported=True, status="real",
        note="GET /api/v6/dex/market/candles for token OHLCV.",
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
_MARKET_CANDLES_PATH = "/api/v6/dex/market/candles"


_CHAIN_IDS = {
    "ethereum": 1, "eth": 1,
    "bsc": 56, "bnb": 56,
    "polygon": 137,
    "arbitrum": 42161,
    "base": 8453,
    "solana": 501,
}

_OKX_BARS = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "6h": "6H",
    "12h": "12H",
    "1d": "1D",
    "day": "1D",
}


@dataclass
class OkxOsWallet(WalletProvider):
    id: str = "okx_os"
    label: str = "OKX Agentic Wallet / Onchain OS"
    account_id: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    api_project_id: str = ""
    base_url: str = _BASE_URL
    workspace: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    def _have_creds(self) -> bool:
        return all([self.api_key, self.api_secret, self.api_passphrase])

    def _onchainos_bin(self) -> str | None:
        configured = str(
            self.config.get("onchainos_bin")
            or self.config.get("binary_path")
            or ""
        ).strip()
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured))
        if self.workspace:
            exe = "onchainos.exe" if os.name == "nt" else "onchainos"
            candidates.append(Path(self.workspace) / "skills" / "_bin" / "onchainos" / exe)
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return shutil.which("onchainos")

    @staticmethod
    def _extract_json(text: str) -> Any | None:
        cleaned = re.sub(r"\x1b\[[0-9;]*m", "", text or "").strip()
        for idx, ch in enumerate(cleaned):
            if ch not in "[{":
                continue
            try:
                return json.loads(cleaned[idx:])
            except json.JSONDecodeError:
                continue
        return None

    def _run_onchainos(self, args: list[str], *, timeout_s: float = 30.0) -> Any:
        binary = self._onchainos_bin()
        if not binary:
            raise WalletDependencyError(
                self.id,
                ["bin:onchainos"],
                "Install OnchainOS from okx/onchainos-skills, then log in with email OTP.",
            )
        try:
            proc = subprocess.run(
                [binary, *args],
                cwd=self.workspace or None,
                env=os.environ.copy(),
                capture_output=True,
                timeout=timeout_s,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise WalletPolicyDenied(
                f"onchainos {' '.join(args)} timed out after {timeout_s}s"
            ) from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise WalletPolicyDenied(
                f"onchainos {' '.join(args)} exited {proc.returncode}: {err[:512]}"
            )
        parsed = self._extract_json(proc.stdout or "")
        return parsed if parsed is not None else {"raw": proc.stdout}

    def readiness(self) -> WalletReadiness:
        if self._have_creds() or self._onchainos_bin():
            return WalletReadiness(provider=self.id, ready=True)
        missing = [
            "bin:onchainos",
            "login:onchainos wallet login <email>",
        ]
        return WalletReadiness(
            provider=self.id,
            ready=False,
            missing=missing,
            install_hint=(
                "Use `onchainos wallet login <email>` and "
                "`onchainos wallet verify <code>` for Agentic Wallet login. "
                "Advanced Open API keys are optional fallback credentials."
            ),
            reason="OnchainOS CLI is not installed and no advanced Open API fallback is configured.",
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

    def _cli_token_klines(
        self,
        *,
        chain: str,
        token: str,
        interval: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        bar = _OKX_BARS.get(str(interval or "1h").lower(), "1H")
        doc = self._run_onchainos(
            [
                "market",
                "kline",
                "--address",
                token,
                "--chain",
                chain,
                "--bar",
                bar,
                "--limit",
                str(max(1, min(int(limit or 100), 299))),
            ],
            timeout_s=45.0,
        )
        rows: Any = doc
        if isinstance(rows, dict):
            rows = (
                rows.get("data")
                or rows.get("result")
                or rows.get("candles")
                or rows.get("raw")
                or []
            )
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            try:
                if isinstance(row, dict):
                    ts = row.get("ts") or row.get("time") or row.get("timestamp")
                    o = row.get("o") or row.get("open")
                    h = row.get("h") or row.get("high")
                    lo = row.get("l") or row.get("low")
                    c = row.get("c") or row.get("close")
                    v = row.get("vol") or row.get("volume") or row.get("volUsd")
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
                    "volume": float(v or 0.0),
                })
            except Exception:
                continue
        out.sort(key=lambda r_: r_["ts"])
        return out

    def _chain_index(self, chain: str) -> int:
        idx = _CHAIN_IDS.get((chain or "").lower())
        if not idx:
            raise WalletPolicyDenied(f"OKX OS: unsupported chain {chain!r}")
        return idx

    def get_token_klines(
        self,
        *,
        chain: str,
        token: str,
        interval: str = "1h",
        limit: int = 100,
        before: str | None = None,
        after: str | None = None,
        **_kw: Any,
    ) -> list[dict[str, Any]]:
        """Fetch token OHLCV from OKX Onchain OS market data.

        The market API is read-only but still signed by OKX Web3 API
        credentials. ``token`` may be a token or pair contract address;
        callers keep that provider-specific detail in the market id.
        """
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        token_s = str(token or "").strip()
        if not token_s:
            raise WalletPolicyDenied("OKX OS market candles require token")
        if not self._have_creds():
            r = self.readiness()
            if not r.ready:
                raise WalletDependencyError(self.id, r.missing, r.install_hint)
            return self._cli_token_klines(
                chain=chain,
                token=token_s,
                interval=interval,
                limit=limit,
            )
        bar = _OKX_BARS.get(str(interval or "1h").lower(), "1H")
        params: dict[str, Any] = {
            "chainIndex": str(self._chain_index(chain)),
            "tokenContractAddress": token_s,
            "bar": bar,
            "limit": str(max(1, min(int(limit or 100), 299))),
        }
        if before:
            params["before"] = str(before)
        if after:
            params["after"] = str(after)
        doc = self._signed_get(_MARKET_CANDLES_PATH, params)
        data = doc.get("data") or []
        if isinstance(data, dict):
            data = data.get("list") or data.get("candles") or data.get("data") or []
        out: list[dict[str, Any]] = []
        for row in data if isinstance(data, list) else []:
            try:
                if isinstance(row, dict):
                    ts = row.get("ts") or row.get("time") or row.get("timestamp")
                    o = row.get("open")
                    h = row.get("high")
                    lo = row.get("low")
                    c = row.get("close")
                    v = row.get("volume") or row.get("vol")
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
                    "volume": float(v or 0.0),
                })
            except Exception:
                continue
        out.sort(key=lambda r_: r_["ts"])
        return out

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

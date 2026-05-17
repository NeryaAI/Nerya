"""Bitget Wallet Skill / Market API provider.

Bitget's official agent skill is a Python script repository with a
keyless/default-token path. Nerya supports that official script first,
and keeps the older Node-skill adapter only as a compatibility fallback.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import sys
import time
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
    market_data=WalletCapability(
        supported=True, status="partial",
        note=(
            "POST /bgw-pro/market/v3/coin/getKline via Bitget Wallet "
            "Markets API. Direct calls require x-api-key credentials."
        ),
    ),
    execution_profile="partial",
    chains=(),
    notes=(
        "Wallet actions depend on the operator-installed skill adapter. "
        "Market K-lines use the separate developer Market API signature path."
    ),
)


_MARKET_BASE_URL = "https://bopenapi.bgwapi.io"
_MARKET_KLINE_PATH = "/bgw-pro/market/v3/coin/getKline"

_BITGET_CHAINS = {
    "ethereum": "eth",
    "eth": "eth",
    "bsc": "bnb",
    "bnb": "bnb",
    "polygon": "matic",
    "matic": "matic",
    "arbitrum": "arbitrum",
    "base": "base",
    "optimism": "optimism",
    "solana": "sol",
    "sol": "sol",
    "avalanche": "avax_c",
    "avax": "avax_c",
    "ton": "ton",
}

_BITGET_PERIODS = {
    "1s": "1s",
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
    "1w": "1w",
}


@dataclass
class BitgetWalletSkill(WalletProvider):
    id: str = "bitget"
    label: str = "Bitget Wallet Skill / Market API"
    skill_path: str = ""
    entry: str = "scripts/bitget-wallet-agent-api.py"
    repo: str = "https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill"
    market_api_key: str = ""
    market_api_secret: str = ""
    market_base_url: str = _MARKET_BASE_URL
    config: dict[str, Any] = field(default_factory=dict)

    def _ref(self) -> NodeSkillRef:
        return NodeSkillRef(
            id=self.id, label=self.label, repo=self.repo, entry=self.entry,
            package="@bitget/wallet-skill", skill_path=self.skill_path,
        )

    def _script_path(self) -> Path | None:
        if not self.skill_path:
            return None
        entry = self.entry or "scripts/bitget-wallet-agent-api.py"
        return Path(self.skill_path) / entry

    def _python_skill_ready(self) -> tuple[bool, list[str]]:
        missing: list[str] = []
        root = Path(self.skill_path) if self.skill_path else None
        script = self._script_path()
        if not root or not root.exists():
            missing.append(f"skill:{self.repo}")
        if script is None or not script.exists():
            missing.append(f"skill:entry({self.entry or 'scripts/bitget-wallet-agent-api.py'})")
        try:
            import requests  # noqa: F401
        except Exception:
            missing.append("pip:requests")
        return (len(missing) == 0), missing

    def _uses_python_skill(self) -> bool:
        return (self.entry or "").endswith(".py")

    def readiness(self) -> WalletReadiness:
        if self._uses_python_skill():
            ok, missing = self._python_skill_ready()
        else:
            ok, missing = self._ref().skill_ready()
        return WalletReadiness(
            provider=self.id, ready=ok, missing=missing,
            install_hint=self._ref().install_hint() if not ok else "",
            reason="" if ok else "Bitget wallet skill not installed or not ready.",
        )

    def capabilities(self) -> WalletCapabilities:
        return _CAPABILITIES

    # ------------------------------------------------------------------
    def _run_python_skill(self, args: list[str], *, timeout_s: float = 30.0) -> dict[str, Any]:
        ok, missing = self._python_skill_ready()
        if not ok:
            raise WalletDependencyError(self.id, missing, self._ref().install_hint())
        script = self._script_path()
        assert script is not None
        try:
            proc = subprocess.run(
                [sys.executable, str(script), *args],
                cwd=str(Path(self.skill_path)),
                capture_output=True,
                timeout=timeout_s,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise WalletPolicyDenied(f"Bitget wallet skill timed out after {timeout_s}s") from exc
        text = (proc.stdout or "").strip()
        if proc.returncode != 0:
            detail = (proc.stderr or text or f"exit {proc.returncode}")[-800:]
            raise WalletPolicyDenied(f"Bitget wallet skill failed: {detail}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise WalletPolicyDenied(f"Bitget wallet skill returned non-JSON output: {text[-800:]}") from exc

    def _market_headers(self, path: str, body_json: str) -> dict[str, str]:
        if not self.market_api_key or not self.market_api_secret:
            missing = [
                m
                for m in (
                    "cred:market_api_key" if not self.market_api_key else "",
                    "cred:market_api_secret" if not self.market_api_secret else "",
                )
                if m
            ]
            raise WalletDependencyError(
                self.id,
                missing,
                (
                    "Create a Bitget Wallet developer API key at "
                    "https://portal-web3.bitget.com and configure "
                    "wallet.bitget.{market_api_key_ref,market_api_secret_ref}."
                ),
            )
        ts = str(int(time.time() * 1000))
        content = json.dumps(
            {
                "apiPath": path,
                "body": body_json,
                "x-api-key": self.market_api_key,
                "x-api-timestamp": ts,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hmac.new(
            self.market_api_secret.encode("utf-8"),
            content.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return {
            "x-api-key": self.market_api_key,
            "x-api-timestamp": ts,
            "x-api-signature": base64.b64encode(digest).decode("ascii"),
            "Content-Type": "application/json",
        }

    def get_token_klines(
        self,
        *,
        chain: str,
        token: str,
        interval: str = "1h",
        limit: int = 100,
        **_kw: Any,
    ) -> list[dict[str, Any]]:
        """Fetch token OHLCV from Bitget Wallet Markets API."""

        if self._uses_python_skill() and self.skill_path:
            interval_s = _BITGET_PERIODS.get(str(interval or "").strip(), "1h")
            doc = self._run_python_skill(
                [
                    "kline",
                    "--chain", str(chain or ""),
                    "--contract", str(token or ""),
                    "--period", interval_s,
                    "--size", str(max(1, min(int(limit or 100), 1440))),
                ],
                timeout_s=45.0,
            )
            rows = ((doc.get("data") or {}).get("list") if isinstance(doc.get("data"), dict) else None)
            out: list[dict[str, Any]] = []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                try:
                    ts = int(row.get("ts") or 0)
                    if ts and ts < 10_000_000_000:
                        ts *= 1000
                    out.append({
                        "ts": ts,
                        "open": float(row.get("open") or 0),
                        "high": float(row.get("high") or 0),
                        "low": float(row.get("low") or 0),
                        "close": float(row.get("close") or 0),
                        "volume": float(row.get("turnover") or row.get("amount") or 0),
                    })
                except (TypeError, ValueError):
                    continue
            out.sort(key=lambda r: r["ts"])
            return out

        chain_id = _BITGET_CHAINS.get((chain or "").strip().lower())
        if not chain_id:
            raise WalletPolicyDenied(f"Bitget Wallet market data: unsupported chain {chain!r}")
        contract = str(token or "").strip()
        if not contract:
            raise WalletPolicyDenied("Bitget Wallet market data requires token contract")
        body = {
            "chain": chain_id,
            "contract": contract,
            "period": _BITGET_PERIODS.get(str(interval or "1h").lower(), "1h"),
            "size": max(1, min(int(limit or 100), 1440)),
        }
        body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        from ...connectors.http import UrllibHttp

        status, doc = UrllibHttp().request(
            "POST",
            f"{self.market_base_url.rstrip('/')}{_MARKET_KLINE_PATH}",
            headers=self._market_headers(_MARKET_KLINE_PATH, body_json),
            body=body_json.encode("utf-8"),
            timeout=20.0,
        )
        if status >= 400:
            raise WalletPolicyDenied(
                f"Bitget Wallet market data returned {status}: {doc}"
            )
        if isinstance(doc, dict) and doc.get("status") not in (None, 0, "0"):
            raise WalletPolicyDenied(f"Bitget Wallet market data error: {doc}")
        data = (doc.get("data") if isinstance(doc, dict) else {}) or {}
        rows = data.get("list") if isinstance(data, dict) else []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                out.append({
                    "ts": int(float(row.get("ts") or 0)),
                    "open": float(row.get("open") or 0),
                    "high": float(row.get("high") or 0),
                    "low": float(row.get("low") or 0),
                    "close": float(row.get("close") or 0),
                    "volume": float(
                        row.get("turnover")
                        or row.get("amount")
                        or row.get("volume")
                        or 0
                    ),
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

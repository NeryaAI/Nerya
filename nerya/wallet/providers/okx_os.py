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

from ..errors import (
    WalletDependencyError,
    WalletError,
    WalletPolicyDenied,
    WalletQuoteError,
    WalletTransportError,
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


_CAPABILITIES = WalletCapabilities(
    balance=WalletCapability(
        supported=True, status="real",
        note=(
            "Per-token: GET /api/v5/wallet/asset/token-balances. "
            "Portfolio USD value (token usd/total): GET "
            "/api/v5/wallet/asset/total-value-by-address."
        ),
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
_TOKEN_BALANCES_PATH = "/api/v5/wallet/asset/token-balances"
_MARKET_CANDLES_PATH = "/api/v6/dex/market/candles"

# Tokens named "" / "usd" / "total" are portfolio-value requests, not
# per-token balance lookups.
_PORTFOLIO_TOKENS = ("", "usd", "total")


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
            raise WalletTransportError(
                f"onchainos {' '.join(args)} timed out after {timeout_s}s"
            ) from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise WalletTransportError(
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
            # Transport/operational failure, not a policy block: the
            # approval flow classifies these separately from
            # WalletPolicyDenied.
            raise WalletTransportError(
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
            # Readiness was already checked above; fall straight through
            # to the OnchainOS CLI transport.
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
    @staticmethod
    def _token_rows(doc: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten a token-balances response into a list of token rows.

        Handles both the documented ``data[0].tokenAssets`` nesting and a
        flat ``data`` list, depending on API version.
        """
        data = doc.get("data")
        if isinstance(data, dict):
            data = data.get("tokenAssets") or data.get("list") or []
        rows: list[Any] = data if isinstance(data, list) else []
        flat: list[dict[str, Any]] = []
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            nested = entry.get("tokenAssets")
            if isinstance(nested, list):
                flat.extend(r for r in nested if isinstance(r, dict))
            elif (
                "tokenSymbol" in entry
                or "symbol" in entry
                or "balance" in entry
            ):
                flat.append(entry)
        return flat

    def _match_token_row(
        self, doc: dict[str, Any], token_s: str,
    ) -> dict[str, Any] | None:
        needle = token_s.lower()
        for row in self._token_rows(doc):
            sym = str(row.get("tokenSymbol") or row.get("symbol") or "").lower()
            contract = str(
                row.get("tokenContractAddress")
                or row.get("contractAddress")
                or row.get("address")
                or ""
            ).lower()
            if needle and needle in (sym, contract):
                return row
        return None

    def get_balance(
        self, *, chain: str, address: str, token: str, **kw: Any,
    ) -> WalletBalance:
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        token_s = str(token or "").strip()
        if token_s.lower() in _PORTFOLIO_TOKENS:
            # Genuine portfolio-value request: keep the total-value
            # endpoint, but label the result honestly as USD / token=""
            # instead of echoing the requested token.
            doc = self._signed_get(_BALANCE_PATH, {
                "address": address, "chains": str(self._chain_index(chain)),
            })
            total = 0.0
            try:
                total = float((doc.get("data") or [{}])[0].get("totalValue") or 0.0)
            except Exception:
                total = 0.0
            return WalletBalance(
                provider=self.id, chain=chain, address=address, token="",
                balance=total, symbol="USD", decimals=2,
            )
        if not address:
            raise WalletPolicyDenied(
                "OKX OS per-token balance requires a wallet address for the "
                f"token-balances lookup (token={token_s!r})"
            )
        doc = self._signed_get(_TOKEN_BALANCES_PATH, {
            "address": address,
            "chainIndex": str(self._chain_index(chain)),
        })
        row = self._match_token_row(doc, token_s)
        if row is None:
            raise WalletError(
                f"OKX OS: token {token_s!r} not found in token balances for "
                f"{address} on chain {chain!r}"
            )
        symbol = str(row.get("tokenSymbol") or row.get("symbol") or token_s)
        try:
            decimals = int(row.get("decimals") or 0)
        except (TypeError, ValueError):
            decimals = 0
        balance: float | None = None
        for key in ("uiAmount", "ui_amount", "uiAmountString"):
            if row.get(key) is None:
                continue
            try:
                balance = float(row[key])
                break
            except (TypeError, ValueError):
                continue
        if balance is None:
            # The token-balances endpoint reports `balance` in raw base
            # units; convert with the row's own decimals.
            try:
                raw = float(row.get("balance") or row.get("amount") or 0.0)
            except (TypeError, ValueError):
                raw = 0.0
            balance = raw / (10 ** max(decimals, 0))
        return WalletBalance(
            provider=self.id, chain=chain, address=address, token=token_s,
            balance=balance, symbol=symbol, decimals=decimals,
        )

    @staticmethod
    def _decimals_kw(kw: dict[str, Any], name: str) -> tuple[int, bool]:
        """Parse a ``decimals_in`` / ``decimals_out`` kwarg.

        Returns ``(value, assumed)``. When the caller omits the kwarg the
        EVM default of 18 is applied and ``assumed`` is True so callers
        can surface the assumption via ``extra["decimals_assumed"]``.
        Explicit ``0`` is honoured (the previous ``or 18`` silently
        turned 0-decimal tokens into 18-decimal ones).
        """
        raw = kw.get(name)
        if raw is None:
            return 18, True
        try:
            val = int(raw)
        except (TypeError, ValueError) as exc:
            raise WalletError(
                f"OKX OS: {name} must be an integer, got {raw!r}"
            ) from exc
        if val < 0:
            raise WalletError(f"OKX OS: {name} must be >= 0, got {val}")
        return val, False

    def quote(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50, **kw: Any,
    ) -> WalletQuote:
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        dec_in, dec_in_assumed = self._decimals_kw(kw, "decimals_in")
        dec_out, dec_out_assumed = self._decimals_kw(kw, "decimals_out")
        doc = self._signed_get(_QUOTE_PATH, {
            "chainId": self._chain_index(chain),
            "fromTokenAddress": token_in,
            "toTokenAddress": token_out,
            "amount": str(int(float(amount_in) * 10 ** dec_in)),
            "slippage": str(slippage_bps / 10_000),
        })
        data = (doc.get("data") or [{}])[0]
        try:
            expected = float(data.get("toTokenAmount") or 0) / 10 ** dec_out
        except (TypeError, ValueError):
            expected = 0.0
        if not expected > 0:
            # An unparseable or zero-output quote must never freeze an
            # approval with a meaningless floor.
            raise WalletQuoteError(
                f"OKX OS quote returned no positive output amount for "
                f"{token_in} -> {token_out}: {str(data)[:256]}"
            )
        extra: dict[str, Any] = {"raw": data}
        if dec_in_assumed or dec_out_assumed:
            # No on-chain decimals lookup here by design; tell downstream
            # that 18 was assumed rather than fetched.
            extra["decimals_assumed"] = 18
        return WalletQuote(
            provider=self.id, chain=chain,
            token_in=token_in, token_out=token_out,
            amount_in=float(amount_in),
            expected_out=expected,
            min_out=expected * (1.0 - slippage_bps / 10_000),
            slippage_bps=slippage_bps,
            extra=extra,
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
        dec_in, _ = self._decimals_kw(kw, "decimals_in")
        dec_out, dec_out_assumed = self._decimals_kw(kw, "decimals_out")
        doc = self._signed_get(_SWAP_PATH, {
            "chainId": self._chain_index(chain),
            "fromTokenAddress": token_in,
            "toTokenAddress": token_out,
            "amount": str(int(float(amount_in) * 10 ** dec_in)),
            "slippage": str(slippage_bps / 10_000),
            "userWalletAddress": receiver,
        })
        data = (doc.get("data") or [{}])[0]
        tx = data.get("tx") or {}
        # Only a real broadcast produces a tx hash. The aggregator's `tx`
        # object (data/to/gasPrice) is unsigned calldata, not a receipt —
        # never report ok=True for it.
        tx_hash = str(data.get("tx_hash") or tx.get("hash") or "")
        amount_out = float(data.get("toTokenAmount") or 0) / 10 ** dec_out
        extra: dict[str, Any] = {
            "unsigned_tx": tx,
            "raw": data,
            "note": "quote/swap assembled but not broadcast — requires a signer",
        }
        if dec_out_assumed:
            extra["decimals_assumed"] = 18
        min_out_requested = kw.get("min_out")
        if min_out_requested is not None:
            try:
                extra["min_out_requested"] = float(min_out_requested)
            except (TypeError, ValueError):
                pass
        # The OKX aggregator swap endpoint expresses only `slippage`; it
        # has no minOut parameter. Record the approved floor and compute
        # the honest expected floor from the returned quote instead of
        # pretending the API enforced it.
        extra["amount_out_min"] = amount_out * (1.0 - slippage_bps / 10_000)
        return WalletSwapResult(
            provider=self.id, chain=chain,
            ok=bool(tx_hash),
            tx_hash=tx_hash,
            amount_in=float(amount_in),
            amount_out=amount_out,
            reason=(
                "" if tx_hash
                else "unsigned tx only — OKX OS returned no broadcast hash; "
                     "an approved signer must broadcast via "
                     "connectors.evm_native.send_raw_transaction"
            ),
            extra=extra,
        )

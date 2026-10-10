"""OKX Agentic Wallet / Onchain OS wallet provider.

Docs:
https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet

The installed CLI uses browser login followed by polling the original
authSessionId. Candle reads can use that CLI session. Direct signed
balance/quote/swap APIs need key + secret + passphrase as vault refs.
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

from ..amounts import to_base_units
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
        note="GET /api/v6/dex/aggregator/quote with resolved token decimals.",
    ),
    swap=WalletCapability(
        supported=True, status="partial",
        note=(
            "Builds exact-input swaps with the v6 aggregator. With signer_ref and chain RPC, "
            "signs, broadcasts and reads transaction receipts after operator approval."
        ),
    ),
    market_data=WalletCapability(
        supported=True, status="real",
        note="GET /api/v6/dex/market/candles for token OHLCV.",
    ),
    execution_profile="partial",
    chains=("ethereum", "bsc", "polygon", "arbitrum", "base", "solana"),
    notes=(
        "Read readiness is separate from signer/RPC readiness. Without a signer, swap remains unsigned."
    ),
)


_BASE_URL = "https://web3.okx.com"
_QUOTE_PATH = "/api/v6/dex/aggregator/quote"
_SWAP_PATH = "/api/v6/dex/aggregator/swap"
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
            if candidate.is_file() and os.access(candidate, os.X_OK):
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
        if isinstance(parsed, dict) and (parsed.get("ok") is False or parsed.get("success") is False):
            raise WalletTransportError(f"onchainos request rejected: {parsed.get('error') or 'provider_error'}")
        if parsed is None:
            raise WalletTransportError("onchainos returned invalid JSON")
        return parsed

    def readiness(self) -> WalletReadiness:
        if self._have_creds():
            return WalletReadiness(provider=self.id, ready=True)
        if self._onchainos_bin():
            try:
                status = self._run_onchainos(["wallet", "status"], timeout_s=5.0)
                data = status.get("data") if isinstance(status, dict) else None
                if isinstance(data, dict) and status.get("ok") is True and data.get("loggedIn") is True:
                    return WalletReadiness(provider=self.id, ready=True,
                                           reason="CLI login authenticated for OnchainOS reads; signed balance/quote/swap APIs still require advanced API credentials.")
            except (WalletTransportError, OSError):
                return WalletReadiness(provider=self.id, ready=False, missing=["status:onchainos"],
                                       reason="OnchainOS login status could not be verified; inspect wallet_auth status before retrying.")
            return WalletReadiness(
                provider=self.id, ready=False, missing=["login:onchainos"],
                install_hint="Use wallet_auth start, complete the browser login, then wallet_auth verify with the original session. CLI: onchainos wallet login --phase init; onchainos wallet login --phase poll --session-id <authSessionId>.",
                reason="OnchainOS CLI is installed but its login session is not authenticated.",
            )
        missing = [
            "bin:onchainos",
            "login:onchainos",
        ]
        return WalletReadiness(
            provider=self.id,
            ready=False,
            missing=missing,
            install_hint=(
                "Use `onchainos wallet login --phase init` and "
                "`onchainos wallet login --phase poll --session-id <authSessionId>` for Agentic Wallet login. "
                "Advanced Open API keys are optional fallback credentials."
            ),
            reason="OnchainOS CLI is not installed and no advanced Open API fallback is configured.",
        )

    def capabilities(self) -> WalletCapabilities:
        from dataclasses import replace
        return replace(_CAPABILITIES,swap_chains=_CAPABILITIES.chains,minimum_output='enforced',receipt_polling=True)

    # ------------------------------------------------------------------
    def _signed_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._have_creds():
            raise WalletDependencyError(self.id, ["cred:api_key/api_secret/api_passphrase"],
                                        "Configure the advanced Web3 API credentials in vault references for signed balance/quote/swap APIs. Browser login only enables CLI reads; do not reinstall or repeat login.")
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
        if isinstance(doc,dict) and str(doc.get('code','0')) != '0':
            raise WalletQuoteError(f"OKX API error {doc.get('code')}: {doc.get('msg','request rejected')}")
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

    def _decimals(self, chain, token, kw, name):
        from ...connectors.evm_native import EVMNative, EVM_CHAIN_IDS
        from ...connectors.solana_native import SolanaNative
        if name in kw and kw[name] is not None:
            value = int(kw[name])
            if not 0 <= value <= 36:
                raise WalletQuoteError("token decimals must be in [0,36]")
            return value
        if token.lower() in ('native','eth','bnb','sol','0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'):
            return 9 if chain.lower() in ('sol','solana') else 18
        if token == 'So11111111111111111111111111111111111111112':
            return 9
        rpc = (self.config.get('rpc_urls') or {}).get(chain)
        if not rpc:
            raise WalletQuoteError(f'{name} unavailable; configure an RPC or supply verified token decimals')
        if chain in ('sol','solana'):
            return SolanaNative(rpc_url=rpc).get_mint_decimals(token)
        return EVMNative(chain=chain,chain_id=EVM_CHAIN_IDS[chain],rpc_url=rpc).get_erc20_decimals(token)

    @staticmethod
    def _token_address(chain, token):
        if token.lower() in ('native','sol','eth','bnb'):
            return ('So11111111111111111111111111111111111111112' if chain in ('sol','solana')
                    else '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee')
        return token

    def _signer_wallet(self):
        from .self_custody import SelfCustodyWallet
        return SelfCustodyWallet(workspace=self.workspace,signer_ref=str(self.config.get('signer_ref') or ''),
                                 rpc_urls=self.config.get('rpc_urls') or {},config=self.config)

    def quote(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50, **kw: Any,
    ) -> WalletQuote:
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        dec_in = self._decimals(chain,token_in,kw,"decimals_in")
        dec_out = self._decimals(chain,token_out,kw,"decimals_out")
        doc = self._signed_get(_QUOTE_PATH, {
            "chainIndex": str(self._chain_index(chain)),
            "fromTokenAddress": self._token_address(chain,token_in),
            "toTokenAddress": self._token_address(chain,token_out),
            "amount": str(to_base_units(amount_in, dec_in)),
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
        extra = {"real_quote":True,"decimals_in":dec_in,"decimals_out":dec_out}
        return WalletQuote(
            provider=self.id, chain=chain,
            token_in=token_in, token_out=token_out,
            amount_in=float(amount_in),
            expected_out=expected,
            min_out=(int(data['toTokenAmount'])*(10000-slippage_bps)//10000)/(10**dec_out),
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
        if not receiver and not self.config.get('signer_ref'):
            raise WalletPolicyDenied("OKX OS swap requires a receiver address")
        dec_in = self._decimals(chain,token_in,kw,"decimals_in")
        dec_out = self._decimals(chain,token_out,kw,"decimals_out")
        signer = self._signer_wallet() if self.config.get('signer_ref') else None
        if signer:
            # Caller precision may be useful for read-only quotes, but signing
            # must verify it against the selected chain.
            for token, supplied in ((token_in,dec_in),(token_out,dec_out)):
                actual_decimals=self._decimals(chain,token,{},'decimals')
                if actual_decimals != supplied:
                    raise WalletPolicyDenied('token decimals changed; request a verified quote')
        key = signer._resolve_signer_key() if signer else ''
        if key:
            if chain in ('solana','sol'):
                from ...connectors.solana_native import _pubkey_from_signer
                owner = _pubkey_from_signer(key)
            else:
                from eth_account import Account
                owner = Account.from_key(key).address
            if receiver and (receiver != owner if chain in ('solana','sol') else receiver.lower()!=owner.lower()):
                raise WalletPolicyDenied('receiver must match the selected signing wallet')
            receiver = owner
        doc = self._signed_get(_SWAP_PATH, {
            "chainIndex": str(self._chain_index(chain)), "fromTokenAddress":self._token_address(chain,token_in),
            "toTokenAddress":self._token_address(chain,token_out),"amount":str(to_base_units(amount_in,dec_in)),
            "slippagePercent":str(slippage_bps/100),"userWalletAddress":receiver,
            "swapMode":"exactIn","autoSlippage":"false",
        })
        data = (doc.get("data") or [{}])[0]
        tx = data.get("tx") or {}
        router = data.get('routerResult') or data
        expected = float(router.get('toTokenAmount') or 0)/(10**dec_out)
        if not signer:
            return WalletSwapResult(provider=self.id,chain=chain,ok=False,amount_in=float(amount_in),
                reason='unsigned tx only; configure signer_ref and rpc_urls to execute',
                extra={'unsigned_tx':tx,'confirmed':False,'expected_out':expected})
        from ..amounts import to_base_units_ceil
        approved = to_base_units_ceil(kw.get('min_out') or 0,dec_out)
        minimum = int(tx.get('minReceiveAmount') or router.get('minReceiveAmount') or
                      (int(router.get('toTokenAmount') or 0)*(10000-slippage_bps)//10000))
        if minimum <= 0 or minimum < approved:
            raise WalletPolicyDenied('OKX executable minimum is missing or below the approved floor')
        try:
            if chain in ('sol','solana'):
                conn = signer._solana_connector(live=True)
                import base58,base64
                # OKX's Solana swap contract returns tx.data in base58.
                encoded = base64.b64encode(base58.b58decode(tx.get('data') or '')).decode()
                out = conn.send_swap_transaction(encoded,key,output_mint=self._token_address(chain,token_out),user_public_key=receiver,
                    on_broadcast=kw.get('on_broadcast'))
                actual = out.get('amount_out')
                return WalletSwapResult(provider=self.id,chain=chain,ok=bool(out.get('confirmed')) and actual is not None,
                    tx_hash=out.get('signature') or '',amount_in=float(amount_in),amount_out=float(actual or 0),
                    extra={'confirmed':bool(out.get('confirmed')),'amount_out_source':'transaction_meta' if actual is not None else 'unknown'})
            from ...connectors.evm_native import EVMNative,EVM_CHAIN_IDS
            from ..receipts import token_received, native_received
            rpc = (self.config.get('rpc_urls') or {}).get(chain)
            if not rpc:
                raise WalletPolicyDenied('OKX signing requires an explicit chain RPC')
            conn = EVMNative(chain=chain,chain_id=EVM_CHAIN_IDS[chain],rpc_url=rpc,live=True)
            sender = tx.get('from')
            if sender and sender.lower()!=receiver.lower():
                raise WalletPolicyDenied('OKX transaction sender mismatch')
            native_in = token_in.lower() in ('native','eth','bnb','0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee')
            native_out = token_out.lower() in ('native','eth','bnb','0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee')
            value = int(str(tx.get('value') or '0'),0) if str(tx.get('value') or '0').startswith('0x') else int(tx.get('value') or 0)
            if value > (to_base_units(amount_in,dec_in) if native_in else 0):
                raise WalletPolicyDenied('OKX transaction value exceeds approved input')
            if not native_in:
                approval = self._signed_get('/api/v6/dex/aggregator/approve-transaction',{
                    'chainIndex':str(self._chain_index(chain)),'tokenContractAddress':token_in,
                    'approveAmount':str(to_base_units(amount_in,dec_in))})
                item=(approval.get('data') or [{}])[0]
                spender=item.get('dexContractAddress') or item.get('spenderAddress')
                if not spender or len(spender)!=42:
                    raise WalletPolicyDenied('OKX approval spender unavailable')
                from ...connectors.bsc_native import BSCNative
                helper=BSCNative(chain=chain,chain_id=EVM_CHAIN_IDS[chain],rpc_url=rpc,live=True)
                allowed=helper.get_erc20_allowance(token_in,receiver,spender,decimals=dec_in)
                if to_base_units(allowed,dec_in)<to_base_units(amount_in,dec_in):
                    out=helper.approve(token=token_in,spender=spender,amount=to_base_units(amount_in,dec_in),
                        signer_private_key=key,on_broadcast=kw.get('on_broadcast'))
                    if not out.get('confirmed'):
                        return WalletSwapResult(provider=self.id,chain=chain,ok=False,tx_hash=out.get('tx_hash',''),
                            reason='allowance_confirmation_pending',extra={'confirmed':False,'phase':'approval'})
            out=conn.send_raw_transaction(to=str(tx.get('to') or ''),data=str(tx.get('data') or ''),value=value,
                signer_private_key=key,gas_limit=int(tx.get('gas') or 300000),
                gas_price_gwei=float(tx['gasPrice'])/1e9 if tx.get('gasPrice') else None,
                on_broadcast=(lambda tx:kw['on_broadcast']({**tx,'token_out':token_out,'decimals_out':dec_out,'receiver':receiver,'native_out':native_out})) if kw.get('on_broadcast') else None)
            actual=(native_received(conn,out['tx_hash'],receiver) if native_out and out.get('confirmed')
                    else None if native_out else token_received(out.get('receipt'),token_out,receiver,dec_out))
            return WalletSwapResult(provider=self.id,chain=chain,ok=bool(out.get('confirmed')) and actual is not None,
                tx_hash=out.get('tx_hash',''),amount_in=float(amount_in),amount_out=float(actual or 0),
                extra={'confirmed':bool(out.get('confirmed')),'amount_out_source':('transaction_trace' if native_out else 'receipt') if actual is not None else 'unknown',
                       'expected_out':expected})
        finally:
            key=''

"""Coinbase Agentic Wallet / CDP wallet provider.

Supports two installation paths, whichever the operator has on their box.
Neither is auto-installed by Nerya:

1. **Agentic Wallet CLI** — Coinbase's authenticate-wallet skill uses
   ``npx awal@2.10.0 auth login <email> --json`` and an email OTP.
   (The session file it produces is NOT a supported transport on
   Nerya's side and does not make this provider ready.)
2. **Python CDP SDK** — ``pip install 'cdp-sdk<1.0'`` (legacy
   ``Cdp``/``Wallet`` API; cdp-sdk 1.x removed it) or
   ``coinbase-agentkit``. We only ever FETCH the operator's existing
   wallet — ``Wallet.create`` is never called. agentkit alone is
   partial (native-asset balance only, no swap).
3. **Node CDP skill** (preferred when Python is partial) — point
   ``wallet.coinbase.skill_path`` at a checkout of
   ``@coinbase/cdp-sdk`` / ``@coinbase/coinbase-sdk`` wrapped in a
   Nerya skill entry (see :mod:`nerya.wallet.providers._node_skill`
   for the wire protocol). When both a Node skill and Python SDKs are
   available, the Node skill wins unless the full legacy cdp-sdk
   ``Wallet`` API is importable — see :meth:`CoinbaseWallet._prefer_node_skill`.

Credentials (API key name + private key) are resolved the same way
everywhere: through ``nerya.yml`` → ``wallet.coinbase.{api_key_ref,
api_private_key_ref}`` → ``SecretVault``. Operators should NOT paste the
raw secret into the config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import (
    WalletDependencyError,
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
            "Real via the legacy cdp-sdk Wallet API or the Node "
            "@coinbase/cdp-sdk skill. The coinbase-agentkit python path "
            "is native-asset-only (its provider balance API takes no "
            "token argument)."
        ),
    ),
    quote=WalletCapability(
        supported=True, status="partial",
        note=(
            "Python cdp-sdk has no standalone quote — quote() returns a "
            "synthetic amount_in*(1-slippage) placeholder. The Node skill "
            "path is real and is preferred whenever it is installed and "
            "the python SDK is not fully functional."
        ),
    ),
    swap=WalletCapability(
        supported=True, status="partial",
        note=(
            "Real via Python cdp-sdk (Wallet.trade on the operator's "
            "EXISTING wallet — never auto-created) and via the Node skill. "
            "The coinbase-agentkit-only path is not wired yet and raises "
            "WalletPolicyDenied."
        ),
    ),
    market_data=WalletCapability(
        supported=True,
        status="real",
        note=(
            "GET /products/{product_id}/candles from Coinbase public product "
            "candles. This is product-pair market data, not token-contract OHLCV."
        ),
    ),
    execution_profile="partial",
    chains=("base", "base-sepolia", "ethereum", "ethereum-sepolia"),
    notes=(
        "Use wallet.coinbase with cdp-sdk for real swaps, or the Node "
        "skill for full quote+swap coverage. agentkit-only installs are "
        "feature-incomplete on the swap path."
    ),
)


_COINBASE_PRODUCTS_URL = "https://api.exchange.coinbase.com/products"
_COINBASE_GRANULARITY = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
}


def _slippage_floor(expected_out: float, slippage_bps: int) -> float:
    """Minimum acceptable out-amount: expected reduced by ``slippage_bps``.

    Pure helper so the fallback math is unit-testable; used everywhere a
    skill doc omits an explicit ``min_out``.
    """
    return float(expected_out) * (1.0 - float(slippage_bps) / 10_000)


# Native assets the agentkit provider's no-argument get_balance() can
# actually report (verified: CdpEvmWalletProvider.get_balance() returns
# the native wei balance; CdpWalletProvider never had a token variant).
_NATIVE_ASSETS = ("", "eth", "gas", "native")


@dataclass
class CoinbaseWallet(WalletProvider):
    id: str = "coinbase"
    label: str = "Coinbase Agentic Wallet / CDP Wallet"

    api_key_name: str = ""
    api_private_key: str = ""
    network_id: str = "base-mainnet"
    # For Node/TS fallback, same shape as bitget/binance_agentic.
    skill_path: str = ""
    entry: str = "dist/index.js"
    repo: str = "https://github.com/coinbase/cdp-sdk"
    # The node skill actually implements the agentkit flows; cdp-sdk is
    # the Python alternative. Kept as separate knowledge below.
    repo_alt: str = "https://github.com/coinbase/coinbase-agentkit"
    config: dict[str, Any] = field(default_factory=dict)

    def _have_creds(self) -> bool:
        return bool(self.api_key_name and self.api_private_key)

    def _configured_wallet_id(self) -> str:
        return str((self.config or {}).get("wallet_id") or "").strip()

    def _configured_address(self) -> str:
        return str(
            (self.config or {}).get("address")
            or (self.config or {}).get("wallet_address")
            or ""
        ).strip()

    def _probe_py(self) -> str | None:
        for mod in _PY_PREFERRED:
            try:
                __import__(mod)
                return mod
            except Exception:
                continue
        return None

    def _python_full(self) -> bool:
        """True when the installed Python SDK backs real balance + swap.

        Only the legacy cdp-sdk ``Wallet`` API does (``Wallet.balance`` /
        ``Wallet.trade``). ``coinbase_agentkit`` alone is partial: its
        provider can read the native asset only and has no swap wired.
        """
        return self._probe_py() in ("cdp", "cdp_sdk")

    def _prefer_node_skill(self) -> bool:
        """Node skill wins when it is usable AND python is not fully functional.

        Precedence rule for the ``balance``/``quote``/``swap`` methods:
        with both a ``skill_path`` checkout and Python SDKs installed, the
        broken/partial Python path must not shadow a working Node skill.
        Python only wins when the full cdp-sdk ``Wallet`` API is present.
        """
        if not self.skill_path:
            return False
        node_ok, _missing = self._ref().skill_ready()
        if not node_ok:
            return False
        return not self._python_full()

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
        """Dependency readiness — only deps that back real code paths.

        The awal "agentic session" file (``agentic_session_path``) is
        intentionally NOT counted here: no code path in this provider
        implements that transport, so counting it made readiness claim
        ``ready`` while every method would fail. Readiness now requires
        a usable Python SDK (or Node skill) *and* API credentials.
        """
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
            "For CDP SDK methods, run `pip install 'cdp-sdk<1.0'` "
            "(the legacy Wallet API; cdp-sdk 1.x removed it) and configure "
            "wallet.coinbase.{api_key_name_ref, api_private_key_ref}. "
            "Alternatively point wallet.coinbase.skill_path at a "
            "@coinbase/cdp-sdk Node skill checkout."
        )
        reason = ""
        if not ready:
            if not (py_mod or node_ok):
                reason = "no python cdp-sdk and no usable coinbase node skill."
            elif not self._have_creds():
                reason = "Coinbase CDP SDK methods require api_key_name + api_private_key."
        return WalletReadiness(
            provider=self.id, ready=ready, missing=missing,
            install_hint=install_hint, reason=reason,
        )

    def capabilities(self) -> WalletCapabilities:
        return _CAPABILITIES

    # --------------------------------------------------------------
    def get_market_klines(
        self,
        *,
        market: str,
        interval: str = "1h",
        limit: int = 100,
        **_kw: Any,
    ) -> list[dict[str, Any]]:
        """Fetch Coinbase public product candles.

        ``market`` is a Coinbase product id such as ``BTC-USD``.
        """

        product = str(market or "").strip().upper().replace("/", "-")
        if not product:
            raise WalletPolicyDenied("Coinbase market data requires product id")
        granularity = _COINBASE_GRANULARITY.get(str(interval or "1h").lower(), 3600)
        from urllib.parse import quote, urlencode

        from ...connectors.http import UrllibHttp

        params = urlencode({"granularity": str(granularity)})
        url = f"{_COINBASE_PRODUCTS_URL}/{quote(product, safe='')}/candles?{params}"
        status, doc = UrllibHttp().request("GET", url, timeout=20.0)
        if status >= 400:
            raise WalletPolicyDenied(f"Coinbase product candles returned {status}: {doc}")
        rows = doc.get("raw") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            rows = doc if isinstance(doc, list) else []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            try:
                if isinstance(row, dict):
                    ts = row.get("time") or row.get("ts")
                    lo = row.get("low")
                    h = row.get("high")
                    o = row.get("open")
                    c = row.get("close")
                    v = row.get("volume")
                elif isinstance(row, (list, tuple)) and len(row) >= 5:
                    ts = row[0]
                    lo = row[1]
                    h = row[2]
                    o = row[3]
                    c = row[4]
                    v = row[5] if len(row) > 5 else 0
                else:
                    continue
                out.append({
                    "ts": int(float(ts)),
                    "open": float(o),
                    "high": float(h),
                    "low": float(lo),
                    "close": float(c),
                    "volume": float(v or 0),
                })
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda r: r["ts"])
        return out[-max(1, min(int(limit or 100), 300)):]

    @staticmethod
    def _wallet_address_str(wallet: Any) -> str:
        addr = getattr(wallet, "address", None)
        if isinstance(addr, str) and addr.strip():
            return addr.strip()
        default_address = getattr(wallet, "default_address", None)
        return str(getattr(default_address, "address_id", "") or "")

    def _fetch_existing_wallet(self, Wallet: Any) -> Any:
        """Fetch the operator's EXISTING CDP wallet — never provision one.

        The previous implementation called ``Wallet.create(...)`` on every
        method call, silently spawning a fresh empty server-side wallet.
        This class never creates wallets.

        API note (verified against PyPI wheels, since no cdp-sdk is
        installed in this venv): ``Wallet.fetch_default_wallet`` does NOT
        exist in the Python SDK — cdp-sdk 0.21.0 is the last release with
        the legacy ``Wallet`` API and it only ships ``Wallet.fetch(id)`` /
        ``Wallet.list()``; cdp-sdk >= 1.0 removed ``Wallet`` entirely.
        The closest real API is therefore:

        - explicit ``wallet_id`` → ``Wallet.fetch(wallet_id)``
        - otherwise → first wallet from ``Wallet.list()`` on our network
          (the operator's default), optionally matched against a
          configured ``address`` / ``wallet_address``.

        Raises :class:`WalletPolicyDenied` when the configured wallet
        can't be found instead of falling back to creation.
        """
        wallet_id = self._configured_wallet_id()
        address = self._configured_address()
        try:
            if wallet_id:
                wallet = Wallet.fetch(wallet_id)
                got = self._wallet_address_str(wallet)
                if address and got and got.lower() != address.lower():
                    raise WalletPolicyDenied(
                        f"coinbase wallet {wallet_id!r} resolves to address "
                        f"{got!r}, not the configured address {address!r}."
                    )
                return wallet
            for wallet in Wallet.list():
                if str(getattr(wallet, "network_id", "") or "") != self.network_id:
                    continue
                got = self._wallet_address_str(wallet)
                if address and got and got.lower() != address.lower():
                    continue
                return wallet
        except (WalletPolicyDenied, WalletDependencyError):
            raise
        except Exception as exc:
            raise WalletPolicyDenied(
                f"coinbase: fetching the existing CDP wallet failed: {exc}"
            ) from exc
        where = f" with id {wallet_id!r}" if wallet_id else f" on network {self.network_id!r}"
        raise WalletPolicyDenied(
            f"no existing CDP wallet found{where}"
            + (f" at address {address!r}" if address and not wallet_id else "")
            + ". Nerya never auto-creates wallets — create it in the CDP "
            "portal first or set wallet.coinbase.{wallet_id, address}."
        )

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
            try:
                from cdp import Cdp, Wallet  # type: ignore
            except ImportError as exc:
                # cdp-sdk >= 1.0 exports only CdpClient; the Wallet API
                # below requires the legacy SDK line.
                raise WalletDependencyError(
                    self.id,
                    ["pip:'cdp-sdk<1.0' (legacy Cdp/Wallet API)"],
                    "pip install 'cdp-sdk<1.0'  # or use the wallet.coinbase "
                    "skill_path Node skill",
                ) from exc
            Cdp.configure(
                api_key_name=self.api_key_name,
                private_key=self.api_private_key,
            )
            wallet = self._fetch_existing_wallet(Wallet)
            return wallet, "cdp"
        if mod == "coinbase_agentkit":
            import coinbase_agentkit as agentkit  # type: ignore

            wallet_secret = str((self.config or {}).get("wallet_secret") or "").strip()
            address = self._configured_address()
            modern_provider = getattr(agentkit, "CdpEvmWalletProvider", None)
            if modern_provider is not None:
                # agentkit >= 0.7 renamed CdpWalletProvider and moved to a
                # pydantic config object; it needs wallet_secret, and it
                # creates a NEW account when no address is passed — always
                # forward the configured address so we fetch the
                # operator's existing account.
                if not wallet_secret:
                    raise WalletDependencyError(
                        self.id,
                        ["cred:wallet_secret"],
                        "agentkit >= 0.7 CdpEvmWalletProvider requires "
                        "wallet.coinbase.wallet_secret in addition to the "
                        "API key pair.",
                    )
                wp = modern_provider(
                    agentkit.CdpEvmWalletProviderConfig(
                        api_key_id=self.api_key_name,
                        api_key_secret=self.api_private_key,
                        wallet_secret=wallet_secret,
                        network_id=self.network_id,
                        address=address or None,
                    )
                )
                return wp, "agentkit"
            CdpWalletProvider = getattr(agentkit, "CdpWalletProvider")
            kwargs: dict[str, Any] = {
                "api_key_name": self.api_key_name,
                "api_key_private_key": self.api_private_key,
                "network_id": self.network_id,
            }
            if address:
                kwargs["address"] = address
            try:
                wp = CdpWalletProvider(**kwargs)
            except TypeError:
                # Older CdpWalletProvider ctor without an address arg.
                kwargs.pop("address", None)
                wp = CdpWalletProvider(**kwargs)
            return wp, "agentkit"
        raise WalletDependencyError(self.id, ["pip:cdp-sdk"], r.install_hint)

    def get_balance(
        self, *, chain: str, address: str, token: str, **kw: Any,
    ) -> WalletBalance:
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)

        if self._prefer_node_skill():
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
            # agentkit: the provider's balance API is native-only and takes
            # NO token argument — ``get_balance(token)`` used to raise
            # AttributeError/TypeError that got swallowed into a generic
            # "get_balance failed". Route token balances to the Node skill
            # instead and be explicit about the native-only limitation.
            token_l = str(token or "").strip().lower()
            if token_l not in _NATIVE_ASSETS:
                raise WalletPolicyDenied(
                    "coinbase agentkit python path can only read the native "
                    "(gas) asset — configure wallet.coinbase.skill_path "
                    "(Node @coinbase/cdp-sdk skill) for token balances."
                )
            wei = wallet.get_balance()  # native balance in wei
            return WalletBalance(
                provider=self.id, chain=chain, address=address,
                token=token, balance=float(wei) / 1e18, symbol="ETH",
            )
        except (WalletDependencyError, WalletPolicyDenied):
            raise
        except Exception as exc:
            raise WalletPolicyDenied(f"coinbase get_balance failed: {exc}") from exc

    def quote(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50, **kw: Any,
    ) -> WalletQuote:
        r = self.readiness()
        if not r.ready:
            raise WalletDependencyError(self.id, r.missing, r.install_hint)
        if self._prefer_node_skill():
            doc = self._ref().invoke("quote", {
                "chain": chain, "token_in": token_in, "token_out": token_out,
                "amount_in": amount_in, "slippage_bps": slippage_bps,
            })
            expected = float(doc.get("expected_out") or 0.0)
            if expected <= 0:
                raise WalletQuoteError(
                    f"coinbase skill quote has no positive expected_out: {doc.get('expected_out')!r}"
                )
            return WalletQuote(
                provider=self.id, chain=chain,
                token_in=token_in, token_out=token_out,
                amount_in=float(amount_in),
                expected_out=expected,
                min_out=float(doc.get("min_out") or _slippage_floor(expected, slippage_bps)),
                slippage_bps=slippage_bps,
                extra={"raw": doc},
            )
        expected = float(amount_in) * (1.0 - slippage_bps / 10_000)
        return WalletQuote(
            provider=self.id, chain=chain,
            token_in=token_in, token_out=token_out,
            amount_in=float(amount_in),
            expected_out=expected,
            min_out=_slippage_floor(expected, slippage_bps),
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

        if self._prefer_node_skill():
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
        except WalletTransportError:
            raise
        except Exception as exc:
            raise WalletTransportError(f"coinbase swap failed: {exc}")

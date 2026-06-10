"""Unified exchange/venue provider spec + registry.

Every venue (``binance``, ``polymarket``, user-authored ``hyperliquid_v2``…)
publishes an :class:`ExchangeProviderSpec` that tells the registry:

* Which venue aliases map to this provider.
* How to instantiate it from a Nerya account config.
* Python package / Node skill install hints surfaced by the dashboard
  when a user tries to enable an unready provider.
* Metadata (runtime, docs url, supports_live, supports_klines, …)
  the UI uses for readiness cards.

The registry merges:

1. **Builtin** specs registered by :func:`_register_builtins` at import
   time (wrapping the native classes + ``CcxtConnector`` + Polymarket).
2. **User-authored** specs loaded from ``workspace/providers/<id>/provider.py``.
   Each such file must define ``SPEC: ExchangeProviderSpec``. The loader
   hot-imports them so operators don't need to restart Nerya after
   approving an exchange_author proposal.

See :mod:`nerya.connectors.registry` for the thin facade used by callers.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .base import Connector


@dataclass(frozen=True)
class CredentialField:
    """One slot a provider needs an operator to fill before live trading.

    The dashboard / CLI / agent intake flow reads this catalogue so each
    venue surfaces only the fields it actually uses (Binance: api_key
    + api_secret; OKX/KuCoin/Bitget: + passphrase; Hyperliquid /
    self-custody DEX: signing private key + RPC; Coinbase CDP: api_key
    name + private key + network_id…).

    ``name`` matches the key persisted under ``accounts.credentials``
    in ``accounts.yml`` (and the field the factory will pull through
    :func:`_resolve_cex_creds`). ``kind`` is a coarse hint for input
    rendering: ``"secret"`` should be a password-style box,
    ``"public"`` is a plain string (account/project id, network id),
    ``"url"`` accepts a URL. ``vault_scope`` is the SecretVault scope
    the system attaches when persisting the value.
    """

    name: str
    label: str
    kind: str = "secret"  # "secret" | "public" | "url"
    required: bool = True
    description: str = ""
    placeholder: str = ""
    vault_scope: str = "exchange"
    sensitive: bool = True  # if True, plaintext must not be echoed back

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "description": self.description,
            "placeholder": self.placeholder,
            "vault_scope": self.vault_scope,
            "sensitive": self.sensitive,
        }


# Common reusable credential field bundles ----------------------------
_CEX_API_KEY = CredentialField(
    name="api_key", label="API Key", kind="secret",
    description="Public API key issued by the exchange.",
    placeholder="paste API key", vault_scope="exchange",
)
_CEX_API_SECRET = CredentialField(
    name="api_secret", label="API Secret", kind="secret",
    description="Private API secret issued alongside the API key.",
    placeholder="paste API secret", vault_scope="exchange",
)
_CEX_API_PASSPHRASE = CredentialField(
    name="api_passphrase", label="API Passphrase", kind="secret",
    required=True,
    description="Passphrase/password set on the API key (OKX / KuCoin / Bitget / Coinbase Exchange-style APIs).",
    placeholder="passphrase", vault_scope="exchange",
)
_CEX_UID = CredentialField(
    name="uid", label="UID / User ID", kind="public",
    sensitive=False,
    description="Exchange UID required by some CCXT venues.",
    placeholder="user id", vault_scope="exchange",
)
_CEX_ACCOUNT_ID = CredentialField(
    name="account_id", label="Account ID", kind="public",
    sensitive=False,
    description="Account/profile id required by some venues.",
    placeholder="account id", vault_scope="exchange",
)
_CEX_LOGIN = CredentialField(
    name="login", label="Login", kind="public",
    sensitive=False,
    description="Login/account number required by some CCXT venues.",
    placeholder="login", vault_scope="exchange",
)
_CEX_PRIVATE_KEY = CredentialField(
    name="private_key", label="Private Key", kind="secret",
    description="Private key used by wallet-signed exchange APIs.",
    placeholder="0x...", vault_scope="exchange",
)
_CEX_WALLET_ADDRESS = CredentialField(
    name="wallet_address", label="Wallet Address", kind="public",
    sensitive=False,
    description="Wallet/account address used by wallet-signed exchange APIs.",
    placeholder="0x...", vault_scope="exchange",
)
_CEX_TOKEN = CredentialField(
    name="token", label="Token", kind="secret",
    description="Provider token required by some CCXT venues.",
    vault_scope="exchange",
)
_HL_PRIVATE_KEY = CredentialField(
    name="private_key", label="Wallet Private Key", kind="secret",
    description="Hex-encoded private key controlling the Hyperliquid account.",
    placeholder="0x...", vault_scope="exchange",
)
_HL_WALLET_ADDRESS = CredentialField(
    name="wallet_address", label="Account / Vault Address", kind="public",
    sensitive=False,
    description="0x address of the Hyperliquid account (sub-account vault address allowed).",
    placeholder="0x...", vault_scope="exchange",
)
_DEX_RPC_URL = CredentialField(
    name="rpc_url", label="RPC URL", kind="url",
    sensitive=False,
    description="HTTPS JSON-RPC endpoint Nerya should use.",
    placeholder="https://...", vault_scope="exchange",
)
_DEX_SIGNER_REF = CredentialField(
    name="signer_ref", label="Signer Vault Ref", kind="public",
    required=False, sensitive=False,
    description=(
        "Optional pointer to a workspace signer policy entry. Leave blank "
        "to keep the connector read-only."
    ),
    placeholder="local:my_signer", vault_scope="exchange",
)


@dataclass
class ExchangeProviderSpec:
    """Describes how to build a Connector for one venue family.

    ``aliases`` lets multiple venue keys share one provider (e.g. okx /
    okex, or binance / binance_spot). The factory is called with the
    raw account config dict + ``workspace`` and ``vault_passphrase``;
    it's responsible for pulling creds, RPC urls, etc.
    """

    id: str
    label: str
    kind: str  # cex|dex|chain|prediction_market|options|futures|derivatives
    runtime: str = "python"  # "python" | "python_ccxt" | "node" | "http"
    aliases: tuple[str, ...] = ()
    factory: Callable[..., Connector] | None = None
    install_hint: str = ""
    install_command: str = ""  # e.g. "pip install ccxt"; empty = nothing to install
    docs_url: str = ""
    links: dict[str, str] = field(default_factory=dict)
    description: str = ""
    supports: dict[str, bool] = field(default_factory=lambda: {
        "ticker": True, "klines": True, "order_book": True,
        "balances": False, "place_order": False,
    })
    # Optional: fine-grained instrument coverage for derivatives venues —
    # e.g. ("spot",), ("perp", "futures"), ("options",), or mixed.
    instrument_types: tuple[str, ...] = ()
    # Per-provider credential schema (sandboxed
    # account intake). Empty tuple means "no credentials needed", which
    # is the right answer for ``mock``/``mock_chain`` and for
    # data-source-only paper accounts.
    credential_fields: tuple[CredentialField, ...] = ()

    def matches(self, venue: str) -> bool:
        v = (venue or "").lower()
        return v == self.id or v in self.aliases

    def to_info(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "kind": self.kind,
            "runtime": self.runtime, "aliases": list(self.aliases),
            "install_hint": self.install_hint,
            "install_command": self.install_command,
            "docs_url": self.docs_url,
            "links": dict(self.links),
            "description": self.description, "supports": dict(self.supports),
            "instrument_types": list(self.instrument_types),
            "credential_fields": [f.to_dict() for f in self.credential_fields],
        }


class ExchangeProviderRegistry:
    """In-process registry of known venue providers.

    ``build(cfg, ...)`` is the single entry point used by
    :mod:`nerya.connectors.registry`. Workspace-authored providers are
    loaded on first ``build`` call (and after any
    :py:meth:`reload_workspace` call).
    """

    def __init__(self) -> None:
        self._specs: list[ExchangeProviderSpec] = []
        self._workspace_loaded_for: Path | None = None

    # ------------------------------------------------------ register
    def register(self, spec: ExchangeProviderSpec) -> None:
        # Replace any existing spec with the same id — user providers
        # can shadow builtins explicitly.
        self._specs = [s for s in self._specs if s.id != spec.id]
        self._specs.append(spec)

    def clear_user_specs(self) -> None:
        self._specs = [s for s in self._specs if not s.id.startswith("user:")]

    # ------------------------------------------------------ lookup
    def find(self, venue: str) -> ExchangeProviderSpec | None:
        v = (venue or "").lower()
        for s in self._specs:
            if s.matches(v):
                return s
        return None

    def list_specs(self) -> list[ExchangeProviderSpec]:
        return list(self._specs)

    # ------------------------------------------------------ hot load
    def reload_workspace(self, workspace: Path | None) -> None:
        self.clear_user_specs()
        if not workspace:
            self._workspace_loaded_for = None
            return
        providers_dir = Path(workspace) / "providers"
        if not providers_dir.exists():
            self._workspace_loaded_for = Path(workspace)
            return
        for pdir in sorted(providers_dir.iterdir()):
            if not pdir.is_dir():
                continue
            f = pdir / "provider.py"
            if not f.exists():
                continue
            spec = _import_user_provider(f, pdir.name)
            if spec is not None:
                # Force the user-provider prefix so the id space can't
                # collide with builtins.
                if not spec.id.startswith("user:"):
                    spec.id = f"user:{pdir.name}"
                if not spec.aliases:
                    spec.aliases = (pdir.name,)
                self.register(spec)
        self._workspace_loaded_for = Path(workspace)

    # ------------------------------------------------------ build
    def build(
        self, account_cfg: dict[str, Any], *,
        workspace: Path | None = None,
        vault_passphrase: str | None = None,
    ) -> Connector:
        if workspace and workspace != self._workspace_loaded_for:
            self.reload_workspace(workspace)
        venue = (account_cfg.get("venue") or "").lower()
        spec = self.find(venue)
        if spec is None or spec.factory is None:
            from ..core.errors import TradingError
            raise TradingError(
                f"unknown venue {venue!r} — known: "
                + ", ".join(sorted({s.id for s in self._specs}))
            )
        return spec.factory(
            account_cfg, workspace=workspace, vault_passphrase=vault_passphrase,
        )


def _import_user_provider(path: Path, name: str) -> ExchangeProviderSpec | None:
    """Import ``workspace/providers/<name>/provider.py`` and return its SPEC.

    Fails softly — a broken user provider must not prevent the rest of
    Nerya from booting. Errors are surfaced via dashboard readiness
    cards, not by crashing the registry.
    """
    spec = importlib.util.spec_from_file_location(
        f"nerya_user_provider_{name}", path,
    )
    if spec is None or spec.loader is None:
        return None
    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except Exception:
        return None
    provider_spec = getattr(module, "SPEC", None)
    if isinstance(provider_spec, ExchangeProviderSpec):
        return provider_spec
    # Convenience: allow `PROVIDER = ExchangeProviderSpec(...)` too.
    provider_spec = getattr(module, "PROVIDER", None)
    if isinstance(provider_spec, ExchangeProviderSpec):
        return provider_spec
    return None


# Singleton
_registry: ExchangeProviderRegistry | None = None


def get_registry() -> ExchangeProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ExchangeProviderRegistry()
        _register_builtins(_registry)
    return _registry


def reset_registry() -> None:
    """Drop the cached singleton — used by tests."""
    global _registry
    _registry = None


_CCXT_CREDENTIAL_FIELDS: dict[str, CredentialField] = {
    "apiKey": _CEX_API_KEY,
    "secret": _CEX_API_SECRET,
    "password": _CEX_API_PASSPHRASE,
    "uid": _CEX_UID,
    "accountId": _CEX_ACCOUNT_ID,
    "login": _CEX_LOGIN,
    "privateKey": _CEX_PRIVATE_KEY,
    "walletAddress": _CEX_WALLET_ADDRESS,
    "token": _CEX_TOKEN,
}
_CCXT_CREDENTIAL_ORDER = (
    "apiKey",
    "secret",
    "password",
    "uid",
    "accountId",
    "login",
    "walletAddress",
    "privateKey",
    "token",
)


def _ccxt_required_credentials(ccxt_id: str) -> dict[str, bool] | None:
    """Return CCXT's live credential contract for an exchange id.

    CCXT is the runtime adapter for these venues, so its
    ``requiredCredentials`` is the source of truth for which constructor
    params must be available before private calls such as
    ``fetch_balance`` can work. If CCXT is absent or does not currently
    ship the exchange class, callers fall back to the static schema.
    """

    try:
        import ccxt  # type: ignore
    except Exception:
        return None
    klass = getattr(ccxt, (ccxt_id or "").lower(), None)
    if klass is None:
        return None
    try:
        required = getattr(klass({}), "requiredCredentials", None)
    except Exception:
        return None
    if not isinstance(required, dict):
        return None
    return {str(k): bool(v) for k, v in required.items()}


def _ccxt_credential_fields(
    ccxt_id: str,
    *,
    fallback_passphrase: bool = False,
) -> tuple[CredentialField, ...]:
    required = _ccxt_required_credentials(ccxt_id)
    if required is None:
        fields: tuple[CredentialField, ...] = (_CEX_API_KEY, _CEX_API_SECRET)
        if fallback_passphrase:
            fields = fields + (_CEX_API_PASSPHRASE,)
        return fields

    fields = []
    for param in _CCXT_CREDENTIAL_ORDER:
        if not required.get(param):
            continue
        field = _CCXT_CREDENTIAL_FIELDS.get(param)
        if field is not None:
            fields.append(replace(field, required=True))
    return tuple(fields)


# --------------------------------------------------------- builtin factories
def _resolve_cex_creds(account_cfg, workspace, vault_passphrase, *,
                      with_passphrase: bool = False):
    from .registry import _resolve_cex_creds as _impl
    return _impl(account_cfg, workspace, vault_passphrase,
                 with_passphrase=with_passphrase)


def _register_builtins(reg: ExchangeProviderRegistry) -> None:
    from .ccxt_adapter import CcxtConnector, supported_exchanges
    from .dex_base import DEXCredentials
    from .evm_native import EVMNative
    from .bsc_native import BSCNative
    from .mock_chain import MockChain
    from .mock_exchange import MockExchange
    from .polymarket import PolymarketConnector
    from .solana_native import SolanaNative
    from .yahoo import YahooFinanceConnector
    from .ibkr import IBKRConnector, IBKRCredentials
    from .mt5 import MT5Connector, MT5Credentials
    from .alpaca import AlpacaConnector, AlpacaCredentials
    from .data_sources import (
        DataSourceCredentials,
        TushareConnector, AkShareConnector, PolygonConnector,
        CoinGeckoConnector, CoinMarketCapConnector, GlassnodeConnector,
        DuneConnector, TencentConnector, MOEXConnector, MessariConnector,
    )
    from .http_data_source import HttpDataSourceConnector, HttpSourceConfig
    from .http_auth import normalize_headers_payload

    def _mock(cfg, **_kw):
        return MockExchange()

    def _mock_chain(cfg, **_kw):
        return MockChain()

    def _ccxt_factory(default_id: str):
        """Build a factory that binds a venue to a concrete ccxt exchange id."""

        def _build(cfg, *, workspace=None, vault_passphrase=None):
            raw = cfg.get("ccxt_id") or cfg.get("exchange_id")
            if not raw:
                venue_raw = str(cfg.get("venue") or "")
                raw = venue_raw if venue_raw.startswith("ccxt:") else default_id
            raw = str(raw).lower()
            if raw.startswith("ccxt:"):
                raw = raw.split(":", 1)[-1]
            # Venue aliases (binance_spot → binance, okex → okx, hl → hyperliquid)
            alias_map = {
                "binance_spot": "binance", "binance_usdm": "binanceusdm",
                "binance_coinm": "binancecoinm",
                "okex": "okx", "hl": "hyperliquid", "bybit_v5": "bybit",
            }
            exchange_id = alias_map.get(raw, raw)
            creds = _resolve_cex_creds(
                cfg, workspace, vault_passphrase, with_passphrase=True,
            )
            options = dict(cfg.get("options") or {})
            # OKX sandbox / Bybit category hints map through ccxt options
            if cfg.get("category") and exchange_id == "bybit":
                options.setdefault("defaultType", cfg["category"])
            timeout_ms = int(
                cfg.get("timeout_ms")
                or (float(cfg.get("timeout_s")) * 1000 if cfg.get("timeout_s") else 15_000)
            )
            return CcxtConnector(
                exchange_id=exchange_id,
                credentials=creds, live=bool(cfg.get("live", False)),
                options=options,
                timeout_ms=timeout_ms,
            )

        return _build

    _binance = _ccxt_factory("binance")
    _bybit = _ccxt_factory("bybit")
    _okx = _ccxt_factory("okx")
    _hyperliquid = _ccxt_factory("hyperliquid")
    _ccxt = _ccxt_factory("binance")

    def _yahoo(cfg, **_kw):
        timeout = float(cfg.get("timeout_s") or 12.0)
        return YahooFinanceConnector(timeout=timeout)

    def _polymarket(cfg, *, workspace=None, vault_passphrase=None):
        creds = _resolve_cex_creds(cfg, workspace, vault_passphrase)
        return PolymarketConnector(
            credentials=creds, live=bool(cfg.get("live", False)),
            clob_url=cfg.get("clob_url") or "https://clob.polymarket.com",
            gamma_url=cfg.get("gamma_url") or "https://gamma-api.polymarket.com",
            data_url=cfg.get("data_url") or cfg.get("clob_url") or "https://clob.polymarket.com",
        )

    def _bsc(cfg, *, workspace=None, vault_passphrase=None):
        rpc = cfg.get("rpc_url") or "https://bsc-dataseed.binance.org"
        return BSCNative(
            chain="bsc",
            chain_id=int(cfg.get("chain_id", 56)),
            rpc_url=rpc,
            live=bool(cfg.get("live", False)),
            router=cfg.get("router") or cfg.get("pancake_router")
                or "0x10ED43C718714eb63d5aA57B78B54704E256024E",
            credentials=DEXCredentials(
                rpc_url=rpc, signer_ref=cfg.get("signer_ref", ""),
            ),
        )

    def _evm(cfg, *, workspace=None, vault_passphrase=None):
        venue = (cfg.get("venue") or "evm").lower()
        chain = venue if venue != "evm" else cfg.get("chain", "ethereum")
        return EVMNative(
            chain=chain,
            chain_id=int(cfg.get("chain_id", 1)),
            rpc_url=cfg.get("rpc_url", ""),
            live=bool(cfg.get("live", False)),
            credentials=DEXCredentials(
                rpc_url=cfg.get("rpc_url", ""),
                signer_ref=cfg.get("signer_ref", ""),
            ),
        )

    def _solana(cfg, *, workspace=None, vault_passphrase=None):
        return SolanaNative(
            chain="solana",
            rpc_url=cfg.get("rpc_url", "https://api.mainnet-beta.solana.com"),
            live=bool(cfg.get("live", False)),
            credentials=DEXCredentials(
                rpc_url=cfg.get("rpc_url", ""),
                signer_ref=cfg.get("signer_ref", ""),
            ),
        )

    reg.register(ExchangeProviderSpec(
        id="mock", label="Mock Exchange", kind="cex",
        aliases=("paper", "mock_exchange"), factory=_mock,
        description="Deterministic local fake exchange for tests + paper trading.",
        supports={"ticker": True, "klines": True, "order_book": True,
                  "balances": True, "place_order": True},
        credential_fields=(),
    ))
    reg.register(ExchangeProviderSpec(
        id="mock_chain", label="Mock Chain", kind="chain",
        aliases=("paper_chain",), factory=_mock_chain,
        description="Deterministic local fake chain connector for tests.",
        credential_fields=(),
    ))
    reg.register(ExchangeProviderSpec(
        id="binance", label="Binance Spot (ccxt)", kind="cex",
        runtime="python_ccxt",
        aliases=("binance_spot", "binanceusdm", "binancecoinm"),
        factory=_binance,
        install_hint="pip install ccxt",
        install_command="pip install ccxt",
        docs_url="https://developers.binance.com/docs/binance-spot-api-docs/rest-api",
        links={"docs": "https://developers.binance.com/docs/binance-spot-api-docs/rest-api",
               "api_keys": "https://www.binance.com/en/my/settings/api-management"},
        supports={"ticker": True, "klines": True, "order_book": True,
                  "balances": True, "place_order": True},
        credential_fields=(_CEX_API_KEY, _CEX_API_SECRET),
    ))
    reg.register(ExchangeProviderSpec(
        id="bybit", label="Bybit v5 (ccxt)", kind="cex",
        runtime="python_ccxt",
        aliases=("bybit_v5",), factory=_bybit,
        install_hint="pip install ccxt",
        install_command="pip install ccxt",
        docs_url="https://bybit-exchange.github.io/docs/v5/intro",
        links={"docs": "https://bybit-exchange.github.io/docs/v5/intro",
               "api_keys": "https://www.bybit.com/app/user/api-management"},
        supports={"ticker": True, "klines": True, "order_book": True,
                  "balances": True, "place_order": True},
        credential_fields=(_CEX_API_KEY, _CEX_API_SECRET),
    ))
    reg.register(ExchangeProviderSpec(
        id="okx", label="OKX (ccxt)", kind="cex", runtime="python_ccxt",
        aliases=("okex",), factory=_okx,
        install_hint="pip install ccxt",
        install_command="pip install ccxt",
        docs_url="https://www.okx.com/docs-v5/en/",
        links={"docs": "https://www.okx.com/docs-v5/en/",
               "api_keys": "https://www.okx.com/account/my-api"},
        supports={"ticker": True, "klines": True, "order_book": True,
                  "balances": True, "place_order": True},
        credential_fields=(_CEX_API_KEY, _CEX_API_SECRET, _CEX_API_PASSPHRASE),
    ))
    reg.register(ExchangeProviderSpec(
        id="hyperliquid", label="Hyperliquid (ccxt)", kind="cex",
        runtime="python_ccxt",
        aliases=("hl",), factory=_hyperliquid,
        install_hint="pip install ccxt",
        install_command="pip install ccxt",
        docs_url="https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api",
        links={"docs": "https://hyperliquid.gitbook.io/hyperliquid-docs/",
               "api": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api"},
        supports={"ticker": True, "klines": True, "order_book": True,
                  "balances": True, "place_order": True},
        # HL identifies an account by the EVM 0x address that signs the
        # EIP-712 order and the corresponding private key.
        credential_fields=(_HL_WALLET_ADDRESS, _HL_PRIVATE_KEY),
    ))
    reg.register(ExchangeProviderSpec(
        id="ccxt", label="ccxt (unified)", kind="cex", runtime="python_ccxt",
        aliases=tuple(f"ccxt:{x}" for x in supported_exchanges()),
        factory=_ccxt,
        install_hint="pip install ccxt",
        install_command="pip install ccxt",
        docs_url="https://docs.ccxt.com/",
        links={"docs": "https://docs.ccxt.com/"},
        description="Unified ccxt adapter — works with 100+ exchanges "
                     "(kraken, gate, mexc, kucoin, coinbase, …). Some "
                     "venues additionally need a passphrase (KuCoin, "
                     "OKX, Bitget, Coinbase Exchange-style APIs) — fill it in if "
                     "the upstream venue requires it.",
        supports={"ticker": True, "klines": True, "order_book": True,
                  "balances": True, "place_order": True},
        credential_fields=(
            _CEX_API_KEY, _CEX_API_SECRET,
            CredentialField(
                name="api_passphrase", label="API Passphrase (optional)",
                kind="secret", required=False,
                description=(
                    "Only needed for venues that require it (KuCoin, "
                    "OKX, Bitget, Coinbase Exchange-style APIs)."
                ),
                vault_scope="exchange",
            ),
        ),
    ))
    reg.register(ExchangeProviderSpec(
        id="yahoo", label="Yahoo Finance", kind="cex", runtime="python",
        aliases=("yf", "yfinance", "stocks"), factory=_yahoo,
        docs_url="https://finance.yahoo.com/",
        links={"docs": "https://finance.yahoo.com/"},
        description="Public Yahoo Finance market-data connector for equities, ETFs, indices, FX, and crypto. Data-source only — pair with a paper account to trade.",
        supports={"ticker": True, "klines": True, "order_book": True,
                  "balances": False, "place_order": False},
        instrument_types=("spot", "equity", "etf", "index", "fx"),
        credential_fields=(),
    ))
    reg.register(ExchangeProviderSpec(
        id="polymarket", label="Polymarket (CLOB v2)",
        kind="prediction_market", runtime="python",
        aliases=("polymarket_v2", "pm"), factory=_polymarket,
        install_hint="pip install py-clob-client  # optional, for EIP-712 signing",
        install_command="pip install py-clob-client",
        docs_url="https://docs.polymarket.com/developers/CLOB/overview",
        links={"docs": "https://docs.polymarket.com/",
               "api": "https://docs.polymarket.com/developers/CLOB/overview"},
        description="Prediction-market orderbook on Polygon. Reads via "
                    "CLOB+Gamma+Data APIs; writes need EIP-712 signing.",
        supports={"ticker": True, "klines": True, "order_book": True,
                  "balances": True, "place_order": True},
        credential_fields=(
            CredentialField(
                name="api_key", label="Polygon Address", kind="public",
                sensitive=False, required=False,
                description="0x address that holds the USDC + Polymarket positions.",
                vault_scope="exchange",
            ),
            CredentialField(
                name="api_secret", label="Polygon Private Key",
                kind="secret", required=False,
                description="Hex private key used for EIP-712 order signing.",
                vault_scope="exchange",
            ),
        ),
    ))
    reg.register(ExchangeProviderSpec(
        id="bsc", label="BSC (PancakeSwap)", kind="dex", runtime="python",
        aliases=("bnb", "binance_chain", "pancakeswap"), factory=_bsc,
        docs_url="https://docs.pancakeswap.finance/",
        links={"docs": "https://docs.pancakeswap.finance/",
               "rpc": "https://docs.bnbchain.org/docs/rpc/"},
        credential_fields=(_DEX_RPC_URL, _DEX_SIGNER_REF),
    ))
    reg.register(ExchangeProviderSpec(
        id="evm", label="EVM (uniswap-style)", kind="dex", runtime="python",
        aliases=("ethereum", "eth", "arbitrum", "polygon", "base"),
        factory=_evm,
        docs_url="https://docs.uniswap.org/",
        links={"docs": "https://docs.uniswap.org/"},
        credential_fields=(_DEX_RPC_URL, _DEX_SIGNER_REF),
    ))
    reg.register(ExchangeProviderSpec(
        id="solana", label="Solana", kind="dex", runtime="python",
        aliases=("sol",), factory=_solana,
        docs_url="https://solana.com/docs/rpc/http",
        links={"docs": "https://solana.com/docs/rpc/http"},
        credential_fields=(_DEX_RPC_URL, _DEX_SIGNER_REF),
    ))

    # ------------------------------------------------------------------
    # Additional first-class CEX entries — same ccxt backend the unified
    # ``ccxt`` provider already uses, but exposed as dedicated venues so
    # the dashboard / agent picker can render their docs URLs and
    # credential schemas (passphrases, sub-account hints, …) instead of
    # forcing the operator through ``ccxt:<id>``. the connector framework ships
    # native connectors for each of these — see
    # ``connector framework/connector framework/connector/exchange/<venue>``.
    # ------------------------------------------------------------------

    def _ccxt_spec(
        venue_id: str, label: str, *,
        ccxt_id: str | None = None,
        with_passphrase: bool = False,
        docs_url: str = "",
        api_keys_url: str = "",
        aliases: tuple[str, ...] = (),
        kind: str = "cex",
    ) -> ExchangeProviderSpec:
        fields = _ccxt_credential_fields(
            ccxt_id or venue_id,
            fallback_passphrase=with_passphrase,
        )
        return ExchangeProviderSpec(
            id=venue_id, label=label, kind=kind, runtime="python_ccxt",
            aliases=aliases or (ccxt_id or venue_id,),
            factory=_ccxt_factory(ccxt_id or venue_id),
            install_hint="pip install ccxt",
            install_command="pip install ccxt",
            docs_url=docs_url,
            links={"docs": docs_url, "api_keys": api_keys_url} if api_keys_url else {"docs": docs_url},
            supports={"ticker": True, "klines": True, "order_book": True,
                      "balances": True, "place_order": True},
            credential_fields=fields,
        )

    reg.register(_ccxt_spec(
        "kraken", "Kraken (ccxt)",
        docs_url="https://docs.kraken.com/rest/",
        api_keys_url="https://www.kraken.com/u/security/api",
    ))
    reg.register(_ccxt_spec(
        "kucoin", "KuCoin (ccxt)",
        with_passphrase=True,
        docs_url="https://www.kucoin.com/docs/beginners/introduction",
        api_keys_url="https://www.kucoin.com/account/api",
    ))
    reg.register(_ccxt_spec(
        "coinbase", "Coinbase Advanced Trade (ccxt)",
        ccxt_id="coinbase",
        aliases=("coinbase_advanced_trade", "coinbaseadvanced"),
        docs_url="https://docs.cdp.coinbase.com/coinbase-business/advanced-trade-apis/rest-api",
        api_keys_url="https://www.coinbase.com/settings/api",
    ))
    reg.register(_ccxt_spec(
        "coinbase_exchange", "Coinbase Exchange (ccxt)",
        ccxt_id="coinbaseexchange",
        with_passphrase=True,
        aliases=("coinbaseexchange", "coinbase_pro", "coinbasepro", "gdax"),
        docs_url="https://docs.cdp.coinbase.com/exchange/rest-api/authentication",
        api_keys_url="https://exchange.coinbase.com/profile/api",
    ))
    reg.register(_ccxt_spec(
        "coinbase_international", "Coinbase International Exchange (ccxt)",
        ccxt_id="coinbaseinternational",
        with_passphrase=True,
        aliases=("coinbaseinternational", "coinbase_intx", "intx"),
        docs_url="https://docs.cdp.coinbase.com/api-reference/international-exchange-api/rest-api/authentication",
        api_keys_url="https://international.coinbase.com/profile/api",
    ))
    reg.register(_ccxt_spec(
        "gate", "Gate.io (ccxt)",
        ccxt_id="gate",
        aliases=("gate_io", "gateio"),
        docs_url="https://www.gate.io/docs/developers/apiv4/",
        api_keys_url="https://www.gate.io/myaccount/apiv4keys",
    ))
    reg.register(_ccxt_spec(
        "mexc", "MEXC (ccxt)",
        docs_url="https://mexcdevelop.github.io/apidocs/spot_v3_en/",
        api_keys_url="https://www.mexc.com/user/openapi",
    ))
    reg.register(_ccxt_spec(
        "htx", "HTX / Huobi (ccxt)",
        ccxt_id="htx",
        aliases=("huobi", "huobipro"),
        docs_url="https://huobiapi.github.io/docs/spot/v1/en/",
        api_keys_url="https://www.htx.com/en-us/account/api",
    ))
    reg.register(_ccxt_spec(
        "bitget", "Bitget (ccxt)",
        with_passphrase=True,
        docs_url="https://www.bitget.com/api-doc/common/intro",
        api_keys_url="https://www.bitget.com/account/newapi",
    ))
    reg.register(_ccxt_spec(
        "bitmart", "BitMart (ccxt)",
        with_passphrase=True,
        docs_url="https://developer-pro.bitmart.com/en/spot/",
        api_keys_url="https://www.bitmart.com/api-config/en-US",
    ))
    reg.register(_ccxt_spec(
        "bitstamp", "Bitstamp (ccxt)",
        docs_url="https://www.bitstamp.net/api/",
        api_keys_url="https://www.bitstamp.net/account/security/api/",
    ))
    reg.register(_ccxt_spec(
        "ascendex", "AscendEX / BitMax (ccxt)",
        aliases=("ascend_ex", "bitmax"),
        docs_url="https://ascendex.github.io/ascendex-pro-api/",
        api_keys_url="https://ascendex.com/en/global-digital-asset-platform/api-management",
    ))
    reg.register(_ccxt_spec(
        "bingx", "BingX (ccxt)",
        ccxt_id="bingx",
        aliases=("bing_x",),
        docs_url="https://bingx-api.github.io/docs/",
        api_keys_url="https://bingx.com/en-us/account/api",
    ))
    reg.register(_ccxt_spec(
        "bitrue", "Bitrue (ccxt)",
        docs_url="https://github.com/Bitrue-exchange/Spot-official-api-docs",
        api_keys_url="https://www.bitrue.com/account/api",
    ))
    reg.register(_ccxt_spec(
        "ndax", "NDAX (ccxt)",
        with_passphrase=True,
        docs_url="https://apidoc.ndax.io/",
        api_keys_url="https://ndax.io/profile/api-keys",
    ))
    reg.register(_ccxt_spec(
        "btcmarkets", "BTC Markets (ccxt)",
        aliases=("btc_markets",),
        docs_url="https://docs.btcmarkets.net/",
        api_keys_url="https://app.btcmarkets.net/account/apikey",
    ))
    reg.register(_ccxt_spec(
        "backpack", "Backpack Exchange (ccxt)",
        docs_url="https://docs.backpack.exchange/",
        api_keys_url="https://backpack.exchange/portfolio/settings/api-keys",
    ))

    # ------------------------------------------------------------------
    # Perpetual / linear-futures venues. These are separate ccxt
    # exchange ids in some cases (binanceusdm, binancecoinm) and route
    # ``options.defaultType`` for unified ones (bybit/okx).
    # ------------------------------------------------------------------

    def _perp_spec(
        venue_id: str, label: str, ccxt_id: str, *,
        default_type: str | None = None,
        with_passphrase: bool = False,
        docs_url: str = "",
        api_keys_url: str = "",
        aliases: tuple[str, ...] = (),
    ) -> ExchangeProviderSpec:
        fields = _ccxt_credential_fields(
            ccxt_id,
            fallback_passphrase=with_passphrase,
        )
        base_factory = _ccxt_factory(ccxt_id)

        def factory(cfg, *, workspace=None, vault_passphrase=None):
            new_cfg = dict(cfg or {})
            if default_type:
                opts = dict(new_cfg.get("options") or {})
                opts.setdefault("defaultType", default_type)
                new_cfg["options"] = opts
            new_cfg.setdefault("category", default_type or "linear")
            return base_factory(new_cfg, workspace=workspace,
                                 vault_passphrase=vault_passphrase)

        return ExchangeProviderSpec(
            id=venue_id, label=label, kind="cex", runtime="python_ccxt",
            aliases=aliases, factory=factory,
            install_hint="pip install ccxt",
            install_command="pip install ccxt",
            docs_url=docs_url,
            links={"docs": docs_url, "api_keys": api_keys_url} if api_keys_url else {"docs": docs_url},
            description=f"Linear / inverse perpetual futures via {ccxt_id}.",
            supports={"ticker": True, "klines": True, "order_book": True,
                      "balances": True, "place_order": True},
            instrument_types=("perpetual", "swap", "future"),
            credential_fields=fields,
        )

    reg.register(_perp_spec(
        "binance_perpetual", "Binance USD-M Perpetual",
        ccxt_id="binanceusdm",
        aliases=("binance_perp", "binanceusdm"),
        docs_url="https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info",
        api_keys_url="https://www.binance.com/en/my/settings/api-management",
    ))
    reg.register(_perp_spec(
        "binance_coinm_perpetual", "Binance COIN-M Perpetual",
        ccxt_id="binancecoinm",
        aliases=("binance_coinm",),
        docs_url="https://developers.binance.com/docs/derivatives/coin-margined-futures/general-info",
    ))
    reg.register(_perp_spec(
        "bybit_perpetual", "Bybit Linear Perpetual",
        ccxt_id="bybit", default_type="swap",
        aliases=("bybit_perp", "bybit_linear"),
        docs_url="https://bybit-exchange.github.io/docs/v5/intro",
        api_keys_url="https://www.bybit.com/app/user/api-management",
    ))
    reg.register(_perp_spec(
        "okx_perpetual", "OKX Perpetual Swap",
        ccxt_id="okx", default_type="swap",
        with_passphrase=True,
        aliases=("okx_perp", "okx_swap"),
        docs_url="https://www.okx.com/docs-v5/en/#trading-account-rest-api",
        api_keys_url="https://www.okx.com/account/my-api",
    ))
    reg.register(_perp_spec(
        "kucoin_perpetual", "KuCoin Futures",
        ccxt_id="kucoinfutures",
        with_passphrase=True,
        aliases=("kucoin_perp", "kucoinfutures"),
        docs_url="https://www.kucoin.com/docs/beginners/futures/introduction",
        api_keys_url="https://www.kucoin.com/account/api",
    ))
    reg.register(_perp_spec(
        "gate_perpetual", "Gate.io Perpetual",
        ccxt_id="gate", default_type="swap",
        aliases=("gate_io_perpetual", "gateio_perp"),
        docs_url="https://www.gate.io/docs/developers/apiv4/#futures",
    ))
    reg.register(_perp_spec(
        "bitget_perpetual", "Bitget Mix (Perpetual)",
        ccxt_id="bitget", default_type="swap",
        with_passphrase=True,
        aliases=("bitget_perp", "bitget_mix"),
        docs_url="https://www.bitget.com/api-doc/contract/intro",
    ))
    reg.register(_perp_spec(
        "hyperliquid_perpetual", "Hyperliquid Perpetual",
        ccxt_id="hyperliquid",
        aliases=("hyperliquid_perp", "hl_perp"),
        docs_url="https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api",
    ))

    # ------------------------------------------------------------------
    # dYdX v4 — uses its own SDK, kept here as a registered entry so the
    # account-creation UI knows to ask for the L1/L2 mnemonic. Concrete
    # factory routes via ccxt where supported, otherwise raises so the
    # operator picks a different connector.
    # ------------------------------------------------------------------

    def _dydx(cfg, *, workspace=None, vault_passphrase=None):
        # dydx v4 isn't covered by ccxt as of 2025-Q4; fall through to the
        # mock connector so a paper account can still be configured.
        from ..core.errors import ConnectorError
        if cfg.get("paper", True):
            return MockExchange()
        raise ConnectorError(
            "live dydx_v4 requires the dydx-v4-client Python SDK; install it "
            "with `pip install v4-client-py` and wire a custom factory."
        )

    reg.register(ExchangeProviderSpec(
        id="dydx_v4", label="dYdX v4 (Cosmos L1)",
        kind="cex", runtime="python",
        aliases=("dydx", "dydxv4"),
        factory=_dydx,
        install_hint="pip install v4-client-py",
        install_command="pip install v4-client-py",
        docs_url="https://docs.dydx.exchange/",
        links={"docs": "https://docs.dydx.exchange/",
               "api": "https://docs.dydx.exchange/api_integration-indexer/"},
        description=("dYdX v4 perpetuals on the Cosmos L1. Currently exposed "
                     "as a paper-only connector unless v4-client-py is wired."),
        supports={"ticker": True, "klines": True, "order_book": True,
                  "balances": False, "place_order": False},
        instrument_types=("perpetual",),
        credential_fields=(
            CredentialField(
                name="mnemonic", label="dYdX Wallet Mnemonic",
                kind="secret", required=True,
                description="BIP-39 mnemonic of the dYdX trading wallet.",
                placeholder="word1 word2 ...", vault_scope="exchange",
            ),
            CredentialField(
                name="address", label="dYdX Address", kind="public",
                sensitive=False, required=True,
                description="dydx1... bech32 address derived from the mnemonic.",
                placeholder="dydx1...", vault_scope="exchange",
            ),
        ),
    ))

    # ------------------------------------------------------------------
    # Broker / data-source-only providers borrowed from broker/data-source tooling:
    # IBKR, MT5, Alpaca, Tushare, Polygon.io, CoinGecko, Glassnode.
    # These are registered with ``supports.place_order=False`` when only
    # data is wired, so the upsert flow bumps them to a paper account
    # automatically; live trading requires installing each broker's
    # SDK (gated through the optional dependency installer).
    # ------------------------------------------------------------------

    def _data_only_factory(label: str):
        """Fallback factory used by the registered specs below.

        Returns the mock connector so paper accounts work even before
        the upstream SDK is installed. Specs that need real reads (IBKR,
        MT5, Alpaca, the various data-source providers) override this
        with their own factory once the SDK detection succeeds.
        """

        def _build(cfg, *, workspace=None, vault_passphrase=None):
            return MockExchange()

        return _build

    def _public_field(cfg: dict[str, Any], key: str) -> Any:
        """Read a non-secret per-venue config field.

        Resolution order:

        1. ``provider_config[key]`` — written by ``upsert_account`` from
           the intake submit.
        2. Bare top-level ``cfg[key]`` — legacy hand-edited yaml.
        3. ``credentials[key]`` — when the operator submitted a public
           field through the same intake form as secrets.
        """

        pc = cfg.get("provider_config") or {}
        if isinstance(pc, dict) and key in pc and pc[key] not in (None, ""):
            return pc[key]
        if key in cfg and cfg[key] not in (None, ""):
            return cfg[key]
        creds_map = cfg.get("credentials") if isinstance(cfg.get("credentials"), dict) else {}
        if key in creds_map and creds_map[key] not in (None, ""):
            return creds_map[key]
        return None

    def _ibkr_factory(cfg, *, workspace=None, vault_passphrase=None):
        return IBKRConnector(
            credentials=IBKRCredentials(
                host=str(_public_field(cfg, "host") or "127.0.0.1"),
                port=int(_public_field(cfg, "port") or 7497),
                client_id=int(_public_field(cfg, "client_id") or 1),
                account_id=str(_public_field(cfg, "account_id") or ""),
            ),
            live=bool(cfg.get("live", False)),
            config=dict(cfg),
        )

    def _mt5_factory(cfg, *, workspace=None, vault_passphrase=None):
        creds_map = cfg.get("credentials") if isinstance(cfg.get("credentials"), dict) else {}
        # The password is sensitive — pull it through the same vault
        # resolver the CEX flow uses so plaintext only lives in memory
        # for the duration of the call.
        from .registry import _resolve_ref
        password = ""
        pw_ref = creds_map.get("password") or cfg.get("password_ref") or ""
        if isinstance(pw_ref, str) and pw_ref.startswith("vault://"):
            password = _resolve_ref(pw_ref, workspace, vault_passphrase,
                                    scope="exchange") or ""
        elif isinstance(pw_ref, str):
            password = pw_ref
        return MT5Connector(
            credentials=MT5Credentials(
                server=str(_public_field(cfg, "server") or ""),
                login=int(_public_field(cfg, "login") or 0),
                password=password,
                path=str(_public_field(cfg, "path") or ""),
            ),
            live=bool(cfg.get("live", False)),
            config=dict(cfg),
        )

    def _alpaca_factory(cfg, *, workspace=None, vault_passphrase=None):
        creds = _resolve_cex_creds(cfg, workspace, vault_passphrase)
        paper_flag = _public_field(cfg, "paper")
        if paper_flag is None:
            paper_flag = True
        if isinstance(paper_flag, str):
            paper_flag = paper_flag.strip().lower() not in ("false", "0", "no")
        return AlpacaConnector(
            credentials=AlpacaCredentials(
                api_key=creds.api_key, api_secret=creds.api_secret,
                paper=bool(paper_flag),
            ),
            live=bool(cfg.get("live", False)),
            config=dict(cfg),
        )

    def _data_source_creds(cfg, workspace, vault_passphrase) -> "DataSourceCredentials":
        creds = _resolve_cex_creds(cfg, workspace, vault_passphrase)
        # Account-level custom auth headers live in
        # ``provider_config.headers`` so they can be edited / rendered
        # alongside the rest of the public connector config. We pass
        # them through ``DataSourceCredentials.extras`` so each
        # connector can decide whether to add them on top of its
        # built-in auth.
        pc = cfg.get("provider_config") if isinstance(cfg.get("provider_config"), dict) else {}
        headers = normalize_headers_payload(pc.get("headers"))
        return DataSourceCredentials(
            api_key=creds.api_key, api_secret=creds.api_secret,
            extras={"headers": headers, "workspace": workspace,
                    "vault_passphrase": vault_passphrase},
        )

    def _ds_factory(connector_cls):
        def _build(cfg, *, workspace=None, vault_passphrase=None):
            return connector_cls(
                credentials=_data_source_creds(cfg, workspace, vault_passphrase),
            )
        return _build

    def _http_source_factory(cfg, *, workspace=None, vault_passphrase=None):
        pc = cfg.get("provider_config") if isinstance(cfg.get("provider_config"), dict) else {}
        headers = normalize_headers_payload(pc.get("headers") or cfg.get("headers"))
        # The bare top-level ``url`` is convenient when the operator
        # creates the account from chat; ``provider_config.url`` is the
        # canonical form once the row is persisted.
        url = str(pc.get("url") or cfg.get("url") or "").strip()
        method = str(pc.get("method") or cfg.get("method") or "GET").upper()
        params = pc.get("params") if isinstance(pc.get("params"), dict) else None
        body = pc.get("body") if isinstance(pc.get("body"), dict) else None
        json_paths = (pc.get("json_paths")
                      if isinstance(pc.get("json_paths"), dict) else None)
        timeout_s = int(pc.get("timeout_s") or cfg.get("timeout_s") or 10)
        return HttpDataSourceConnector(
            HttpSourceConfig(
                url=url, method=method, headers=headers,
                params=params, body=body, json_paths=json_paths,
                timeout_s=timeout_s,
            ),
            workspace=workspace,
            vault_passphrase=vault_passphrase,
        )

    reg.register(ExchangeProviderSpec(
        id="ibkr", label="Interactive Brokers (TWS / IB Gateway)",
        kind="broker", runtime="python",
        aliases=("interactive_brokers", "tws"),
        factory=_ibkr_factory,
        install_hint=("install IB Gateway / TWS, then "
                      "`pip install ib_async` (or `ib_insync`)."),
        install_command="pip install ib_async",
        docs_url="https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/",
        links={
            "docs": "https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/",
            "tws": "https://www.interactivebrokers.com/en/trading/tws.php",
        },
        description=("Interactive Brokers connector — equities, futures, "
                     "options, FX. Requires a running TWS or IB Gateway "
                     "process for trading; data-only paper mode is "
                     "available without it."),
        supports={"ticker": True, "klines": True, "order_book": True,
                  "balances": True, "place_order": True},
        instrument_types=("equity", "future", "option", "fx", "etf"),
        credential_fields=(
            CredentialField(
                name="host", label="TWS / Gateway Host", kind="public",
                sensitive=False, description="IP address of the TWS or IB Gateway.",
                placeholder="127.0.0.1", vault_scope="exchange",
            ),
            CredentialField(
                name="port", label="TWS / Gateway Port", kind="public",
                sensitive=False, description="7497 (paper) / 7496 (live) for TWS, 4002 / 4001 for IB Gateway.",
                placeholder="7497", vault_scope="exchange",
            ),
            CredentialField(
                name="client_id", label="Client ID", kind="public",
                sensitive=False, description="Numeric client ID — must be unique per connection.",
                placeholder="1", vault_scope="exchange",
            ),
            CredentialField(
                name="account_id", label="IBKR Account ID", kind="public",
                sensitive=False, required=False,
                description="DU... (paper) or U... (live). Optional unless multiple accounts are linked.",
                placeholder="DU1234567", vault_scope="exchange",
            ),
        ),
    ))
    reg.register(ExchangeProviderSpec(
        id="mt5", label="MetaTrader 5 Broker",
        kind="broker", runtime="python",
        aliases=("metatrader5", "metatrader"),
        factory=_mt5_factory,
        install_hint=("install MetaTrader 5 + your broker's terminal, "
                      "then `pip install MetaTrader5` on Windows."),
        install_command="pip install MetaTrader5",
        docs_url="https://www.mql5.com/en/docs/integration/python_metatrader5",
        links={"docs": "https://www.mql5.com/en/docs/integration/python_metatrader5"},
        description=("MetaTrader 5 connector for forex / CFDs / futures. "
                     "Windows only; the Python package speaks to a "
                     "running MT5 terminal over local IPC."),
        supports={"ticker": True, "klines": True, "order_book": False,
                  "balances": True, "place_order": True},
        instrument_types=("fx", "cfd", "future", "equity"),
        credential_fields=(
            CredentialField(
                name="server", label="Broker Server", kind="public",
                sensitive=False,
                description="Server name from the MT5 terminal (e.g. ICMarkets-Demo).",
                placeholder="ICMarkets-Demo", vault_scope="exchange",
            ),
            CredentialField(
                name="login", label="MT5 Login", kind="public",
                sensitive=False, description="Numeric account login.",
                placeholder="12345678", vault_scope="exchange",
            ),
            CredentialField(
                name="password", label="MT5 Password", kind="secret",
                description="Investor or master password for the account.",
                vault_scope="exchange",
            ),
            CredentialField(
                name="path", label="Terminal Path (optional)", kind="public",
                sensitive=False, required=False,
                description="Absolute path to terminal64.exe; leave blank to auto-detect.",
                placeholder="C:\\Program Files\\MetaTrader 5\\terminal64.exe",
                vault_scope="exchange",
            ),
        ),
    ))
    reg.register(ExchangeProviderSpec(
        id="alpaca", label="Alpaca Markets",
        kind="broker", runtime="python",
        aliases=("alpaca_markets", "alpaca_data"),
        factory=_alpaca_factory,
        install_hint="pip install alpaca-py",
        install_command="pip install alpaca-py",
        docs_url="https://alpaca.markets/docs/api-references/",
        links={"docs": "https://alpaca.markets/docs/api-references/",
               "api_keys": "https://app.alpaca.markets/paper/dashboard/overview"},
        description=("US equities + crypto broker with a clean REST API. "
                     "Free paper accounts; live trading requires an "
                     "approved brokerage account."),
        supports={"ticker": True, "klines": True, "order_book": True,
                  "balances": True, "place_order": True},
        instrument_types=("equity", "etf", "spot", "option"),
        credential_fields=(
            _CEX_API_KEY, _CEX_API_SECRET,
            CredentialField(
                name="paper", label="Paper Mode", kind="public",
                sensitive=False, required=False,
                description="True to use the paper trading environment (default).",
                placeholder="true", vault_scope="exchange",
            ),
        ),
    ))

    # ------------------------------------------------------------------
    # Pure data-source connectors. ``place_order=False`` so the upsert
    # path automatically forces them into paper-account mode; the agent
    # can still bind a strategy to them for backtests + live data
    # without ever needing trading credentials.
    # ------------------------------------------------------------------

    _DATA_SOURCE_CLASSES = {
        "tushare": TushareConnector,
        "akshare": AkShareConnector,
        "polygon_io": PolygonConnector,
        "coingecko": CoinGeckoConnector,
        "coinmarketcap": CoinMarketCapConnector,
        "glassnode": GlassnodeConnector,
        "dune": DuneConnector,
        "tencent": TencentConnector,
        "moex": MOEXConnector,
        "messari": MessariConnector,
    }

    def _register_data_source(
        venue_id: str, label: str, *, aliases: tuple[str, ...] = (),
        install_command: str = "", install_hint: str = "",
        docs_url: str = "", api_keys_url: str = "",
        token_field_label: str = "API Token",
        token_field_required: bool = False,
        instrument_types: tuple[str, ...] = ("spot",),
    ) -> None:
        creds = ()
        if token_field_required or install_command:
            creds = (
                CredentialField(
                    name="api_key", label=token_field_label, kind="secret",
                    required=token_field_required,
                    description=f"API token issued by {label}.",
                    vault_scope="exchange",
                ),
            )
        connector_cls = _DATA_SOURCE_CLASSES.get(venue_id)
        factory = (_ds_factory(connector_cls) if connector_cls is not None
                   else _data_only_factory(venue_id))
        reg.register(ExchangeProviderSpec(
            id=venue_id, label=label, kind="data_source",
            runtime="python", aliases=aliases,
            factory=factory,
            install_hint=install_hint, install_command=install_command,
            docs_url=docs_url,
            links={"docs": docs_url, "api_keys": api_keys_url} if api_keys_url else {"docs": docs_url},
            description=f"{label} — data-source-only. Pair with a paper account to trade.",
            supports={"ticker": True, "klines": True, "order_book": False,
                      "balances": False, "place_order": False},
            instrument_types=instrument_types,
            credential_fields=creds,
        ))

    _register_data_source(
        "tushare", "Tushare (China A-shares + futures)",
        aliases=("tushare_pro",),
        install_command="pip install tushare",
        install_hint="register at https://tushare.pro/ for a free token, then `pip install tushare`.",
        docs_url="https://tushare.pro/document/2",
        api_keys_url="https://tushare.pro/user/token",
        token_field_label="Tushare Token",
        token_field_required=True,
        instrument_types=("equity", "future", "fx"),
    )
    _register_data_source(
        "akshare", "AkShare (multi-market open data)",
        install_command="pip install akshare",
        install_hint="`pip install akshare` — no API key required.",
        docs_url="https://akshare.akfamily.xyz/",
        instrument_types=("equity", "future", "fx", "spot"),
    )
    _register_data_source(
        "polygon_io", "Polygon.io (US equities + crypto)",
        aliases=("polygon",),
        install_command="pip install polygon-api-client",
        install_hint="register at https://polygon.io/ for a token, then `pip install polygon-api-client`.",
        docs_url="https://polygon.io/docs",
        api_keys_url="https://polygon.io/dashboard/api-keys",
        token_field_label="Polygon API Key",
        token_field_required=True,
        instrument_types=("equity", "option", "fx", "spot"),
    )
    _register_data_source(
        "coingecko", "CoinGecko (crypto market data)",
        install_command="pip install pycoingecko",
        install_hint="`pip install pycoingecko` — free tier needs no key.",
        docs_url="https://docs.coingecko.com/reference/introduction",
        api_keys_url="https://www.coingecko.com/en/developers/dashboard",
        token_field_label="Pro API Key (optional)",
        token_field_required=False,
        instrument_types=("spot",),
    )
    _register_data_source(
        "coinmarketcap", "CoinMarketCap",
        install_command="pip install requests",
        install_hint="register at https://coinmarketcap.com/api/ for a token.",
        docs_url="https://coinmarketcap.com/api/documentation/v1/",
        api_keys_url="https://coinmarketcap.com/api/",
        token_field_label="CMC API Key",
        token_field_required=True,
    )
    _register_data_source(
        "glassnode", "Glassnode (on-chain metrics)",
        install_command="",
        install_hint="register at https://glassnode.com/ for a token.",
        docs_url="https://docs.glassnode.com/",
        api_keys_url="https://studio.glassnode.com/settings/api",
        token_field_label="Glassnode API Key",
        token_field_required=True,
        instrument_types=("spot", "onchain"),
    )
    _register_data_source(
        "dune", "Dune Analytics (on-chain SQL)",
        install_command="pip install dune-client",
        install_hint="register at https://dune.com/ for a token.",
        docs_url="https://docs.dune.com/api-reference/overview/introduction",
        api_keys_url="https://dune.com/settings/api",
        token_field_label="Dune API Key",
        token_field_required=True,
        instrument_types=("onchain",),
    )
    _register_data_source(
        "messari", "Messari (crypto + on-chain)",
        docs_url="https://messari.io/api/docs",
        api_keys_url="https://messari.io/api",
        token_field_label="Messari API Key",
        token_field_required=False,
    )
    _register_data_source(
        "tencent", "Tencent Stock (HK + CN free quotes)",
        install_command="",
        install_hint="no install required — uses public Tencent quote endpoints.",
        docs_url="https://qt.gtimg.cn/",
        instrument_types=("equity",),
    )
    _register_data_source(
        "moex", "MOEX (Russian Stock Exchange)",
        install_command="pip install apimoex",
        install_hint="`pip install apimoex` — public ISS API, no token needed.",
        docs_url="https://iss.moex.com/iss/reference/",
        instrument_types=("equity", "fx", "future"),
    )

    # Generic HTTP data source — operators / agent supply URL +
    # ``provider_config.headers`` (vault-aware) + json_paths. No
    # upstream SDK required.
    reg.register(ExchangeProviderSpec(
        id="http", label="Generic HTTP Data Source",
        kind="data_source", runtime="python",
        aliases=("http_data", "rest"),
        factory=_http_source_factory,
        install_command="",
        install_hint=(
            "Configure provider_config.url, provider_config.headers (with "
            "optional vault://... refs), and provider_config.json_paths. "
            "No additional install required."
        ),
        docs_url="",
        description=(
            "Generic REST data source — point at any JSON HTTP endpoint, "
            "configure custom auth headers (Bearer, X-API-Key, …) with "
            "vault-backed values, and map JSON paths to ticker fields."
        ),
        supports={"ticker": True, "klines": False, "order_book": False,
                  "balances": False, "place_order": False},
        instrument_types=("spot", "equity", "fx", "future", "onchain"),
        credential_fields=(
            CredentialField(
                name="auth_token", label="Auth Token (optional)",
                kind="secret", required=False,
                description=(
                    "Optional shared token; reference it from "
                    "provider_config.headers as `vault://acct_<id>_auth_token`."
                ),
                vault_scope="exchange",
            ),
        ),
    ))


__all__ = [
    "CredentialField", "ExchangeProviderSpec", "ExchangeProviderRegistry",
    "get_registry", "reset_registry",
]

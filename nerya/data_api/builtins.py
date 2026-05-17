"""Built-in read-only data API providers."""

from __future__ import annotations

import importlib
import inspect
import re
from typing import Any

from .registry import DataApiRegistry
from .types import DataActionSpec, DataApiContext, DataApiError


AKSHARE_DOCS = "https://akshare.akfamily.xyz/data/index.html"
AKSHARE_HTTP_DOCS = "https://akshare.akfamily.xyz/deploy_http.html"

_AKSHARE_CURATED_SCHEMAS: dict[str, dict[str, Any]] = {
    "stock_zh_a_hist": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "A-share code, e.g. 000001"},
            "period": {"type": "string", "default": "daily"},
            "start_date": {"type": "string", "description": "YYYYMMDD"},
            "end_date": {"type": "string", "description": "YYYYMMDD"},
            "adjust": {"type": "string", "default": ""},
        },
        "required": ["symbol"],
        "additionalProperties": True,
    },
    "stock_zh_a_spot_em": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "stock_hk_spot_em": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "fund_etf_spot_em": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "macro_china_cpi": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

_AKSHARE_CURATED_DESCRIPTIONS: dict[str, str] = {
    "stock_zh_a_hist": "A-share historical price table.",
    "stock_zh_a_spot_em": "A-share realtime snapshot table from Eastmoney.",
    "stock_hk_spot_em": "Hong Kong stock realtime snapshot table.",
    "fund_etf_spot_em": "ETF realtime snapshot table.",
    "macro_china_cpi": "China CPI macro data table.",
}

_PUBLIC_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class AkShareDataProvider:
    """Lazy AkShare function bridge.

    AkShare exposes a large read-only function catalog, so this adapter
    discovers names and signatures on demand instead of putting hundreds
    of functions into the model prompt.
    """

    provider = "akshare"

    def list_actions(
        self,
        *,
        query: str = "",
        tags: tuple[str, ...] = (),
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query_l = (query or "").strip().lower()
        try:
            module = importlib.import_module("akshare")
        except Exception:
            rows = []
            for name in sorted(_AKSHARE_CURATED_DESCRIPTIONS):
                row = self._preview(name, None, available=False)
                if query_l and not _row_matches(row, query_l):
                    continue
                rows.append(row)
                if len(rows) >= limit:
                    break
            return rows

        rows: list[dict[str, Any]] = []
        for name in sorted(dir(module)):
            if not self._is_public_action(name):
                continue
            fn = getattr(module, name, None)
            if not callable(fn) or inspect.isclass(fn):
                continue
            row = self._preview(name, fn, available=True)
            if query_l and not _row_matches(row, query_l):
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    def schema(self, action: str) -> dict[str, Any]:
        action_name = self._validate_action(action)
        schema = _AKSHARE_CURATED_SCHEMAS.get(action_name)
        if schema is None:
            try:
                fn = getattr(importlib.import_module("akshare"), action_name)
            except Exception as exc:
                raise DataApiError(
                    "akshare is not installed or the function is unavailable",
                    kind="not_found",
                    detail={
                        "provider": "akshare",
                        "action": action_name,
                        "install_hint": "pip install akshare",
                    },
                retryable=False,
            ) from exc
            schema = _schema_from_signature(fn)
            description = _first_doc_line(fn)
        else:
            description = _AKSHARE_CURATED_DESCRIPTIONS.get(action_name, "")
        return {
            "provider": "akshare",
            "action": action_name,
            "title": action_name,
            "description": description,
            "tags": ["akshare", "market", "macro", "table"],
            "docs_url": AKSHARE_DOCS,
            "input_schema": schema,
        }

    def call(
        self,
        action: str,
        args: dict[str, Any],
        *,
        context: DataApiContext,
    ) -> Any:
        action_name = self._validate_action(action)
        try:
            module = importlib.import_module("akshare")
        except Exception as exc:
            raise DataApiError(
                "akshare is not installed; install it or expose AkShare through AKTools/HTTP",
                kind="provider_error",
                detail={
                    "provider": "akshare",
                    "action": action_name,
                    "install_hint": "pip install akshare",
                    "http_docs": AKSHARE_HTTP_DOCS,
                },
                retryable=False,
            ) from exc
        fn = getattr(module, action_name, None)
        if not callable(fn) or inspect.isclass(fn):
            raise DataApiError(
                f"akshare action not found: {action_name}",
                kind="not_found",
                detail={"provider": "akshare", "action": action_name},
                retryable=False,
            )
        try:
            return fn(**dict(args or {}))
        except TypeError as exc:
            raise DataApiError(
                f"akshare.{action_name} rejected the provided arguments",
                kind="schema_validation",
                detail={"error": str(exc), "input_schema": self.schema(action_name)["input_schema"]},
                retryable=False,
            ) from exc

    def _preview(
        self,
        name: str,
        fn: Any | None,
        *,
        available: bool,
    ) -> dict[str, Any]:
        description = _AKSHARE_CURATED_DESCRIPTIONS.get(name)
        if not description and fn is not None:
            description = _first_doc_line(fn)
        row = {
            "provider": "akshare",
            "action": name,
            "title": name,
            "description": description or "AkShare read-only data function.",
            "tags": ["akshare", "market", "macro", "table"],
            "output_kind": "table",
            "available": available,
            "docs_url": AKSHARE_DOCS,
        }
        if fn is not None:
            try:
                row["signature"] = str(inspect.signature(fn))
            except Exception:
                pass
        if not available:
            row["install_hint"] = "pip install akshare"
        return row

    def _validate_action(self, action: str) -> str:
        action_name = str(action or "").strip()
        if not self._is_public_action(action_name):
            raise DataApiError(
                "akshare action must be a public function name",
                kind="schema_validation",
                detail={"action": action},
                retryable=False,
            )
        return action_name

    @staticmethod
    def _is_public_action(name: str) -> bool:
        return bool(_PUBLIC_ACTION_RE.match(name or "")) and not name.startswith("_")


def build_data_api_registry() -> DataApiRegistry:
    registry = DataApiRegistry()
    registry.register_provider(AkShareDataProvider())
    for spec in _wallet_specs():
        registry.register_action(spec)
    for spec in _onchainos_specs():
        registry.register_action(spec)
    for alias in (
        "wallets",
        "wallet_provider",
        "agentic_wallet",
        "agentic_wallets",
        "xagt",
        "xagt_agent_plugin",
        "xagent",
        "x_agent",
        "xagt_onchain",
    ):
        registry.register_provider_alias(alias, "wallet")
    for alias in (
        "onchain",
        "onchain_os",
        "okx_os",
        "okx_onchain",
        "okx_agentic_wallet",
    ):
        registry.register_provider_alias(alias, "onchainos")
    return registry


def _wallet_specs() -> list[DataActionSpec]:
    return [
        DataActionSpec(
            provider="wallet",
            action="list_sources",
            title="List wallet data sources",
            description=(
                "List configured wallet bindings and wallet-backed market data "
                "sources without exposing secrets. Use this before claiming "
                "XAgent/xagt_agent_plugin/agentic wallet or on-chain meme/DEX "
                "data is unavailable."
            ),
            tags=("wallet", "onchain", "catalog", "xagt", "xagent", "dex", "meme", "token"),
            output_kind="json",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_wallet_list_sources,
        ),
        DataActionSpec(
            provider="wallet",
            action="readiness",
            title="Wallet provider readiness",
            description=(
                "Return wallet provider dependency readiness and static "
                "read/write capabilities for OnchainOS, XAgent/xagt, and other "
                "wallet-backed on-chain data sources."
            ),
            tags=("wallet", "onchain", "catalog", "xagt", "xagent", "dex", "meme", "token"),
            output_kind="json",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_wallet_readiness,
        ),
        DataActionSpec(
            provider="wallet",
            action="capability_catalog",
            title="Wallet capability catalog",
            description=(
                "Return a compact catalog of configured/logged-in wallet "
                "provider functions, callable read-only data_api actions, "
                "wallet-backed market_data venues, and gated execution paths. "
                "Use this before building on-chain or meme strategies."
            ),
            tags=(
                "wallet",
                "onchain",
                "catalog",
                "capabilities",
                "xagt",
                "xagent",
                "dex",
                "meme",
                "token",
            ),
            output_kind="json",
            input_schema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "default": "all",
                        "description": "Use 'meme' to return only meme-relevant read actions.",
                    },
                    "include_live_status": {
                        "type": "boolean",
                        "default": True,
                        "description": "When true, also probes OnchainOS wallet status and redacts sensitive fields.",
                    },
                    "timeout_s": {"type": "number", "default": 8},
                },
                "additionalProperties": False,
            },
            handler=_wallet_capability_catalog,
        ),
        DataActionSpec(
            provider="wallet",
            action="meme_strategy_guide",
            title="Meme strategy wallet data guide",
            description=(
                "Return the recommended data workflow for on-chain meme "
                "strategy research, honest backtests, wallet balance/quote "
                "checks, and live-trading guardrails."
            ),
            tags=("wallet", "onchain", "strategy", "meme", "dex", "token", "guide"),
            output_kind="json",
            input_schema={
                "type": "object",
                "properties": {
                    "chain": {"type": "string", "default": "solana"},
                    "token": {
                        "type": "string",
                        "description": "Optional token contract selected after discovery.",
                    },
                },
                "additionalProperties": False,
            },
            handler=_wallet_meme_strategy_guide,
        ),
        DataActionSpec(
            provider="wallet",
            action="balance",
            title="Read wallet balance",
            description="Read a wallet token balance through a configured wallet provider.",
            tags=("wallet", "balance", "onchain", "token"),
            output_kind="json",
            input_schema={
                "type": "object",
                "properties": {
                    "wallet_id": {"type": "string"},
                    "provider": {"type": "string"},
                    "chain": {"type": "string"},
                    "address": {"type": "string"},
                    "token": {"type": "string", "default": "native"},
                },
                "required": ["chain", "address"],
                "additionalProperties": True,
            },
            handler=_wallet_balance,
        ),
        DataActionSpec(
            provider="wallet",
            action="quote",
            title="Read wallet swap quote",
            description="Get a read-only token swap quote through a configured wallet provider.",
            tags=("wallet", "quote", "onchain", "dex", "token"),
            output_kind="json",
            input_schema={
                "type": "object",
                "properties": {
                    "wallet_id": {"type": "string"},
                    "provider": {"type": "string"},
                    "chain": {"type": "string"},
                    "token_in": {"type": "string"},
                    "token_out": {"type": "string"},
                    "amount_in": {"type": "number"},
                    "slippage_bps": {"type": "integer", "default": 50},
                },
                "required": ["chain", "token_in", "token_out", "amount_in"],
                "additionalProperties": True,
            },
            handler=_wallet_quote,
        ),
    ]


def _onchainos_specs() -> list[DataActionSpec]:
    commands = sorted(_ONCHAINOS_COMMANDS)
    command_enum = {"type": "string", "enum": commands}
    return [
        DataActionSpec(
            provider="onchainos",
            action="cli_read",
            title="Run an allowlisted OnchainOS read command",
            description=(
                "Read OnchainOS wallet, DeFi, token, and portfolio data "
                "through an allowlisted command map. Use for on-chain "
                "meme/DEX strategy discovery before falling back to CEX proxy data."
            ),
            tags=("onchainos", "onchain", "wallet", "defi", "dex", "meme", "token"),
            output_kind="json",
            input_schema={
                "type": "object",
                "properties": {
                    "command": command_enum,
                    "params": {"type": "object", "additionalProperties": True},
                    "timeout_s": {"type": "number", "default": 30},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=_onchainos_cli_read,
        ),
        *[
            DataActionSpec(
                provider="onchainos",
                action=name,
                title=f"OnchainOS {name}",
                description=meta["description"],
                tags=("onchainos", "onchain", "dex", "meme", "token", *tuple(meta.get("tags") or ())),
                output_kind="json",
                input_schema=meta["schema"],
                handler=_make_onchainos_handler(name),
            )
            for name, meta in sorted(_ONCHAINOS_COMMANDS.items())
        ],
    ]


def _wallet_list_sources(context: DataApiContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..wallet import list_configured_providers, list_wallet_market_data_sources

    config = context.config_data
    bindings = [_safe_binding(row) for row in list_configured_providers(config)]
    return {
        "bindings": bindings,
        "market_data_sources": list_wallet_market_data_sources(),
    }


def _wallet_readiness(context: DataApiContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    from ..wallet import readiness_report

    return readiness_report(
        context.config_data,
        workspace=context.workspace,
        vault_passphrase=context.vault_passphrase,
    )


def _wallet_capability_catalog(context: DataApiContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..wallet import (
        list_configured_providers,
        list_wallet_market_data_sources,
        market_data_sources_for_provider,
        readiness_report,
    )

    topic = str(args.get("topic") or "all").strip().lower()
    include_live_status = bool(args.get("include_live_status", True))
    timeout_s = _float_arg(args, "timeout_s", default=8.0, minimum=1.0, maximum=30.0)
    bindings = [_safe_binding(row) for row in list_configured_providers(context.config_data)]
    readiness_rows_full = readiness_report(
        context.config_data,
        workspace=context.workspace,
        vault_passphrase=context.vault_passphrase,
    )
    readiness_by_provider = {
        str(row.get("id") or row.get("readiness", {}).get("provider") or "").lower(): row
        for row in readiness_rows_full
        if isinstance(row, dict)
    }
    readiness_rows = [
        _compact_wallet_provider_status(row)
        for row in readiness_rows_full
        if isinstance(row, dict)
    ]

    binding_catalog: list[dict[str, Any]] = []
    for binding in bindings:
        provider_name = str(binding.get("provider") or "").lower()
        readiness = readiness_by_provider.get(provider_name) or {}
        market_sources = market_data_sources_for_provider(provider_name)
        binding_catalog.append({
            **binding,
            "ready": bool((readiness.get("readiness") or {}).get("ready")),
            "stability": readiness.get("stability"),
            "chains": ((readiness.get("capabilities") or {}).get("chains") or []),
            "functions": _wallet_binding_functions(binding, readiness, market_sources),
        })

    live_status: dict[str, Any] = {}
    if include_live_status:
        try:
            live_status["onchainos"] = _redact_sensitive(
                _run_onchainos_command(context, "wallet_status", {}, timeout_s=timeout_s)
            )
        except Exception as exc:  # noqa: BLE001 - status is best-effort catalog data.
            live_status["onchainos"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    chain = str(args.get("chain") or "solana").strip() or "solana"
    token = str(args.get("token") or "").strip()
    selection = _wallet_data_selection(
        binding_catalog,
        readiness_rows,
        chain=chain,
        token=token,
        live_status=live_status,
    )

    return {
        "bindings": binding_catalog,
        "providers": readiness_rows,
        "market_data_sources": list_wallet_market_data_sources(),
        "selection": selection,
        "callable_read_actions": {
            "wallet": _wallet_global_actions(),
            "onchainos": _onchainos_read_catalog(topic=topic),
        },
        "gated_execution_actions": _gated_wallet_actions(),
        "live_status": live_status,
        "meme_strategy_workflow": _meme_strategy_workflow(chain=chain, token=token, selection=selection),
        "rules": [
            "Do not infer wallet-backed on-chain data from connector_list; use data_api wallet/onchainos first.",
            "Choose the data route from installed/logged-in wallet bindings; do not hardcode OKX/XAgent/GOAT.",
            "When no wallet binding is ready, bootstrap the GOAT/self_custody fallback for generic ONCHAIN candles and recommend installing richer wallet providers.",
            "Use data_api for discovery/enrichment, market_data for OHLCV, and trade_intent_submit/strategy runtime for execution.",
            "data_api is read-only; transfers, swaps, signing, bridge, and calldata-building stay behind RiskGate/ApprovalGate/live flags.",
            "For an on-chain meme backtest, CEX pairs such as DOGEUSDT are proxies only, not valid chain-native evidence.",
        ],
    }


def _compact_wallet_provider_status(row: dict[str, Any]) -> dict[str, Any]:
    provider_id = str(row.get("id") or row.get("readiness", {}).get("provider") or "")
    return {
        "id": provider_id,
        "label": row.get("label"),
        "configured_wallet_id": row.get("configured_wallet_id"),
        "readiness": _compact_wallet_readiness(row.get("readiness")),
        "capabilities": _compact_wallet_capabilities(row.get("capabilities")),
        "stability": row.get("stability"),
        "market_data_sources": row.get("market_data_sources") or [],
        "install_hint": row.get("install_hint"),
        "install_command": row.get("install_command"),
        "install_alternatives": row.get("install_alternatives") or [],
    }


def _compact_wallet_readiness(readiness: Any) -> dict[str, Any] | None:
    if not isinstance(readiness, dict):
        return None
    out = {
        "provider": readiness.get("provider"),
        "ready": bool(readiness.get("ready")),
        "installed": bool(readiness.get("installed", True)),
        "missing": readiness.get("missing") or [],
        "reason": _short_text(readiness.get("reason"), 180),
    }
    if not out["ready"]:
        out["install_hint"] = _short_text(readiness.get("install_hint"), 220)
    return out


def _compact_wallet_capabilities(caps: Any) -> dict[str, Any] | None:
    if not isinstance(caps, dict):
        return None
    methods: dict[str, Any] = {}
    for name in ("balance", "quote", "swap", "market_data"):
        raw = caps.get(name)
        if not isinstance(raw, dict):
            continue
        methods[name] = {
            "supported": bool(raw.get("supported")),
            "status": raw.get("status"),
        }
    return {
        "methods": methods,
        "execution_profile": caps.get("execution_profile"),
        "chains": caps.get("chains") or [],
    }


def _short_text(value: Any, limit: int = 180) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _wallet_data_selection(
    bindings: list[dict[str, Any]],
    providers: list[dict[str, Any]],
    *,
    chain: str,
    token: str,
    live_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token_hint = token or "<token_contract>"
    routes = _wallet_market_data_routes(bindings, chain=chain, token=token_hint)
    ready_routes = [row for row in routes if row.get("ready")]
    selected = ready_routes[0] if ready_routes else None
    no_wallet_ready = selected is None
    fallback = _goat_self_custody_fallback(chain=chain, token=token_hint, active=no_wallet_ready)
    installed = [row for row in bindings if row.get("ready")]
    onchainos_logged_in = _onchainos_logged_in(live_status or {})
    return {
        "mode": "wallet_binding" if selected else "goat_self_custody_fallback",
        "installed_logged_in_wallets": [
            {
                "wallet_id": row.get("wallet_id"),
                "provider": row.get("provider"),
                "label": row.get("label"),
                "market_data_ready": any(
                    route.get("wallet_id") == row.get("wallet_id") and route.get("ready")
                    for route in routes
                ),
            }
            for row in installed
        ],
        "onchainos_logged_in": onchainos_logged_in,
        "selected_route": selected or fallback["market_data_route"],
        "available_routes": routes,
        "fallback": fallback,
        "install_recommendations": _wallet_install_recommendations(
            bindings,
            providers,
            include_self_custody=no_wallet_ready,
        ),
        "routing_rules": [
            "Prefer a ready wallet binding that exposes chain:token market data for meme tokens.",
            "Use OKX OnchainOS/XAgent for discovery, security, holders, traders, and memepump feeds when logged in.",
            "Use generic ONCHAIN candles as the fallback when no richer wallet data source is installed/logged in.",
            "Execution remains gated; this selection is for read-only data and sizing evidence.",
        ],
    }


def _wallet_market_data_routes(
    bindings: list[dict[str, Any]],
    *,
    chain: str,
    token: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for binding in bindings:
        for fn in binding.get("functions") or []:
            if not isinstance(fn, dict) or fn.get("action") != "market_data.get_candles":
                continue
            venue = str(fn.get("venue") or "")
            canonical = str(fn.get("canonical") or venue.upper())
            market_format = str(fn.get("market_format") or "")
            if market_format == "chain:token":
                market = f"{canonical}:{chain}:{token}"
            else:
                market = f"{canonical}:<market>"
            out.append({
                "wallet_id": binding.get("wallet_id"),
                "provider": binding.get("provider"),
                "label": binding.get("label"),
                "ready": bool(binding.get("ready")),
                "priority": _wallet_route_priority(str(binding.get("provider") or ""), market_format),
                "venue": venue,
                "canonical": canonical,
                "market_format": market_format,
                "market": market,
                "call": {
                    "action": "get_candles",
                    "venue": venue,
                    "market": market,
                    "interval": "5m",
                    "count": 300,
                },
                "use_for": _route_use_cases(str(binding.get("provider") or ""), market_format),
                "note": fn.get("note"),
            })
    out.sort(key=lambda row: (not bool(row.get("ready")), int(row.get("priority") or 100)))
    return out


def _wallet_route_priority(provider: str, market_format: str) -> int:
    provider_l = provider.lower()
    if provider_l == "okx_os":
        return 10
    if provider_l == "xagt_agent_plugin":
        return 20
    if provider_l == "bitget":
        return 30
    if market_format == "chain:token":
        return 40
    if provider_l == "binance_agentic":
        return 60
    if provider_l == "coinbase":
        return 70
    return 90


def _route_use_cases(provider: str, market_format: str) -> list[str]:
    provider_l = provider.lower()
    if provider_l in {"okx_os", "xagt_agent_plugin"}:
        return ["meme discovery", "token risk enrichment", "chain-native OHLCV", "wallet balance/quote checks"]
    if provider_l == "bitget":
        return ["token OHLCV", "wallet quote/balance checks", "secondary meme evidence"]
    if provider_l == "binance_agentic":
        return ["Binance Alpha candles", "wallet session checks", "not enough for arbitrary token-contract backtests"]
    if provider_l == "coinbase":
        return ["Coinbase product candles", "Base wallet operations", "not enough for arbitrary token-contract backtests"]
    if market_format == "chain:token":
        return ["chain-native OHLCV"]
    return ["wallet-backed market data"]


def _goat_self_custody_fallback(*, chain: str, token: str, active: bool) -> dict[str, Any]:
    return {
        "active": bool(active),
        "provider": "self_custody",
        "label": "GOAT/self-custody fallback",
        "why": (
            "No installed/logged-in wallet binding exposes a ready meme data route; "
            "bootstrap a minimal self-custody/GOAT lane for generic on-chain candles."
        ),
        "install": {
            "provider": "self_custody",
            "tool": "wallet_install",
            "tool_call": {"provider": "self_custody", "mode": "goat", "approve": True},
            "endpoint": "/wallet/install",
            "payload": {"provider": "self_custody", "approve": True},
            "automatic_only_when": [
                "runtime.allow_auto_install is true",
                "or NERYA_ALLOW_AUTO_INSTALL=1 is set",
                "or the operator explicitly approves the install request",
            ],
            "commands": [
                "npm:@goat-sdk/core",
                "npm:@goat-sdk/wallet-viem",
                "pip install eth-account web3 solders solana",
            ],
            "docs": "https://github.com/goat-sdk/goat",
        },
        "market_data_route": {
            "wallet_id": "self_custody_fallback",
            "provider": "self_custody",
            "ready": False,
            "fallback": True,
            "venue": "onchain",
            "canonical": "ONCHAIN",
            "market_format": "chain:token",
            "market": f"ONCHAIN:{chain}:{token}",
            "call": {
                "action": "get_candles",
                "venue": "onchain",
                "market": f"ONCHAIN:{chain}:{token}",
                "interval": "5m",
                "count": 300,
            },
            "limitations": [
                "Generic candles do not replace wallet-native token discovery, security scan, holder, and smart-money data.",
                "Quote/swap support depends on the configured GOAT/self-custody signer and plugins.",
            ],
        },
    }


def _wallet_install_recommendations(
    bindings: list[dict[str, Any]],
    providers: list[dict[str, Any]],
    *,
    include_self_custody: bool,
) -> list[dict[str, Any]]:
    configured = {str(row.get("provider") or "").lower() for row in bindings}
    provider_rows = {str(row.get("id") or "").lower(): row for row in providers}
    priority = [
        ("okx_os", "Best default for meme/token discovery, OnchainOS reads, security, holders, traders, and OHLCV."),
        ("xagt_agent_plugin", "Useful when the logged-in XAgent wallet already carries OnchainOS-backed data."),
        ("bitget", "Useful secondary wallet market-data and quote/balance source."),
        ("binance_agentic", "Useful for Binance Alpha symbols; not enough alone for arbitrary contract memes."),
        ("coinbase", "Useful for Coinbase/Base wallet/product data; weaker for long-tail meme discovery."),
    ]
    if include_self_custody:
        priority.insert(0, (
            "self_custody",
            "Install GOAT/self-custody fallback when no wallet is installed or logged in.",
        ))
    out: list[dict[str, Any]] = []
    for provider_id, why in priority:
        row = provider_rows.get(provider_id) or {}
        readiness = row.get("readiness") if isinstance(row.get("readiness"), dict) else {}
        already_configured = provider_id in configured
        ready = bool(readiness.get("ready")) if isinstance(readiness, dict) else False
        if already_configured and ready:
            continue
        out.append({
            "provider": provider_id,
            "label": row.get("label"),
            "reason": why,
            "configured": already_configured,
            "ready": ready,
            "next_step": (
                "complete login/device approval and re-run wallet.capability_catalog"
                if already_configured
                else "install provider, finish login, then re-run wallet.capability_catalog"
            ),
            "install_hint": (readiness or {}).get("install_hint") or row.get("install_hint"),
            "install_command": row.get("install_command"),
            "install_alternatives": row.get("install_alternatives") or [],
        })
    return out[:5]


def _onchainos_logged_in(live_status: dict[str, Any]) -> bool | None:
    status = live_status.get("onchainos")
    if not isinstance(status, dict):
        return None
    for key in ("loggedIn", "logged_in", "isLoggedIn", "authenticated"):
        if key in status:
            return bool(status.get(key))
    data = status.get("data")
    if isinstance(data, dict):
        for key in ("loggedIn", "logged_in", "isLoggedIn", "authenticated"):
            if key in data:
                return bool(data.get(key))
    return None


def _wallet_meme_strategy_guide(context: DataApiContext, args: dict[str, Any]) -> dict[str, Any]:
    chain = str(args.get("chain") or "solana").strip() or "solana"
    token = str(args.get("token") or "").strip()
    catalog = _wallet_capability_catalog(
        context,
        {
            "topic": "meme",
            "include_live_status": False,
            "chain": chain,
            "token": token,
        },
    )
    selection = catalog.get("selection") if isinstance(catalog, dict) else {}
    return {
        "selection": selection,
        "workflow": _meme_strategy_workflow(chain=chain, token=token, selection=selection),
        "minimum_evidence": [
            "wallet.capability_catalog is called first and its selection.selected_route is used instead of hardcoding a wallet.",
            "If selection.mode is goat_self_custody_fallback, install/approve GOAT/self_custody first and treat generic ONCHAIN candles as fallback evidence.",
            "candidate tokens come from onchainos token_hot_tokens or memepump_tokens, not a CEX ticker substitution.",
            "token risk is checked with token_report plus security_token_scan before strategy generation.",
            "historical replay uses market_data get_candles on OKX_ONCHAIN/XAGT_ONCHAIN/BITGET_ONCHAIN/ONCHAIN with chain:token format.",
            "live execution is never started from data_api; it must flow through strategy/trading submit intent, RiskGate, ApprovalGate, and runtime.live_trading_enabled.",
        ],
        "preferred_read_actions": [
            row["action"]
            for row in _onchainos_read_catalog(topic="meme")
            if row.get("provider") == "onchainos"
        ],
        "anti_patterns": [
            "Do not stop after connector_list returns no meme connector; wallet and onchainos data live in data_api.",
            "Do not use provider='coingecko' with data_api; CoinGecko is an MCP namespace.",
            "Do not claim an honest on-chain backtest if only Binance/Coinbase CEX candles were used.",
            "Do not execute wallet swap/send/sign commands directly from a research turn.",
        ],
    }


def _wallet_global_actions() -> list[dict[str, Any]]:
    return [
        {
            "action": "wallet.list_sources",
            "read_only": True,
            "call": {"op": "call", "provider": "wallet", "action": "list_sources"},
        },
        {
            "action": "wallet.readiness",
            "read_only": True,
            "call": {"op": "call", "provider": "wallet", "action": "readiness"},
        },
        {
            "action": "wallet.capability_catalog",
            "read_only": True,
            "call": {
                "op": "call",
                "provider": "wallet",
                "action": "capability_catalog",
                "args": {"topic": "meme"},
            },
        },
        {
            "action": "wallet.meme_strategy_guide",
            "read_only": True,
            "call": {
                "op": "call",
                "provider": "wallet",
                "action": "meme_strategy_guide",
            },
        },
    ]


def _wallet_binding_functions(
    binding: dict[str, Any],
    readiness: dict[str, Any],
    market_sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    wallet_id = str(binding.get("wallet_id") or "")
    provider = str(binding.get("provider") or "")
    capabilities = readiness.get("capabilities") if isinstance(readiness, dict) else {}
    if not isinstance(capabilities, dict):
        capabilities = {}
    out: list[dict[str, Any]] = []
    for name in ("balance", "quote"):
        cap = capabilities.get(name) if isinstance(capabilities.get(name), dict) else {}
        if not cap.get("supported"):
            continue
        args_template: dict[str, Any] = {"wallet_id": wallet_id}
        if name == "balance":
            args_template.update({"chain": "<chain>", "address": "<wallet>", "token": "native"})
        else:
            args_template.update({
                "chain": "<chain>",
                "token_in": "<token_in>",
                "token_out": "<token_out>",
                "amount_in": 0,
                "slippage_bps": 50,
            })
        out.append({
            "action": f"wallet.{name}",
            "provider": provider,
            "wallet_id": wallet_id,
            "status": cap.get("status"),
            "note": _short_text(cap.get("note"), 160),
            "read_only": True,
            "call": {
                "op": "call",
                "provider": "wallet",
                "action": name,
                "args": args_template,
            },
        })

    swap_cap = capabilities.get("swap") if isinstance(capabilities.get("swap"), dict) else {}
    if swap_cap.get("supported"):
        out.append({
            "action": "wallet.swap",
            "provider": provider,
            "wallet_id": wallet_id,
            "status": swap_cap.get("status"),
            "note": _short_text(swap_cap.get("note"), 160),
            "read_only": False,
            "call_path": "strategy ctx.trading.submit_intent or native trade_intent_submit",
            "guardrails": [
                "requires runtime.live_trading_enabled for live execution",
                "requires RiskGate and ApprovalGate",
                "not executable through data_api",
            ],
        })

    for source in market_sources:
        source = dict(source)
        venue = str(source.get("venue") or source.get("canonical") or "")
        market_format = str(source.get("market_format") or "")
        if market_format == "chain:token":
            market_template = f"{source.get('canonical') or venue.upper()}:<chain>:<token_contract>"
        else:
            market_template = f"{source.get('canonical') or venue.upper()}:<market>"
        out.append({
            "action": "market_data.get_candles",
            "provider": provider,
            "wallet_id": wallet_id,
            "venue": venue,
            "canonical": source.get("canonical"),
            "market_format": market_format,
            "read_only": True,
            "call": {
                "action": "get_candles",
                "venue": venue,
                "market": market_template,
                "interval": "5m",
                "count": 200,
            },
            "note": _short_text(source.get("description"), 160),
        })
    return out


def _onchainos_read_catalog(*, topic: str = "all") -> list[dict[str, Any]]:
    topic_l = str(topic or "all").lower()
    topic_tags = {
        "meme": {"meme", "memepump", "token", "signal", "smart_money", "security", "trader", "dex", "social"},
        "wallet": {"wallet", "portfolio", "balance", "defi"},
    }.get(topic_l)
    out: list[dict[str, Any]] = []
    for name, meta in sorted(_ONCHAINOS_COMMANDS.items()):
        tags = {"onchainos", *[str(t) for t in (meta.get("tags") or ())]}
        if topic_tags and not tags.intersection(topic_tags):
            continue
        required = [str(v) for v in (meta.get("required") or ())]
        out.append({
            "provider": "onchainos",
            "action": name,
            "command": " ".join(str(v) for v in meta.get("base") or ()),
            "tags": sorted(tags),
            "required": required,
            "schema_call": {"op": "schema", "provider": "onchainos", "action": name},
        })
    return out


def _gated_wallet_actions() -> list[dict[str, Any]]:
    return [
        {
            "surface": "onchainos",
            "commands": [
                "wallet send",
                "wallet sign-message",
                "wallet contract-call",
                "wallet gas-station",
                "swap",
                "cross-chain",
                "gateway",
                "defi deposit/redeem/claim/invest/withdraw/collect",
                "payment",
                "competition",
            ],
            "reason": "May transfer funds, sign messages, build/broadcast transactions, or mutate wallet/account state.",
            "required_path": "strategy/trading submit intent with RiskGate, ApprovalGate, and explicit live-trading enablement.",
        },
        {
            "surface": "wallet login/session",
            "commands": ["wallet login", "wallet verify", "wallet add", "wallet switch", "wallet logout"],
            "reason": "Mutates the local wallet session or active account.",
            "required_path": "operator account/wallet onboarding flow, not autonomous strategy research.",
        },
    ]


def _meme_strategy_workflow(
    *,
    chain: str,
    token: str = "",
    selection: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    chain = chain or "solana"
    token_hint = token or "<token_contract>"
    selection = selection if isinstance(selection, dict) else {}
    selected_route = selection.get("selected_route") if isinstance(selection.get("selected_route"), dict) else {}
    selected_call = selected_route.get("call") if isinstance(selected_route.get("call"), dict) else {
        "action": "get_candles",
        "venue": "onchain",
        "market": f"ONCHAIN:{chain}:{token_hint}",
        "interval": "5m",
        "count": 300,
    }
    fallback = selection.get("fallback") if isinstance(selection.get("fallback"), dict) else {}
    install_step: list[dict[str, Any]] = []
    if fallback.get("active"):
        install_step.append({
            "step": "bootstrap_goat_self_custody_fallback",
            "tool": "wallet_install",
            "call": (fallback.get("install") or {}).get("tool_call") or {
                "provider": "self_custody",
                "mode": "goat",
                "approve": True,
            },
            "purpose": "No wallet is installed/logged in; install GOAT/self-custody fallback before fetching generic ONCHAIN candles.",
        })
    return [
        {
            "step": "discover_wallet_capabilities",
            "tool": "data_api",
            "call": {
                "op": "call",
                "provider": "wallet",
                "action": "capability_catalog",
                "args": {"topic": "meme"},
            },
            "purpose": "Find ready wallet providers, market_data venues, and gated execution paths.",
        },
        *install_step,
        {
            "step": "find_candidates",
            "tool": "data_api",
            "calls": [
                {
                    "op": "call",
                    "provider": "onchainos",
                    "action": "token_hot_tokens",
                    "args": {
                        "chain": chain,
                        "limit": 20,
                        "risk_filter": "true",
                        "stable_token_filter": "true",
                    },
                },
                {
                    "op": "call",
                    "provider": "onchainos",
                    "action": "memepump_tokens",
                    "args": {"chain": chain, "stage": "NEW"},
                },
                {
                    "op": "call",
                    "provider": "onchainos",
                    "action": "signal_list",
                    "args": {"chain": chain, "limit": 20},
                },
            ],
            "purpose": "Use chain-native token/meme/smart-money feeds instead of CEX proxies.",
        },
        {
            "step": "enrich_and_filter",
            "tool": "data_api",
            "calls": [
                {
                    "op": "call",
                    "provider": "onchainos",
                    "action": "token_report",
                    "args": {"chain": chain, "address": token_hint},
                },
                {
                    "op": "call",
                    "provider": "onchainos",
                    "action": "security_token_scan",
                    "args": {"tokens": f"{chain}:{token_hint}"},
                },
                {
                    "op": "call",
                    "provider": "onchainos",
                    "action": "token_holders",
                    "args": {"chain": chain, "address": token_hint, "limit": 50},
                },
                {
                    "op": "call",
                    "provider": "onchainos",
                    "action": "token_top_trader",
                    "args": {"chain": chain, "address": token_hint, "limit": 50},
                },
                {
                    "op": "call",
                    "provider": "onchainos",
                    "action": "token_trades",
                    "args": {"chain": chain, "address": token_hint, "limit": 100},
                },
            ],
            "purpose": "Check liquidity, risk flags, holder concentration, trader quality, and flow before authoring strategy rules.",
        },
        {
            "step": "fetch_historical_ohlcv",
            "tool": "market_data",
            "call": selected_call,
            "purpose": "Only call a backtest honest if this returns real chain-native candles or another durable on-chain replay source.",
        },
        {
            "step": "pre_trade_checks",
            "tool": "data_api",
            "calls": [
                {
                    "op": "call",
                    "provider": "wallet",
                    "action": "balance",
                    "args": {"wallet_id": "<wallet_id>", "chain": chain, "address": "<wallet_address>"},
                },
                {
                    "op": "call",
                    "provider": "wallet",
                    "action": "quote",
                    "args": {
                        "wallet_id": "<wallet_id>",
                        "chain": chain,
                        "token_in": "<base_token>",
                        "token_out": token_hint,
                        "amount_in": 0,
                        "slippage_bps": 100,
                    },
                },
            ],
            "purpose": "Balance and quote are read-only; use them for sizing and slippage assumptions.",
        },
        {
            "step": "execution_guardrail",
            "tool": "trade_intent_submit or strategy runtime",
            "purpose": "If promoted beyond research/paper, execution must be routed through Nerya's RiskGate, ApprovalGate, account/wallet binding, and live flag checks.",
        },
    ]


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if any(part in key_s.lower() for part in ("email", "secret", "token", "key", "passphrase")):
                out[key_s] = "<redacted>"
            else:
                out[key_s] = _redact_sensitive(item)
        return out
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _wallet_balance(context: DataApiContext, args: dict[str, Any]) -> Any:
    provider, _summary = _resolve_wallet_provider(context, args)
    chain = _required(args, "chain")
    address = _required(args, "address")
    token = str(args.get("token") or "native")
    return provider.get_balance(chain=chain, address=address, token=token)


def _wallet_quote(context: DataApiContext, args: dict[str, Any]) -> Any:
    provider, _summary = _resolve_wallet_provider(context, args)
    chain = _required(args, "chain")
    token_in = _required(args, "token_in")
    token_out = _required(args, "token_out")
    try:
        amount_in = float(args.get("amount_in"))
    except (TypeError, ValueError) as exc:
        raise DataApiError(
            "amount_in must be numeric",
            kind="schema_validation",
            detail={"field": "amount_in"},
            retryable=False,
        ) from exc
    slippage_bps = _int_arg(args, "slippage_bps", default=50, minimum=0, maximum=5000)
    return provider.quote(
        chain=chain,
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_in,
        slippage_bps=slippage_bps,
    )


def _resolve_wallet_provider(context: DataApiContext, args: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    from ..wallet import build_provider, list_configured_providers

    wallet_id = str(args.get("wallet_id") or "").strip()
    provider_hint = str(args.get("provider") or "").strip().lower()
    bindings = list_configured_providers(context.config_data)
    selected: dict[str, Any] | None = None
    if wallet_id:
        selected = next((row for row in bindings if row.get("wallet_id") == wallet_id), None)
    if selected is None and provider_hint:
        selected = next((row for row in bindings if row.get("provider") == provider_hint), None)
    if selected is None and bindings and not provider_hint:
        selected = bindings[0]
    if selected is not None:
        provider_name = str(selected.get("provider") or "")
        provider_cfg = dict(selected.get("config") or {})
        return (
            build_provider(
                provider_name,
                provider_cfg,
                workspace=context.workspace,
                vault_passphrase=context.vault_passphrase,
            ),
            _safe_binding(selected),
        )
    if provider_hint:
        return (
            build_provider(
                provider_hint,
                {},
                workspace=context.workspace,
                vault_passphrase=context.vault_passphrase,
            ),
            {"provider": provider_hint, "source": "adhoc"},
        )
    raise DataApiError(
        "no wallet provider is configured; pass provider for an ad-hoc read-only provider",
        kind="not_found",
        detail={"configured": [_safe_binding(row) for row in bindings]},
        retryable=False,
    )


def _safe_binding(row: dict[str, Any]) -> dict[str, Any]:
    cfg = row.get("config") if isinstance(row.get("config"), dict) else {}
    return {
        "wallet_id": row.get("wallet_id"),
        "provider": row.get("provider"),
        "label": row.get("label"),
        "source": row.get("source"),
        "config_keys": sorted(str(k) for k in cfg.keys()),
    }


def _onchainos_cli_read(context: DataApiContext, args: dict[str, Any]) -> Any:
    command = str(args.get("command") or "").strip()
    params = args.get("params")
    if not isinstance(params, dict):
        params = {}
    timeout_s = _float_arg(args, "timeout_s", default=30.0, minimum=1.0, maximum=120.0)
    return _run_onchainos_command(context, command, params, timeout_s=timeout_s)


def _make_onchainos_handler(command: str):
    def handler(context: DataApiContext, args: dict[str, Any]) -> Any:
        timeout_s = _float_arg(args, "timeout_s", default=30.0, minimum=1.0, maximum=120.0)
        params = {k: v for k, v in args.items() if k != "timeout_s"}
        return _run_onchainos_command(context, command, params, timeout_s=timeout_s)

    return handler


def _schema(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
    additional: bool = False,
) -> dict[str, Any]:
    props = dict(properties)
    props.setdefault("timeout_s", {"type": "number", "default": 30})
    out: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": additional,
    }
    if required:
        out["required"] = list(required)
    return out


_ONCHAINOS_COMMANDS: dict[str, dict[str, Any]] = {
    "token_hot_tokens": {
        "description": "Read hot/trending token list ranked by token score or X mentions.",
        "tags": ("token", "meme", "dex", "social"),
        "base": ["token", "hot-tokens"],
        "params": {
            "ranking_type": "--ranking-type",
            "chain": "--chain",
            "rank_by": "--rank-by",
            "time_frame": "--time-frame",
            "risk_filter": "--risk-filter",
            "stable_token_filter": "--stable-token-filter",
            "project_id": "--project-id",
            "price_change_min": "--price-change-min",
            "price_change_max": "--price-change-max",
            "volume_min": "--volume-min",
            "volume_max": "--volume-max",
            "market_cap_min": "--market-cap-min",
            "market_cap_max": "--market-cap-max",
            "liquidity_min": "--liquidity-min",
            "liquidity_max": "--liquidity-max",
            "transaction_min": "--transaction-min",
            "holders_min": "--holders-min",
            "mentioned_count_min": "--mentioned-count-min",
            "social_score_min": "--social-score-min",
            "limit": "--limit",
            "cursor": "--cursor",
        },
        "schema": _schema({
            "ranking_type": {"type": "string", "default": "4", "description": "4=Trending, 5=X mentions."},
            "chain": {"type": "string", "description": "Optional chain, e.g. solana, bsc, ethereum."},
            "rank_by": {"type": "string", "description": "15=token score, 11=mentions, 7=liquidity, 5=volume."},
            "time_frame": {"type": "string", "description": "1=5m, 2=1h, 3=4h, 4=24h."},
            "risk_filter": {"type": "string", "description": "true/false; pass as string."},
            "stable_token_filter": {"type": "string", "description": "true/false; pass as string."},
            "project_id": {"type": "string", "description": "Protocol id filter, e.g. Pump.fun id."},
            "price_change_min": {"type": "number"},
            "price_change_max": {"type": "number"},
            "volume_min": {"type": "number"},
            "volume_max": {"type": "number"},
            "market_cap_min": {"type": "number"},
            "market_cap_max": {"type": "number"},
            "liquidity_min": {"type": "number"},
            "liquidity_max": {"type": "number"},
            "transaction_min": {"type": "integer"},
            "holders_min": {"type": "integer"},
            "mentioned_count_min": {"type": "integer"},
            "social_score_min": {"type": "number"},
            "limit": {"type": "integer", "default": 20, "maximum": 100},
            "cursor": {"type": "string"},
        }),
    },
    "token_search": {
        "description": "Search tokens by name, symbol, or contract address.",
        "tags": ("token", "meme", "search"),
        "base": ["token", "search"],
        "params": {
            "query": "--query",
            "chain": "--chain",
            "chains": "--chains",
            "limit": "--limit",
            "cursor": "--cursor",
        },
        "required": ("query",),
        "schema": _schema({
            "query": {"type": "string"},
            "chain": {"type": "string"},
            "chains": {"type": "string", "description": "Comma-separated chains."},
            "limit": {"type": "integer", "default": 20, "maximum": 100},
            "cursor": {"type": "string"},
        }, required=("query",)),
    },
    "token_info": {
        "description": "Read token basic info such as name, symbol, decimals, and logo.",
        "tags": ("token", "meme", "metadata"),
        "base": ["token", "info"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "token_report": {
        "description": "Composite token info, price info, advanced info, and security scan.",
        "tags": ("token", "meme", "security", "report"),
        "base": ["token", "report"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "token_advanced_info": {
        "description": "Read advanced token risk, creator, dev stats, and holder concentration.",
        "tags": ("token", "meme", "risk", "creator", "holders"),
        "base": ["token", "advanced-info"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "token_price_info": {
        "description": "Read token price, market cap, liquidity, volume, and 24h change.",
        "tags": ("token", "price", "liquidity", "meme"),
        "base": ["token", "price-info"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "token_liquidity": {
        "description": "Read top liquidity pools for a token.",
        "tags": ("token", "liquidity", "dex", "meme"),
        "base": ["token", "liquidity"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "token_holders": {
        "description": "Read token holder distribution and optional tag-filtered holders.",
        "tags": ("token", "holders", "meme", "risk"),
        "base": ["token", "holders"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "tag_filter": "--tag-filter",
            "limit": "--limit",
            "cursor": "--cursor",
        },
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "tag_filter": {"type": "string", "description": "1=KOL, 3=Smart Money, 4=Whale, 7=Sniper, etc."},
            "limit": {"type": "integer", "default": 20, "maximum": 100},
            "cursor": {"type": "string"},
        }, required=("address",)),
    },
    "token_top_trader": {
        "description": "Read profitable top traders for a token.",
        "tags": ("token", "trader", "smart_money", "meme"),
        "base": ["token", "top-trader"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "tag_filter": "--tag-filter",
            "limit": "--limit",
            "cursor": "--cursor",
        },
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "tag_filter": {"type": "string"},
            "limit": {"type": "integer", "default": 20, "maximum": 100},
            "cursor": {"type": "string"},
        }, required=("address",)),
    },
    "token_trades": {
        "description": "Read DEX trade history for a token, optionally filtered by trader tag or wallets.",
        "tags": ("token", "trades", "dex", "meme"),
        "base": ["token", "trades"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "limit": "--limit",
            "tag_filter": "--tag-filter",
            "wallet_filter": "--wallet-filter",
        },
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "limit": {"type": "integer", "default": 100, "maximum": 500},
            "tag_filter": {"type": "string"},
            "wallet_filter": {"type": "string", "description": "Comma-separated wallet addresses, max 10."},
        }, required=("address",)),
    },
    "token_cluster_overview": {
        "description": "Read token holder cluster concentration, rug-pull, and new-address overview.",
        "tags": ("token", "cluster", "holders", "meme", "risk"),
        "base": ["token", "cluster-overview"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "token_cluster_top_holders": {
        "description": "Read top holder concentration overview by rank bucket.",
        "tags": ("token", "cluster", "holders", "meme", "risk"),
        "base": ["token", "cluster-top-holders"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "range_filter": "--range-filter",
        },
        "required": ("address", "range_filter"),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "range_filter": {"type": "string", "description": "1=top10, 2=top50, 3=top100."},
        }, required=("address", "range_filter")),
    },
    "token_cluster_list": {
        "description": "Read holder cluster list for top holder clusters with address details.",
        "tags": ("token", "cluster", "holders", "meme", "risk"),
        "base": ["token", "cluster-list"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "token_cluster_supported_chains": {
        "description": "Read supported chains for token holder cluster analysis.",
        "tags": ("token", "cluster", "chain"),
        "base": ["token", "cluster-supported-chains"],
        "params": {"chain": "--chain"},
        "schema": _schema({"chain": {"type": "string"}}),
    },
    "memepump_chains": {
        "description": "Read supported chains and protocols for Meme Pump scanning.",
        "tags": ("memepump", "meme", "pumpfun", "chain"),
        "base": ["memepump", "chains"],
        "params": {"chain": "--chain"},
        "schema": _schema({"chain": {"type": "string"}}),
    },
    "memepump_tokens": {
        "description": "Read filtered Meme Pump token list for a chain.",
        "tags": ("memepump", "meme", "pumpfun", "token"),
        "base": ["memepump", "tokens"],
        "params": {
            "chain": "--chain",
            "stage": "--stage",
            "wallet_address": "--wallet-address",
            "protocol_id_list": "--protocol-id-list",
            "min_market_cap": "--min-market-cap",
            "max_market_cap": "--max-market-cap",
            "min_volume": "--min-volume",
            "max_volume": "--max-volume",
            "min_tx_count": "--min-tx-count",
            "min_holders": "--min-holders",
            "max_token_age": "--max-token-age",
            "has_at_least_one_social_link": "--has-at-least-one-social-link",
            "has_x": "--has-x",
            "has_telegram": "--has-telegram",
            "dev_sell_all": "--dev-sell-all",
            "dev_still_holding": "--dev-still-holding",
            "community_takeover": "--community-takeover",
            "keywords_include": "--keywords-include",
            "keywords_exclude": "--keywords-exclude",
        },
        "required": ("chain",),
        "schema": _schema({
            "chain": {"type": "string", "description": "Required, e.g. solana or bsc."},
            "stage": {"type": "string", "default": "NEW", "description": "NEW, MIGRATING, or MIGRATED."},
            "wallet_address": {"type": "string"},
            "protocol_id_list": {"type": "string"},
            "min_market_cap": {"type": "number"},
            "max_market_cap": {"type": "number"},
            "min_volume": {"type": "number"},
            "max_volume": {"type": "number"},
            "min_tx_count": {"type": "integer"},
            "min_holders": {"type": "integer"},
            "max_token_age": {"type": "integer", "description": "Minutes."},
            "has_at_least_one_social_link": {"type": "string", "description": "true/false; pass as string."},
            "has_x": {"type": "string"},
            "has_telegram": {"type": "string"},
            "dev_sell_all": {"type": "string"},
            "dev_still_holding": {"type": "string"},
            "community_takeover": {"type": "string"},
            "keywords_include": {"type": "string"},
            "keywords_exclude": {"type": "string"},
        }, required=("chain",)),
    },
    "memepump_token_details": {
        "description": "Read Meme Pump token detail for a contract.",
        "tags": ("memepump", "meme", "token", "details"),
        "base": ["memepump", "token-details"],
        "params": {"address": "--address", "chain": "--chain", "wallet": "--wallet"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "wallet": {"type": "string"},
        }, required=("address",)),
    },
    "memepump_token_dev_info": {
        "description": "Read Meme Pump token developer info.",
        "tags": ("memepump", "meme", "token", "dev", "risk"),
        "base": ["memepump", "token-dev-info"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "memepump_similar_tokens": {
        "description": "Read similar Meme Pump tokens for a contract.",
        "tags": ("memepump", "meme", "token", "similar"),
        "base": ["memepump", "similar-tokens"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "memepump_token_bundle_info": {
        "description": "Read Meme Pump bundler/sniper bundle info for a token.",
        "tags": ("memepump", "meme", "token", "bundle", "sniper", "risk"),
        "base": ["memepump", "token-bundle-info"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "memepump_aped_wallet": {
        "description": "Read co-invested/apED wallet data for a Meme Pump token.",
        "tags": ("memepump", "meme", "wallet", "smart_money", "token"),
        "base": ["memepump", "aped-wallet"],
        "params": {"address": "--address", "chain": "--chain", "wallet": "--wallet"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "wallet": {"type": "string", "description": "Optional user wallet to highlight."},
        }, required=("address",)),
    },
    "signal_chains": {
        "description": "Read supported chains for smart money / whale / KOL signals.",
        "tags": ("signal", "smart_money", "whale", "kol", "chain"),
        "base": ["signal", "chains"],
        "params": {"chain": "--chain"},
        "schema": _schema({"chain": {"type": "string"}}),
    },
    "signal_list": {
        "description": "Read latest smart money, KOL, and whale activity signals.",
        "tags": ("signal", "smart_money", "whale", "kol", "meme", "dex"),
        "base": ["signal", "list"],
        "params": {
            "chain": "--chain",
            "wallet_type": "--wallet-type",
            "min_amount_usd": "--min-amount-usd",
            "token_address": "--token-address",
            "min_market_cap_usd": "--min-market-cap-usd",
            "min_liquidity_usd": "--min-liquidity-usd",
            "limit": "--limit",
            "cursor": "--cursor",
        },
        "required": ("chain",),
        "schema": _schema({
            "chain": {"type": "string"},
            "wallet_type": {"type": "string", "description": "1=Smart Money, 2=KOL, 3=Whale."},
            "min_amount_usd": {"type": "number"},
            "token_address": {"type": "string"},
            "min_market_cap_usd": {"type": "number"},
            "min_liquidity_usd": {"type": "number"},
            "limit": {"type": "integer", "default": 20, "maximum": 100},
            "cursor": {"type": "string"},
        }, required=("chain",)),
    },
    "leaderboard_supported_chains": {
        "description": "Read chains supported by trader leaderboard.",
        "tags": ("leaderboard", "smart_money", "chain"),
        "base": ["leaderboard", "supported-chains"],
        "params": {"chain": "--chain"},
        "schema": _schema({"chain": {"type": "string"}}),
    },
    "leaderboard_list": {
        "description": "Read top traders ranked by PnL, win rate, tx count, volume, or ROI.",
        "tags": ("leaderboard", "smart_money", "trader", "meme"),
        "base": ["leaderboard", "list"],
        "params": {
            "chain": "--chain",
            "time_frame": "--time-frame",
            "sort_by": "--sort-by",
            "wallet_type": "--wallet-type",
            "min_realized_pnl_usd": "--min-realized-pnl-usd",
            "min_win_rate_percent": "--min-win-rate-percent",
            "min_txs": "--min-txs",
            "min_tx_volume": "--min-tx-volume",
        },
        "required": ("chain", "time_frame", "sort_by"),
        "schema": _schema({
            "chain": {"type": "string"},
            "time_frame": {"type": "string", "description": "1=1D, 2=3D, 3=7D, 4=1M, 5=3M."},
            "sort_by": {"type": "string", "description": "1=PnL, 2=Win Rate, 3=Tx number, 4=Volume, 5=ROI."},
            "wallet_type": {"type": "string", "description": "sniper, dev, fresh, pump, smartMoney, influencer."},
            "min_realized_pnl_usd": {"type": "number"},
            "min_win_rate_percent": {"type": "number"},
            "min_txs": {"type": "integer"},
            "min_tx_volume": {"type": "number"},
        }, required=("chain", "time_frame", "sort_by")),
    },
    "security_token_scan": {
        "description": "Run read-only token security scan for explicit tokens or a wallet's holdings.",
        "tags": ("security", "token", "meme", "risk"),
        "base": ["security", "token-scan"],
        "params": {"tokens": "--tokens", "address": "--address", "chain": "--chain"},
        "schema": _schema({
            "tokens": {"type": "string", "description": "chainId:address comma-separated, up to 10."},
            "address": {"type": "string", "description": "Wallet address to scan holdings."},
            "chain": {"type": "string"},
        }),
    },
    "security_dapp_scan": {
        "description": "Run read-only DApp/domain phishing and blacklist scan.",
        "tags": ("security", "dapp", "risk"),
        "base": ["security", "dapp-scan"],
        "params": {"domain": "--domain", "chain": "--chain"},
        "required": ("domain",),
        "schema": _schema({
            "domain": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("domain",)),
    },
    "security_approvals": {
        "description": "Query token approval and permit2 authorizations for a wallet.",
        "tags": ("security", "wallet", "approvals", "risk", "meme"),
        "base": ["security", "approvals"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "limit": "--limit",
            "cursor": "--cursor",
        },
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string", "description": "Comma-separated chain names or indexes."},
            "limit": {"type": "integer", "default": 20, "maximum": 100},
            "cursor": {"type": "string"},
        }, required=("address",)),
    },
    "security_tx_scan": {
        "description": "Run read-only pre-execution transaction security scan.",
        "tags": ("security", "transaction", "risk", "wallet"),
        "base": ["security", "tx-scan"],
        "params": {
            "from": "--from",
            "to": "--to",
            "chain": "--chain",
            "data": "--data",
            "value": "--value",
            "gas": "--gas",
            "gas_price": "--gas-price",
            "encoding": "--encoding",
            "transactions": "--transactions",
        },
        "required": ("from", "chain"),
        "schema": _schema({
            "from": {"type": "string"},
            "to": {"type": "string"},
            "chain": {"type": "string"},
            "data": {"type": "string"},
            "value": {"type": "string"},
            "gas": {"type": "string"},
            "gas_price": {"type": "string"},
            "encoding": {"type": "string"},
            "transactions": {"type": "string", "description": "Comma-separated Solana transaction payloads."},
        }, required=("from", "chain")),
    },
    "security_sig_scan": {
        "description": "Run read-only message signature phishing/security scan.",
        "tags": ("security", "signature", "risk", "wallet"),
        "base": ["security", "sig-scan"],
        "params": {
            "from": "--from",
            "chain": "--chain",
            "sig_method": "--sig-method",
            "message": "--message",
        },
        "required": ("from", "chain", "sig_method", "message"),
        "schema": _schema({
            "from": {"type": "string"},
            "chain": {"type": "string"},
            "sig_method": {"type": "string", "description": "personal_sign or eth_signTypedData_v4."},
            "message": {"type": "string"},
        }, required=("from", "chain", "sig_method", "message")),
    },
    "tracker_activities": {
        "description": "Read latest DEX activities for smart money, KOL, or custom tracked addresses.",
        "tags": ("tracker", "smart_money", "kol", "dex", "meme"),
        "base": ["tracker", "activities"],
        "params": {
            "tracker_type": "--tracker-type",
            "wallet_address": "--wallet-address",
            "trade_type": "--trade-type",
            "chain": "--chain",
            "min_volume": "--min-volume",
            "min_holders": "--min-holders",
            "min_market_cap": "--min-market-cap",
            "min_liquidity": "--min-liquidity",
        },
        "required": ("tracker_type",),
        "schema": _schema({
            "tracker_type": {"type": "string", "description": "smart_money/1, kol/2, multi_address/3."},
            "wallet_address": {"type": "string", "description": "Required for multi_address; comma-separated, max 20."},
            "trade_type": {"type": "string", "description": "0=all, 1=buy, 2=sell."},
            "chain": {"type": "string"},
            "min_volume": {"type": "number"},
            "min_holders": {"type": "integer"},
            "min_market_cap": {"type": "number"},
            "min_liquidity": {"type": "number"},
        }, required=("tracker_type",)),
    },
    "market_price": {
        "description": "Read token price by contract address.",
        "tags": ("market", "price", "token", "meme"),
        "base": ["market", "price"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "market_prices": {
        "description": "Read prices for multiple tokens in one batch query.",
        "tags": ("market", "price", "token", "meme", "batch"),
        "base": ["market", "prices"],
        "params": {"tokens": "--tokens", "chain": "--chain"},
        "required": ("tokens",),
        "schema": _schema({
            "tokens": {
                "type": "string",
                "description": "Comma-separated chainIndex:address pairs.",
            },
            "chain": {"type": "string"},
        }, required=("tokens",)),
    },
    "market_kline": {
        "description": "Read token K-line/candlestick data through OnchainOS.",
        "tags": ("market", "kline", "ohlcv", "token", "meme"),
        "base": ["market", "kline"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "bar": "--bar",
            "limit": "--limit",
        },
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "bar": {"type": "string", "default": "1H"},
            "limit": {"type": "integer", "default": 100, "maximum": 299},
        }, required=("address",)),
    },
    "market_index": {
        "description": "Read aggregated index price for a token/native asset.",
        "tags": ("market", "index", "price", "token"),
        "base": ["market", "index"],
        "params": {"address": "--address", "chain": "--chain"},
        "required": ("address",),
        "schema": _schema({
            "address": {"type": "string", "description": "Token contract address; empty string for native token."},
            "chain": {"type": "string"},
        }, required=("address",)),
    },
    "portfolio_supported_chains": {
        "description": "Read supported chains for wallet portfolio PnL endpoints.",
        "tags": ("portfolio", "wallet", "chain"),
        "base": ["market", "portfolio-supported-chains"],
        "params": {"chain": "--chain"},
        "schema": _schema({"chain": {"type": "string"}}),
    },
    "portfolio_overview": {
        "description": "Read wallet realized/unrealized PnL, win rate, and trading stats.",
        "tags": ("portfolio", "pnl", "wallet", "trader"),
        "base": ["market", "portfolio-overview"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "time_frame": "--time-frame",
        },
        "required": ("address", "chain"),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "time_frame": {"type": "string", "default": "4", "description": "1=1D, 2=3D, 3=7D, 4=1M, 5=3M."},
        }, required=("address", "chain")),
    },
    "portfolio_dex_history": {
        "description": "Read wallet DEX transaction history for a time window.",
        "tags": ("portfolio", "wallet", "dex", "history", "meme"),
        "base": ["market", "portfolio-dex-history"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "begin": "--begin",
            "end": "--end",
            "limit": "--limit",
            "cursor": "--cursor",
            "token": "--token",
            "tx_type": "--tx-type",
        },
        "required": ("address", "chain", "begin", "end"),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "begin": {"type": "string", "description": "Start timestamp in milliseconds."},
            "end": {"type": "string", "description": "End timestamp in milliseconds."},
            "limit": {"type": "integer", "default": 20, "maximum": 100},
            "cursor": {"type": "string"},
            "token": {"type": "string"},
            "tx_type": {"type": "string", "description": "1=BUY, 2=SELL, 3=Transfer In, 4=Transfer Out; comma-separated."},
        }, required=("address", "chain", "begin", "end")),
    },
    "portfolio_recent_pnl": {
        "description": "Read recent token PnL records for a wallet.",
        "tags": ("portfolio", "wallet", "pnl", "meme", "trader"),
        "base": ["market", "portfolio-recent-pnl"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "limit": "--limit",
            "cursor": "--cursor",
        },
        "required": ("address", "chain"),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "limit": {"type": "integer", "default": 20, "maximum": 100},
            "cursor": {"type": "string"},
        }, required=("address", "chain")),
    },
    "portfolio_token_pnl": {
        "description": "Read latest PnL snapshot for a specific wallet/token pair.",
        "tags": ("portfolio", "wallet", "pnl", "token", "meme"),
        "base": ["market", "portfolio-token-pnl"],
        "params": {"address": "--address", "chain": "--chain", "token": "--token"},
        "required": ("address", "chain", "token"),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "token": {"type": "string"},
        }, required=("address", "chain", "token")),
    },
    "portfolio_chains": {
        "description": "Read supported chains for wallet balance queries.",
        "tags": ("portfolio", "wallet", "balance", "chain"),
        "base": ["portfolio", "chains"],
        "params": {"chain": "--chain"},
        "schema": _schema({"chain": {"type": "string"}}),
    },
    "portfolio_total_value": {
        "description": "Read total asset value for a wallet address.",
        "tags": ("portfolio", "wallet", "balance", "value"),
        "base": ["portfolio", "total-value"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "chains": "--chains",
            "asset_type": "--asset-type",
            "exclude_risk": "--exclude-risk",
        },
        "required": ("address", "chains"),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "chains": {"type": "string", "description": "Comma-separated chain IDs or names."},
            "asset_type": {"type": "string", "description": "0=all, 1=tokens only, 2=DeFi only."},
            "exclude_risk": {"type": "string", "description": "true/false; pass as string."},
        }, required=("address", "chains")),
    },
    "portfolio_all_balances": {
        "description": "Read all token balances for a wallet address.",
        "tags": ("portfolio", "wallet", "balance", "token"),
        "base": ["portfolio", "all-balances"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "chains": "--chains",
            "exclude_risk": "--exclude-risk",
            "filter": "--filter",
        },
        "required": ("address", "chains"),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "chains": {"type": "string", "description": "Comma-separated chain IDs or names."},
            "exclude_risk": {"type": "string", "description": "0=filter risk tokens, 1=include."},
            "filter": {"type": "string", "description": "0=default, 1=all tokens."},
        }, required=("address", "chains")),
    },
    "portfolio_token_balances": {
        "description": "Read specific token balances for a wallet address.",
        "tags": ("portfolio", "wallet", "balance", "token"),
        "base": ["portfolio", "token-balances"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "tokens": "--tokens",
            "exclude_risk": "--exclude-risk",
        },
        "required": ("address", "tokens"),
        "schema": _schema({
            "address": {"type": "string"},
            "chain": {"type": "string"},
            "tokens": {"type": "string", "description": "Comma-separated chainIndex:tokenAddress pairs."},
            "exclude_risk": {"type": "string", "description": "0=filter risk tokens, 1=include."},
        }, required=("address", "tokens")),
    },
    "wallet_status": {
        "description": "Read OnchainOS wallet login/status state.",
        "tags": ("wallet",),
        "base": ["wallet", "status"],
        "params": {},
        "schema": {
            "type": "object",
            "properties": {"timeout_s": {"type": "number", "default": 30}},
            "additionalProperties": False,
        },
    },
    "wallet_addresses": {
        "description": "List OnchainOS wallet addresses.",
        "tags": ("wallet",),
        "base": ["wallet", "addresses"],
        "params": {},
        "schema": {
            "type": "object",
            "properties": {"timeout_s": {"type": "number", "default": 30}},
            "additionalProperties": False,
        },
    },
    "wallet_chains": {
        "description": "List chains supported by the OnchainOS wallet.",
        "tags": ("wallet",),
        "base": ["wallet", "chains"],
        "params": {},
        "schema": {
            "type": "object",
            "properties": {"timeout_s": {"type": "number", "default": 30}},
            "additionalProperties": False,
        },
    },
    "wallet_balance": {
        "description": "Read OnchainOS wallet balances, optionally filtered by chain/token.",
        "tags": ("wallet", "balance"),
        "base": ["wallet", "balance"],
        "params": {
            "chain": "--chain",
            "token_address": "--token-address",
            "all": "--all",
        },
        "schema": {
            "type": "object",
            "properties": {
                "chain": {"type": "string"},
                "token_address": {"type": "string"},
                "all": {"type": "boolean"},
                "timeout_s": {"type": "number", "default": 30},
            },
            "additionalProperties": False,
        },
    },
    "defi_support_chains": {
        "description": "Read supported chains for OnchainOS DeFi product data.",
        "tags": ("defi", "chain"),
        "base": ["defi", "support-chains"],
        "params": {"chain": "--chain"},
        "schema": _schema({"chain": {"type": "string"}}),
    },
    "defi_support_platforms": {
        "description": "Read supported DeFi platforms.",
        "tags": ("defi", "platform"),
        "base": ["defi", "support-platforms"],
        "params": {"chain": "--chain"},
        "schema": _schema({"chain": {"type": "string"}}),
    },
    "defi_list": {
        "description": "List DeFi products.",
        "tags": ("defi", "product"),
        "base": ["defi", "list"],
        "params": {"chain": "--chain", "page_num": "--page-num"},
        "schema": _schema({
            "chain": {"type": "string"},
            "page_num": {"type": "integer", "default": 1},
        }),
    },
    "defi_search": {
        "description": "Search DeFi products by token, platform, product group, and chain.",
        "tags": ("defi", "product", "search"),
        "base": ["defi", "search"],
        "params": {
            "token": "--token",
            "platform": "--platform",
            "chain": "--chain",
            "product_group": "--product-group",
            "page_num": "--page-num",
        },
        "schema": _schema({
            "token": {"type": "string", "description": "Comma-separated token keywords."},
            "platform": {"type": "string", "description": "Comma-separated platform keywords."},
            "chain": {"type": "string"},
            "product_group": {"type": "string", "description": "SINGLE_EARN, DEX_POOL, or LENDING."},
            "page_num": {"type": "integer", "default": 1},
        }),
    },
    "defi_detail": {
        "description": "Read DeFi product detail and APY.",
        "tags": ("defi", "product", "apy"),
        "base": ["defi", "detail"],
        "params": {"investment_id": "--investment-id", "chain": "--chain"},
        "required": ("investment_id",),
        "schema": _schema({
            "investment_id": {"type": "string"},
            "chain": {"type": "string"},
        }, required=("investment_id",)),
    },
    "defi_rate_chart": {
        "description": "Read historical APY chart data for a DeFi product.",
        "tags": ("defi", "apy", "chart"),
        "base": ["defi", "rate-chart"],
        "params": {
            "investment_id": "--investment-id",
            "chain": "--chain",
            "time_range": "--time-range",
        },
        "required": ("investment_id",),
        "schema": _schema({
            "investment_id": {"type": "string"},
            "chain": {"type": "string"},
            "time_range": {"type": "string", "description": "DAY, WEEK, MONTH, SEASON, or YEAR."},
        }, required=("investment_id",)),
    },
    "defi_tvl_chart": {
        "description": "Read historical TVL chart data for a DeFi product.",
        "tags": ("defi", "tvl", "chart"),
        "base": ["defi", "tvl-chart"],
        "params": {
            "investment_id": "--investment-id",
            "chain": "--chain",
            "time_range": "--time-range",
        },
        "required": ("investment_id",),
        "schema": _schema({
            "investment_id": {"type": "string"},
            "chain": {"type": "string"},
            "time_range": {"type": "string", "description": "DAY, WEEK, MONTH, SEASON, or YEAR."},
        }, required=("investment_id",)),
    },
    "defi_depth_price_chart": {
        "description": "Read V3 pool depth or price chart data.",
        "tags": ("defi", "depth", "price", "chart"),
        "base": ["defi", "depth-price-chart"],
        "params": {
            "investment_id": "--investment-id",
            "chain": "--chain",
            "chart_type": "--chart-type",
            "time_range": "--time-range",
        },
        "required": ("investment_id",),
        "schema": _schema({
            "investment_id": {"type": "string"},
            "chain": {"type": "string"},
            "chart_type": {"type": "string", "description": "DEPTH or PRICE."},
            "time_range": {"type": "string", "description": "DAY or WEEK for PRICE mode."},
        }, required=("investment_id",)),
    },
    "defi_positions": {
        "description": "Read DeFi positions for an address through OnchainOS.",
        "tags": ("defi", "portfolio"),
        "base": ["defi", "positions"],
        "params": {
            "address": "--address",
            "chains": "--chains",
        },
        "required": ("address",),
        "schema": {
            "type": "object",
            "properties": {
                "address": {"type": "string"},
                "chains": {"type": "string", "description": "Comma-separated chain names."},
                "timeout_s": {"type": "number", "default": 30},
            },
            "required": ["address"],
            "additionalProperties": False,
        },
    },
    "defi_position_detail": {
        "description": "Read a specific DeFi position detail through OnchainOS.",
        "tags": ("defi", "portfolio"),
        "base": ["defi", "position-detail"],
        "params": {
            "address": "--address",
            "chain": "--chain",
            "platform_id": "--platform-id",
        },
        "required": ("address", "chain", "platform_id"),
        "schema": {
            "type": "object",
            "properties": {
                "address": {"type": "string"},
                "chain": {"type": "string"},
                "platform_id": {"type": "string"},
                "timeout_s": {"type": "number", "default": 30},
            },
            "required": ["address", "chain", "platform_id"],
            "additionalProperties": False,
        },
    },
}


def _run_onchainos_command(
    context: DataApiContext,
    command: str,
    params: dict[str, Any],
    *,
    timeout_s: float,
) -> Any:
    meta = _ONCHAINOS_COMMANDS.get(command)
    if meta is None:
        raise DataApiError(
            "onchainos command is not allowlisted",
            kind="schema_validation",
            detail={"command": command, "allowed": sorted(_ONCHAINOS_COMMANDS)},
            retryable=False,
        )
    for key in meta.get("required") or ():
        _required(params, str(key))
    wallet = _okx_onchainos_wallet(context)
    argv = list(meta["base"])
    param_flags = dict(meta.get("params") or {})
    for key, flag in param_flags.items():
        val = params.get(key)
        if val is None or val == "":
            continue
        if isinstance(val, bool):
            if val:
                argv.append(flag)
            continue
        argv.extend([flag, str(val)])
    return wallet._run_onchainos(argv, timeout_s=timeout_s)


def _okx_onchainos_wallet(context: DataApiContext) -> Any:
    from ..wallet import build_provider, list_configured_providers

    bindings = list_configured_providers(context.config_data)
    selected = next((row for row in bindings if row.get("provider") == "okx_os"), None)
    cfg = dict(selected.get("config") or {}) if selected else {}
    return build_provider(
        "okx_os",
        cfg,
        workspace=context.workspace,
        vault_passphrase=context.vault_passphrase,
    )


def _schema_from_signature(fn: Any) -> dict[str, Any]:
    try:
        sig = inspect.signature(fn)
    except Exception:
        return {"type": "object", "additionalProperties": True}
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name in {"self", "cls"}:
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        entry = {"type": _json_type_from_annotation(param.annotation)}
        if param.default is not inspect._empty:
            default = param.default
            if isinstance(default, (str, int, float, bool)) or default is None:
                entry["default"] = default
        else:
            required.append(name)
        props[name] = entry
    schema = {
        "type": "object",
        "properties": props,
        "additionalProperties": True,
    }
    if required:
        schema["required"] = required
    return schema


def _json_type_from_annotation(annotation: Any) -> str:
    if annotation in (int, "int"):
        return "integer"
    if annotation in (float, "float"):
        return "number"
    if annotation in (bool, "bool"):
        return "boolean"
    if annotation in (dict, "dict"):
        return "object"
    if annotation in (list, tuple, "list", "tuple"):
        return "array"
    return "string"


def _safe_getattr(module: Any, name: str) -> Any | None:
    try:
        return getattr(module, name, None)
    except Exception:
        return None


def _first_doc_line(fn: Any | None) -> str:
    if fn is None:
        return ""
    try:
        doc = inspect.getdoc(fn) or ""
    except Exception:
        return ""
    return (doc.strip().splitlines() or [""])[0][:240]


def _row_matches(row: dict[str, Any], query: str) -> bool:
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("action", "title", "description", "tags", "signature")
    ).lower()
    return query in haystack


def _required(args: dict[str, Any], key: str) -> str:
    val = args.get(key)
    if val is None or str(val).strip() == "":
        raise DataApiError(
            f"missing required argument: {key}",
            kind="schema_validation",
            detail={"field": key},
            retryable=False,
        )
    return str(val)


def _int_arg(
    args: dict[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        return max(minimum, min(int(args.get(key, default)), maximum))
    except (TypeError, ValueError):
        return default


def _float_arg(
    args: dict[str, Any],
    key: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        return max(minimum, min(float(args.get(key, default)), maximum))
    except (TypeError, ValueError):
        return default

"""Registry for read-only long-tail data APIs.

The native ``market_data`` tool intentionally stays compact and focused
on ticker / candles / indicators. This registry is the escape hatch for
provider-specific tables and analytics that are useful to agents but too
large to inline into the prompt.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .types import (
    DataActionSpec,
    DataApiContext,
    DataApiError,
    DynamicDataProvider,
)


class DataApiRegistry:
    """Small provider/action registry with lazy provider support."""

    def __init__(self) -> None:
        self._actions: dict[tuple[str, str], DataActionSpec] = {}
        self._providers: dict[str, DynamicDataProvider] = {}
        self._aliases: dict[str, str] = {}

    def register_action(self, spec: DataActionSpec) -> None:
        provider = _norm(spec.provider)
        action = _norm(spec.action)
        if not provider or not action:
            raise ValueError("data action provider/action cannot be empty")
        self._actions[(provider, action)] = spec

    def register_provider(self, provider: DynamicDataProvider) -> None:
        name = _norm(provider.provider)
        if not name:
            raise ValueError("dynamic data provider name cannot be empty")
        self._providers[name] = provider

    def register_provider_alias(self, alias: str, provider: str) -> None:
        alias_l = _norm(alias)
        provider_l = _norm(provider)
        if not alias_l or not provider_l:
            raise ValueError("data provider alias/provider cannot be empty")
        self._aliases[alias_l] = provider_l

    def list(
        self,
        *,
        provider: str | None = None,
        query: str = "",
        tags: Iterable[str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        requested_provider = _norm(provider or "")
        provider_l = self.resolve_provider(requested_provider)
        tag_set = {_norm(t) for t in (tags or []) if _norm(t)}
        query_l = (query or "").strip().lower()
        max_rows = _bounded_int(limit, default=20, minimum=1, maximum=100)
        if requested_provider and not self._known_provider(provider_l):
            return {
                "providers": self.providers(),
                "aliases": self.aliases(),
                "count": 0,
                "limit": max_rows,
                "actions": [],
                "hint": (
                    f"Unknown data_api provider {requested_provider!r}. "
                    "For wallet-backed on-chain data use provider='wallet' "
                    "or aliases such as 'xagt_agent_plugin'. For OKX "
                    "OnchainOS CLI reads use provider='onchainos'. "
                    "For MCP namespaces such as coingecko use mcp_describe/"
                    "mcp_call instead of data_api."
                ),
            }

        rows: list[dict[str, Any]] = []
        for (p, _a), spec in sorted(self._actions.items()):
            if provider_l and p != provider_l:
                continue
            if tag_set and not tag_set.intersection({_norm(t) for t in spec.tags}):
                continue
            preview = spec.preview()
            if query_l and not _matches(preview, query_l):
                continue
            rows.append(preview)
            if len(rows) >= max_rows:
                break

        if len(rows) < max_rows:
            providers = (
                [(provider_l, self._providers[provider_l])]
                if provider_l in self._providers
                else sorted(self._providers.items())
            )
            for p, adapter in providers:
                if provider_l and p != provider_l:
                    continue
                try:
                    dynamic_rows = adapter.list_actions(
                        query=query_l,
                        tags=tuple(tag_set),
                        limit=max_rows - len(rows),
                    )
                except DataApiError as exc:
                    rows.append({
                        "provider": p,
                        "error": exc.message,
                        "error_kind": exc.kind,
                        "detail": exc.detail,
                    })
                    continue
                rows.extend(dynamic_rows[: max_rows - len(rows)])
                if len(rows) >= max_rows:
                    break

        next_required_action: dict[str, Any] | None = None
        alias_target = self.aliases().get(query_l, "") if query_l else ""
        wallet_alias_query = bool(query_l and alias_target == "wallet")
        if provider_l == "wallet" and (not query_l or wallet_alias_query):
            readiness_args = {"provider": query_l} if query_l else {}
            next_required_action = {
                "tool": "data_api",
                "message": (
                    "Call data_api wallet readiness before coding docs, "
                    "reload_subsystem, provider authoring, or finalizing a "
                    "wallet/provider availability answer. Use the operator-"
                    "named provider when present; otherwise call readiness "
                    "without args to inspect configured wallet providers."
                ),
                "arguments": {
                    "op": "call",
                    "provider": "wallet",
                    "action": "readiness",
                    **({"args": readiness_args} if readiness_args else {}),
                },
            }

        return {
            "providers": self.providers(),
            "aliases": self.aliases(),
            **(
                {"requested_provider": requested_provider, "provider": provider_l}
                if requested_provider
                else {}
            ),
            "count": len(rows),
            "limit": max_rows,
            "actions": rows[:max_rows],
            **(
                {"next_required_action": next_required_action}
                if next_required_action is not None
                else {}
            ),
            "hint": (
                "For chain/meme/DEX discovery, inspect both "
                "provider='wallet' and provider='onchainos' before deciding "
                "a data source is unavailable."
            ) if not rows and not requested_provider else "",
        }

    def providers(self) -> list[str]:
        names = {p for p, _a in self._actions}
        names.update(self._providers)
        return sorted(names)

    def aliases(self) -> dict[str, str]:
        return dict(sorted(self._aliases.items()))

    def resolve_provider(self, provider: str) -> str:
        provider_l = _norm(provider)
        return self._aliases.get(provider_l, provider_l)

    def schema(self, provider: str, action: str) -> dict[str, Any]:
        provider_l = self.resolve_provider(provider)
        action_l = _norm(action)
        spec = self._actions.get((provider_l, action_l))
        if spec is not None:
            return {
                **spec.preview(),
                "input_schema": spec.input_schema,
            }
        adapter = self._providers.get(provider_l)
        if adapter is None:
            raise DataApiError(
                f"unknown data_api provider/action: {provider}.{action}",
                kind="not_found",
                detail={
                    "provider": provider,
                    "resolved_provider": provider_l,
                    "action": action,
                    "providers": self.providers(),
                    "aliases": self.aliases(),
                },
                retryable=False,
            )
        return adapter.schema(action)

    def call(
        self,
        provider: str,
        action: str,
        args: dict[str, Any] | None,
        *,
        context: DataApiContext,
    ) -> Any:
        provider_l = self.resolve_provider(provider)
        action_l = _norm(action)
        spec = self._actions.get((provider_l, action_l))
        if spec is not None:
            return spec.handler(context, dict(args or {}))
        adapter = self._providers.get(provider_l)
        if adapter is None:
            raise DataApiError(
                f"unknown data_api provider/action: {provider}.{action}",
                kind="not_found",
                detail={
                    "provider": provider,
                    "resolved_provider": provider_l,
                    "action": action,
                    "providers": self.providers(),
                    "aliases": self.aliases(),
                },
                retryable=False,
            )
        return adapter.call(action, dict(args or {}), context=context)

    def _known_provider(self, provider: str) -> bool:
        if provider in self._providers:
            return True
        return any(p == provider for p, _a in self._actions)


def compact_data_result(
    provider: str,
    action: str,
    raw: Any,
    *,
    limit: int = 50,
    columns: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a bounded JSON payload for LLM consumption."""

    max_rows = _bounded_int(limit, default=50, minimum=1, maximum=500)
    cols = [str(c) for c in (columns or []) if str(c)]
    rows = _records(raw)
    if rows is not None:
        json_rows = [
            _with_common_aliases(_jsonable(row))
            for row in rows
        ]
        if cols:
            json_rows = [
                {key: row.get(key) for key in cols if isinstance(row, dict) and key in row}
                if isinstance(row, dict)
                else row
                for row in json_rows
            ]
        payload = {
            "provider": provider,
            "action": action,
            "kind": "table",
            "row_count": len(json_rows),
            "limit": max_rows,
            "truncated": len(json_rows) > max_rows,
            "rows": json_rows[:max_rows],
        }
        if isinstance(raw, dict):
            if raw.get("source_url"):
                payload["source_url"] = raw.get("source_url")
            envelope = raw.get("_envelope")
            if isinstance(envelope, dict):
                payload["_envelope"] = _jsonable(envelope)
        return payload
    return {
        "provider": provider,
        "action": action,
        "kind": "object",
        "data": _jsonable(raw),
    }


def _with_common_aliases(row: Any) -> Any:
    if not isinstance(row, dict):
        return row
    out = dict(row)

    def set_alias(name: str, *sources: str) -> None:
        if out.get(name) not in (None, ""):
            return
        for source in sources:
            value = out.get(source)
            if value not in (None, ""):
                out[name] = value
                return

    tags = out.get("tags") if isinstance(out.get("tags"), dict) else {}
    market = out.get("market") if isinstance(out.get("market"), dict) else {}
    set_alias("address", "address", "tokenContractAddress", "tokenAddress", "contractAddress")
    set_alias("symbol", "symbol", "tokenSymbol")
    set_alias("market_cap", "marketCap", "marketCapUsd", "market_cap")
    set_alias("volume_24h", "volume", "volume24h", "volumeUsd24h", "volume_usd_24h")
    set_alias("liquidity_usd", "liquidity", "liquidityUsd", "liquidity_usd")
    set_alias("created_at", "created_at", "firstTradeTime", "createdTimestamp")
    if out.get("holders") in (None, "") and tags.get("totalHolders") not in (None, ""):
        out["holders"] = tags.get("totalHolders")
    if out.get("top_holder_pct") in (None, ""):
        for source in ("top10HoldPercent", "top10HoldingsPercent"):
            value = out.get(source)
            if value not in (None, ""):
                out["top_holder_pct"] = value
                break
        else:
            value = tags.get("top10HoldingsPercent")
            if value not in (None, ""):
                out["top_holder_pct"] = value
    if out.get("volume_1h") in (None, "") and market.get("volumeUsd1h") not in (None, ""):
        out["volume_1h"] = market.get("volumeUsd1h")
    return out


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _matches(row: dict[str, Any], query: str) -> bool:
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("provider", "action", "title", "description", "tags")
    ).lower()
    return query in haystack


def _records(raw: Any) -> list[Any] | None:
    if raw is None:
        return []
    if hasattr(raw, "to_dict"):
        try:
            records = raw.to_dict(orient="records")
            if isinstance(records, list):
                return records
        except TypeError:
            pass
        except Exception:
            pass
    if isinstance(raw, list):
        return raw
    if isinstance(raw, tuple):
        return list(raw)
    if isinstance(raw, dict):
        for key in ("rows", "data", "result", "items", "records"):
            val = raw.get(key)
            if isinstance(val, list):
                return val
    return None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)

"""Connector / venue native tools.

These exist so the agent can authoritatively answer the question
*"is exchange X / data source Y already integrated?"* by querying the
in-process :class:`ExchangeProviderRegistry` instead of guessing from
the SKILL.md prose. The model historically claimed Polymarket "isn't
integrated" while a fully working :class:`PolymarketConnector` lived
under ``nerya/connectors/polymarket.py`` — that gap caused the operator
to ship a placeholder strategy with no real data feed. Promoting the
registry to a tool closes the loop.

Two tools live here:

* :func:`connector_list_handler` — every registered provider with
  ``id``, ``label``, ``kind``, ``aliases``, ``runtime``,
  ``install_hint``, ``description``, ``supports`` matrix, doc links
  *and* the absolute path to its source file when it's a builtin /
  workspace provider. The agent can read the source to learn the
  exact API shape before depending on it in a strategy.
* :func:`connector_view_handler` — the same payload narrowed to one
  provider, plus a ``source`` field that holds the first ~24KB of the
  module so the agent can confirm endpoint URLs / method names without
  also calling :func:`read_file`.

Both tools are read-only and safe — they don't open sockets, they
just enumerate the in-memory specs that ``provider_spec`` already built
at import time.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from ...connectors.provider_spec import ExchangeProviderSpec, get_registry
from ..types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


CONNECTOR_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "description": (
                "Optional filter. One of cex|dex|chain|prediction_market|"
                "options|futures|derivatives. Case-insensitive."
            ),
        },
        "query": {
            "type": "string",
            "description": (
                "Optional free-text match against id / label / aliases / "
                "description (case-insensitive substring). Useful when the "
                "operator asks 'is polymarket integrated?' — pass query="
                "'polymarket' and look at ``count``."
            ),
        },
        "include_source": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, include the module file path for each builtin "
                "provider. Off by default to keep responses small."
            ),
        },
    },
}


CONNECTOR_VIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": (
                "Provider id (or a venue alias). e.g. ``polymarket``, "
                "``binance``, ``bsc``. Case-insensitive."
            ),
        },
        "max_source_bytes": {
            "type": "integer",
            "minimum": 0,
            "default": 24000,
            "description": (
                "Cap on how many bytes of the connector source to inline. "
                "Set to 0 to skip the source body and only get metadata."
            ),
        },
    },
    "required": ["id"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _factory_module_path(spec: ExchangeProviderSpec) -> Path | None:
    """Resolve the on-disk module that defines this provider's connector.

    ``spec.factory`` is a closure built inside ``_register_builtins`` so
    its ``__module__`` always points at ``nerya.connectors.provider_spec``,
    not at ``polymarket`` / ``ccxt_adapter`` / etc. We grab the module
    of the first non-callable global the factory touches by inspecting
    its closure cells.
    """

    factory = spec.factory
    if factory is None:
        return None
    # Closures expose freevars / cells — try them first.
    try:
        cells = getattr(factory, "__closure__", None) or ()
        for cell in cells:
            try:
                obj = cell.cell_contents
            except ValueError:
                continue
            mod = inspect.getmodule(obj)
            if mod is None:
                continue
            f = getattr(mod, "__file__", None)
            if f and "connectors" in f and "provider_spec" not in f:
                return Path(f)
    except Exception:
        pass
    # Fallback: rely on the spec id (best-effort, may miss for ccxt).
    candidates = {
        "polymarket": "polymarket.py",
        "binance": "ccxt_adapter.py",
        "bybit": "ccxt_adapter.py",
        "okx": "ccxt_adapter.py",
        "hyperliquid": "ccxt_adapter.py",
        "ccxt": "ccxt_adapter.py",
        "mock": "mock_exchange.py",
        "mock_chain": "mock_chain.py",
        "evm": "evm_native.py",
        "bsc": "bsc_native.py",
        "solana": "solana_native.py",
    }
    fname = candidates.get(spec.id)
    if not fname:
        return None
    here = Path(__file__).resolve().parents[2] / "connectors" / fname
    return here if here.exists() else None


def _spec_to_dict(
    spec: ExchangeProviderSpec, *, include_source_path: bool = False,
) -> dict[str, Any]:
    info = spec.to_info()
    if include_source_path:
        src = _factory_module_path(spec)
        info["source_path"] = str(src) if src is not None else None
    return info


def _usage_error(call: ToolCall, message: str) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=ToolErrorKind.SCHEMA_VALIDATION,
            message=message,
        ),
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def connector_list_handler(call: ToolCall) -> ToolResult:
    """List all connectors registered in the in-process provider spec."""

    args = call.arguments or {}
    kind = (args.get("kind") or "").strip().lower()
    query = (args.get("query") or "").strip().lower()
    include_source = bool(args.get("include_source", False))

    rows: list[dict[str, Any]] = []
    for spec in get_registry().list_specs():
        if kind and spec.kind.lower() != kind:
            continue
        if query:
            haystack = " ".join([
                spec.id, spec.label, spec.description,
                " ".join(spec.aliases),
            ]).lower()
            if query not in haystack:
                continue
        rows.append(_spec_to_dict(spec, include_source_path=include_source))

    rows.sort(key=lambda r: (r.get("kind", ""), r.get("id", "")))
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "count": len(rows),
            "connectors": rows,
            "hint": (
                "If a venue / data source is in this list it is *already "
                "integrated* — wire the strategy to it directly. If it is "
                "missing, follow the coding skill (extending-nerya.md) "
                "to author a real Connector subclass under "
                "nerya/connectors/<vendor>.py + register it via "
                "_register_builtins. Do not mock the data and do not "
                "ship a temp script."
            ),
        },
    )


def connector_view_handler(call: ToolCall) -> ToolResult:
    """Detail for a single provider, including (optionally) its source."""

    args = call.arguments or {}
    raw = (args.get("id") or "").strip().lower()
    if not raw:
        return _usage_error(call, "id is required (provider id or alias).")
    try:
        max_bytes = int(args.get("max_source_bytes", 24000))
    except (TypeError, ValueError):
        return _usage_error(call, "max_source_bytes must be an integer.")
    if max_bytes < 0:
        return _usage_error(call, "max_source_bytes must be >= 0.")

    spec = get_registry().find(raw)
    if spec is None:
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "found": False,
                "id": raw,
                "hint": (
                    f"No provider matches {raw!r}. This venue is *not* "
                    "integrated — author a real Connector under "
                    "nerya/connectors/<vendor>.py and register it in "
                    "provider_spec._register_builtins. Read official "
                    "docs first; do not mock."
                ),
            },
        )

    info = _spec_to_dict(spec, include_source_path=True)
    info["found"] = True

    src_path = _factory_module_path(spec)
    if max_bytes > 0 and src_path is not None and src_path.exists():
        try:
            text = src_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            text = f"<failed to read {src_path}: {exc}>"
        if len(text) > max_bytes:
            info["source"] = text[:max_bytes]
            info["source_truncated"] = True
            info["source_total_bytes"] = len(text)
        else:
            info["source"] = text
            info["source_truncated"] = False
            info["source_total_bytes"] = len(text)
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name, data=info,
    )


__all__ = [
    "CONNECTOR_LIST_SCHEMA",
    "CONNECTOR_VIEW_SCHEMA",
    "connector_list_handler",
    "connector_view_handler",
]

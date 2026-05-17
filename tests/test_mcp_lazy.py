"""Phase K — MCP lazy-loading tests.

Covers:

1. ``ToolDescriptor.lazy`` field default + override.
2. ``MCPServerConfig.always_eager`` YAML parsing.
3. ``LazyMcpState.is_visible`` semantics across non-lazy / eager / lazy /
   described tools.
4. The 3 meta-tools (``mcp_namespaces`` / ``mcp_describe`` / ``mcp_call``)
   wired against a fake registry.
5. ``mcp_describe`` per-session caching — calling it once promotes the
   namespace and subsequent ``is_visible`` calls accept the underlying
   tools without re-paying the describe.
6. The agent loop's ``_render_tools`` automatically applies the lazy
   filter when ``registry.lazy_mcp_state`` is set, and behaves
   identically to the legacy path when no state is attached.
7. Bootstrap end-to-end: per-server ``always_eager`` honored; meta-tools
   are installed onto the registry.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nerya.mcp.connectors.config import (
    MCPServerConfig,
    load_mcp_servers_config,
)
from nerya.mcp.lazy import (
    META_CALL_TOOL,
    META_DESCRIBE_TOOL,
    META_NAMESPACES_TOOL,
    LazyMcpState,
    attach_lazy_state,
    install_meta_tools,
    server_id_of,
)
from nerya.mcp.session_adapter import MCPSessionAdapter, register_external_mcp_tools
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolDescriptor,
    ToolResult,
)


pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _native_tool(name: str = "native_demo") -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description="native test tool",
        input_schema={"type": "object", "properties": {}},
        handler=lambda call: ToolResult.from_text(
            tool_use_id=call.id, name=call.name, text="ok",
        ),
        risk=RiskLevel.READ,
        namespace="native",
    )


def _mcp_tool(server_id: str, tool_name: str, *, lazy: bool) -> ToolDescriptor:
    """Mimic what register_external_mcp_tools writes — namespace=mcp +
    tags=('mcp', server_id), public name mcp__<server>__<tool>."""
    return ToolDescriptor(
        name=f"mcp__{server_id}__{tool_name}",
        description=f"{server_id}/{tool_name}",
        input_schema={"type": "object", "properties": {}},
        handler=lambda call: ToolResult.from_text(
            tool_use_id=call.id, name=call.name, text=f"ran {call.name}",
        ),
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NETWORK,
        tags=("mcp", server_id),
        namespace="mcp",
        lazy=lazy,
    )


# ---------------------------------------------------------------------------
# K-1 schema additions
# ---------------------------------------------------------------------------


def test_tool_descriptor_lazy_defaults_false() -> None:
    """ToolDescriptor.lazy default is False — every existing tool stays
    visible, no behavior change for callers that don't opt in."""
    d = _native_tool()
    assert d.lazy is False


def test_tool_descriptor_lazy_can_be_set_true() -> None:
    d = _mcp_tool("edgar", "get_filings", lazy=True)
    assert d.lazy is True
    # to_provider_tool stays a stable shape regardless of lazy.
    rendered = d.to_provider_tool()
    assert "name" in rendered
    assert "description" in rendered
    assert "input_schema" in rendered


def test_mcp_server_config_always_eager_default_false(tmp_path: Path) -> None:
    """Without an explicit ``always_eager:`` key the parser defaults to
    False so existing operator configs implicitly become lazy."""
    cfg = MCPServerConfig.from_raw({
        "id": "svr",
        "enabled": True,
        "transport": {"kind": "http", "url": "https://example.com/mcp"},
        "auth": {"kind": "none"},
    })
    assert cfg.always_eager is False


def test_mcp_server_config_always_eager_explicit_true() -> None:
    cfg = MCPServerConfig.from_raw({
        "id": "svr",
        "enabled": True,
        "always_eager": True,
        "transport": {"kind": "http", "url": "https://example.com/mcp"},
        "auth": {"kind": "none"},
    })
    assert cfg.always_eager is True


def test_load_mcp_servers_config_round_trips_always_eager(tmp_path: Path) -> None:
    """Parse one server with always_eager=true via the YAML loader."""
    yml = tmp_path / "mcp.yml"
    yml.write_text(
        "version: 1\n"
        "servers:\n"
        "  - id: hot\n"
        "    enabled: true\n"
        "    always_eager: true\n"
        "    transport:\n"
        "      kind: stdio\n"
        "      command: [\"echo\", \"hi\"]\n"
        "    auth:\n"
        "      kind: none\n"
        "  - id: cold\n"
        "    enabled: true\n"
        "    transport:\n"
        "      kind: stdio\n"
        "      command: [\"echo\", \"hi\"]\n"
        "    auth:\n"
        "      kind: none\n",
        encoding="utf-8",
    )
    cfg = load_mcp_servers_config(yml)
    by_id = {s.id: s for s in cfg.servers}
    assert by_id["hot"].always_eager is True
    assert by_id["cold"].always_eager is False


# ---------------------------------------------------------------------------
# K-2 server_id_of
# ---------------------------------------------------------------------------


def test_server_id_of_native_returns_none() -> None:
    assert server_id_of(_native_tool()) is None


def test_server_id_of_extracts_from_tags() -> None:
    d = _mcp_tool("edgar", "get_filings", lazy=True)
    assert server_id_of(d) == "edgar"


# ---------------------------------------------------------------------------
# K-2 LazyMcpState
# ---------------------------------------------------------------------------


def test_lazy_state_is_visible_passes_non_lazy_native() -> None:
    state = LazyMcpState()
    state.register_namespace("edgar", ["mcp__edgar__t1"], always_eager=False)
    assert state.is_visible(_native_tool()) is True


def test_lazy_state_is_visible_hides_lazy_until_described() -> None:
    state = LazyMcpState()
    state.register_namespace(
        "edgar", ["mcp__edgar__get_filings"], always_eager=False,
    )
    tool = _mcp_tool("edgar", "get_filings", lazy=True)

    # Before describe → hidden
    assert state.is_visible(tool) is False

    # After describe → visible (per-session cache)
    newly = state.mark_described("edgar")
    assert newly is True
    assert state.is_visible(tool) is True

    # Idempotent — second describe says "not newly"
    assert state.mark_described("edgar") is False


def test_lazy_state_always_eager_namespace_is_visible_without_describe() -> None:
    state = LazyMcpState()
    state.register_namespace(
        "yahoo", ["mcp__yahoo__quote"], always_eager=True,
    )
    tool = _mcp_tool("yahoo", "quote", lazy=True)
    # Even though the descriptor itself is lazy, the namespace's
    # always_eager flag exempts it.
    assert state.is_visible(tool) is True


def test_lazy_state_reset_session_clears_only_described() -> None:
    state = LazyMcpState()
    state.register_namespace("edgar", ["t1"], always_eager=False)
    state.register_namespace("yahoo", ["t2"], always_eager=True)
    state.mark_described("edgar")
    snap_before = state.snapshot()
    assert "edgar" in snap_before["described"]

    state.reset_session()

    snap_after = state.snapshot()
    assert snap_after["described"] == []
    # namespace_index + always_eager preserved (bootstrap-time facts).
    assert "edgar" in snap_after["namespaces"]
    assert "yahoo" in snap_after["always_eager"]


def test_lazy_state_concurrent_describe_is_thread_safe() -> None:
    state = LazyMcpState()
    state.register_namespace("a", ["t1"], always_eager=False)
    state.register_namespace("b", ["t2"], always_eager=False)
    state.register_namespace("c", ["t3"], always_eager=False)

    def hammer(ns: str) -> None:
        for _ in range(50):
            state.mark_described(ns)

    threads = [
        threading.Thread(target=hammer, args=(ns,)) for ns in ("a", "b", "c")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state.snapshot()["described"] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# K-2 meta-tools
# ---------------------------------------------------------------------------


def _build_registry_with_two_namespaces(
    *, capture_args: bool = False,
):
    """Build a 2-namespace MCP test registry.

    When ``capture_args=True``, each MCP tool's handler records its
    invocation args into a shared dict keyed by ``"<ns>.<bare_tool>"``,
    and the helper returns ``(registry, recorded)`` instead of
    ``(registry, state)``. Tests that need to verify that flat→nested
    arg promotion produced the right underlying call use this mode.
    """

    registry = ToolRegistry()
    state = LazyMcpState()

    if capture_args:
        recorded: dict[str, dict[str, Any]] = {}

        def _make(server_id: str, tool_name: str) -> ToolDescriptor:
            public_name = f"mcp__{server_id}__{tool_name}"

            def handler(call: ToolCall) -> ToolResult:
                recorded[f"{server_id}.{tool_name}"] = dict(
                    call.arguments or {},
                )
                return ToolResult.from_text(
                    tool_use_id=call.id, name=call.name,
                    text=f"ran {public_name}",
                )

            return ToolDescriptor(
                name=public_name,
                description=f"{server_id}/{tool_name}",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.NETWORK,
                tags=("mcp", server_id),
                namespace="mcp",
                lazy=True,
            )
        registry.register(_native_tool("native_demo"))
        registry.register(_make("edgar", "get_filings"))
        registry.register(_make("edgar", "get_company"))
        registry.register(_make("yahoo", "quote"))
    else:
        registry.register(_native_tool("native_demo"))
        registry.register(_mcp_tool("edgar", "get_filings", lazy=True))
        registry.register(_mcp_tool("edgar", "get_company", lazy=True))
        registry.register(_mcp_tool("yahoo", "quote", lazy=True))

    state.register_namespace(
        "edgar",
        ["mcp__edgar__get_filings", "mcp__edgar__get_company"],
        always_eager=False,
    )
    state.register_namespace("yahoo", ["mcp__yahoo__quote"], always_eager=True)
    attach_lazy_state(registry, state)
    install_meta_tools(registry=registry, state=state)
    if capture_args:
        return registry, recorded
    return registry, state


def test_meta_tools_are_registered_and_eager() -> None:
    registry, _ = _build_registry_with_two_namespaces()
    for name in (META_NAMESPACES_TOOL, META_DESCRIBE_TOOL, META_CALL_TOOL):
        d = registry.find(name)
        assert d is not None, f"missing meta-tool {name}"
        # Meta-tools must NOT themselves be lazy or they'd hide.
        assert d.lazy is False, f"meta-tool {name} should be eager"


def test_mcp_namespaces_meta_tool_returns_status_per_namespace() -> None:
    registry, state = _build_registry_with_two_namespaces()
    descriptor = registry.find(META_NAMESPACES_TOOL)
    assert descriptor is not None

    result = descriptor.handler(ToolCall(name=META_NAMESPACES_TOOL))
    assert isinstance(result, ToolResult)
    assert not result.is_error
    payload = result.content[0].data
    namespaces = {row["namespace"]: row for row in payload["namespaces"]}
    assert namespaces["edgar"]["visibility"] == "lazy"
    assert namespaces["edgar"]["tool_count"] == 2
    assert namespaces["yahoo"]["visibility"] == "eager"
    assert namespaces["yahoo"]["tool_count"] == 1


def test_mcp_describe_meta_tool_promotes_namespace_and_returns_schemas() -> None:
    registry, state = _build_registry_with_two_namespaces()
    describe = registry.find(META_DESCRIBE_TOOL)
    assert describe is not None

    # Pre-condition: edgar is lazy, not described.
    edgar_tool = registry.find("mcp__edgar__get_filings")
    assert edgar_tool is not None
    assert state.is_visible(edgar_tool) is False

    result = describe.handler(
        ToolCall(name=META_DESCRIBE_TOOL, arguments={"namespace": "edgar"}),
    )
    assert not result.is_error
    payload = result.content[0].data
    assert payload["namespace"] == "edgar"
    assert payload["newly_described"] is True
    tool_names = {t["name"] for t in payload["tools"]}
    assert tool_names == {"mcp__edgar__get_filings", "mcp__edgar__get_company"}

    # Post-condition: edgar is now visible.
    assert state.is_visible(edgar_tool) is True

    # Idempotent — describing again returns newly_described=False.
    again = describe.handler(
        ToolCall(name=META_DESCRIBE_TOOL, arguments={"namespace": "edgar"}),
    )
    assert again.content[0].data["newly_described"] is False


def test_mcp_describe_unknown_namespace_returns_typed_error() -> None:
    registry, _ = _build_registry_with_two_namespaces()
    describe = registry.find(META_DESCRIBE_TOOL)
    assert describe is not None
    result = describe.handler(
        ToolCall(name=META_DESCRIBE_TOOL, arguments={"namespace": "ghost"}),
    )
    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind.value == "not_found"


def test_mcp_describe_missing_namespace_arg_returns_schema_error() -> None:
    registry, _ = _build_registry_with_two_namespaces()
    describe = registry.find(META_DESCRIBE_TOOL)
    assert describe is not None
    result = describe.handler(
        ToolCall(name=META_DESCRIBE_TOOL, arguments={}),
    )
    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind.value == "schema_validation"


def test_mcp_call_dispatches_to_underlying_tool_handler() -> None:
    registry, _ = _build_registry_with_two_namespaces()
    call_tool = registry.find(META_CALL_TOOL)
    assert call_tool is not None

    result = call_tool.handler(
        ToolCall(
            name=META_CALL_TOOL,
            arguments={
                "namespace": "edgar",
                "tool": "get_filings",
                "args": {"ticker": "AAPL"},
            },
        ),
    )
    assert not result.is_error
    # Body should come from the underlying handler.
    assert result.content[0].text == "ran mcp__edgar__get_filings"
    # Provenance metadata attached.
    assert result.metadata.get("via_mcp_call") is True
    assert result.metadata.get("mcp_namespace") == "edgar"
    assert (
        result.metadata.get("mcp_underlying_tool")
        == "mcp__edgar__get_filings"
    )


def test_mcp_call_validates_underlying_schema_before_dispatch() -> None:
    registry = ToolRegistry()
    state = LazyMcpState()
    dispatched = False

    def handler(call: ToolCall) -> ToolResult:
        nonlocal dispatched
        dispatched = True
        return ToolResult.from_text(
            tool_use_id=call.id,
            name=call.name,
            text="should not run",
        )

    registry.register(
        ToolDescriptor(
            name="mcp__coingecko__execute",
            description="run CoinGecko SDK code",
            input_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "intent": {"type": "string"},
                },
                "required": ["code"],
            },
            handler=handler,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            tags=("mcp", "coingecko"),
            namespace="mcp",
        ),
    )
    state.register_namespace(
        "coingecko",
        ["mcp__coingecko__execute"],
        always_eager=True,
    )
    attach_lazy_state(registry, state)
    install_meta_tools(registry=registry, state=state)

    call_tool = registry.find(META_CALL_TOOL)
    assert call_tool is not None
    result = call_tool.handler(
        ToolCall(
            name=META_CALL_TOOL,
            arguments={
                "namespace": "coingecko",
                "tool": "mcp__coingecko__execute",
                "args": {"query": "trending"},
            },
        ),
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind.value == "schema_validation"
    assert "`code` is missing" in result.error.message
    assert result.error.retryable is True
    assert dispatched is False


def test_mcp_call_accepts_fully_qualified_tool_name() -> None:
    registry, _ = _build_registry_with_two_namespaces()
    call_tool = registry.find(META_CALL_TOOL)
    assert call_tool is not None
    result = call_tool.handler(
        ToolCall(
            name=META_CALL_TOOL,
            arguments={
                "namespace": "yahoo",
                "tool": "mcp__yahoo__quote",
            },
        ),
    )
    assert not result.is_error
    assert result.content[0].text == "ran mcp__yahoo__quote"


def test_mcp_call_unknown_tool_in_known_namespace_errors() -> None:
    registry, _ = _build_registry_with_two_namespaces()
    call_tool = registry.find(META_CALL_TOOL)
    assert call_tool is not None
    result = call_tool.handler(
        ToolCall(
            name=META_CALL_TOOL,
            arguments={"namespace": "edgar", "tool": "no_such_tool"},
        ),
    )
    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind.value == "not_found"


# ---------------------------------------------------------------------------
# mcp_call permissive args (flat-style auto-promote)
# ---------------------------------------------------------------------------
#
# Models overwhelmingly try the flat style first
# (mcp_call(ns='X', tool='Y', ticker='Z')) instead of the nested
# (mcp_call(ns='X', tool='Y', args={'ticker':'Z'})). Strict additional-
# Properties=False rejection killed autonomous MCP usage in research
# turns. These tests lock in the permissive behavior.


def test_mcp_call_permissive_flat_args_promoted_to_underlying() -> None:
    """Flat-style: extra top-level params (other than namespace/tool)
    are auto-promoted to the underlying tool's args."""
    registry, _ = _build_registry_with_two_namespaces()
    call_tool = registry.find(META_CALL_TOOL)
    assert call_tool is not None
    result = call_tool.handler(
        ToolCall(
            name=META_CALL_TOOL,
            arguments={
                "namespace": "edgar",
                "tool": "get_filings",
                # No 'args' key — these become the tool's args.
                "ticker": "AAPL",
                "form_type": "10-K",
            },
        ),
    )
    assert not result.is_error, f"got error: {result.error}"
    assert result.content[0].text == "ran mcp__edgar__get_filings"
    assert result.metadata.get("via_mcp_call") is True


def test_mcp_call_permissive_explicit_args_wins_when_mixed() -> None:
    """If both nested ``args`` and top-level extras are present,
    nested args take precedence (defensive symmetry)."""
    registry, recorded = _build_registry_with_two_namespaces(
        capture_args=True,
    )
    call_tool = registry.find(META_CALL_TOOL)
    assert call_tool is not None
    result = call_tool.handler(
        ToolCall(
            name=META_CALL_TOOL,
            arguments={
                "namespace": "edgar",
                "tool": "get_filings",
                "args": {"ticker": "MSFT", "form_type": "10-Q"},
                # These extras would lose to nested args:
                "ticker": "AAPL",
                "limit": 5,
            },
        ),
    )
    assert not result.is_error
    seen = recorded["edgar.get_filings"]
    assert seen["ticker"] == "MSFT", "nested args.ticker should win"
    assert seen["form_type"] == "10-Q"
    assert seen["limit"] == 5, "extras lose key collisions but unique extras still merge"


def test_mcp_call_permissive_no_extras_no_args_works() -> None:
    """Calling without any args at all still dispatches with empty {}."""
    registry, recorded = _build_registry_with_two_namespaces(
        capture_args=True,
    )
    call_tool = registry.find(META_CALL_TOOL)
    assert call_tool is not None
    result = call_tool.handler(
        ToolCall(
            name=META_CALL_TOOL,
            arguments={"namespace": "yahoo", "tool": "quote"},
        ),
    )
    assert not result.is_error
    assert recorded["yahoo.quote"] == {}


def test_mcp_call_args_must_still_be_dict_when_explicit() -> None:
    """A non-dict ``args`` is still a schema error (don't accidentally
    accept strings/lists)."""
    registry, _ = _build_registry_with_two_namespaces()
    call_tool = registry.find(META_CALL_TOOL)
    assert call_tool is not None
    result = call_tool.handler(
        ToolCall(
            name=META_CALL_TOOL,
            arguments={
                "namespace": "edgar",
                "tool": "get_filings",
                "args": ["not", "a", "dict"],
            },
        ),
    )
    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind.value == "schema_validation"


# ---------------------------------------------------------------------------
# K-3 register_external_mcp_tools propagates lazy
# ---------------------------------------------------------------------------


def _fake_adapter(server_id: str, tool_specs: list[dict]) -> MCPSessionAdapter:
    """Minimal MCPSessionAdapter stub — only ``client.list_tools`` and
    ``server_id`` are needed for register_external_mcp_tools."""
    client = MagicMock()
    client.list_tools.return_value = tool_specs
    return MCPSessionAdapter(client=client, server_id=server_id)


def test_register_external_mcp_tools_default_eager() -> None:
    """When ``lazy=False`` (legacy default), descriptors land with
    ``lazy=False`` so the prompt-time filter does not hide them."""
    registry = ToolRegistry()
    adapter = _fake_adapter(
        "edgar",
        [
            {
                "name": "get_filings",
                "description": "fetch filings",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ],
    )
    names = register_external_mcp_tools(
        registry=registry, adapter=adapter, lazy=False,
    )
    assert names == ["mcp__edgar__get_filings"]
    d = registry.find("mcp__edgar__get_filings")
    assert d is not None
    assert d.lazy is False


def test_register_external_mcp_tools_lazy_true_marks_descriptors() -> None:
    registry = ToolRegistry()
    adapter = _fake_adapter(
        "edgar",
        [
            {
                "name": "get_filings",
                "description": "fetch filings",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "get_company",
                "description": "company facts",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ],
    )
    register_external_mcp_tools(
        registry=registry, adapter=adapter, lazy=True,
    )
    for n in ("mcp__edgar__get_filings", "mcp__edgar__get_company"):
        d = registry.find(n)
        assert d is not None and d.lazy is True


# ---------------------------------------------------------------------------
# Phase L — per-server deny_tools / allow_tools filter
# ---------------------------------------------------------------------------


def _three_yahoo_tools() -> list[dict]:
    """Tool list mirroring the live yahoo_finance MCP shape (subset)."""

    return [
        {
            "name": "get_historical_stock_prices",
            "description": "OHLCV bars",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_stock_info",
            "description": "quote + metrics",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_financial_statement",
            "description": "income/balance/cashflow",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def test_phase_l_deny_tools_drops_named_tool_before_registration() -> None:
    """The denied upstream name must not produce any registry entry."""
    registry = ToolRegistry()
    adapter = _fake_adapter("yahoo", _three_yahoo_tools())

    names = register_external_mcp_tools(
        registry=registry,
        adapter=adapter,
        deny_tools=["get_historical_stock_prices"],
    )

    # Public namespaced names of survivors only.
    assert names == ["mcp__yahoo__get_stock_info",
                     "mcp__yahoo__get_financial_statement"]
    # Denied tool absent from the registry.
    assert registry.find("mcp__yahoo__get_historical_stock_prices") is None
    # Survivors present.
    assert registry.find("mcp__yahoo__get_stock_info") is not None
    assert registry.find("mcp__yahoo__get_financial_statement") is not None


def test_phase_l_deny_tools_empty_means_no_filter() -> None:
    """Backwards-compat: omitting deny_tools keeps current eager behaviour."""
    registry = ToolRegistry()
    adapter = _fake_adapter("yahoo", _three_yahoo_tools())

    names = register_external_mcp_tools(registry=registry, adapter=adapter)

    assert sorted(names) == [
        "mcp__yahoo__get_financial_statement",
        "mcp__yahoo__get_historical_stock_prices",
        "mcp__yahoo__get_stock_info",
    ]


def test_phase_l_allow_tools_acts_as_strict_whitelist() -> None:
    """When allow_tools is set, only listed names survive."""
    registry = ToolRegistry()
    adapter = _fake_adapter("yahoo", _three_yahoo_tools())

    names = register_external_mcp_tools(
        registry=registry,
        adapter=adapter,
        allow_tools=["get_stock_info"],
    )

    assert names == ["mcp__yahoo__get_stock_info"]
    assert registry.find("mcp__yahoo__get_historical_stock_prices") is None
    assert registry.find("mcp__yahoo__get_financial_statement") is None


def test_phase_l_deny_takes_precedence_over_allow() -> None:
    """When a name is in both deny and allow, deny wins (defense in depth)."""
    registry = ToolRegistry()
    adapter = _fake_adapter("yahoo", _three_yahoo_tools())

    names = register_external_mcp_tools(
        registry=registry,
        adapter=adapter,
        deny_tools=["get_historical_stock_prices"],
        allow_tools=[
            "get_historical_stock_prices",  # in both — must still be dropped
            "get_stock_info",
        ],
    )

    assert names == ["mcp__yahoo__get_stock_info"]


def test_phase_l_denied_tool_cannot_be_dispatched_via_mcp_call() -> None:
    """End-to-end: denied tools never reach mcp_call's dispatch path
    because they were never registered."""
    from nerya.mcp.lazy import (
        LazyMcpState,
        attach_lazy_state,
        install_meta_tools,
        META_CALL_TOOL,
    )
    from nerya.tools.types import ToolCall

    registry = ToolRegistry()
    adapter = _fake_adapter("yahoo", _three_yahoo_tools())
    register_external_mcp_tools(
        registry=registry,
        adapter=adapter,
        lazy=True,
        deny_tools=["get_historical_stock_prices"],
    )
    state = LazyMcpState()
    state.mark_described("yahoo")  # so mcp_call sees the namespace as live
    attach_lazy_state(registry=registry, state=state)
    install_meta_tools(registry=registry, state=state)

    call_tool = registry.find(META_CALL_TOOL)
    assert call_tool is not None
    result = call_tool.handler(
        ToolCall(
            id="t1", name=META_CALL_TOOL,
            arguments={
                "namespace": "yahoo",
                "tool": "get_historical_stock_prices",  # the denied one
                "args": {"ticker": "AAPL"},
            },
        ),
    )
    # Denied tool was never registered, so mcp_call cannot find it.
    assert result.is_error is True
    assert result.error is not None
    assert (
        "not found" in (result.error.message or "").lower()
        or "unknown" in (result.error.message or "").lower()
    )


def test_phase_l_attach_mcp_adapters_threads_per_server_filters() -> None:
    """attach_mcp_adapters fans the {server_id: deny_list} map to each
    register_external_mcp_tools call."""
    from nerya.mcp.session_adapter import attach_mcp_adapters

    registry = ToolRegistry()
    yahoo_adapter = _fake_adapter("yahoo", _three_yahoo_tools())
    edgar_adapter = _fake_adapter(
        "edgar",
        [
            {
                "name": "get_filings",
                "description": "10-K/Q",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "get_company",
                "description": "facts",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ],
    )

    class _StubExecutor:
        def add_post_hook(self, hook):  # noqa: D401, ANN001
            pass

    out = attach_mcp_adapters(
        registry=registry,
        executor=_StubExecutor(),
        adapters=[yahoo_adapter, edgar_adapter],
        # yahoo: drop OHLC; edgar: keep all
        deny_tools_by_server={"yahoo": ["get_historical_stock_prices"]},
    )

    # yahoo: 2 of 3 survive; edgar: 2 of 2 survive.
    assert sorted(out["yahoo"]) == [
        "mcp__yahoo__get_financial_statement",
        "mcp__yahoo__get_stock_info",
    ]
    assert sorted(out["edgar"]) == [
        "mcp__edgar__get_company",
        "mcp__edgar__get_filings",
    ]
    assert registry.find("mcp__yahoo__get_historical_stock_prices") is None


def test_phase_l_config_from_raw_parses_deny_tools_list() -> None:
    """MCPServerConfig.from_raw accepts deny_tools as a list of strings."""
    from nerya.mcp.connectors.config import MCPServerConfig

    raw = {
        "id": "yahoo_finance",
        "enabled": True,
        "namespace": "yahoo",
        "transport": {"kind": "stdio", "command": ["uvx", "yahoo-finance-mcp"]},
        "auth": {"kind": "none"},
        "deny_tools": ["get_historical_stock_prices", "another_tool"],
    }
    cfg = MCPServerConfig.from_raw(raw)

    assert cfg.deny_tools == ("get_historical_stock_prices", "another_tool")
    assert cfg.allow_tools is None


def test_phase_l_config_from_raw_rejects_non_list_deny_tools() -> None:
    """A scalar / dict / string for deny_tools must be a typed error
    (operators editing yaml deserve a precise message)."""
    from nerya.mcp.connectors.config import (
        ConnectorConfigError,
        MCPServerConfig,
    )

    raw = {
        "id": "yahoo_finance",
        "enabled": True,
        "namespace": "yahoo",
        "transport": {"kind": "stdio", "command": ["uvx", "yahoo-finance-mcp"]},
        "auth": {"kind": "none"},
        "deny_tools": "get_historical_stock_prices",  # str, not list
    }
    with pytest.raises(ConnectorConfigError, match="deny_tools"):
        MCPServerConfig.from_raw(raw)


def test_phase_l_config_from_raw_omitted_deny_tools_defaults_to_empty() -> None:
    """Backwards-compat: existing yamls without deny_tools must not break."""
    from nerya.mcp.connectors.config import MCPServerConfig

    raw = {
        "id": "sec_edgar",
        "enabled": True,
        "namespace": "edgar",
        "transport": {"kind": "stdio", "command": ["uvx", "sec-edgar-mcp"]},
        "auth": {"kind": "none"},
    }
    cfg = MCPServerConfig.from_raw(raw)

    assert cfg.deny_tools == ()
    assert cfg.allow_tools is None


# ---------------------------------------------------------------------------
# K-5 agent loop _render_tools — duck-typed lazy filter
# ---------------------------------------------------------------------------


def test_render_tools_filter_via_lazy_mcp_state_attribute() -> None:
    """Simulate what AgentLoop._render_tools does: read the lazy state
    from the registry and filter through it."""
    registry, state = _build_registry_with_two_namespaces()

    # Mirror the loop's exact logic:
    tools = registry.list_tools()
    lazy_state = getattr(registry, "lazy_mcp_state", None)
    assert lazy_state is state
    is_visible = getattr(lazy_state, "is_visible", None)
    assert callable(is_visible)
    visible = [t for t in tools if is_visible(t)]
    visible_names = {t.name for t in visible}

    # native demo + 3 meta tools + yahoo (always_eager) — but NOT edgar
    # (lazy, undescribed).
    assert "native_demo" in visible_names
    assert META_NAMESPACES_TOOL in visible_names
    assert META_DESCRIBE_TOOL in visible_names
    assert META_CALL_TOOL in visible_names
    assert "mcp__yahoo__quote" in visible_names
    assert "mcp__edgar__get_filings" not in visible_names
    assert "mcp__edgar__get_company" not in visible_names

    # After describing edgar, it appears.
    state.mark_described("edgar")
    visible_after = [t for t in registry.list_tools() if is_visible(t)]
    visible_after_names = {t.name for t in visible_after}
    assert "mcp__edgar__get_filings" in visible_after_names
    assert "mcp__edgar__get_company" in visible_after_names


def test_render_tools_no_lazy_state_keeps_legacy_behavior() -> None:
    """When no LazyMcpState attached, every registered tool is rendered —
    duck-typing failure is fail-open (the legacy path)."""
    registry = ToolRegistry()
    registry.register(_native_tool("a"))
    # A lazy-flagged tool that has NO state to interpret it: should
    # remain visible because the loop's filter is gated on the state.
    registry.register(_mcp_tool("ghost", "tool", lazy=True))

    tools = registry.list_tools()
    lazy_state = getattr(registry, "lazy_mcp_state", None)
    assert lazy_state is None
    # Loop's pre-filter step: NO-OP. Both stay visible.
    assert {"a", "mcp__ghost__tool"}.issubset({t.name for t in tools})


# ---------------------------------------------------------------------------
# K-3 attach_lazy_state idempotency / merging
# ---------------------------------------------------------------------------


def test_attach_lazy_state_first_attach_returns_same_instance() -> None:
    registry = ToolRegistry()
    state = LazyMcpState()
    state.register_namespace("a", ["t1"], always_eager=False)
    out = attach_lazy_state(registry, state)
    assert out is state
    assert getattr(registry, "lazy_mcp_state") is state


def test_attach_lazy_state_second_attach_merges_and_preserves_described() -> None:
    """Re-running bootstrap mid-session must NOT clobber the model's
    earlier ``mcp_describe`` calls (per-session cache invariant)."""
    registry = ToolRegistry()
    state1 = LazyMcpState()
    state1.register_namespace("edgar", ["t1"], always_eager=False)
    state1.mark_described("edgar")
    attach_lazy_state(registry, state1)

    state2 = LazyMcpState()
    state2.register_namespace("yahoo", ["q1"], always_eager=True)
    out = attach_lazy_state(registry, state2)

    # Same identity as state1 — we merged, didn't replace.
    assert out is state1
    # Both namespaces visible.
    snap = state1.snapshot()
    assert "edgar" in snap["namespaces"]
    assert "yahoo" in snap["namespaces"]
    assert "yahoo" in snap["always_eager"]
    # And the previously-described edgar is still described.
    assert "edgar" in snap["described"]

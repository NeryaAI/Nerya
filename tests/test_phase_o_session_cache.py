"""Phase O — per-session MCP describe cache tests.

Phase O extends ``LazyMcpState`` with a ``describe_response_cache``
field and adds a process-level session-keyed cache so the
``described_namespaces`` set + cached describe payloads survive across
turns of the same chat conversation.

Coverage map (mirrors ``phase_o_done.md`` § 3):

1. ``LazyMcpState.describe_response_cache`` field exists, defaults to
   ``{}``, and is wiped by :meth:`reset_session`.
2. ``_make_describe_handler`` populates the cache on first call and
   surfaces ``from_cache=False``.
3. Second describe of the same namespace returns ``from_cache=True``
   with the SAME tools payload and does NOT re-scan the registry.
4. Cache hit still calls ``mark_described`` (idempotent) so the
   visibility filter sees the namespace as described.
5. ``get_or_create_session_state`` returns the same ``LazyMcpState``
   instance for the same ``(workspace, session_id)`` key and a
   different one for a different key.
6. ``pull_session_cache_into`` copies ``described_namespaces`` and
   ``describe_response_cache`` into the per-request state.
7. ``push_state_into_session_cache`` mirrors the per-request state's
   describes back to the session-scoped state.
8. End-to-end round-trip: describe → push → fresh state → pull →
   describe again returns from_cache=True.
9. Workspace isolation: same session_id under two different workspace
   roots → independent caches.
10. ``reset_session_cache`` removes the entry; subsequent get_or_create
    builds a fresh empty state.
11. ``session_cache_size`` reflects the live entry count.
12. Pull / push are no-ops when ``session_id`` is falsy.
"""

from __future__ import annotations

from typing import Any

import pytest

from nerya.mcp.lazy import (
    META_DESCRIBE_TOOL,
    LazyMcpState,
    _clear_all_session_caches,
    get_or_create_session_state,
    pull_session_cache_into,
    push_state_into_session_cache,
    reset_session_cache,
    session_cache_size,
)
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
# Shared fixtures
# ---------------------------------------------------------------------------


def _mcp_tool(server_id: str, tool_name: str) -> ToolDescriptor:
    return ToolDescriptor(
        name=f"mcp__{server_id}__{tool_name}",
        description=f"{server_id}/{tool_name}",
        input_schema={
            "type": "object",
            "properties": {"foo": {"type": "string"}},
        },
        handler=lambda call: ToolResult.from_text(
            tool_use_id=call.id, name=call.name, text="ok",
        ),
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NETWORK,
        tags=("mcp", server_id),
        namespace="mcp",
        lazy=True,
    )


def _build_two_namespace_registry() -> tuple[ToolRegistry, LazyMcpState]:
    """Mirror the Phase K helper but Phase O-specific (don't import
    private Phase K helpers from another test module)."""

    from nerya.mcp.lazy import attach_lazy_state, install_meta_tools

    registry = ToolRegistry()
    state = LazyMcpState()
    registry.register(_mcp_tool("edgar", "get_filings"))
    registry.register(_mcp_tool("edgar", "get_company"))
    registry.register(_mcp_tool("yahoo", "quote"))
    state.register_namespace(
        "edgar",
        ["mcp__edgar__get_filings", "mcp__edgar__get_company"],
        always_eager=False,
    )
    state.register_namespace("yahoo", ["mcp__yahoo__quote"], always_eager=False)
    attach_lazy_state(registry, state)
    install_meta_tools(registry=registry, state=state)
    return registry, state


@pytest.fixture(autouse=True)
def _clean_session_cache():
    """Phase O cache is process-level; reset between tests so one test
    can't leak state into another."""

    _clear_all_session_caches()
    yield
    _clear_all_session_caches()


# ---------------------------------------------------------------------------
# 1. Field default + reset semantics
# ---------------------------------------------------------------------------


def test_phase_o_describe_response_cache_defaults_empty() -> None:
    state = LazyMcpState()
    assert state.describe_response_cache == {}


def test_phase_o_reset_session_wipes_describe_cache() -> None:
    """``reset_session`` must clear the new cache too — it's part of
    the same logical session as ``described_namespaces``."""

    state = LazyMcpState()
    state.describe_response_cache["edgar"] = {"tools": [{"name": "x"}]}
    state.described_namespaces.add("edgar")
    state.reset_session()
    assert state.describe_response_cache == {}
    assert state.described_namespaces == set()


# ---------------------------------------------------------------------------
# 2 + 3 + 4. Describe handler caches + reuses
# ---------------------------------------------------------------------------


def test_phase_o_describe_handler_first_call_populates_cache_and_marks_fresh() -> None:
    registry, state = _build_two_namespace_registry()
    describe = registry.find(META_DESCRIBE_TOOL)
    assert describe is not None

    result = describe.handler(
        ToolCall(name=META_DESCRIBE_TOOL, arguments={"namespace": "edgar"}),
    )
    assert not result.is_error
    payload = result.content[0].data

    assert payload["from_cache"] is False
    assert payload["newly_described"] is True
    assert {t["name"] for t in payload["tools"]} == {
        "mcp__edgar__get_filings",
        "mcp__edgar__get_company",
    }
    # Cache was populated with the same payload shape.
    assert "edgar" in state.describe_response_cache
    cached = state.describe_response_cache["edgar"]
    assert {t["name"] for t in cached["tools"]} == {
        "mcp__edgar__get_filings",
        "mcp__edgar__get_company",
    }


def test_phase_o_describe_handler_second_call_returns_from_cache() -> None:
    registry, state = _build_two_namespace_registry()
    describe = registry.find(META_DESCRIBE_TOOL)
    assert describe is not None

    first = describe.handler(
        ToolCall(name=META_DESCRIBE_TOOL, arguments={"namespace": "edgar"}),
    )
    second = describe.handler(
        ToolCall(name=META_DESCRIBE_TOOL, arguments={"namespace": "edgar"}),
    )
    assert not second.is_error

    p2 = second.content[0].data
    assert p2["from_cache"] is True
    # newly_described is False on a re-describe — was True the first time.
    assert p2["newly_described"] is False
    # Same tool names as first call.
    p1 = first.content[0].data
    assert {t["name"] for t in p2["tools"]} == {
        t["name"] for t in p1["tools"]
    }
    # Cache hit MUST still mark the namespace described so the
    # visibility filter sees it as eligible.
    assert "edgar" in state.described_namespaces


def test_phase_o_describe_handler_cache_does_not_leak_across_namespaces() -> None:
    registry, state = _build_two_namespace_registry()
    describe = registry.find(META_DESCRIBE_TOOL)
    assert describe is not None

    # Describe edgar first — populates cache for "edgar" only.
    describe.handler(
        ToolCall(name=META_DESCRIBE_TOOL, arguments={"namespace": "edgar"}),
    )
    # Describe yahoo — should be a fresh fetch, not a cache hit.
    yahoo_result = describe.handler(
        ToolCall(name=META_DESCRIBE_TOOL, arguments={"namespace": "yahoo"}),
    )
    payload = yahoo_result.content[0].data
    assert payload["from_cache"] is False
    assert payload["newly_described"] is True
    # Both namespaces now cached + described.
    assert set(state.describe_response_cache.keys()) == {"edgar", "yahoo"}
    assert state.described_namespaces == {"edgar", "yahoo"}


# ---------------------------------------------------------------------------
# 5. Session-keyed registry identity
# ---------------------------------------------------------------------------


def test_phase_o_session_state_same_key_returns_same_instance() -> None:
    s1 = get_or_create_session_state("/ws", "sess-A")
    s2 = get_or_create_session_state("/ws", "sess-A")
    assert s1 is s2


def test_phase_o_session_state_different_session_returns_different_instance() -> None:
    s1 = get_or_create_session_state("/ws", "sess-A")
    s2 = get_or_create_session_state("/ws", "sess-B")
    assert s1 is not s2


def test_phase_o_session_state_different_workspace_returns_different_instance() -> None:
    """Same session_id under two different workspaces must NOT share a
    cache (operator scoping invariant)."""

    s_a = get_or_create_session_state("/ws-A", "sess-X")
    s_b = get_or_create_session_state("/ws-B", "sess-X")
    assert s_a is not s_b


# ---------------------------------------------------------------------------
# 6. pull_session_cache_into copies described + describe payloads
# ---------------------------------------------------------------------------


def test_phase_o_pull_promotes_described_namespaces() -> None:
    cached = get_or_create_session_state("/ws", "sess-1")
    cached.described_namespaces.update({"edgar", "yahoo"})
    cached.describe_response_cache["edgar"] = {
        "tools": [{"name": "mcp__edgar__x"}],
        "hint": "from prior session",
    }

    target = LazyMcpState()
    promoted = pull_session_cache_into(
        target, workspace_root="/ws", session_id="sess-1",
    )
    assert promoted == 2
    assert target.described_namespaces == {"edgar", "yahoo"}
    assert "edgar" in target.describe_response_cache
    assert target.describe_response_cache["edgar"]["hint"] == "from prior session"


def test_phase_o_pull_preserves_target_namespace_index_and_eager() -> None:
    """Pull must NOT overwrite bootstrap-time facts on the per-request
    target state — only the session-mutable parts."""

    cached = get_or_create_session_state("/ws", "sess-2")
    cached.described_namespaces.add("edgar")

    target = LazyMcpState()
    target.namespace_index["edgar"] = ["mcp__edgar__t1"]
    target.always_eager_namespaces.add("yahoo")

    pull_session_cache_into(
        target, workspace_root="/ws", session_id="sess-2",
    )
    # Bootstrap facts intact.
    assert target.namespace_index == {"edgar": ["mcp__edgar__t1"]}
    assert target.always_eager_namespaces == {"yahoo"}
    # Plus the pulled describe.
    assert target.described_namespaces == {"edgar"}


def test_phase_o_pull_with_falsy_session_id_is_noop() -> None:
    """Strategy / fixed-config turns may not have a session_id; the
    pull must silently no-op rather than crash or build a junk key."""

    target = LazyMcpState()
    promoted = pull_session_cache_into(
        target, workspace_root="/ws", session_id=None,
    )
    assert promoted == 0
    assert target.described_namespaces == set()
    # And no spurious cache key got created.
    assert session_cache_size() == 0


# ---------------------------------------------------------------------------
# 7. push_state_into_session_cache mirrors back
# ---------------------------------------------------------------------------


def test_phase_o_push_persists_described_into_session_cache() -> None:
    live = LazyMcpState()
    live.described_namespaces.update({"edgar", "yahoo"})
    live.describe_response_cache["edgar"] = {"tools": [{"name": "x"}]}

    persisted = push_state_into_session_cache(
        live, workspace_root="/ws", session_id="sess-3",
    )
    assert persisted == 2

    cached = get_or_create_session_state("/ws", "sess-3")
    assert cached.described_namespaces == {"edgar", "yahoo"}
    assert "edgar" in cached.describe_response_cache


def test_phase_o_push_with_falsy_session_id_is_noop() -> None:
    live = LazyMcpState()
    live.described_namespaces.add("edgar")
    persisted = push_state_into_session_cache(
        live, workspace_root="/ws", session_id="",
    )
    assert persisted == 0
    assert session_cache_size() == 0


# ---------------------------------------------------------------------------
# 8. End-to-end round-trip: turn-A describe → turn-B see cached
# ---------------------------------------------------------------------------


def test_phase_o_round_trip_simulates_two_consecutive_turns() -> None:
    """This is the headline behaviour: a model that called
    ``mcp_describe(edgar)`` in turn 1 must see edgar's tools as
    described in turn 2 of the same session, without re-paying the
    describe."""

    # ---- Turn 1: fresh kernel, describe edgar, push at end of turn ----
    reg1, state1 = _build_two_namespace_registry()
    desc1 = reg1.find(META_DESCRIBE_TOOL)
    assert desc1 is not None
    result1 = desc1.handler(
        ToolCall(name=META_DESCRIBE_TOOL, arguments={"namespace": "edgar"}),
    )
    assert not result1.is_error
    assert result1.content[0].data["from_cache"] is False

    push_state_into_session_cache(
        state1, workspace_root="/ws", session_id="conv-A",
    )

    # ---- Turn 2: brand-new kernel, pull from session cache, describe ----
    reg2, state2 = _build_two_namespace_registry()  # fresh state
    assert state2.described_namespaces == set()
    assert state2.describe_response_cache == {}

    pull_session_cache_into(
        state2, workspace_root="/ws", session_id="conv-A",
    )
    # After the pull, turn 2 already sees edgar as described.
    assert "edgar" in state2.described_namespaces
    assert "edgar" in state2.describe_response_cache

    # And the describe handler returns from_cache=True without
    # re-scanning the registry.
    desc2 = reg2.find(META_DESCRIBE_TOOL)
    assert desc2 is not None
    result2 = desc2.handler(
        ToolCall(name=META_DESCRIBE_TOOL, arguments={"namespace": "edgar"}),
    )
    p2 = result2.content[0].data
    assert p2["from_cache"] is True
    # Same tool names as turn 1's first describe.
    assert {t["name"] for t in p2["tools"]} == {
        t["name"] for t in result1.content[0].data["tools"]
    }


# ---------------------------------------------------------------------------
# 9 + 10 + 11. Reset + size accounting
# ---------------------------------------------------------------------------


def test_phase_o_reset_session_cache_drops_entry_and_returns_true() -> None:
    s = get_or_create_session_state("/ws", "sess-Z")
    s.described_namespaces.add("edgar")
    assert session_cache_size() == 1

    removed = reset_session_cache(workspace_root="/ws", session_id="sess-Z")
    assert removed is True
    assert session_cache_size() == 0

    # Subsequent get_or_create builds a fresh empty state.
    s2 = get_or_create_session_state("/ws", "sess-Z")
    assert s2.described_namespaces == set()
    assert s2 is not s


def test_phase_o_reset_session_cache_unknown_key_returns_false() -> None:
    removed = reset_session_cache(workspace_root="/ws", session_id="ghost")
    assert removed is False


def test_phase_o_session_cache_size_tracks_distinct_keys() -> None:
    assert session_cache_size() == 0
    get_or_create_session_state("/ws", "a")
    get_or_create_session_state("/ws", "b")
    get_or_create_session_state("/ws-2", "a")  # different workspace, same session id
    assert session_cache_size() == 3

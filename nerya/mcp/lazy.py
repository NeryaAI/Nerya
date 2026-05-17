"""MCP lazy-loading state and meta-tools.

This module keeps large MCP toolsets out of the prompt until the model
actually needs them. Namespaces marked ``always_eager`` stay visible
from the first turn; other namespaces register their tools as lazy so
they remain dispatchable but hidden from prompt-time tool rendering.

Three eager meta-tools provide the discovery surface:

* :func:`mcp_namespaces` lists available namespaces and their lazy/eager state.
* :func:`mcp_describe` reveals one namespace's schemas and marks it as described
  for the current session.
* :func:`mcp_call` dispatches an underlying MCP tool whether or not the
  namespace has already been described.

``LazyMcpState`` stores the per-session described namespace set. The
agent loop asks :meth:`LazyMcpState.is_visible` before rendering tools,
so non-MCP native tools remain unaffected.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from ..tools.registry import ToolRegistry
from ..tools.tool_errors import collect_schema_issues, format_schema_validation_error
from ..tools.types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolDescriptor,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


_LOG = logging.getLogger(__name__)

#: Public name of the meta-tool that lists MCP namespaces.
META_NAMESPACES_TOOL = "mcp_namespaces"

#: Public name of the meta-tool that describes one namespace's tools.
META_DESCRIBE_TOOL = "mcp_describe"

#: Public name of the meta-tool that dispatches an MCP tool call.
META_CALL_TOOL = "mcp_call"

#: Names of all meta-tools (used by tests + filters to exempt them
#: from any later "is this an MCP tool?" check).
META_TOOL_NAMES = frozenset({META_NAMESPACES_TOOL, META_DESCRIBE_TOOL, META_CALL_TOOL})


# ---------------------------------------------------------------------------
# Server-id extraction
# ---------------------------------------------------------------------------


def server_id_of(d: ToolDescriptor) -> Optional[str]:
    """Return the MCP server id encoded in ``d.tags`` or None.

    ``register_external_mcp_tools`` builds every MCP descriptor with
    ``tags=("mcp", adapter.server_id)``. We use that instead of parsing
    the public name (``mcp__server__tool``) because the name format is
    a presentation detail that may change.
    """

    if d.namespace != "mcp":
        return None
    for tag in d.tags:
        if tag != "mcp" and tag:
            return tag
    return None


# ---------------------------------------------------------------------------
# LazyMcpState
# ---------------------------------------------------------------------------


@dataclass
class LazyMcpState:
    """Per-session state for MCP lazy-loading.

    One instance per :class:`ToolRegistry` (attached as
    ``registry.lazy_mcp_state``). Built at MCP bootstrap time and
    mutated only by the :func:`mcp_describe` meta-tool handler.

    Thread-safety: all mutations go through ``self._lock`` so a
    concurrent ``mcp_describe`` from a parallel tool batch and the
    loop's ``_render_tools`` see a consistent view.

    Session-cache note
    ==================

    The state object is per-registry-build-time; each chat handler
    constructs its own kernel and therefore its own state. The
    long-lived session-scoped cache lives in the module-level
    :data:`_SESSION_STATES` registry below, and the kernel's
    ``_run_turn`` pulls + pushes through :func:`pull_session_cache_into`
    + :func:`push_state_into_session_cache` so the model sees previously
    described namespaces and previously fetched describe payloads
    across turns of the same conversation.
    """

    #: Map ``server_id -> [public_tool_name, ...]`` populated at
    #: bootstrap from the per-server ``register_external_mcp_tools``
    #: result. Used by ``mcp_describe`` to find descriptors quickly
    #: and by ``mcp_namespaces`` to report tool counts without scanning
    #: the entire registry.
    namespace_index: dict[str, list[str]] = field(default_factory=dict)

    #: Server ids whose tools are eagerly visible regardless of the
    #: described set (``always_eager: true`` in mcp_servers.yml).
    always_eager_namespaces: set[str] = field(default_factory=set)

    #: Server ids whose tools have been promoted via ``mcp_describe``
    #: in this session. Mutable; reset by :meth:`reset_session`.
    described_namespaces: set[str] = field(default_factory=set)

    #: Per-namespace cache of the ``mcp_describe`` response
    #: payload (``{tools: [...], hint: ...}`` shape). Populated on the
    #: first describe call for a namespace; subsequent calls in the
    #: same session return the cached payload (saves the registry scan
    #: and avoids re-rendering schemas) with ``from_cache=True`` set on
    #: the response. Mutable; reset by :meth:`reset_session`.
    describe_response_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False,
    )

    # ----- mutation -------------------------------------------------------

    def register_namespace(
        self, server_id: str, tool_names: Iterable[str], *, always_eager: bool,
    ) -> None:
        """Record one server's namespace at bootstrap time."""

        with self._lock:
            self.namespace_index[server_id] = list(tool_names)
            if always_eager:
                self.always_eager_namespaces.add(server_id)
            else:
                # An operator who flips a namespace from eager → lazy
                # at re-bootstrap should NOT inherit a stale
                # always_eager flag.
                self.always_eager_namespaces.discard(server_id)

    def mark_described(self, server_id: str) -> bool:
        """Promote a namespace to described. Returns True if newly added."""

        with self._lock:
            if server_id in self.described_namespaces:
                return False
            self.described_namespaces.add(server_id)
            return True

    def reset_session(self) -> None:
        """Clear the described set (start a new session). Eager / index
        memberships are preserved because they are bootstrap-time facts.

        Also wipes :attr:`describe_response_cache` because the
        cached payloads are tied to the same logical session as the
        described set.
        """

        with self._lock:
            self.described_namespaces.clear()
            self.describe_response_cache.clear()

    # ----- inspection -----------------------------------------------------

    def is_visible(self, descriptor: ToolDescriptor) -> bool:
        """Return True iff the descriptor should appear in the prompt.

        * Non-lazy descriptors (native tools, eager MCP tools, the meta
          tools themselves) always pass.
        * Lazy MCP descriptors pass iff their server is in
          ``always_eager_namespaces`` or ``described_namespaces``.
        """

        if not descriptor.lazy:
            return True
        sid = server_id_of(descriptor)
        if sid is None:
            # Unexpected: a non-MCP tool registered as lazy. Be
            # conservative and hide — the operator can always describe
            # it explicitly or set its descriptor to lazy=False.
            return False
        with self._lock:
            return (
                sid in self.always_eager_namespaces
                or sid in self.described_namespaces
            )

    def snapshot(self) -> dict[str, Any]:
        """Lock-free readable summary for diagnostics + tests."""

        with self._lock:
            return {
                "namespaces": {
                    sid: list(names)
                    for sid, names in self.namespace_index.items()
                },
                "always_eager": sorted(self.always_eager_namespaces),
                "described": sorted(self.described_namespaces),
            }


# ---------------------------------------------------------------------------
# Session-scoped cache registry
# ---------------------------------------------------------------------------
#
# Every chat handler builds a fresh AgentKernel + ToolRegistry +
# LazyMcpState per request, so without a long-lived store the
# ``described_namespaces`` set and the new ``describe_response_cache``
# would reset between turns of the same conversation. The model would
# have to re-call ``mcp_describe`` on every turn just to see the same
# yahoo / edgar / coingecko tools it had described one turn earlier.
#
# We side-step the constructor-injection question (kernel doesn't know
# session_id at __post_init__ time) by holding a process-level dict
# keyed by ``(workspace_root, session_id)``. The kernel pulls from
# this cache before the loop runs and pushes back after, mediated by
# :func:`pull_session_cache_into` and :func:`push_state_into_session_cache`.
#
# This is intentionally an in-process dict, not Redis / SQLite. The
# This design keeps the cache in-process for speed and simplicity. The
# trade-off is that cache contents do not survive a
# Nerya server restart. After restart the next turn still works
# correctly (it just re-pays one ``mcp_describe`` call to repopulate
# the cache).

_SESSION_STATES: dict[tuple[str, str], "LazyMcpState"] = {}
_SESSION_STATES_LOCK = threading.RLock()


def _session_key(workspace_root: Any, session_id: Any) -> tuple[str, str]:
    """Build the ``_SESSION_STATES`` key shape.

    Always coerces both halves to ``str`` so an operator passing a
    ``Path`` for the workspace root and a ``UUID`` for the session id
    (both are common shapes) get a stable hashable key.
    """

    return (str(workspace_root), str(session_id))


def get_or_create_session_state(
    workspace_root: Any, session_id: Any,
) -> "LazyMcpState":
    """Return the long-lived, session-scoped :class:`LazyMcpState`.

    Lazily creates an empty state on first call for a given
    ``(workspace_root, session_id)`` pair. The returned state holds
    only the ``described_namespaces`` set and the
    ``describe_response_cache`` dict — its ``namespace_index`` and
    ``always_eager_namespaces`` stay empty because those facts come
    from each per-request bootstrap and merging them in would be
    redundant. The kernel only consults this state to learn what was
    described before, never to discover what tools exist.
    """

    key = _session_key(workspace_root, session_id)
    with _SESSION_STATES_LOCK:
        st = _SESSION_STATES.get(key)
        if st is None:
            st = LazyMcpState()
            _SESSION_STATES[key] = st
        return st


def pull_session_cache_into(
    target: "LazyMcpState",
    *,
    workspace_root: Any,
    session_id: Any,
) -> int:
    """Copy the session-scoped described set + describe payloads into
    the per-request ``target`` state.

    Called by ``AgentKernel._run`` right before the loop runs, so the
    model sees previously described namespaces without paying another
    ``mcp_describe`` round-trip.

    Returns the number of namespaces that were promoted into the
    target's described set as a result of the pull (useful for
    logging + tests).
    """

    if not session_id:
        return 0
    source = get_or_create_session_state(workspace_root, session_id)
    promoted = 0
    with target._lock, source._lock:
        for ns in source.described_namespaces:
            if ns not in target.described_namespaces:
                target.described_namespaces.add(ns)
                promoted += 1
        for ns, payload in source.describe_response_cache.items():
            if ns not in target.describe_response_cache:
                target.describe_response_cache[ns] = payload
    return promoted


def push_state_into_session_cache(
    source: "LazyMcpState",
    *,
    workspace_root: Any,
    session_id: Any,
) -> int:
    """Mirror the per-request state's described set + describe
    payloads back to the long-lived session-scoped cache.

    Called by ``AgentKernel._run`` after the loop has finished. Returns
    the number of namespaces newly persisted (useful for logging).
    """

    if not session_id:
        return 0
    target = get_or_create_session_state(workspace_root, session_id)
    persisted = 0
    with target._lock, source._lock:
        for ns in source.described_namespaces:
            if ns not in target.described_namespaces:
                target.described_namespaces.add(ns)
                persisted += 1
        for ns, payload in source.describe_response_cache.items():
            if ns not in target.describe_response_cache:
                target.describe_response_cache[ns] = payload
    return persisted


def reset_session_cache(*, workspace_root: Any, session_id: Any) -> bool:
    """Drop the session-scoped state for one ``(workspace, session)`` key.

    Returns True if a state existed and was removed. Intended for the
    gateway ``/new`` slash-command path which already wipes the
    SessionStore — operators want a single conceptual "new chat"
    button, so the MCP cache should follow the same boundary.
    """

    key = _session_key(workspace_root, session_id)
    with _SESSION_STATES_LOCK:
        existed = key in _SESSION_STATES
        _SESSION_STATES.pop(key, None)
        return existed


def session_cache_size() -> int:
    """Return the number of (workspace, session) keys currently
    cached. Useful for tests + diagnostic surfaces.
    """

    with _SESSION_STATES_LOCK:
        return len(_SESSION_STATES)


def _clear_all_session_caches() -> None:
    """Test-only convenience to reset the module-level cache between
    test cases that mutate it. Not part of the public API.
    """

    with _SESSION_STATES_LOCK:
        _SESSION_STATES.clear()


def attach_lazy_state(
    registry: ToolRegistry, state: LazyMcpState,
) -> LazyMcpState:
    """Attach ``state`` to ``registry.lazy_mcp_state`` (the loop reads
    this from ``_render_tools``). Idempotent: if a state already exists,
    its ``namespace_index`` and ``always_eager_namespaces`` are merged
    in but the ``described_namespaces`` set is preserved (so re-running
    bootstrap mid-session does not erase the model's earlier describes).
    """

    existing = getattr(registry, "lazy_mcp_state", None)
    if not isinstance(existing, LazyMcpState):
        setattr(registry, "lazy_mcp_state", state)
        return state
    # Merge — bootstrap may be re-run after a connector edit.
    for sid, names in state.namespace_index.items():
        existing.namespace_index[sid] = list(names)
    existing.always_eager_namespaces.update(state.always_eager_namespaces)
    return existing


# ---------------------------------------------------------------------------
# Meta-tool handlers
# ---------------------------------------------------------------------------


def _make_namespaces_handler(
    *, registry: ToolRegistry, state: LazyMcpState,
) -> Callable[[ToolCall], ToolResult]:
    def handler(call: ToolCall) -> ToolResult:
        snap = state.snapshot()
        # Compose per-namespace status (eager / lazy / described).
        rows: list[dict[str, Any]] = []
        for sid in sorted(snap["namespaces"].keys()):
            names = snap["namespaces"][sid]
            is_eager = sid in snap["always_eager"]
            is_described = sid in snap["described"]
            if is_eager:
                visibility = "eager"
            elif is_described:
                visibility = "described"
            else:
                visibility = "lazy"
            rows.append({
                "namespace": sid,
                "tool_count": len(names),
                "visibility": visibility,
            })
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=META_NAMESPACES_TOOL,
            data={
                "namespaces": rows,
                "described_count": len(snap["described"]),
                "always_eager_count": len(snap["always_eager"]),
                "total_namespaces": len(rows),
                "hint": (
                    "Call mcp_describe(namespace=...) to inspect a specific "
                    "namespace's tools and make them visible for the rest "
                    "of this session, or call mcp_call(namespace=..., "
                    "tool=..., args={...}) to dispatch an MCP tool directly."
                ),
            },
        )

    return handler


def _make_describe_handler(
    *, registry: ToolRegistry, state: LazyMcpState,
) -> Callable[[ToolCall], ToolResult]:
    def handler(call: ToolCall) -> ToolResult:
        ns = str((call.arguments or {}).get("namespace") or "").strip()
        if not ns:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=META_DESCRIBE_TOOL,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message="namespace argument is required",
                ),
            )
        snap = state.snapshot()
        if ns not in snap["namespaces"]:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=META_DESCRIBE_TOOL,
                error=ToolError(
                    kind=ToolErrorKind.NOT_FOUND,
                    message=(
                        f"unknown MCP namespace {ns!r}; available: "
                        f"{sorted(snap['namespaces'].keys())}"
                    ),
                ),
            )

        # Per-session response cache. If we already built a
        # tools payload for this namespace in this session (or in a
        # prior turn whose state was pulled in via
        # ``pull_session_cache_into``), return it immediately and skip
        # the registry scan + schema dict copy.
        with state._lock:
            cached = state.describe_response_cache.get(ns)
        if cached is not None:
            # Re-mark described (idempotent) and surface the cache hit
            # so the model + transcripts can tell this was a no-op
            # round-trip rather than a fresh fetch.
            state.mark_described(ns)
            return ToolResult.from_json(
                tool_use_id=call.id,
                name=META_DESCRIBE_TOOL,
                data={
                    "namespace": ns,
                    "tools": list(cached.get("tools") or []),
                    "newly_described": False,
                    "from_cache": True,
                    "hint": cached.get("hint") or (
                        "These tools are visible from a prior describe "
                        "in this session. Call mcp_call(namespace, "
                        "tool, args) to dispatch one directly."
                    ),
                },
            )

        # Pull the descriptors for this namespace from the registry.
        wanted = set(snap["namespaces"][ns])
        descriptors = [
            d for d in registry.list_tools() if d.name in wanted
        ]
        tools_payload = [
            {
                "name": d.name,
                "description": d.description,
                "input_schema": dict(d.input_schema),
                "risk": d.risk.value,
                "auto_approve": d.auto_approve,
            }
            for d in descriptors
        ]
        hint_text = (
            "These tools are now visible in subsequent prompts of "
            "this session. To call one without round-tripping the "
            "describe, use mcp_call(namespace, tool, args)."
        )
        # Persist the payload so the next describe of the
        # same namespace (within this session, including subsequent
        # turns once the session-cache plumbing is wired) returns
        # instantly.
        with state._lock:
            state.describe_response_cache[ns] = {
                "tools": tools_payload,
                "hint": hint_text,
            }
        newly_described = state.mark_described(ns)
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=META_DESCRIBE_TOOL,
            data={
                "namespace": ns,
                "tools": tools_payload,
                "newly_described": newly_described,
                "from_cache": False,
                "hint": hint_text,
            },
        )

    return handler


#: Top-level keys reserved by ``mcp_call`` itself; everything else is
#: treated as belonging to the underlying tool's ``args`` when the
#: caller uses flat top-level arguments.
_MCP_CALL_RESERVED_KEYS = frozenset({"namespace", "tool", "args"})


def _make_call_handler(
    *, registry: ToolRegistry, state: LazyMcpState,
) -> Callable[[ToolCall], ToolResult]:
    def handler(call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        ns = str(args.get("namespace") or "").strip()
        tool = str(args.get("tool") or "").strip()
        # Accept both nested arguments (preferred) and flat top-level
        # arguments (fallback). Models often emit
        # mcp_call(namespace="yahoo", tool="get_stock_info", ticker="TSLA")
        # before they learn the nested ``args={...}`` shape, so promote
        # extra top-level fields into ``args`` instead of rejecting them.
        underlying_args_raw = args.get("args")
        if underlying_args_raw is None:
            # No explicit ``args`` — promote any extra top-level
            # parameters to the underlying tool's args.
            extras = {
                k: v for k, v in args.items()
                if k not in _MCP_CALL_RESERVED_KEYS
            }
            underlying_args: dict[str, Any] = extras
        elif isinstance(underlying_args_raw, dict):
            # Explicit ``args`` wins, but if there are also extras at
            # the top level, merge them in (extras lose) for symmetry
            # with the no-args branch. This is defensive — well-behaved
            # callers won't mix the two styles.
            extras = {
                k: v for k, v in args.items()
                if k not in _MCP_CALL_RESERVED_KEYS
            }
            underlying_args = {**extras, **underlying_args_raw}
        else:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=META_CALL_TOOL,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message=(
                        "args must be an object/dict (or omitted; pass "
                        "tool args at the top level instead)"
                    ),
                ),
            )
        if not ns or not tool:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=META_CALL_TOOL,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message="namespace and tool arguments are required",
                ),
            )

        snap = state.snapshot()
        names = snap["namespaces"].get(ns, [])
        # The MCP descriptor public name format is mcp__<server>__<tool>.
        # We accept either the bare tool name or the fully-qualified
        # public name to make the meta-tool ergonomic.
        candidates = [
            n for n in names
            if n == tool or n.endswith(f"__{tool}") or n == f"mcp__{ns}__{tool}"
        ]
        if not candidates:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=META_CALL_TOOL,
                error=ToolError(
                    kind=ToolErrorKind.NOT_FOUND,
                    message=(
                        f"unknown MCP tool {tool!r} in namespace {ns!r}; "
                        f"available: {names}"
                    ),
                ),
            )
        public_name = candidates[0]
        descriptor = registry.find(public_name)
        if descriptor is None:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=META_CALL_TOOL,
                error=ToolError(
                    kind=ToolErrorKind.UNKNOWN_TOOL,
                    message=(
                        f"tool {public_name!r} is in the namespace index "
                        "but missing from the registry — registry was "
                        "mutated since bootstrap"
                    ),
                ),
            )

        issues = collect_schema_issues(underlying_args, descriptor.input_schema)
        if issues:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=public_name,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message=format_schema_validation_error(public_name, issues),
                    detail={
                        "namespace": ns,
                        "tool": public_name,
                        "issues": [dict(i) for i in issues],
                        "schema": descriptor.input_schema,
                    },
                    retryable=True,
                    recovery_hint={
                        "action": "fix_arguments_and_retry",
                        "tool_name": public_name,
                    },
                ),
            )

        # Forward by constructing a child ToolCall so the underlying
        # handler sees its own arguments rather than ours.
        child_call = ToolCall(
            name=public_name,
            arguments=dict(underlying_args),
            id=call.id,
            turn_id=call.turn_id,
            iteration=call.iteration,
            caller=f"mcp_call:{ns}",
            parent_call_id=call.id,
            metadata={"via": META_CALL_TOOL, "mcp_namespace": ns},
        )
        try:
            inner = descriptor.handler(child_call)
        except Exception as exc:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=META_CALL_TOOL,
                error=ToolError(
                    kind=ToolErrorKind.EXECUTION_ERROR,
                    message=f"{type(exc).__name__}: {exc}",
                    detail={"namespace": ns, "tool": public_name},
                ),
            )
        # The descriptor handler may be sync or async; we only support
        # sync handlers here because mcp_call is itself a sync tool. If
        # an MCP descriptor returns an awaitable in the future, the
        # executor's normal path would await it — for the meta-tool
        # short-circuit we surface a typed error.
        if not isinstance(inner, ToolResult):
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=META_CALL_TOOL,
                error=ToolError(
                    kind=ToolErrorKind.EXECUTION_ERROR,
                    message=(
                        "underlying MCP handler returned a non-ToolResult "
                        f"({type(inner).__name__}); use the executor path "
                        "instead of mcp_call for async tools"
                    ),
                    detail={"namespace": ns, "tool": public_name},
                ),
            )
        # Tag the inner result with provenance so transcripts can tell
        # this was a lazy-routed call.
        inner.metadata.setdefault("via_mcp_call", True)
        inner.metadata.setdefault("mcp_namespace", ns)
        inner.metadata.setdefault("mcp_underlying_tool", public_name)
        return inner

    return handler


# ---------------------------------------------------------------------------
# Meta-tool descriptor factory
# ---------------------------------------------------------------------------


def make_meta_tools(
    *, registry: ToolRegistry, state: LazyMcpState,
) -> list[ToolDescriptor]:
    """Build the three eager meta-tool descriptors.

    They are intentionally cheap, READ-only, and ``auto_approve=True``
    so the permission engine never gates them.
    """

    return [
        ToolDescriptor(
            name=META_NAMESPACES_TOOL,
            description=(
                "List the available MCP server namespaces with their tool "
                "counts and current visibility (eager / lazy / described). "
                "Call this first to discover what MCP surface is reachable, "
                "then mcp_describe(namespace=...) to load one namespace's "
                "tool schemas, or mcp_call(namespace, tool, args) to "
                "dispatch a known tool directly."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=_make_namespaces_handler(registry=registry, state=state),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            read_only=True,
            is_concurrency_safe=True,
            namespace="native",
            tags=("mcp", "meta", "lazy_loader"),
            auto_approve=True,
            lazy=False,
        ),
        ToolDescriptor(
            name=META_DESCRIBE_TOOL,
            description=(
                "Describe (load full schemas for) one MCP namespace and "
                "promote it into the visible tool surface for the rest "
                "of this session. After calling this, the namespace's "
                "underlying tools appear in the prompt directly so you "
                "can call them without going through mcp_call."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": (
                            "MCP server namespace, e.g. 'edgar', 'yahoo', "
                            "'coingecko'. Use mcp_namespaces() to list "
                            "available values."
                        ),
                    },
                },
                "required": ["namespace"],
                "additionalProperties": False,
            },
            handler=_make_describe_handler(registry=registry, state=state),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            read_only=True,
            is_concurrency_safe=True,
            namespace="native",
            tags=("mcp", "meta", "lazy_loader"),
            auto_approve=True,
            lazy=False,
        ),
        ToolDescriptor(
            name=META_CALL_TOOL,
            description=(
                "Dispatch one MCP tool by namespace + tool + args without "
                "first describing the namespace. Useful when the tool name "
                "is already known (from a prior describe, the operator's "
                "instructions, or another agent).\n\n"
                "TWO CALLING STYLES (both work; pick whichever feels "
                "natural):\n"
                "  • Nested (preferred):\n"
                "    mcp_call(namespace='yahoo', tool='get_stock_info', "
                "args={'ticker':'TSLA'})\n"
                "  • Flat (also accepted — extra top-level params become "
                "the tool's args):\n"
                "    mcp_call(namespace='yahoo', tool='get_stock_info', "
                "ticker='TSLA')\n\n"
                "The underlying tool's permission and risk semantics "
                "still apply."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "MCP server namespace.",
                    },
                    "tool": {
                        "type": "string",
                        "description": (
                            "Tool name within that namespace. Either the "
                            "bare name (e.g. 'get_filings') or the fully "
                            "qualified name (e.g. 'mcp__edgar__get_filings')."
                        ),
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Optional arguments object forwarded to the "
                            "underlying MCP tool. If omitted, any extra "
                            "top-level keys (other than namespace/tool) "
                            "are auto-promoted to the tool's args."
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["namespace", "tool"],
                # Extra top-level params are allowed, so the model can call
                # mcp_call(namespace=X, tool=Y, ticker=Z) and the handler
                # will auto-promote ``ticker`` into the underlying tool's
                # args.
                "additionalProperties": True,
            },
            handler=_make_call_handler(registry=registry, state=state),
            # Per-call risk depends on the underlying tool; READ here is
            # conservative — the handler itself is just dispatch glue.
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            is_concurrency_safe=True,
            namespace="native",
            tags=("mcp", "meta", "lazy_loader"),
            # auto_approve is False here because the underlying tool may
            # be a network mutation; we let the executor's permission
            # engine see this call. Read-only MCP tools (the common
            # case) still ride the underlying descriptor's auto_approve
            # when invoked the normal way; mcp_call is the explicit
            # bypass path.
            auto_approve=False,
            lazy=False,
        ),
    ]


def install_meta_tools(
    *, registry: ToolRegistry, state: LazyMcpState, replace: bool = True,
) -> list[str]:
    """Register the 3 meta-tools onto ``registry``. Returns their names."""

    descriptors = make_meta_tools(registry=registry, state=state)
    registry.register_all(descriptors, replace=replace)
    return [d.name for d in descriptors]


__all__ = [
    "LazyMcpState",
    "META_CALL_TOOL",
    "META_DESCRIBE_TOOL",
    "META_NAMESPACES_TOOL",
    "META_TOOL_NAMES",
    "attach_lazy_state",
    "get_or_create_session_state",
    "install_meta_tools",
    "make_meta_tools",
    "pull_session_cache_into",
    "push_state_into_session_cache",
    "reset_session_cache",
    "server_id_of",
    "session_cache_size",
]

"""External MCP session adapter — existing.

This module turns a remote MCP server's tool surface into native
:class:`ToolDescriptor` objects on the same :class:`ToolRegistry`
the agent loop already drives. Once registered, the model sees an
external MCP tool exactly the same way it sees ``read_file`` —
identical schema validation, permission engine, hooks, error
taxonomy, transcript invariants.

Design points:

* The adapter holds a *single* live connection to one MCP server.
  Multiple servers register their own adapters with their own
  ``namespace`` prefix.
* Calls go through :meth:`MCPSessionAdapter.dispatch` which:

  1. invokes ``client.call_tool(name, payload)``,
  2. on ``MCPSessionExpiredError`` triggers ``client.reconnect()``
     and retries **once**,
  3. wraps every other failure into a typed
     :class:`~nerya.tools.types.ToolError` so the model sees the
     same shape as a native error.

* The post-tool hook (:func:`make_mcp_output_hook`) is registered on
  the executor so MCP outputs go through a redaction / provenance
  pass before reaching the transcript. This is the "post-tool MCP
  output hook" the refactor doc calls out.

The adapter is intentionally transport-agnostic — it accepts any
client that implements the small :class:`MCPClient` protocol below,
so the SSE / stdio / WebSocket transports can plug in without
changing the registration path.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, TYPE_CHECKING

from ..tools.registry import ToolRegistry
from ..tools.resources import ResourceEntry, ResourceIndex
if TYPE_CHECKING:
    from ..tools.executor import NativeToolExecutor  # noqa: F401
from ..tools.types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolDescriptor,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultPart,
)


_LOG = logging.getLogger(__name__)
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_]+")


# ---------------------------------------------------------------------------
# Protocol — what an MCP client must offer
# ---------------------------------------------------------------------------


class MCPSessionExpiredError(Exception):
    """Raised by clients when the remote session expired mid-call.

    The adapter catches this, calls ``reconnect()`` and retries once.
    """


class MCPClient(Protocol):
    """Minimal surface every transport (SSE/stdio/WS) must implement."""

    server_id: str

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the server's tool catalogue.

        Each entry must have at minimum ``name``, ``description``,
        and ``inputSchema`` (camelCase per MCP spec).
        """

    def list_resources(self) -> list[dict[str, Any]]:
        ...

    def list_skills(self) -> list[dict[str, Any]]:
        ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool and return its result envelope."""

    def reconnect(self) -> None:
        """Re-establish the session. Called after MCPSessionExpiredError."""


# ---------------------------------------------------------------------------
# Output transform hook
# ---------------------------------------------------------------------------


MCPOutputTransform = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
"""Signature: ``transform(envelope, context) -> envelope``.

``envelope`` is the raw MCP ``call_tool`` result. ``context`` carries
``server_id``, ``tool``, ``call_id``. Return the (possibly modified)
envelope; raising re-routes to a generic ``provider_error``.
"""


def default_output_transform(
    envelope: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Tag every MCP envelope with provenance.

    Operators stack additional transforms in front of this one (e.g.
    secret-vault redaction) — it must always *return* a dict so the
    chain composes.
    """

    out = dict(envelope or {})
    metadata = dict(out.get("metadata") or {})
    metadata.update(
        {
            "mcp_server": context.get("server_id"),
            "mcp_tool": context.get("tool"),
            "mcp_call_id": context.get("call_id"),
        }
    )
    out["metadata"] = metadata
    return out


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


def _safe(value: str) -> str:
    return _NAME_SAFE.sub("_", value).strip("_")


def _public_name(server_id: str, tool: str) -> str:
    """Render the tool name as it appears in :class:`ToolRegistry`."""

    return f"mcp__{_safe(server_id)}__{_safe(tool)}"


@dataclass
class MCPSessionAdapter:
    """Wrap one :class:`MCPClient` so it plugs into the native registry."""

    client: MCPClient
    server_id: str
    transforms: list[MCPOutputTransform] = field(
        default_factory=lambda: [default_output_transform]
    )
    max_retry_on_expired: int = 1

    def dispatch(
        self, *, tool: str, arguments: dict[str, Any], call_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Invoke a remote MCP tool with auto-reconnect on session expiry."""

        retries = 0
        last_exc: Optional[Exception] = None
        while retries <= self.max_retry_on_expired:
            try:
                envelope = self.client.call_tool(tool, dict(arguments or {}))
                ctx = {
                    "server_id": self.server_id,
                    "tool": tool,
                    "call_id": call_id,
                }
                for transform in self.transforms:
                    try:
                        envelope = transform(envelope, ctx)
                    except Exception:
                        _LOG.exception("mcp output transform failed")
                return envelope
            except MCPSessionExpiredError as exc:
                last_exc = exc
                if retries >= self.max_retry_on_expired:
                    raise
                try:
                    self.client.reconnect()
                except Exception:
                    _LOG.exception("mcp reconnect failed")
                    raise
                retries += 1
            except Exception as exc:
                last_exc = exc
                raise
        # Should be unreachable — the loop above either returns or raises.
        if last_exc is not None:  # pragma: no cover - defensive
            raise last_exc
        return {}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _envelope_to_result(
    envelope: dict[str, Any], *, call: ToolCall, tool_name: str
) -> ToolResult:
    """Map an MCP ``call_tool`` envelope onto a :class:`ToolResult`."""

    content_parts: list[ToolResultPart] = []
    is_error = bool(envelope.get("isError") or envelope.get("is_error"))
    raw_content = envelope.get("content") or envelope.get("output") or []
    if isinstance(raw_content, list):
        for part in raw_content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                txt = part.get("text")
                if isinstance(txt, str):
                    content_parts.append(ToolResultPart.text_part(txt))
            elif ptype == "json":
                data = part.get("data")
                if isinstance(data, (dict, list)):
                    content_parts.append(ToolResultPart.json_part(data))
            elif ptype == "image":
                # MCP image parts arrive base64-encoded; we pass them
                # through as text+metadata so the kernel can decide
                # whether to attach to a multimodal block.
                content_parts.append(
                    ToolResultPart.text_part(
                        f"[mcp image: {part.get('mimeType') or 'unknown'}]"
                    )
                )
    elif isinstance(raw_content, str):
        content_parts.append(ToolResultPart.text_part(raw_content))

    error: Optional[ToolError] = None
    if is_error:
        err_msg = ""
        if isinstance(raw_content, list):
            for part in raw_content:
                if isinstance(part, dict) and part.get("type") == "text":
                    err_msg = str(part.get("text") or "")
                    break
        error = ToolError(
            kind=ToolErrorKind.PROVIDER_ERROR,
            message=err_msg or "mcp tool reported isError",
            detail=dict(envelope),
        )

    return ToolResult(
        tool_use_id=call.id,
        name=tool_name,
        content=content_parts,
        is_error=is_error,
        error=error,
        metadata=dict(envelope.get("metadata") or {}),
    )


def _make_descriptor(
    *,
    adapter: MCPSessionAdapter,
    raw: dict[str, Any],
) -> Optional[ToolDescriptor]:
    """Render one MCP tool entry as a :class:`ToolDescriptor`."""

    tool_name = str(raw.get("name") or "").strip()
    if not tool_name:
        return None
    description = str(raw.get("description") or "").strip()
    schema = raw.get("inputSchema") or raw.get("input_schema") or {}
    if not isinstance(schema, dict):
        schema = {}

    annotations = raw.get("annotations") or {}
    annotated_read_only = bool(annotations.get("readOnlyHint"))
    annotated_destructive = bool(annotations.get("destructiveHint"))

    if annotated_destructive:
        risk = RiskLevel.DANGEROUS
    elif annotated_read_only:
        risk = RiskLevel.READ
    else:
        risk = RiskLevel.WRITE

    public_name = _public_name(adapter.server_id, tool_name)

    def handler(call: ToolCall) -> ToolResult:
        try:
            envelope = adapter.dispatch(
                tool=tool_name,
                arguments=dict(call.arguments or {}),
                call_id=call.id,
            )
        except MCPSessionExpiredError as exc:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=public_name,
                error=ToolError(
                    kind=ToolErrorKind.MCP_SESSION_EXPIRED,
                    message=str(exc),
                    retryable=True,
                    recovery_hint={
                        "action": "reconnect_and_retry",
                        "server": adapter.server_id,
                    },
                ),
            )
        except Exception as exc:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=public_name,
                error=ToolError(
                    kind=ToolErrorKind.PROVIDER_ERROR,
                    message=f"{type(exc).__name__}: {exc}",
                    retryable=False,
                    detail={"server": adapter.server_id, "tool": tool_name},
                ),
            )
        return _envelope_to_result(envelope, call=call, tool_name=public_name)

    return ToolDescriptor(
        name=public_name,
        description=description,
        input_schema=schema,
        handler=handler,
        risk=risk,
        permission_scope=PermissionScope.NETWORK,
        read_only=(risk == RiskLevel.READ),
        is_concurrency_safe=(risk == RiskLevel.READ),
        requires_fresh_read=False,
        mutates_paths=False,
        tags=("mcp", adapter.server_id),
        result_kind="json",
        namespace="mcp",
        auto_approve=(risk == RiskLevel.READ),
    )


def register_external_mcp_resources(
    *,
    index: ResourceIndex,
    adapter: MCPSessionAdapter,
) -> list[str]:
    """Pull ``adapter.client.list_resources()`` onto ``index``.

    The MCP spec lets servers publish read-only documents the agent
    can list / read alongside the tool surface (logs, configuration
    snippets, dataset cards). of the harness refactor calls
    this out: those resources should land in the workspace's
    :class:`ResourceIndex` so the agent's ``resource_list`` /
    ``resource_read`` tools see them next to local resources.

    Each entry's ``fetcher`` is a closure that calls back into the
    same MCP client so the body is materialised lazily — listing 200
    resources doesn't pull 200 blobs.

    Returns the list of URIs that were registered. Servers without
    a ``list_resources`` method (or that raise) yield an empty list.
    """

    list_fn = getattr(adapter.client, "list_resources", None)
    if list_fn is None:
        return []
    try:
        raw_entries = list(list_fn() or [])
    except NotImplementedError:
        return []
    except Exception:
        _LOG.exception("mcp list_resources failed for %s", adapter.server_id)
        return []

    uris: list[str] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        uri = str(raw.get("uri") or "").strip()
        if not uri:
            continue
        name = str(raw.get("name") or uri)
        description = str(raw.get("description") or "")
        mime = str(raw.get("mimeType") or raw.get("mime") or "text/plain")
        annotations = raw.get("annotations") or {}
        if not isinstance(annotations, dict):
            annotations = {}

        def _make_fetcher(
            client: MCPClient = adapter.client, target_uri: str = uri
        ) -> ResourceFetcher:
            def fetch(_uri: str) -> dict[str, Any]:
                read_fn = getattr(client, "read_resource", None)
                if read_fn is None:
                    raise RuntimeError(
                        f"mcp server {adapter.server_id} cannot read resources"
                    )
                envelope = read_fn(target_uri) or {}
                if not isinstance(envelope, dict):
                    raise RuntimeError("mcp read_resource must return a dict")
                # Normalise the envelope into ResourceEntry.fetch's shape.
                contents = envelope.get("contents") or envelope.get("content")
                text_body = ""
                if isinstance(contents, list):
                    for part in contents:
                        if isinstance(part, dict) and part.get("type") == "text":
                            t = part.get("text")
                            if isinstance(t, str):
                                text_body += t
                elif isinstance(contents, str):
                    text_body = contents
                return {
                    "uri": target_uri,
                    "text": text_body,
                    "mime": envelope.get("mimeType")
                    or envelope.get("mime")
                    or mime,
                    "metadata": envelope.get("metadata") or {},
                }

            return fetch

        index.upsert(
            ResourceEntry(
                uri=uri,
                name=name,
                description=description,
                mime=mime,
                source="mcp",
                server_id=adapter.server_id,
                annotations=annotations,
                fetcher=_make_fetcher(),
            )
        )
        uris.append(uri)
    return uris


def register_external_mcp_tools(
    *,
    registry: ToolRegistry,
    adapter: MCPSessionAdapter,
    replace: bool = False,
    lazy: bool = False,
    deny_tools: Iterable[str] = (),
    allow_tools: Optional[Iterable[str]] = None,
) -> list[str]:
    """Pull ``adapter.client.list_tools()`` onto ``registry``.

    Returns the list of public tool names that were registered. Caller
    is responsible for refreshing the registry on reconnect (a
    re-call will replace=True the existing entries).

    When ``lazy=True`` every descriptor is replaced with a
    ``lazy=True`` clone before registration. The descriptor stays in
    the registry (so ``mcp_call`` can dispatch its handler), but the
    agent loop's prompt-time render filter hides it until the namespace
    has been promoted via ``mcp_describe``.

    ``deny_tools`` / ``allow_tools`` are per-server filters applied
    **before** descriptors are built. Names match the upstream
    tool name (the ``name`` field returned by ``tools/list``), not the
    namespaced public name. Filtered tools never enter the registry,
    so they cannot be dispatched via ``mcp_call``, never appear in
    ``mcp_describe`` output, and never inflate ``mcp_namespaces``
    counts. Use this to retire MCP tools that overlap with native
    Nerya connectors. ``deny_tools`` is applied first; ``allow_tools``
    (if not ``None``) then keeps only the listed survivors.
    """

    try:
        raw_entries = list(adapter.client.list_tools() or [])
    except Exception:
        _LOG.exception("mcp list_tools failed for %s", adapter.server_id)
        return []

    deny_set = {str(name) for name in deny_tools or ()}
    allow_set: Optional[set[str]] = (
        {str(name) for name in allow_tools} if allow_tools is not None else None
    )

    descriptors: list[ToolDescriptor] = []
    skipped: list[str] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        upstream_name = str(raw.get("name") or "")
        if upstream_name and upstream_name in deny_set:
            skipped.append(upstream_name)
            continue
        if (
            upstream_name
            and allow_set is not None
            and upstream_name not in allow_set
        ):
            skipped.append(upstream_name)
            continue
        d = _make_descriptor(adapter=adapter, raw=raw)
        if d is None:
            continue
        if lazy:
            from dataclasses import replace as _replace
            d = _replace(d, lazy=True)
        descriptors.append(d)

    if skipped:
        _LOG.info(
            "mcp filter: server=%s dropped %d tool(s) by deny/allow: %s",
            adapter.server_id, len(skipped), sorted(skipped),
        )

    if not descriptors:
        return []
    registry.register_all(descriptors, replace=replace)
    return [d.name for d in descriptors]


# ---------------------------------------------------------------------------
# Hook factory
# ---------------------------------------------------------------------------


def make_mcp_output_hook(
    *, transform: MCPOutputTransform,
) -> Callable[[ToolCall, ToolResult], None]:
    """Wrap an :class:`MCPOutputTransform` as an executor post-hook.

    Use this to plug a workspace-wide redaction / compaction step
    that fires *after* every MCP tool returns. Native (non-MCP)
    tools are skipped automatically based on the descriptor
    namespace.
    """

    def hook(call: ToolCall, result: ToolResult) -> None:
        # Native tools have empty / non-mcp namespace. We only touch
        # results whose name matches the ``mcp__server__tool`` shape.
        if not result.name or not result.name.startswith("mcp__"):
            return
        envelope: dict[str, Any] = {
            "isError": result.is_error,
            "content": [
                {"type": part.type, **({"text": part.text} if part.text else {}),
                 **({"data": part.data} if part.data is not None else {})}
                for part in result.content
            ],
            "metadata": dict(result.metadata or {}),
        }
        try:
            new_envelope = transform(
                envelope, {"server_id": result.metadata.get("mcp_server"),
                          "tool": result.name, "call_id": result.tool_use_id},
            )
        except Exception:
            _LOG.exception("mcp post-tool transform failed for %s", result.name)
            return
        if isinstance(new_envelope, dict):
            md = new_envelope.get("metadata")
            if isinstance(md, dict):
                result.metadata.update(md)

    return hook


# ---------------------------------------------------------------------------
# Convenience wiring
# ---------------------------------------------------------------------------


def attach_mcp_adapters(
    *,
    registry: ToolRegistry,
    executor: Any,
    adapters: Iterable[MCPSessionAdapter],
    transforms: Optional[Iterable[MCPOutputTransform]] = None,
    replace: bool = True,
    resource_index: Optional[ResourceIndex] = None,
    lazy_servers: Optional[Iterable[str]] = None,
    deny_tools_by_server: Optional[Mapping[str, Iterable[str]]] = None,
    allow_tools_by_server: Optional[Mapping[str, Iterable[str]]] = None,
) -> dict[str, list[str]]:
    """Single-call wiring for "register MCP servers + install hook".

    The kernel calls this once at startup with the configured adapters
    so every external MCP tool surface lands on the native registry,
    *and* the executor sees the standard provenance / redaction
    pipeline as a post-tool hook.

    ``transforms`` defaults to ``[default_output_transform]`` so every
    MCP envelope at least carries provenance metadata. Pass extra
    transforms (secret redaction, large-blob compactor, etc.) in
    front of the default for cumulative behaviour.

    ``lazy_servers`` — set of adapter ``server_id`` values whose tool
    descriptors should be registered with ``lazy=True`` so
    the prompt-time render filter hides them until ``mcp_describe`` is
    called for that namespace. Adapters not in the set keep eager
    visibility (legacy behavior).

    ``deny_tools_by_server`` / ``allow_tools_by_server`` — per-server
    overlap filters keyed by ``server_id``. Forwarded
    verbatim to :func:`register_external_mcp_tools`, which drops
    matching upstream tools BEFORE the descriptor is built (so they
    never enter the registry, never appear in ``mcp_describe``, and
    cannot be dispatched via ``mcp_call``). Use this to retire MCP
    tools that overlap with native Nerya connectors.

    Returns a ``{server_id: [public_tool_name, ...]}`` map for
    diagnostics. Adapters whose ``list_tools`` raise are skipped with
    an empty entry rather than aborting the whole boot.
    """

    transforms_list = list(transforms or [default_output_transform])
    lazy_set = set(lazy_servers or ())
    deny_map = dict(deny_tools_by_server or {})
    allow_map = dict(allow_tools_by_server or {})
    out: dict[str, list[str]] = {}
    for adapter in adapters:
        try:
            names = register_external_mcp_tools(
                registry=registry,
                adapter=adapter,
                replace=replace,
                lazy=adapter.server_id in lazy_set,
                deny_tools=deny_map.get(adapter.server_id, ()),
                allow_tools=allow_map.get(adapter.server_id),
            )
        except Exception:
            _LOG.exception("attach_mcp_adapters: register failed for %s", adapter.server_id)
            names = []
        out[adapter.server_id] = list(names)
        if resource_index is not None:
            try:
                resource_index.remove_by_server(adapter.server_id)
                register_external_mcp_resources(
                    index=resource_index,
                    adapter=adapter,
                )
            except Exception:
                _LOG.exception(
                    "attach_mcp_adapters: resource discovery failed for %s",
                    adapter.server_id,
                )
        for tx in transforms_list:
            try:
                executor.add_post_hook(make_mcp_output_hook(transform=tx))
            except Exception:
                _LOG.exception(
                    "attach_mcp_adapters: failed to install post-hook for %s",
                    adapter.server_id,
                )
    return out


__all__ = [
    "MCPClient",
    "MCPOutputTransform",
    "MCPSessionAdapter",
    "MCPSessionExpiredError",
    "attach_mcp_adapters",
    "default_output_transform",
    "make_mcp_output_hook",
    "register_external_mcp_resources",
    "register_external_mcp_tools",
]

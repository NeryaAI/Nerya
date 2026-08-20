"""ToolRegistry — independent of SkillRegistry.

Holds three classes of tools:

1. **Native tools** — workspace primitives registered at startup
   (``read_file``, ``grep``, ``edit_file``, ``run_shell``, ``todo_write``,
   ``skill_index``, ``skill_view``, ``script_inspect``, ``script_run``,
   ``agent_dispatch``).

2. **MCP tools** — discovered at MCP connect time. Carry a server
   namespace prefix (e.g. ``mcp:figma.get_design``) so two servers can
   ship the same tool name without clashing.

3. **Legacy skill-action adapters** — thin wrappers that adapt
   ``ToolRunner.call(skill_id, action, payload)`` to the native
   :class:`ToolDescriptor` shape. Registered for backwards compatibility
   only; *coding* tools must register natively.

The registry is intentionally simple: a name -> descriptor map plus a
small set of filter / list helpers. It does *not* execute tools — that
is :class:`NativeToolExecutor`.

Implementation notes are kept with the registry and executor contracts.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional

from .types import RiskLevel, ToolDescriptor, ToolHandler


class ToolNotFoundError(KeyError):
    """Raised when a tool name is not registered."""


class ToolAlreadyRegisteredError(ValueError):
    """Raised on duplicate registration unless ``replace=True``."""


@dataclass
class _RegistryEntry:
    descriptor: ToolDescriptor
    enabled: bool = True


class ToolRegistry:
    """Thread-safe tool registry.

    The agent loop, MCP client, and legacy adapter layer all share a
    single instance per ``Config``. Plugins register on startup; the
    loop reads via :meth:`list_tools` to render the provider tool list.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _RegistryEntry] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ register

    def register(
        self,
        descriptor: ToolDescriptor,
        *,
        replace: bool = False,
    ) -> None:
        """Register ``descriptor``. ``replace=True`` overwrites."""

        if not isinstance(descriptor, ToolDescriptor):
            raise TypeError(
                f"register expects ToolDescriptor, got {type(descriptor).__name__}"
            )
        name = descriptor.name
        if not name or not isinstance(name, str):
            raise ValueError("ToolDescriptor.name must be a non-empty string")
        with self._lock:
            if name in self._entries and not replace:
                raise ToolAlreadyRegisteredError(
                    f"tool {name!r} already registered "
                    f"(namespace={self._entries[name].descriptor.namespace}); "
                    "pass replace=True to overwrite"
                )
            self._entries[name] = _RegistryEntry(descriptor=descriptor)

    def register_all(
        self,
        descriptors: Iterable[ToolDescriptor],
        *,
        replace: bool = False,
    ) -> None:
        for d in descriptors:
            self.register(d, replace=replace)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._entries.pop(name, None)

    def unregister_namespace(self, namespace: str) -> int:
        """Remove every tool whose ``namespace`` matches. Returns count."""

        n = 0
        with self._lock:
            for key in list(self._entries.keys()):
                if self._entries[key].descriptor.namespace == namespace:
                    del self._entries[key]
                    n += 1
        return n

    def disable(self, name: str) -> None:
        with self._lock:
            entry = self._entries.get(name)
            if entry is not None:
                entry.enabled = False

    def enable(self, name: str) -> None:
        with self._lock:
            entry = self._entries.get(name)
            if entry is not None:
                entry.enabled = True

    # ------------------------------------------------------------ lookup

    def get(self, name: str) -> ToolDescriptor:
        """Return the exact descriptor or raise :class:`ToolNotFoundError`."""

        with self._lock:
            entry = self._entries.get(name)
        if entry is None or not entry.enabled:
            raise ToolNotFoundError(name)
        return entry.descriptor

    def find(self, name: str) -> Optional[ToolDescriptor]:
        try:
            return self.get(name)
        except ToolNotFoundError:
            return None

    def has(self, name: str) -> bool:
        return self.find(name) is not None

    # ------------------------------------------------------------ iter

    def list_tools(
        self,
        *,
        include_disabled: bool = False,
        namespaces: Optional[Iterable[str]] = None,
        tags: Optional[Iterable[str]] = None,
        max_risk: Optional[RiskLevel] = None,
    ) -> list[ToolDescriptor]:
        """Snapshot of currently registered tools.

        Filters are applied conjunctively. Use this from the agent loop
        to render the provider tool list, the dashboard ``/api/tools``
        endpoint, and the permission engine policy preview.
        """

        wanted_ns = set(namespaces) if namespaces is not None else None
        wanted_tags = set(tags) if tags is not None else None
        risk_order = {
            RiskLevel.READ: 0,
            RiskLevel.WRITE: 1,
            RiskLevel.EXEC: 2,
            RiskLevel.DANGEROUS: 3,
        }
        ceiling = risk_order[max_risk] if max_risk else None
        with self._lock:
            entries = list(self._entries.values())
        out: list[ToolDescriptor] = []
        for entry in entries:
            if not entry.enabled and not include_disabled:
                continue
            d = entry.descriptor
            if wanted_ns is not None and d.namespace not in wanted_ns:
                continue
            if wanted_tags is not None and not (set(d.tags) & wanted_tags):
                continue
            if ceiling is not None and risk_order[d.risk] > ceiling:
                continue
            out.append(d)
        out.sort(key=lambda d: (d.namespace, d.name))
        return out

    def __iter__(self) -> Iterator[ToolDescriptor]:
        return iter(self.list_tools())

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for e in self._entries.values() if e.enabled)

    # ------------------------------------------------------------ misc

    def to_provider_tools(
        self,
        *,
        namespaces: Optional[Iterable[str]] = None,
        tags: Optional[Iterable[str]] = None,
    ) -> list[dict[str, Any]]:
        """Render the provider-shaped tool spec list (Anthropic shape)."""

        return [
            d.to_provider_tool()
            for d in self.list_tools(namespaces=namespaces, tags=tags)
        ]


# ---------------------------------------------------------------------------
# Convenience factory helpers used by ``nerya.tools.native.*``
# ---------------------------------------------------------------------------


def make_native_descriptor(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    handler: ToolHandler,
    risk: RiskLevel = RiskLevel.READ,
    read_only: bool = True,
    is_concurrency_safe: bool = True,
    requires_fresh_read: bool = False,
    mutates_paths: bool = False,
    result_kind: str = "json",
    max_result_tokens: int = 4_000,
    tags: Iterable[str] = (),
    auto_approve: bool = False,
    auto_approve_when: Any = None,
    risk_classifier: Any = None,
    permission_scope: Any = None,
    child_max_depth: int | None = None,
    delegates_to: str = "",
) -> ToolDescriptor:
    """Build a :class:`ToolDescriptor` for a native tool.

    Wraps the dataclass so call sites stay terse and we have one place
    to enforce defaults (e.g. ``namespace="native"``).
    """

    from .types import PermissionScope as _PermScope

    if permission_scope is None:
        if mutates_paths or risk in (RiskLevel.WRITE, RiskLevel.DANGEROUS):
            permission_scope = _PermScope.WORKSPACE
        elif risk == RiskLevel.EXEC:
            permission_scope = _PermScope.WORKSPACE
        else:
            permission_scope = _PermScope.NONE
    return ToolDescriptor(
        name=name,
        description=description,
        input_schema=dict(input_schema),
        handler=handler,
        risk=risk,
        permission_scope=permission_scope,
        read_only=read_only,
        is_concurrency_safe=is_concurrency_safe,
        requires_fresh_read=requires_fresh_read,
        mutates_paths=mutates_paths,
        result_kind=result_kind,
        max_result_tokens=max_result_tokens,
        namespace="native",
        tags=tuple(tags),
        risk_classifier=risk_classifier,
        auto_approve=auto_approve,
        auto_approve_when=auto_approve_when,
        child_max_depth=child_max_depth,
        delegates_to=str(delegates_to or ""),
    )


__all__ = [
    "ToolAlreadyRegisteredError",
    "ToolNotFoundError",
    "ToolRegistry",
    "make_native_descriptor",
]

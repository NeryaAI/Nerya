"""ResourceIndex — existing.
MCP servers can publish three orthogonal asset classes:

* **Tools**       — invokable RPCs (already covered by
  :class:`ToolRegistry`)
* **Skills**      — markdown playbooks (covered by
  :class:`SkillIndex`)
* **Resources**   — read-only documents identified by URI
  (``file://``, ``mcp://server/...``, ``http://...``)

Up to now Nerya only had a `ToolRegistry` — incoming MCP resources
had nowhere to land. This module fills that gap. The agent doesn't
*invoke* a resource; it lists / reads them as context. The
ResourceIndex therefore looks like a directory of named blobs:

    >>> idx = ResourceIndex()
    >>> idx.upsert(ResourceEntry(uri="mcp://repo/AGENTS.md", ...))
    >>> idx.list_uris()
    ['mcp://repo/AGENTS.md']
    >>> idx.fetch("mcp://repo/AGENTS.md")
    {"uri": ..., "mime": ..., "text": ...}

Two native tools are wired around it via
:mod:`nerya.tools.native.resources`:

* ``resource_list`` — read-only enumeration
* ``resource_read`` — fetch one resource (proxies to the source MCP
  client when the resource came from one)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


ResourceFetcher = Callable[[str], dict[str, Any]]
"""Called with the resource URI; returns ``{"text": ...,
"mime": ..., "metadata": {...}}`` or raises."""


@dataclass
class ResourceEntry:
    """One resource published by an MCP server (or a local source).

    The fetcher is intentionally lazy — listing resources only walks
    metadata, the body is pulled on demand via :meth:`fetch`.
    """

    uri: str
    name: str = ""
    description: str = ""
    mime: str = "text/plain"
    source: str = "mcp"            # mcp | workspace | local
    server_id: Optional[str] = None
    annotations: dict[str, Any] = field(default_factory=dict)
    fetcher: Optional[ResourceFetcher] = field(default=None, repr=False)

    def asdict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name or self.uri,
            "description": self.description,
            "mime": self.mime,
            "source": self.source,
            "server_id": self.server_id,
            "annotations": dict(self.annotations or {}),
        }

    def fetch(self) -> dict[str, Any]:
        if self.fetcher is None:
            raise RuntimeError(f"resource {self.uri!r} has no fetcher attached")
        body = self.fetcher(self.uri)
        if not isinstance(body, dict):
            raise RuntimeError(
                f"resource fetcher must return a dict, got {type(body).__name__}"
            )
        # Always echo the entry's metadata back so the caller has the
        # full picture in one place.
        out = dict(body)
        out.setdefault("uri", self.uri)
        out.setdefault("mime", self.mime)
        out.setdefault("metadata", {})
        out["metadata"].update(
            {
                "source": self.source,
                "server_id": self.server_id,
                **dict(self.annotations or {}),
            }
        )
        return out


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class ResourceIndex:
    """Workspace-wide resource registry.

    Thread-safe — MCP discoveries can happen on a background thread
    while the agent loop reads the index from the main thread.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, ResourceEntry] = {}

    def upsert(self, entry: ResourceEntry) -> None:
        with self._lock:
            self._entries[entry.uri] = entry

    def upsert_all(self, entries: Iterable[ResourceEntry]) -> None:
        with self._lock:
            for e in entries:
                self._entries[e.uri] = e

    def remove(self, uri: str) -> Optional[ResourceEntry]:
        with self._lock:
            return self._entries.pop(uri, None)

    def remove_by_server(self, server_id: str) -> int:
        with self._lock:
            to_drop = [
                u for u, e in self._entries.items() if e.server_id == server_id
            ]
            for u in to_drop:
                self._entries.pop(u, None)
            return len(to_drop)

    def get(self, uri: str) -> Optional[ResourceEntry]:
        with self._lock:
            return self._entries.get(uri)

    def list_uris(self, *, source: Optional[str] = None) -> list[str]:
        with self._lock:
            return [
                u for u, e in self._entries.items()
                if source is None or e.source == source
            ]

    def list_entries(self, *, source: Optional[str] = None) -> list[ResourceEntry]:
        with self._lock:
            return [
                e for e in self._entries.values()
                if source is None or e.source == source
            ]

    def fetch(self, uri: str) -> dict[str, Any]:
        entry = self.get(uri)
        if entry is None:
            raise KeyError(f"unknown resource: {uri}")
        return entry.fetch()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = [
    "ResourceEntry",
    "ResourceFetcher",
    "ResourceIndex",
]

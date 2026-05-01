"""Flexible result envelopes.

Historically every Nerya API returned a plain JSON dict with whatever
fields the caller happened to need. That makes it hard to return:

* a list of rows with a shared column header (``TableEnvelope``);
* a page of results with a continuation cursor (``PagedEnvelope``);
* a partial result while more is still being produced (``PartialEnvelope``);
* a reference to a binary blob on disk the client should stream
  (``BlobRef``).

All envelopes round-trip through plain JSON — they are only
dataclasses for ergonomic construction. Dashboards, MCP clients, or
any other consumer that reads these can detect the envelope kind by
the ``kind`` field on the dict.

Envelopes compose with :class:`nerya.core.truth.RuntimeEnvelope` — when
both are relevant the truth envelope goes under ``_envelope`` and the
result envelope is the top-level shape.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Generic, Iterable, TypeVar


T = TypeVar("T")


# ----------------------------------------------------------------- table
@dataclass
class TableEnvelope(Generic[T]):
    """Uniform row-oriented envelope.

    ``columns`` is the authoritative column order; ``rows`` is a list of
    dicts (one per row). Consumers can render as a table without having
    to introspect every row.
    """
    columns: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)
    kind: str = "table"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_records(cls, records: Iterable[dict[str, Any]],
                     *, columns: list[str] | None = None) -> "TableEnvelope":
        rows = list(records)
        if columns is None:
            cols: list[str] = []
            for r in rows:
                for k in r.keys():
                    if k not in cols:
                        cols.append(k)
            columns = cols
        return cls(columns=columns, rows=rows)


# ----------------------------------------------------------------- paged
@dataclass
class PagedEnvelope:
    """A page of results with an optional continuation cursor.

    ``items`` is the payload slice; ``cursor`` is an opaque string the
    caller hands back to fetch the next page (``None`` when exhausted).
    """
    items: list[Any]
    cursor: str | None = None
    has_more: bool = False
    total: int | None = None
    kind: str = "paged"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------- partial
@dataclass
class PartialEnvelope:
    """A partial / streaming-friendly result chunk.

    ``done`` is ``False`` while more chunks are being produced and
    ``True`` on the final chunk. ``seq`` is a monotonic counter so
    clients can detect re-ordering.
    """
    items: list[Any]
    seq: int = 0
    done: bool = False
    kind: str = "partial"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------- blob ref
@dataclass
class BlobRef:
    """A reference to a binary blob on disk the client should stream.

    The runtime refuses to inline large results; it writes them into the
    workspace ``outbox`` tree and returns a :class:`BlobRef` pointing at
    the relative path. Clients are expected to fetch / stream the blob
    out-of-band.
    """
    path: str            # workspace-relative path
    size_bytes: int = 0
    content_type: str = "application/octet-stream"
    kind: str = "blob_ref"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_file(cls, root: Path, absolute: Path, *,
                  content_type: str = "application/octet-stream",
                  ) -> "BlobRef":
        rel = absolute.resolve().relative_to(root.resolve())
        return cls(
            path=str(rel).replace("\\", "/"),
            size_bytes=absolute.stat().st_size if absolute.exists() else 0,
            content_type=content_type,
        )


# ----------------------------------------------------------------- sniff
def envelope_kind(doc: Any) -> str | None:
    """Return the envelope kind if ``doc`` looks like an envelope, else None."""
    if isinstance(doc, dict):
        kind = doc.get("kind")
        if kind in {"table", "paged", "partial", "blob_ref"}:
            return str(kind)
    return None


__all__ = [
    "TableEnvelope",
    "PagedEnvelope",
    "PartialEnvelope",
    "BlobRef",
    "envelope_kind",
]

"""Durable raw-tool-result store.

When :func:`nerya.llm.tool_compaction.compact_tool_result` swaps a large
tool output for a structured summary, the *original* payload is written
to disk so the operator (or a downstream skill / SDK caller) can still
fetch it via the :func:`read` helper or the
``GET /runtime/tool_raw`` HTTP route.

Layout on disk::

    workspace/state/tool_raw/<yyyy-mm-dd>/<tool_use_id>.json

Each file is a JSON object::

    {
      "tool_use_id": "tu_...",
      "tool_name": "shell",
      "stored_at": "2026-05-13T09:55:00Z",
      "size_bytes": 12345,
      "payload": <original output as JSON, or {"text": "..."} for plain text>
    }

Refs returned by :meth:`RawResultStore.write` use the ``raw://`` scheme::

    raw://<yyyy-mm-dd>/<tool_use_id>

so the rest of the system can route them consistently. Old ``call:<id>``
references emitted before this store existed are still accepted by
:func:`resolve_ref` for backward-compat — they resolve by scanning all
date-segmented subdirectories for a matching filename.

Hygiene: callers may set ``NERYA_TOOL_RAW_TTL_DAYS`` (default 30) to
prune old files; the store does not auto-prune on every write (cheap),
prune is invoked from :func:`prune_expired` when wired into a janitor
job.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


_LOG = logging.getLogger(__name__)

_REF_RE = re.compile(r"^raw://(?P<day>\d{4}-\d{2}-\d{2})/(?P<tu>[A-Za-z0-9_\-]+)$")
_TOOL_USE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _today_segment() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RawRecord:
    tool_use_id: str
    tool_name: str
    stored_at: str
    size_bytes: int
    payload: Any
    ref: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "stored_at": self.stored_at,
            "size_bytes": self.size_bytes,
            "payload": self.payload,
            "ref": self.ref,
        }


@dataclass(frozen=True)
class RawResultStore:
    """Filesystem-backed store for raw tool outputs.

    Construct with the workspace root (``client.config.paths.root``).
    """

    workspace_root: Path

    @property
    def root(self) -> Path:
        return self.workspace_root / "state" / "tool_raw"

    def _day_dir(self, day: str) -> Path:
        return self.root / day

    @staticmethod
    def make_ref(day: str, tool_use_id: str) -> str:
        return f"raw://{day}/{tool_use_id}"

    @staticmethod
    def parse_ref(ref: str) -> Optional[tuple[str, str]]:
        if not ref:
            return None
        m = _REF_RE.match(ref)
        if not m:
            return None
        return m.group("day"), m.group("tu")

    def write(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        payload: Any,
    ) -> str:
        """Persist ``payload`` and return its ``raw://`` ref.

        The payload is serialized as JSON when possible; plain strings
        are wrapped as ``{"text": "..."}`` so the on-disk schema stays
        uniform. Failures fall back to a string repr to guarantee the
        record is written.
        """

        if not tool_use_id or not _TOOL_USE_ID_RE.match(str(tool_use_id)):
            tool_use_id = "anon_" + datetime.now(timezone.utc).strftime("%H%M%S%f")
        day = _today_segment()
        day_dir = self._day_dir(day)
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            _LOG.exception("tool_raw_store: mkdir failed for %s", day_dir)
            return ""

        if isinstance(payload, (str, bytes, bytearray)):
            if isinstance(payload, (bytes, bytearray)):
                try:
                    payload_obj: Any = {"text": payload.decode("utf-8", errors="replace")}
                except Exception:
                    payload_obj = {"text": repr(payload)}
            else:
                payload_obj = {"text": payload}
        elif isinstance(payload, (dict, list, int, float, bool)) or payload is None:
            payload_obj = payload
        else:
            try:
                payload_obj = json.loads(json.dumps(payload, default=str))
            except Exception:
                payload_obj = {"repr": repr(payload)}

        try:
            size = len(json.dumps(payload_obj, default=str).encode("utf-8", errors="ignore"))
        except Exception:
            size = 0

        record: dict[str, Any] = {
            "tool_use_id": tool_use_id,
            "tool_name": tool_name or "",
            "stored_at": _now_iso(),
            "size_bytes": size,
            "payload": payload_obj,
        }
        path = day_dir / f"{tool_use_id}.json"
        try:
            path.write_text(
                json.dumps(record, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception:
            _LOG.exception("tool_raw_store: write failed for %s", path)
            return ""
        return self.make_ref(day, tool_use_id)

    def read(self, ref: str) -> Optional[RawRecord]:
        """Resolve ``ref`` (``raw://...`` or legacy ``call:<id>``) to a record.

        Returns ``None`` when the underlying file is missing or unreadable.
        Legacy refs (``call:<tool_use_id>``) are resolved by scanning
        the date-segmented subdirectories for a matching filename — the
        first match wins.
        """

        parsed = self.parse_ref(ref or "")
        path: Optional[Path] = None
        day: Optional[str] = None
        tool_use_id: Optional[str] = None
        if parsed:
            day, tool_use_id = parsed
            path = self._day_dir(day) / f"{tool_use_id}.json"
        elif ref and ref.startswith("call:"):
            tool_use_id = ref[len("call:") :]
            if not _TOOL_USE_ID_RE.match(tool_use_id or ""):
                return None
            # Search newest-first across day directories.
            if not self.root.exists():
                return None
            try:
                days = sorted(
                    (p for p in self.root.iterdir() if p.is_dir()),
                    reverse=True,
                )
            except Exception:
                days = []
            for d in days:
                candidate = d / f"{tool_use_id}.json"
                if candidate.exists():
                    path = candidate
                    day = d.name
                    break
        else:
            return None

        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return RawRecord(
            tool_use_id=str(data.get("tool_use_id") or tool_use_id or ""),
            tool_name=str(data.get("tool_name") or ""),
            stored_at=str(data.get("stored_at") or ""),
            size_bytes=int(data.get("size_bytes") or 0),
            payload=data.get("payload"),
            ref=self.make_ref(day or _today_segment(), tool_use_id or ""),
        )

    def exists(self, ref: str) -> bool:
        return self.read(ref) is not None

    def list_recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return at most ``limit`` recently-stored records (metadata only)."""

        if not self.root.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            days = sorted(
                (p for p in self.root.iterdir() if p.is_dir()),
                reverse=True,
            )
        except Exception:
            days = []
        for d in days:
            try:
                files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            except Exception:
                files = []
            for f in files:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                out.append({
                    "ref": self.make_ref(d.name, f.stem),
                    "tool_use_id": data.get("tool_use_id") or f.stem,
                    "tool_name": data.get("tool_name") or "",
                    "stored_at": data.get("stored_at") or "",
                    "size_bytes": int(data.get("size_bytes") or 0),
                })
                if len(out) >= limit:
                    return out
        return out

    def prune_expired(self, *, ttl_days: Optional[int] = None) -> int:
        """Delete records older than ``ttl_days`` and return the count.

        ``ttl_days`` defaults to ``NERYA_TOOL_RAW_TTL_DAYS`` (or 30).
        Empty day directories are also removed.
        """

        if ttl_days is None:
            raw_env = os.environ.get("NERYA_TOOL_RAW_TTL_DAYS", "")
            try:
                ttl_days = int(raw_env) if raw_env else 30
            except Exception:
                ttl_days = 30
        if not self.root.exists():
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(ttl_days))
        removed = 0
        for d in list(self.root.iterdir()):
            if not d.is_dir():
                continue
            try:
                day_dt = datetime.strptime(d.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if day_dt >= cutoff:
                continue
            for f in list(d.glob("*.json")):
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    continue
            try:
                d.rmdir()
            except Exception:
                pass
        return removed


def open_store(
    client: Any = None,
    *,
    workspace_root: Any = None,
) -> RawResultStore:
    """Open the store rooted at ``client.config.paths.root`` or the active workspace.

    When ``client`` is ``None`` (e.g. from the agent loop which has no
    direct handle) we resolve the workspace via
    :func:`nerya.core.paths.resolve_workspace`, the same way
    :mod:`nerya.runtime.feature_flags` falls back when no client is
    provided. This keeps the helper safe to call from anywhere without
    plumbing a client through every constructor.
    """

    root: Optional[Path] = None
    if client is not None:
        try:
            root = client.config.paths.root  # type: ignore[union-attr]
        except Exception:
            root = None
    if root is None and workspace_root is not None:
        try:
            root = Path(workspace_root)
        except Exception:
            root = None
    if root is None:
        try:
            from ..core.paths import resolve_workspace
            root = resolve_workspace().root
        except Exception:
            root = Path("workspace")
    return RawResultStore(workspace_root=root)


def write_default(
    *,
    tool_use_id: str,
    tool_name: str,
    payload: Any,
    client: Any = None,
    workspace_root: Any = None,
) -> str:
    """Convenience: persist ``payload`` using the default store.

    Returns ``""`` on any failure so the caller can swallow it without
    breaking the agent loop. The returned ref is ``raw://<day>/<id>``.
    """

    try:
        return open_store(client, workspace_root=workspace_root).write(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            payload=payload,
        )
    except Exception:
        _LOG.exception("tool_raw_store.write_default failed")
        return ""


__all__ = [
    "RawRecord",
    "RawResultStore",
    "open_store",
    "write_default",
]

"""Tool-result persistence for oversized observations.

The runtime' ``run_agent.py`` persists oversized tool outputs to disk and
substitutes a reference into the agent's context (see the runtime
``run_agent.py``: ``_persist_tool_result`` / ``tool_result_store``).
Without that, every long ``ls`` / ``read_file`` / ``terminal`` reply
would balloon the prompt window.

This module is the Nerya analogue. It is deliberately tiny and
synchronous: callers ask for a "ref" (a stable filename in
``state/tool_results/``), write the full payload, and substitute the
ref into the planner observation. Read-back (``load``) is best-effort
and returns ``None`` when the file rotated out.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResultRef:
    """Stable handle to a persisted tool result."""

    ref_id: str
    path: str
    bytes: int
    kind: str = "text"
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "path": self.path,
            "bytes": self.bytes,
            "kind": self.kind,
            "summary": self.summary,
        }


class ResultStore:
    """File-backed store under ``state/tool_results/``.

    Files are JSON envelopes ``{"kind", "summary", "payload"}`` so the
    UI / dashboard / replay tooling can still see the metadata without
    parsing the payload. ``payload`` is either an inline string or a
    sidecar pointer when the body is huge.
    """

    def __init__(self, paths) -> None:
        self.root = Path(paths.state) / "tool_results"
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # write paths
    # ------------------------------------------------------------------

    def store(
        self,
        payload: Any,
        *,
        kind: str = "text",
        summary: str = "",
    ) -> ResultRef:
        """Persist ``payload`` and return a :class:`ResultRef`."""

        ref_id = f"tr_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        path = self.root / f"{ref_id}.json"
        if isinstance(payload, (bytes, bytearray)):
            try:
                body: Any = payload.decode("utf-8")
            except UnicodeDecodeError:
                body = payload.decode("utf-8", errors="replace")
        elif isinstance(payload, (dict, list)):
            body = payload
        else:
            body = str(payload)
        envelope = {
            "ref_id": ref_id,
            "kind": kind,
            "summary": summary or "",
            "stored_at": int(time.time()),
            "payload": body,
        }
        text = json.dumps(envelope, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        size = len(text.encode("utf-8"))
        return ResultRef(
            ref_id=ref_id,
            path=str(path),
            bytes=size,
            kind=kind,
            summary=summary or "",
        )

    # ------------------------------------------------------------------
    # read paths
    # ------------------------------------------------------------------

    def load(self, ref_id: str) -> dict[str, Any] | None:
        if not ref_id:
            return None
        path = self.root / f"{ref_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_refs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to ``limit`` recent refs, newest first."""

        try:
            files = sorted(
                (p for p in self.root.iterdir() if p.is_file() and p.suffix == ".json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except FileNotFoundError:
            return []
        out: list[dict[str, Any]] = []
        for p in files[:max(0, limit)]:
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "ref_id": doc.get("ref_id"),
                "kind": doc.get("kind"),
                "summary": doc.get("summary"),
                "stored_at": doc.get("stored_at"),
                "bytes": p.stat().st_size,
                "path": str(p),
            })
        return out

    # ------------------------------------------------------------------
    # housekeeping
    # ------------------------------------------------------------------

    def prune(self, *, keep: int = 200) -> int:
        """Drop the oldest envelopes, keep the most recent ``keep``."""

        files = sorted(
            (p for p in self.root.iterdir() if p.is_file() and p.suffix == ".json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        dropped = 0
        for p in files[max(0, keep):]:
            try:
                p.unlink()
                dropped += 1
            except OSError:
                pass
        return dropped


def maybe_persist(
    paths,
    payload: Any,
    *,
    threshold_bytes: int = 16 * 1024,
    kind: str = "text",
    summary: str = "",
) -> tuple[Any, ResultRef | None]:
    """Persist ``payload`` to disk when its serialised form exceeds the
    threshold. Returns ``(payload_or_summary, ref_or_none)`` so callers
    can splice a ref dict into their observation when the body was
    rotated out.
    """

    try:
        size = len(json.dumps(payload, default=str).encode("utf-8"))
    except Exception:
        size = len(str(payload).encode("utf-8"))
    if size <= threshold_bytes:
        return payload, None
    store = ResultStore(paths)
    ref = store.store(payload, kind=kind, summary=summary or f"oversized {kind} payload ({size} bytes)")
    summary_payload = {
        "summary": summary or f"oversized {kind} payload (see ref {ref.ref_id})",
        "ref_id": ref.ref_id,
        "kind": kind,
        "bytes": size,
    }
    return summary_payload, ref

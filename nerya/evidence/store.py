"""Evidence vault store.

On-disk layout::

    workspace/evidence/
      index.jsonl                # one record per EvidenceDoc.as_dict()
      <YYYY-MM-DD>/<evidence_id>.md

The index is the only fast lookup. ``Markdown`` files are the inspectable
source of truth for an operator with a text editor. Search uses simple
token matching to keep the implementation dependency-free; a future
upgrade can layer a vector index on top.

ACL model:

- ``scope="shared"`` (default) — visible to any caller.
- ``scope="strategy"`` — only callers passing the same ``strategy_id``.
- ``scope="session"`` — only callers passing the same ``session_id``.

The matching function :func:`EvidenceStore.search` enforces these rules.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ..security.prompt_injection import wrap_untrusted  # noqa: F401  (re-export idea)
from .schemas import EvidenceDoc, Provenance, SecurityInfo, now_iso, today_path_segment


_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\s*[:=]\s*[A-Za-z0-9._\-]{6,}"),
    re.compile(r"vault://[A-Za-z0-9._\-/]+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
)


def _redact(text: str) -> tuple[str, bool]:
    redacted = text
    found = False
    for pat in _REDACT_PATTERNS:
        if pat.search(redacted):
            found = True
            redacted = pat.sub("[redacted]", redacted)
    return redacted, found


def _evidence_id() -> str:
    return "ev_" + secrets.token_hex(8)


@dataclass
class EvidenceStore:
    workspace_root: Path

    @property
    def root(self) -> Path:
        return self.workspace_root / "evidence"

    @property
    def index_file(self) -> Path:
        return self.root / "index.jsonl"

    def _doc_path(self, doc: EvidenceDoc) -> Path:
        seg = today_path_segment()
        return self.root / seg / f"{doc.evidence_id}.md"

    def ingest(
        self,
        *,
        source_type: str,
        source_id: str,
        title: str,
        body: str,
        summary: str = "",
        tags: Optional[list[str]] = None,
        scope: str = "shared",
        strategy_id: Optional[str] = None,
        session_id: Optional[str] = None,
        route: str = "",
        created_by: str = "runtime",
        artifact_refs: Optional[list[str]] = None,
    ) -> EvidenceDoc:
        redacted_body, body_had_secret = _redact(body or "")
        redacted_summary, summary_had_secret = _redact(summary or "")
        evidence_id = _evidence_id()
        doc = EvidenceDoc(
            evidence_id=evidence_id,
            source_type=source_type,
            source_id=source_id,
            title=title,
            summary=redacted_summary or _truncate(redacted_body, 200),
            provenance=Provenance(
                route=route,
                strategy_id=strategy_id or "",
                session_id=session_id or "",
                artifact_refs=list(artifact_refs or []),
                created_by=created_by,
            ),
            security=SecurityInfo(
                contains_secret=body_had_secret or summary_had_secret,
                redaction_applied=True,
            ),
            tags=list(tags or []),
            created_at=now_iso(),
            body=redacted_body,
            scope=scope,
            strategy_id=strategy_id,
            session_id=session_id,
        )
        doc.workspace_path = str(self._doc_path(doc).relative_to(self.workspace_root))
        self._write_markdown(doc)
        self._append_index(doc)
        return doc

    def _write_markdown(self, doc: EvidenceDoc) -> None:
        path = self.workspace_root / doc.workspace_path
        path.parent.mkdir(parents=True, exist_ok=True)
        meta_lines = [
            "---",
            f"evidence_id: {doc.evidence_id}",
            f"source_type: {doc.source_type}",
            f"source_id: {doc.source_id}",
            f"title: {doc.title}",
            f"created_at: {doc.created_at}",
            f"scope: {doc.scope}",
        ]
        if doc.strategy_id:
            meta_lines.append(f"strategy_id: {doc.strategy_id}")
        if doc.session_id:
            meta_lines.append(f"session_id: {doc.session_id}")
        if doc.tags:
            meta_lines.append("tags: [" + ", ".join(doc.tags) + "]")
        meta_lines.append("---\n")
        text = "\n".join(meta_lines) + f"# {doc.title}\n\n"
        if doc.summary:
            text += f"_Summary_: {doc.summary}\n\n"
        text += doc.body
        path.write_text(text, encoding="utf-8")

    def _append_index(self, doc: EvidenceDoc) -> None:
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        with self.index_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc.as_dict(), ensure_ascii=False) + "\n")

    def _iter_index(self) -> Iterable[dict[str, Any]]:
        if not self.index_file.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.index_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def get(self, evidence_id: str) -> Optional[dict[str, Any]]:
        for rec in self._iter_index():
            if rec.get("evidence_id") == evidence_id:
                rec = dict(rec)
                p = self.workspace_root / rec.get("workspace_path", "")
                if p.exists():
                    rec["body"] = p.read_text(encoding="utf-8")
                return rec
        return None

    def list_sources(self) -> list[dict[str, Any]]:
        by_source: dict[str, dict[str, Any]] = {}
        for rec in self._iter_index():
            sid = rec.get("source_id") or ""
            st = rec.get("source_type") or ""
            key = f"{st}:{sid}"
            entry = by_source.setdefault(key, {
                "source_type": st, "source_id": sid, "count": 0,
                "latest_at": "",
            })
            entry["count"] += 1
            created = rec.get("created_at") or ""
            if created > entry["latest_at"]:
                entry["latest_at"] = created
        return sorted(by_source.values(), key=lambda r: (r["source_type"], r["source_id"]))

    def topics(self) -> list[dict[str, Any]]:
        tag_counts: dict[str, int] = {}
        for rec in self._iter_index():
            for tag in rec.get("tags") or []:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        return sorted(
            [{"topic": t, "count": c} for t, c in tag_counts.items()],
            key=lambda r: (-r["count"], r["topic"]),
        )

    def search(
        self,
        *,
        query: str = "",
        source_type: str = "",
        topic: str = "",
        scope: str = "any",
        strategy_id: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Search evidence with ACL enforcement.

        ``scope`` semantics:

        * ``"shared"`` - only documents marked ``shared``.
        * ``"strategy"`` - only documents with ``scope=strategy`` and matching
          ``strategy_id``. Requires ``strategy_id``.
        * ``"session"`` - only documents with ``scope=session`` and matching
          ``session_id``. Requires ``session_id``.
        * ``"any"`` (default) - operator-level view; returns shared
          documents and strategy-scoped/session-scoped documents only when
          the caller passes the matching id. Strategy/session evidence
          *never* leaks across strategies/sessions.
        """

        q = (query or "").lower().strip()
        scope = (scope or "any").lower()
        results: list[tuple[float, dict[str, Any]]] = []
        for rec in self._iter_index():
            if source_type and rec.get("source_type") != source_type:
                continue
            if topic and topic not in (rec.get("tags") or []):
                continue
            # ACL: enforce scope match
            rec_scope = rec.get("scope") or "shared"
            if scope == "shared":
                if rec_scope != "shared":
                    continue
            elif scope == "strategy":
                if rec_scope != "strategy":
                    continue
                if not strategy_id or rec.get("strategy_id") != strategy_id:
                    continue
            elif scope == "session":
                if rec_scope != "session":
                    continue
                if not session_id or rec.get("session_id") != session_id:
                    continue
            else:  # "any" — operator-level view; ACL-FILTERED
                # Shared docs are always visible. Strategy/session docs are
                # visible only when the caller passes the matching id.
                # Strategy/session evidence never leaks across boundaries.
                if rec_scope == "shared":
                    pass
                elif rec_scope == "strategy":
                    if not strategy_id or rec.get("strategy_id") != strategy_id:
                        continue
                elif rec_scope == "session":
                    if not session_id or rec.get("session_id") != session_id:
                        continue
                else:
                    # Unknown scope label — treat conservatively as private.
                    continue
            score = 0.0
            if q:
                hay = " ".join([
                    str(rec.get("title") or ""),
                    str(rec.get("summary") or ""),
                    " ".join(rec.get("tags") or []),
                    str(rec.get("source_id") or ""),
                ]).lower()
                if q in hay:
                    score = 1.0 + hay.count(q) * 0.1
                else:
                    continue
            else:
                # no query — rank by recency
                score = 0.5
            results.append((score, rec))
        results.sort(key=lambda r: (-r[0], r[1].get("created_at", "")), reverse=False)
        results.sort(key=lambda r: (-r[0], -_to_epoch(r[1].get("created_at"))))
        return [rec for _, rec in results[:limit]]


def _to_epoch(s: Any) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def open_store(client=None) -> EvidenceStore:
    """Open the evidence store for the workspace of ``client``.

    When ``client`` is ``None`` (or lacks ``config.paths``) we resolve
    the active workspace via :func:`nerya.core.paths.resolve_workspace`,
    matching the fallback behaviour of :mod:`nerya.runtime.feature_flags`.
    This lets background-thread call sites (gateway event recording,
    janitor tasks, etc.) drop evidence rows without having to plumb a
    client through every function signature.
    """
    root = None
    if client is not None:
        try:
            root = client.config.paths.root  # type: ignore[union-attr]
        except Exception:
            root = None
    if root is None:
        try:
            from ..core.paths import resolve_workspace
            root = resolve_workspace().root
        except Exception:
            from pathlib import Path
            root = Path("workspace")
    return EvidenceStore(workspace_root=root)

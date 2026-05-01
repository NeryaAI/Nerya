"""EvidenceBundle — structured per-turn ledger of agent activity.

- Mirrors the the runtime "evidence ledger" pattern + coding-agent's per-turn
  activity log used to render the dashboard timeline and to seed
  retrospective compaction.

Why
---
The kernel already journals every tool call to ``tool.jsonl``, but
the LLM benefits from a higher-level summary in its observation feed
("you have read 3 files, edited 2, and run 1 test command"). The
dashboard benefits from a structured timeline ("17:42:03  read
``nerya/agent/kernel.py``  L1-200"). And the post-turn replay tool
benefits from a single shape it can serialize to disk.

EvidenceBundle is that shape. The kernel constructs one per turn,
hands it down through ``ctx`` to every tool call, and asks each
mutating skill to ``add_*`` an entry. Read-only enumerations stay
out of the ledger by default — they are noisy and the journal already
covers them — unless they fail, in which case we record the failure
so error_recovery can surface it.

Scope
-----
The structure is deliberately schema-light: every entry is a dict with
a stable ``kind`` discriminator, a ``ts`` timestamp, and free-form
payload fields. New entry kinds can be added without breaking
existing readers. We provide typed helpers for the common kinds:

* ``file_read`` / ``file_edit`` / ``file_write`` / ``file_delete``
* ``shell_run``  (``terminal`` + ``process_*`` calls)
* ``approval_request`` / ``approval_decision``
* ``validation``  (test runner / lint / typecheck triggers)
* ``error``  (recoverable + fatal — both go through error_recovery)

The kernel calls :meth:`finalise` at end of turn to attach the
ledger to the turn record + ship it to the streaming bus + persist
it under ``.nerya/evidence/<turn_id>.json``.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


__all__ = [
    "EvidenceBundle",
    "EvidenceEntry",
]


@dataclass
class EvidenceEntry:
    kind: str
    ts: float
    seq: int
    payload: dict[str, Any] = field(default_factory=dict)
    entry_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ts": self.ts,
            "seq": self.seq,
            "entry_id": self.entry_id,
            **self.payload,
        }


class EvidenceBundle:
    """Per-turn ledger. Thread-safe.

    Construction is cheap; all mutating helpers acquire a small lock
    so worker threads in :meth:`ToolRunner.call_parallel` can safely
    add entries. Order is by ``seq`` (monotonic), not strictly by
    ``ts``, because clock skew on multi-thread machines is real.
    """

    def __init__(
        self,
        *,
        turn_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self.turn_id = turn_id or uuid.uuid4().hex
        self.session_id = session_id or ""
        self.run_id = run_id or ""
        self.started_at = time.time()
        self.completed_at: float | None = None
        self._lock = threading.RLock()
        self._seq = 0
        self._entries: list[EvidenceEntry] = []
        self._counts: dict[str, int] = {}

    # ---- raw add ------------------------------------------------------------

    def add(self, kind: str, **payload: Any) -> EvidenceEntry:
        with self._lock:
            self._seq += 1
            entry = EvidenceEntry(
                kind=kind,
                ts=time.time(),
                seq=self._seq,
                payload=dict(payload),
                entry_id=uuid.uuid4().hex[:12],
            )
            self._entries.append(entry)
            self._counts[kind] = self._counts.get(kind, 0) + 1
            return entry

    # ---- typed helpers ------------------------------------------------------

    def add_file_read(
        self,
        path: str,
        *,
        bytes_seen: int = 0,
        line_count: int = 0,
        truncated: bool = False,
        content_hash: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> EvidenceEntry:
        return self.add(
            "file_read",
            path=path,
            bytes_seen=bytes_seen,
            line_count=line_count,
            truncated=truncated,
            content_hash=content_hash,
            start_line=start_line,
            end_line=end_line,
        )

    def add_file_edit(
        self,
        path: str,
        *,
        kind: str = "string_replace",
        bytes_before: int = 0,
        bytes_after: int = 0,
        edits: int = 1,
        content_hash_before: str = "",
        content_hash_after: str = "",
    ) -> EvidenceEntry:
        return self.add(
            "file_edit",
            path=path,
            edit_kind=kind,
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            edits=edits,
            content_hash_before=content_hash_before,
            content_hash_after=content_hash_after,
        )

    def add_file_write(
        self,
        path: str,
        *,
        mode: str,
        bytes_written: int,
        content_hash: str = "",
    ) -> EvidenceEntry:
        return self.add(
            "file_write",
            path=path,
            mode=mode,
            bytes_written=bytes_written,
            content_hash=content_hash,
        )

    def add_file_delete(self, path: str) -> EvidenceEntry:
        return self.add("file_delete", path=path)

    def add_shell_run(
        self,
        cmd: str,
        *,
        cwd: str = "",
        exit_code: int | None = None,
        elapsed_ms: float = 0.0,
        truncated: bool = False,
        refused: bool = False,
        timeout: bool = False,
        risk: str = "",
    ) -> EvidenceEntry:
        return self.add(
            "shell_run",
            cmd=cmd,
            cwd=cwd,
            exit_code=exit_code,
            elapsed_ms=elapsed_ms,
            truncated=truncated,
            refused=refused,
            timeout=timeout,
            risk=risk,
        )

    def add_approval_request(
        self,
        skill_id: str,
        action: str,
        *,
        reason: str = "",
        request_id: str = "",
    ) -> EvidenceEntry:
        return self.add(
            "approval_request",
            skill_id=skill_id,
            action=action,
            reason=reason,
            request_id=request_id,
        )

    def add_approval_decision(
        self,
        skill_id: str,
        action: str,
        *,
        decision: str,
        operator: str = "",
        request_id: str = "",
    ) -> EvidenceEntry:
        return self.add(
            "approval_decision",
            skill_id=skill_id,
            action=action,
            decision=decision,
            operator=operator,
            request_id=request_id,
        )

    def add_validation(
        self,
        kind: str,
        *,
        ok: bool,
        target: str = "",
        summary: str = "",
        details: dict[str, Any] | None = None,
    ) -> EvidenceEntry:
        return self.add(
            "validation",
            validation_kind=kind,
            ok=ok,
            target=target,
            summary=summary,
            details=dict(details or {}),
        )

    def add_error(
        self,
        *,
        category: str,
        where: str,
        message: str,
        recoverable: bool = True,
        recovery_hint: str = "",
    ) -> EvidenceEntry:
        return self.add(
            "error",
            category=category,
            where=where,
            message=message,
            recoverable=recoverable,
            recovery_hint=recovery_hint,
        )

    # ---- queries ------------------------------------------------------------

    def entries(self, *, kinds: Iterable[str] | None = None) -> list[EvidenceEntry]:
        with self._lock:
            if kinds is None:
                return list(self._entries)
            keep = set(kinds)
            return [e for e in self._entries if e.kind in keep]

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def summary(self) -> dict[str, Any]:
        """Compact, LLM-friendly summary suitable for observation feeds."""

        with self._lock:
            counts = dict(self._counts)
            files_touched: list[str] = []
            seen: set[str] = set()
            for e in self._entries:
                if e.kind not in {"file_read", "file_edit", "file_write", "file_delete"}:
                    continue
                p = str(e.payload.get("path") or "")
                if p and p not in seen:
                    seen.add(p)
                    files_touched.append(p)
            return {
                "turn_id": self.turn_id,
                "counts": counts,
                "files_touched": files_touched,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "entries": len(self._entries),
            }

    # ---- finalisation -------------------------------------------------------

    def finalise(self) -> None:
        if self.completed_at is None:
            self.completed_at = time.time()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "turn_id": self.turn_id,
                "session_id": self.session_id,
                "run_id": self.run_id,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "counts": dict(self._counts),
                "entries": [e.as_dict() for e in self._entries],
            }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, default=str)

    def write_to(self, path: str | Path) -> Path:
        """Persist the bundle to disk (one JSON file per turn)."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "EvidenceBundle":
        """Load a previously persisted bundle (best-effort)."""

        text = Path(path).read_text(encoding="utf-8")
        doc = json.loads(text)
        bundle = cls(
            turn_id=doc.get("turn_id"),
            session_id=doc.get("session_id"),
            run_id=doc.get("run_id"),
        )
        bundle.started_at = float(doc.get("started_at") or bundle.started_at)
        bundle.completed_at = (
            float(doc["completed_at"])
            if doc.get("completed_at") is not None
            else None
        )
        for raw in doc.get("entries") or []:
            kind = str(raw.get("kind") or "")
            payload = {k: v for k, v in raw.items()
                       if k not in {"kind", "ts", "seq", "entry_id"}}
            entry = EvidenceEntry(
                kind=kind,
                ts=float(raw.get("ts") or 0.0),
                seq=int(raw.get("seq") or 0),
                payload=payload,
                entry_id=str(raw.get("entry_id") or ""),
            )
            bundle._entries.append(entry)
            bundle._counts[kind] = bundle._counts.get(kind, 0) + 1
            bundle._seq = max(bundle._seq, entry.seq)
        return bundle

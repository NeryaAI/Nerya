"""Per-run mailbox over :class:`TeamStore`.

The mailbox is intentionally simple: messages are stored as
``inboxes/<agent>/msg-<seq>-<id>.json`` files. ``send`` writes a fresh
``.json`` file; ``receive`` renames the file to ``.consumed`` (atomic
on POSIX/Windows) and returns it. Reading is FIFO by filename, so the
``seq`` prefix preserves order even after process restarts.

This is enough for our orchestrator (single coordinating process) and
keeps replay-safe semantics if a future supervisor crashes between
``read`` and ``commit``.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from ..core.time import now_iso
from .models import TeamMessage
from .store import TeamStore, _slug


class Mailbox:
    """File-backed FIFO mailbox keyed by agent name.

    Concurrency: a single :class:`Mailbox` instance is thread-safe via
    a per-instance lock, so the orchestrator can call ``send``/
    ``receive`` from worker threads without corrupting filenames. Cross-
    process ordering relies on monotonically-increasing sequence
    numbers stored in the filename.
    """

    def __init__(self, store: TeamStore, run_id: str):
        self.store = store
        self.run_id = run_id
        self._lock = threading.Lock()
        self._seq = 0

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _agent_dir(self, agent: str) -> Path:
        return self.store.inbox_dir(self.run_id, agent)

    # ------------------------------------------------------------ send
    def send(
        self,
        *,
        from_agent: str,
        to: Optional[str],
        type: str,
        content: str = "",
        artifact_refs: Optional[list[str]] = None,
        broadcast_members: Optional[list[str]] = None,
    ) -> list[TeamMessage]:
        """Send a message to one recipient or broadcast to ``broadcast_members``.

        Returns the list of materialised :class:`TeamMessage` objects so
        the caller can inspect ids/timestamps.
        """

        recipients: list[str]
        if to is not None:
            recipients = [to]
        elif broadcast_members:
            recipients = [m for m in broadcast_members if m != from_agent]
        else:
            recipients = []
        out: list[TeamMessage] = []
        for rcpt in recipients:
            msg = TeamMessage(
                id=uuid.uuid4().hex[:12],
                run_id=self.run_id,
                type=type,
                from_agent=from_agent,
                to=rcpt,
                content=content,
                artifact_refs=list(artifact_refs or []),
                created_at=now_iso(),
            )
            self._write(rcpt, msg)
            out.append(msg)
        # Always append a broadcast event so the audit trail captures sends with no recipient.
        self.store.append_event(
            self.run_id,
            kind="message.sent",
            type=type,
            from_agent=from_agent,
            to=to,
            recipients=len(out),
            recipient_names=[m.to for m in out],
            content=content,
            artifact_refs=list(artifact_refs or []),
        )
        return out

    def _write(self, rcpt: str, msg: TeamMessage) -> None:
        d = self._agent_dir(rcpt)
        d.mkdir(parents=True, exist_ok=True)
        seq = self._next_seq()
        name = f"msg-{seq:08d}-{_slug(msg.id)}.json"
        path = d / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(msg), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------ peek/receive
    def peek(self, agent: str, limit: int = 5) -> list[TeamMessage]:
        d = self._agent_dir(agent)
        if not d.exists():
            return []
        out: list[TeamMessage] = []
        for path in sorted(d.glob("msg-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                out.append(TeamMessage(**{k: v for k, v in data.items()
                                          if k in TeamMessage.__dataclass_fields__}))
            except Exception:
                continue
            if len(out) >= limit:
                break
        return out

    def receive(self, agent: str, limit: int = 10) -> list[TeamMessage]:
        d = self._agent_dir(agent)
        if not d.exists():
            return []
        out: list[TeamMessage] = []
        for path in sorted(d.glob("msg-*.json")):
            consumed = path.with_suffix(".consumed")
            try:
                os.replace(path, consumed)
            except FileNotFoundError:
                continue
            try:
                data = json.loads(consumed.read_text(encoding="utf-8"))
                msg = TeamMessage(**{k: v for k, v in data.items()
                                     if k in TeamMessage.__dataclass_fields__})
                msg.consumed_at = now_iso()
                out.append(msg)
            except Exception:
                # Quarantine bad files alongside as .deadletter
                consumed.rename(consumed.with_suffix(".deadletter"))
                continue
            if len(out) >= limit:
                break
        if out:
            self.store.append_event(
                self.run_id,
                kind="message.received",
                agent=agent,
                count=len(out),
            )
        return out


__all__ = ["Mailbox"]

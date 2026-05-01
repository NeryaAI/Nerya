"""Unified gateway mirror — multi-channel inbox/outbox + cross-channel pairing.

Every message that flows through Nerya's messaging surface — inbound from
a user or outbound from a skill — is written to a single mirror journal
so operators can replay *what was said where* without hopping between
Telegram, Discord, webhook payload dumps, and the dashboard feed.

Responsibilities
----------------
* :class:`GatewayMirror.record_inbound` / :meth:`record_outbound` append
  a :class:`MirrorEntry` to ``journals/messaging_mirror.jsonl``.
* Per-session state (last inbound, last outbound, agent turn context)
  is tracked in memory and snapshotted to ``sessions/<id>.yml`` under
  ``workspace/messaging/``.
* Cross-channel **pairing** binds a user's handles across channels so
  Nerya can address the same human as ``tg:@alice`` *and* ``discord:alice#1``
  without re-prompting.

Design constraints
------------------
* No outbound network I/O — the mirror is a read/append surface, not a
  sender. Actual delivery lives in :mod:`nerya.messaging.pipeline`.
* Idempotent: re-recording a row with the same ``message_id`` is a no-op.
* Channel-agnostic: the payload dict is stored verbatim; no schema
  coupling to any concrete transport.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core import jsonl, yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.ids import new_id
from ..core.paths import WorkspacePaths
from ..core.time import now_iso


Direction = Literal["in", "out"]


_VALID_DIRECTIONS: frozenset[Direction] = frozenset({"in", "out"})


@dataclass
class MirrorEntry:
    message_id: str
    ts: str
    direction: Direction
    channel: str
    session_id: str | None
    handle: str | None
    payload: dict[str, Any]
    strategy_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionContext:
    session_id: str
    channel: str
    handle: str | None = None
    strategy_id: str | None = None
    last_inbound_ts: str | None = None
    last_outbound_ts: str | None = None
    turns: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class GatewayMirror:
    """In-process mirror of all messages across every channel.

    Instantiate once per runtime. It serialises writes with a lock so
    concurrent sends from different skill calls don't interleave the
    mirror journal.
    """

    def __init__(self, paths: WorkspacePaths) -> None:
        self._paths = paths
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionContext] = {}
        self._seen_ids: set[str] = set()
        self._dir = paths.root / "workspace" / "messaging"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir = self._dir / "sessions"
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._pairings_file = self._dir / "pairings.yml"
        self._pairings: dict[str, dict[str, str]] = self._load_pairings()

    # ------------------------------------------------------------------ core

    def record_inbound(self, *, channel: str, payload: dict[str, Any],
                       handle: str | None = None,
                       session_id: str | None = None,
                       strategy_id: str | None = None,
                       message_id: str | None = None) -> MirrorEntry:
        return self._record("in", channel=channel, payload=payload,
                            handle=handle, session_id=session_id,
                            strategy_id=strategy_id,
                            message_id=message_id)

    def record_outbound(self, *, channel: str, payload: dict[str, Any],
                        handle: str | None = None,
                        session_id: str | None = None,
                        strategy_id: str | None = None,
                        message_id: str | None = None) -> MirrorEntry:
        return self._record("out", channel=channel, payload=payload,
                            handle=handle, session_id=session_id,
                            strategy_id=strategy_id,
                            message_id=message_id)

    # --------------------------------------------------------------- session

    def session(self, session_id: str) -> SessionContext | None:
        with self._lock:
            ctx = self._sessions.get(session_id)
            return (
                SessionContext(
                    session_id=ctx.session_id,
                    channel=ctx.channel,
                    handle=ctx.handle,
                    strategy_id=ctx.strategy_id,
                    last_inbound_ts=ctx.last_inbound_ts,
                    last_outbound_ts=ctx.last_outbound_ts,
                    turns=ctx.turns,
                    meta=dict(ctx.meta),
                )
                if ctx is not None else None
            )

    def sessions(self) -> list[SessionContext]:
        with self._lock:
            return [self.session(sid) for sid in self._sessions]  # type: ignore[list-item]

    # ---------------------------------------------------------------- pair

    def pair(self, primary: str, *aliases: str) -> None:
        """Bind ``primary`` to every alias. Both sides are symmetric."""
        if not primary:
            raise ValueError("pair() primary handle must be non-empty")
        with self._lock:
            group = self._pairings.setdefault(primary, {"primary": primary,
                                                        "aliases": []})
            current = set(group.get("aliases") or [])
            for a in aliases:
                if a and a != primary:
                    current.add(a)
            group["aliases"] = sorted(current)
            self._save_pairings()

    def resolve(self, handle: str) -> str:
        """Return the primary handle for ``handle`` (itself if unpaired)."""
        with self._lock:
            for primary, group in self._pairings.items():
                if handle == primary or handle in (group.get("aliases") or []):
                    return primary
            return handle

    def pairings(self) -> dict[str, list[str]]:
        with self._lock:
            return {
                p: list(g.get("aliases") or [])
                for p, g in self._pairings.items()
            }

    # --------------------------------------------------------------- replay

    def replay(self, *, channel: str | None = None,
               session_id: str | None = None,
               handle: str | None = None,
               limit: int | None = None) -> list[MirrorEntry]:
        rows = jsonl.read_all(self._journal_file())
        out: list[MirrorEntry] = []
        for r in rows:
            if channel and r.get("channel") != channel:
                continue
            if session_id and r.get("session_id") != session_id:
                continue
            if handle and r.get("handle") != handle:
                continue
            out.append(MirrorEntry(
                message_id=r["message_id"],
                ts=r["ts"],
                direction=r["direction"],  # type: ignore[arg-type]
                channel=r["channel"],
                session_id=r.get("session_id"),
                handle=r.get("handle"),
                payload=r.get("payload") or {},
                strategy_id=r.get("strategy_id"),
            ))
        if limit is not None:
            out = out[-limit:]
        return out

    # =================================================================
    # internals
    # =================================================================

    def _record(self, direction: Direction, *, channel: str,
                payload: dict[str, Any], handle: str | None,
                session_id: str | None, strategy_id: str | None,
                message_id: str | None) -> MirrorEntry:
        if direction not in _VALID_DIRECTIONS:
            raise ValueError(f"invalid direction: {direction!r}")
        if not channel:
            raise ValueError("channel is required")

        mid = message_id or new_id("msg")
        ts = now_iso()

        with self._lock:
            if mid in self._seen_ids:
                # Re-delivery (rare): emit a dedupe-marker row so
                # operators see the resend, but don't double-count.
                entry = MirrorEntry(
                    message_id=mid, ts=ts, direction=direction,
                    channel=channel, session_id=session_id, handle=handle,
                    payload={"deduped": True, **payload},
                    strategy_id=strategy_id,
                )
                self._append_journal(entry)
                return entry

            self._seen_ids.add(mid)

            entry = MirrorEntry(
                message_id=mid, ts=ts, direction=direction,
                channel=channel, session_id=session_id, handle=handle,
                payload=payload, strategy_id=strategy_id,
            )
            self._append_journal(entry)
            if session_id:
                self._touch_session(direction, session_id, channel,
                                    handle, strategy_id)
            return entry

    def _append_journal(self, entry: MirrorEntry) -> None:
        jsonl.append(self._journal_file(), entry.as_dict())

    def _journal_file(self):
        return self._paths.journal("messaging_mirror")

    def _touch_session(self, direction: Direction, session_id: str,
                       channel: str, handle: str | None,
                       strategy_id: str | None) -> None:
        ctx = self._sessions.get(session_id)
        if ctx is None:
            ctx = SessionContext(session_id=session_id, channel=channel,
                                 handle=handle, strategy_id=strategy_id)
            self._sessions[session_id] = ctx
        if direction == "in":
            ctx.last_inbound_ts = now_iso()
            ctx.turns += 1
        else:
            ctx.last_outbound_ts = now_iso()
        ctx.handle = ctx.handle or handle
        ctx.strategy_id = ctx.strategy_id or strategy_id
        atomic_write_text(
            self._sessions_dir / f"{session_id}.yml",
            yaml_io.dumps(ctx.as_dict()),
        )

    def _load_pairings(self) -> dict[str, dict[str, str]]:
        doc = yaml_io.load(self._pairings_file, default={"pairings": []}) or {}
        out: dict[str, dict[str, str]] = {}
        for row in (doc.get("pairings") or []):
            primary = row.get("primary")
            if not primary:
                continue
            out[primary] = {
                "primary": primary,
                "aliases": list(row.get("aliases") or []),
            }
        return out

    def _save_pairings(self) -> None:
        doc = {"pairings": [
            {"primary": p, "aliases": list(g.get("aliases") or [])}
            for p, g in sorted(self._pairings.items())
        ]}
        atomic_write_text(self._pairings_file, yaml_io.dumps(doc))

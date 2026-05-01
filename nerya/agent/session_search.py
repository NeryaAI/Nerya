"""Session search over persisted turn/skill journals.

the runtime ships an FTS5 index over its session table so operators can
``runtime session search "redis token loop"`` and recover what happened
last week. Nerya stored the equivalent telemetry in journal files and
session JSONs but did not expose any search surface, which made
post-mortems painful for long-running operators.

This module ships *both* lanes:

- A pure-python streaming scan (:func:`search`/:func:`recent_events`)
  that works on tiny SQLite-less environments and matches the original
  contract.
- An optional FTS5 mirror (:mod:`nerya.agent.session_search_fts`) that
  the same :func:`search` consults first when SQLite ships FTS5. The
  mirror is rebuilt incrementally from the JSONL journals so the
  on-disk files remain the source of truth — there is no risk of the
  index disagreeing with the underlying journal because the scan is
  always the fallback.

Public surface:

- :func:`search` — full text search over turn-step journal entries.
- :func:`recent_events` — most-recent N events for a session/strategy.

P0 §5,
P0 §2.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from . import session_search_fts


_DEFAULT_JOURNALS = (
    "turn_steps",      # main turn loop
    "agent_decisions", # parsed decisions
    "skills",          # skill.call.start / done / error
    "messages",        # outbox/inbox audit
)


@dataclass
class SessionEvent:
    """One journal row, normalised for search/output."""

    journal: str
    turn_id: Optional[str]
    session_id: Optional[str]
    strategy_id: Optional[str]
    kind: str
    ts: str
    text: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "journal": self.journal,
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "strategy_id": self.strategy_id,
            "kind": self.kind,
            "ts": self.ts,
            "preview": self.text[:240],
            "payload": self.payload,
        }


def _iter_journal(path: Path, journal: str) -> Iterable[SessionEvent]:
    if not path.exists():
        return []
    out: list[SessionEvent] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        out.append(SessionEvent(
            journal=journal,
            turn_id=row.get("turn_id") or row.get("agent_turn_id"),
            session_id=row.get("session_id"),
            strategy_id=row.get("strategy_id"),
            kind=str(row.get("kind") or row.get("event") or journal),
            ts=str(row.get("ts") or row.get("timestamp") or ""),
            text=line,
            payload=row,
        ))
    return out


def _all_events(paths, journals: Iterable[str]) -> list[SessionEvent]:
    out: list[SessionEvent] = []
    for journal in journals:
        out.extend(_iter_journal(paths.journal(journal), journal))
    return out


def _fts_search(
    paths,
    query: str,
    *,
    journals: Iterable[str],
    session_id: Optional[str],
    strategy_id: Optional[str],
    limit: int,
) -> Optional[list[dict[str, Any]]]:
    """Try FTS5 first. Returns ``None`` when the FTS lane should be
    skipped (regex query, FTS unsupported, open failed). Returns an
    empty list when FTS5 is available but had nothing to match.
    """

    raw = (query or "").strip()
    if not raw:
        return None
    # Regex queries (``/foo/``) and case-sensitive scans need the
    # python lane; FTS5 is token/prefix-based.
    if raw.startswith("/") and raw.endswith("/") and len(raw) > 2:
        return None
    index = session_search_fts.maybe_open_index(paths)
    if index is None:
        return None
    try:
        index.ensure_fresh(paths, journals=journals)
        rows = index.search(
            raw,
            journals=journals,
            session_id=session_id,
            strategy_id=strategy_id,
            limit=limit,
        )
        return rows
    except Exception:
        # Defensive: never let FTS5 break the search surface.
        return None
    finally:
        index.close()


def search(
    paths,
    query: str,
    *,
    journals: Iterable[str] = _DEFAULT_JOURNALS,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    limit: int = 50,
    case_sensitive: bool = False,
    use_fts: bool = True,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` matching events newest-first.

    ``query`` is a plain substring (or regex if surrounded by
    ``/.../``). Filters apply *before* the substring match so a
    session-scoped search of a unique skill name is fast.

    When ``use_fts`` is true (the default) and SQLite ships FTS5,
    this consults the SQLite mirror first; if FTS5 returns hits we
    use those, otherwise we fall back to the in-memory streaming
    scan. The streaming scan also handles regex (``/foo/``) and
    case-sensitive queries that FTS5 cannot serve.
    """

    if use_fts and not case_sensitive:
        rows = _fts_search(
            paths,
            query,
            journals=journals,
            session_id=session_id,
            strategy_id=strategy_id,
            limit=limit,
        )
        if rows:
            return rows[:limit]

    raw = (query or "").strip()
    if raw.startswith("/") and raw.endswith("/") and len(raw) > 2:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern: Optional[re.Pattern[str]] = re.compile(raw[1:-1], flags)
        except re.error:
            pattern = None
        needle = raw[1:-1]
    else:
        pattern = None
        needle = raw if case_sensitive else raw.lower()

    events = _all_events(paths, journals)
    matched: list[SessionEvent] = []
    for ev in reversed(events):  # newest journals append at the bottom
        if session_id and ev.session_id != session_id:
            continue
        if strategy_id and ev.strategy_id != strategy_id:
            continue
        haystack = ev.text if case_sensitive else ev.text.lower()
        if pattern is not None:
            if not pattern.search(ev.text):
                continue
        elif needle and needle not in haystack:
            continue
        matched.append(ev)
        if len(matched) >= limit:
            break
    return [ev.to_dict() for ev in matched]


def recent_events(
    paths,
    *,
    session_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    journals: Iterable[str] = _DEFAULT_JOURNALS,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Most-recent N events for a session/strategy (no text query)."""

    events = _all_events(paths, journals)
    rows: list[SessionEvent] = []
    for ev in reversed(events):
        if session_id and ev.session_id != session_id:
            continue
        if strategy_id and ev.strategy_id != strategy_id:
            continue
        rows.append(ev)
        if len(rows) >= limit:
            break
    return [ev.to_dict() for ev in rows]


def session_transcript(
    paths,
    *,
    session_id: str,
    per_msg_cap: int = 12_000,
    max_pairs: int = 200,
) -> list[dict[str, Any]]:
    """Reconstruct a chat-shaped transcript for a session.

    Walks the agent journal and pairs each ``agent.turn.start`` (which
    records the user prompt) with the matching ``agent.turn.end`` (which
    records the assistant final reply). Returns a flat
    ``[{role, content, turn_id, ts}, ...]`` list, sorted chronologically.

    This is the one place the dashboard reads to merge runtime sessions
    (curl, gateway, scripted runs) into the chat sidebar so that every
    conversation produced by the agent — regardless of who triggered it
    — is visible to the operator.
    """
    if not session_id:
        return []
    journal = paths.journal("agent")
    if not journal.exists():
        return []
    starts: dict[str, dict[str, Any]] = {}
    ends: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for ev in _iter_journal(journal, "agent"):
        if ev.session_id != session_id:
            continue
        row = ev.payload
        kind = row.get("kind")
        tid = row.get("turn_id")
        if not tid:
            continue
        tid = str(tid)
        if kind == "agent.turn.start":
            user_text = row.get("user_text")
            if isinstance(user_text, str) and user_text:
                starts[tid] = {
                    "content": user_text[:per_msg_cap],
                    "ts": str(row.get("ts") or ""),
                }
                if tid not in order:
                    order.append(tid)
        elif kind == "agent.turn.end":
            final_text = row.get("final_text")
            if isinstance(final_text, str) and final_text:
                ends[tid] = {
                    "content": final_text[:per_msg_cap],
                    "ts": str(row.get("ts") or ""),
                }
    ordered = [t for t in order if t in starts]
    if max_pairs > 0 and len(ordered) > max_pairs:
        ordered = ordered[-max_pairs:]
    out: list[dict[str, Any]] = []
    for tid in ordered:
        u = starts[tid]
        out.append({
            "role": "user",
            "content": u["content"],
            "turn_id": tid,
            "ts": u["ts"],
        })
        a = ends.get(tid)
        if a and a.get("content"):
            out.append({
                "role": "assistant",
                "content": a["content"],
                "turn_id": tid,
                "ts": a["ts"],
            })
    return out


__all__ = ["SessionEvent", "search", "recent_events", "session_transcript"]

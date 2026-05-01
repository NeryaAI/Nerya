"""Long-term structured memory index.

The original ``nerya.agent.memory.Memory`` API is great for "append a
dated note to a markdown file and grab the tail" but it has zero query
shape. The 2026-04-25 prompt-battery surfaced the resulting failure
mode: when the operator says "你还记得我之前告诉过你哪些关键偏好吗?"
the LLM had to call ``recall`` repeatedly with no signal of what to
look for, and ended up returning either an empty preview or a
fabricated list.

This module adds a small, dependency-free **fact index** that lives at
``<workspace>/memory/index.jsonl``. Every line is a JSON record with:

* ``ts``        ISO-8601 UTC stamp.
* ``scope``     ``"global"`` or ``"strategy"``.
* ``file``      whitelisted markdown file the fact was also written to
                (``global.md`` / ``mistakes.md`` / ``market_regimes.md``
                / ``skill_learnings.md``), or ``""`` for strategy scope.
* ``strategy_id`` strategy id when ``scope == "strategy"``.
* ``key``       short stable key (e.g. ``"trading.preferred_horizon"``)
                — optional but recommended.
* ``value``     the actual content (free-form string).
* ``tags``      list of normalised lower-case tag strings.
* ``source_turn`` opaque turn id supplied by the caller; lets us trace
                facts back to the conversation that produced them.

Reads are line-oriented, sorted newest-first, with a substring +
keyword filter in :meth:`MemoryIndex.search`. We deliberately avoid
embeddings / SQLite for now: the index is small (operator-scale,
not corpus-scale) and the goal is to make ``recall`` return the right
fact with one call rather than multi-pass guessing.

The append path is **idempotent on (key, scope, strategy_id)**: when a
caller supplies ``key`` we mark all earlier records with the same key
as superseded so :meth:`search` and :meth:`recent` only see the latest
value. Records are never physically deleted (so the markdown trail and
the index stay in sync); the supersession is a logical
``superseded: true`` flag on the older lines.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from ..core.paths import WorkspacePaths

_TAG_SPLIT = re.compile(r"[\s,;]+")
_KEYWORD_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalise_tags(tags: Iterable[str] | None) -> list[str]:
    if not tags:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not raw:
            continue
        for piece in _TAG_SPLIT.split(str(raw).strip().lower()):
            piece = piece.strip("#@,;: ")
            if piece and piece not in seen:
                seen.add(piece)
                out.append(piece)
    return out


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    return {m.group(0).lower() for m in _KEYWORD_RE.finditer(text)}


@dataclass
class FactRecord:
    ts: str
    scope: str
    file: str
    strategy_id: str
    key: str
    value: str
    tags: list[str] = field(default_factory=list)
    source_turn: str = ""
    superseded: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> "FactRecord":
        return cls(
            ts=str(raw.get("ts") or ""),
            scope=str(raw.get("scope") or "global"),
            file=str(raw.get("file") or ""),
            strategy_id=str(raw.get("strategy_id") or ""),
            key=str(raw.get("key") or ""),
            value=str(raw.get("value") or ""),
            tags=list(raw.get("tags") or []),
            source_turn=str(raw.get("source_turn") or ""),
            superseded=bool(raw.get("superseded") or False),
        )

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "scope": self.scope,
            "file": self.file,
            "strategy_id": self.strategy_id,
            "key": self.key,
            "value": self.value,
            "tags": self.tags,
            "source_turn": self.source_turn,
            "superseded": self.superseded,
        }


@dataclass
class MemoryIndex:
    paths: WorkspacePaths

    @property
    def _path(self) -> Path:
        return self.paths.memory_index

    # ------------------------------------------------------------------ io
    def _ensure_dir(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read_all(self) -> list[FactRecord]:
        path = self._path
        if not path.exists():
            return []
        out: list[FactRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Bad lines are ignored, never raised: the index is a
                # best-effort log, never a single source of truth.
                continue
            if isinstance(obj, dict):
                out.append(FactRecord.from_dict(obj))
        return out

    def _rewrite(self, records: list[FactRecord]) -> None:
        self._ensure_dir()
        with self._path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")

    def _append_line(self, rec: FactRecord) -> None:
        self._ensure_dir()
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- write
    def remember(
        self,
        *,
        value: str,
        scope: str = "global",
        file: str = "",
        strategy_id: str = "",
        key: str = "",
        tags: Iterable[str] | None = None,
        source_turn: str = "",
        ts: Optional[str] = None,
    ) -> FactRecord:
        """Append a fact and (optionally) supersede prior facts on the same key.

        ``key`` is the stable identifier callers use to *update* a fact:
        if you call ``remember(key="trading.max_leverage", value="3x")``
        and later ``remember(key="trading.max_leverage", value="2x")``,
        the older record is flipped to ``superseded=True`` so
        :meth:`current` returns only the latest.
        """
        scope_norm = (scope or "global").strip().lower()
        if scope_norm not in {"global", "strategy"}:
            scope_norm = "global"
        rec = FactRecord(
            ts=ts or _now_utc(),
            scope=scope_norm,
            file=(file or "").strip(),
            strategy_id=(strategy_id or "").strip(),
            key=(key or "").strip(),
            value=(value or "").strip(),
            tags=_normalise_tags(tags),
            source_turn=(source_turn or "").strip(),
            superseded=False,
        )
        if not rec.value:
            raise ValueError("MemoryIndex.remember requires a non-empty value")

        if rec.key:
            # Atomic-ish supersede: read all, mark matches as superseded,
            # rewrite, then append the new record. The window is small so
            # this is fine for operator-scale indices (<10k entries).
            existing = self._read_all()
            changed = False
            for prior in existing:
                if (
                    not prior.superseded
                    and prior.key == rec.key
                    and prior.scope == rec.scope
                    and prior.strategy_id == rec.strategy_id
                ):
                    prior.superseded = True
                    changed = True
            if changed:
                self._rewrite(existing)
        self._append_line(rec)
        return rec

    # ----------------------------------------------------------------- read
    def all_records(
        self, *, include_superseded: bool = False
    ) -> list[FactRecord]:
        records = self._read_all()
        if include_superseded:
            return records
        return [r for r in records if not r.superseded]

    def current(self) -> list[FactRecord]:
        """Latest non-superseded facts, newest first."""
        records = self.all_records()
        records.sort(key=lambda r: r.ts, reverse=True)
        return records

    def recent(self, *, limit: int = 20) -> list[FactRecord]:
        return self.current()[: max(0, int(limit))]

    def search(
        self,
        *,
        query: str = "",
        tags: Iterable[str] | None = None,
        key_prefix: str = "",
        scope: str = "",
        strategy_id: str = "",
        limit: int = 10,
        include_superseded: bool = False,
    ) -> list[FactRecord]:
        """Substring + keyword + tag filter over the index.

        - ``query``: tokenised case-insensitively and matched against
          ``key + " " + value + " " + tags`` of each record. Records
          matching the most query tokens rank first; ties broken by
          newest ``ts``.
        - ``tags``: every supplied tag must be present on the record.
        - ``key_prefix``: e.g. ``"trading."`` returns only
          ``trading.*`` keys.
        - ``scope`` / ``strategy_id``: strict equality filter when set.
        """
        records = self.all_records(include_superseded=include_superseded)
        wanted_tags = set(_normalise_tags(tags))
        scope_filter = (scope or "").strip().lower()
        sid_filter = (strategy_id or "").strip()
        prefix = (key_prefix or "").strip()
        q_tokens = _tokens(query)

        scored: list[tuple[int, str, FactRecord]] = []
        for rec in records:
            if scope_filter and rec.scope != scope_filter:
                continue
            if sid_filter and rec.strategy_id != sid_filter:
                continue
            if prefix and not rec.key.startswith(prefix):
                continue
            if wanted_tags and not wanted_tags.issubset(set(rec.tags)):
                continue
            score = 0
            if q_tokens:
                hay = _tokens(
                    f"{rec.key} {rec.value} {' '.join(rec.tags)}"
                )
                score = len(q_tokens & hay)
                if score == 0:
                    # When the caller supplied a query, require at least
                    # one matching token. Otherwise the result set is
                    # noise (every record).
                    continue
            scored.append((score, rec.ts, rec))

        # Sort: higher score first, then newer ts first.
        scored.sort(key=lambda t: (-t[0], -self._ts_rank(t[1])))
        return [rec for _, _, rec in scored[: max(0, int(limit))]]

    @staticmethod
    def _ts_rank(ts: str) -> float:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    # --------------------------------------------------------- maintenance
    def forget(
        self,
        *,
        key: str = "",
        scope: str = "global",
        strategy_id: str = "",
    ) -> int:
        """Mark a fact (matched by key) as superseded without writing a new value.

        Returns the number of records flipped. Used by an explicit
        ``forget`` action and by tests; physical removal is intentionally
        not exposed.
        """
        if not key:
            return 0
        scope_norm = (scope or "global").strip().lower()
        sid = (strategy_id or "").strip()
        records = self._read_all()
        flipped = 0
        for rec in records:
            if (
                not rec.superseded
                and rec.key == key
                and rec.scope == scope_norm
                and rec.strategy_id == sid
            ):
                rec.superseded = True
                flipped += 1
        if flipped:
            self._rewrite(records)
        return flipped

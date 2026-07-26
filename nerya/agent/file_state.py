"""FileStateCache — fresh-read tracking for file edits.

  "fresh read before edit" invariant that the file_first lane enforces).

Why
---
Every coding environment has the same pathology: the model edits a file
based on stale context (its own
memory of what the file looked like a few turns ago), the on-disk copy
has since been changed by the user / a sibling tool / git, and the
edit either silently overwrites the user's work or fails because the
``find`` block no longer matches.

The fix everyone converges on is the same: track *for this turn* which
files the agent has read, hash the bytes it saw, and refuse any edit
whose target is either (a) unread or (b) read at a stale hash. The
agent must always run ``operator.read_file`` on a file before editing
it, and re-run if the disk has moved underneath.

Scope
-----
``FileStateCache`` is per-turn, in-memory state. Each entry tracks:

- ``content_hash`` — sha256 of the bytes the agent last saw on disk.
- ``last_read_seq`` — monotonic counter so we can answer "did I read
  this *after* my last write?".
- ``last_read_at`` — wall-clock timestamp for evidence bundles.
- ``last_write_seq`` — bumped whenever an edit succeeds.

The cache lives on the per-turn agent context so the kernel/operator
skill/workspace-native loop all share one view. Cross-turn carryover
is intentional: the user can keep editing the same file across many
turns without having to re-read it on every loop, *as long as* nothing
else has touched it. We re-validate by re-hashing on the way into the
edit; a stale hash trips ``StaleFileReadError`` and the agent must
``read_file`` again.

This module is deliberately tiny + deps-free so the kernel, skills
runtime, and tests can all import it without ordering hazards.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


__all__ = [
    "FileStateEntry",
    "FileStateCache",
    "StaleFileReadError",
    "compute_file_hash",
]


def compute_file_hash(content: str | bytes) -> str:
    """Return the sha256 hex digest of ``content``.

    We hash the *bytes the agent saw* — i.e. the post-decode UTF-8
    payload for text and the raw bytes for binaries. The same helper
    is used by the operator skill on read and on the way into an edit
    so both ends of the contract use one canonical digest.
    """

    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


@dataclass
class FileStateEntry:
    """One row in :class:`FileStateCache`.

    ``content_hash`` is the digest of the bytes the agent saw on its
    last successful read. ``last_read_seq`` and ``last_write_seq`` are
    monotonic counters minted by the cache; ``last_read_seq`` >
    ``last_write_seq`` means "the agent has observed every write it
    has made", which is the precondition the file_first lane requires
    before allowing a follow-up edit.
    """

    path: str
    content_hash: str = ""
    last_read_seq: int = 0
    last_read_at: float = 0.0
    last_write_seq: int = 0
    last_write_at: float = 0.0
    bytes_seen: int = 0
    line_count: int = 0
    truncated: bool = False

    def is_fresh(self) -> bool:
        """True if the read sequence covers every write so far."""

        return self.last_read_seq >= self.last_write_seq


class StaleFileReadError(Exception):
    """Raised when an edit targets a file that was not freshly read.

    Carries enough metadata for the kernel's error_recovery taxonomy
    to translate the failure into a user-facing observation that
    points the agent at the recovery action (call ``read_file`` again
    on this exact path).
    """

    def __init__(
        self,
        path: str,
        *,
        reason: str,
        expected_hash: Optional[str] = None,
        actual_hash: Optional[str] = None,
    ) -> None:
        self.path = path
        self.reason = reason
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        super().__init__(f"stale read on {path}: {reason}")

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "stale_file_read",
            "path": self.path,
            "reason": self.reason,
            "expected_hash": self.expected_hash,
            "actual_hash": self.actual_hash,
            "recovery": (
                f"call operator.read_file path={self.path!r} again before retrying the edit"
            ),
        }


class FileStateCache:
    """Per-turn fresh-read tracker. Thread-safe.

    The kernel constructs one cache per turn (or per session, when
    the runtime mode wants long-running file context) and passes it
    into both the file primitives in the operator skill and the
    workspace-native agent loop.
    """

    def __init__(self) -> None:
        self._entries: dict[str, FileStateEntry] = {}
        self._lock = threading.RLock()
        self._read_seq = 0
        self._write_seq = 0

    # ---- normalisation -----------------------------------------------------

    @staticmethod
    def _key(path: str | Path) -> str:
        """Canonicalise ``path`` so case / separators / dot-segments
        cannot smuggle the same file into the cache twice."""

        if isinstance(path, Path):
            p = path
        else:
            p = Path(str(path))
        try:
            p = p.resolve()
        except OSError:
            p = p.absolute()
        return str(p)

    # ---- read tracking -----------------------------------------------------

    def record_read(
        self,
        path: str | Path,
        *,
        content: str | bytes,
        bytes_seen: int | None = None,
        line_count: int | None = None,
        truncated: bool = False,
    ) -> FileStateEntry:
        """Note that ``path`` was read; remember its hash + counters."""

        key = self._key(path)
        digest = compute_file_hash(content)
        with self._lock:
            self._read_seq += 1
            entry = self._entries.get(key) or FileStateEntry(path=key)
            entry.path = key
            entry.content_hash = digest
            entry.last_read_seq = self._read_seq
            entry.last_read_at = time.time()
            entry.bytes_seen = (
                bytes_seen
                if bytes_seen is not None
                else len(content) if isinstance(content, (str, bytes)) else 0
            )
            entry.line_count = line_count if line_count is not None else 0
            entry.truncated = truncated
            self._entries[key] = entry
            return entry

    def get(self, path: str | Path) -> Optional[FileStateEntry]:
        with self._lock:
            return self._entries.get(self._key(path))

    # ---- edit precondition --------------------------------------------------

    def assert_fresh_for_edit(
        self,
        path: str | Path,
        *,
        on_disk: str | bytes,
        require_read: bool = True,
    ) -> FileStateEntry:
        """Refuse the edit unless the agent has a fresh read of ``path``.

        Caller passes the *current* on-disk content (``on_disk``); we
        re-hash it and compare to the digest captured at last read. A
        mismatch means the file has moved underneath the agent and we
        require a re-read. ``require_read=True`` (default) also
        refuses paths the agent has never read.
        """

        key = self._key(path)
        actual = compute_file_hash(on_disk)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                if require_read:
                    raise StaleFileReadError(
                        key,
                        reason="file has not been read in this session; "
                        "edits require a prior read so the agent operates "
                        "on the current content",
                    )
                return FileStateEntry(path=key, content_hash=actual)
            if entry.content_hash and entry.content_hash != actual:
                raise StaleFileReadError(
                    key,
                    reason="file changed on disk since it was last read",
                    expected_hash=entry.content_hash,
                    actual_hash=actual,
                )
            return entry

    # ---- write tracking -----------------------------------------------------

    def record_write(
        self,
        path: str | Path,
        *,
        new_content: str | bytes,
        bytes_written: int | None = None,
        line_count: int | None = None,
    ) -> FileStateEntry:
        """Note a successful edit; refresh hash so we don't trip on
        our own write next time."""

        key = self._key(path)
        digest = compute_file_hash(new_content)
        with self._lock:
            self._write_seq += 1
            self._read_seq += 1
            entry = self._entries.get(key) or FileStateEntry(path=key)
            entry.path = key
            entry.content_hash = digest
            entry.last_write_seq = self._write_seq
            entry.last_write_at = time.time()
            entry.last_read_seq = self._read_seq
            entry.last_read_at = entry.last_write_at
            entry.bytes_seen = (
                bytes_written
                if bytes_written is not None
                else len(new_content) if isinstance(new_content, (str, bytes)) else 0
            )
            entry.line_count = line_count if line_count is not None else 0
            entry.truncated = False
            self._entries[key] = entry
            return entry

    # ---- introspection ------------------------------------------------------

    def snapshot(self) -> list[dict[str, object]]:
        """Return a JSON-friendly snapshot for evidence bundles + tests."""

        with self._lock:
            return [
                {
                    "path": e.path,
                    "content_hash": e.content_hash,
                    "last_read_seq": e.last_read_seq,
                    "last_read_at": e.last_read_at,
                    "last_write_seq": e.last_write_seq,
                    "last_write_at": e.last_write_at,
                    "bytes_seen": e.bytes_seen,
                    "line_count": e.line_count,
                    "truncated": e.truncated,
                    "is_fresh": e.is_fresh(),
                }
                for e in self._entries.values()
            ]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._read_seq = 0
            self._write_seq = 0

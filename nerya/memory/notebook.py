"""Curated agent notebook stored in AGENT.md / OPERATOR.md.

Two bounded plain-text files that get injected verbatim into the LLM
system prompt:

* ``AGENT.md``    — agent's own working notes (environment quirks,
                    project conventions, things learned from a tool
                    output that should survive a context compression).
* ``OPERATOR.md`` — what the agent has learned about the human operator
                    (preferences, communication style, policy
                    decisions, "always confirm before X").

The store is intentionally **char-bounded** (not token-bounded) so it
behaves identically across model families. Once the limit is reached
the agent must replace or remove an older entry before it can add a
new one — that pressure forces curation instead of unbounded growth.

### Frozen-snapshot pattern

When :class:`MemoryNotebook.load` runs at session start it captures a
``_system_prompt_snapshot`` per target. Subsequent :meth:`add` /
:meth:`replace` / :meth:`remove` calls update both the in-memory
entries and the on-disk file atomically, but the snapshot **does not
change** until the next session. Reason: the system-prompt block is
byte-stable across all turns, which keeps the LLM provider's prefix
cache hot and avoids re-tokenisation cost on every turn. The live
state is still surfaced through tool responses so the agent always
sees what it just wrote.

### Concurrency

Each target file has a sibling ``.lock`` file used purely for
mutual exclusion (so atomic ``os.replace()`` of the data file still
works). Locking uses ``msvcrt`` on Windows and ``fcntl`` elsewhere.
Read paths don't lock — atomic rename means a concurrent reader
always sees either the old or the new complete file, never a
half-written one.

### Defence in depth

Every :meth:`add` / :meth:`replace` runs the content through
:func:`nerya.memory.content_scanner.scan_memory_content` first. If the
content matches a known prompt-injection / exfiltration pattern it is
rejected with a human-readable error. This is critical because the
notebook is one of the few writable surfaces that lands directly in
the system prompt.

The file format is intentionally simple so prompt assembly, tooling,
and manual edits stay predictable across sessions.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .content_scanner import scan_memory_content


_LOG = logging.getLogger(__name__)


# Cross-platform file locking. ``fcntl`` exists on POSIX, ``msvcrt`` on
# Windows; we only need *exclusive* locking so the simplest API on each
# platform is fine. When neither exists (extremely rare) the lock is a
# no-op and the atomic temp-file + ``os.replace`` write still keeps the
# data file consistent — only the read-modify-write window inside a
# single process is unprotected, which is acceptable for a single-user
# workspace tool.
_fcntl: Any
_msvcrt: Any
try:  # pragma: no cover - platform branch
    import fcntl as _fcntl
    _msvcrt = None
except ImportError:  # pragma: no cover - platform branch
    _fcntl = None
    try:
        import msvcrt as _msvcrt
    except ImportError:
        _msvcrt = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


ENTRY_DELIMITER: str = "\n§\n"
"""Inter-entry separator used for simple parsing and manual editing."""


VALID_TARGETS: tuple[str, ...] = ("agent", "operator")
"""Order matters — used as iteration order in ``snapshot_blocks``."""


_TARGET_FILES: dict[str, str] = {
    "agent": "AGENT.md",
    "operator": "OPERATOR.md",
}


_TARGET_HEADERS: dict[str, str] = {
    "agent": "AGENT NOTEBOOK (your personal notes, frozen at session start)",
    "operator": "OPERATOR PROFILE (what you know about the operator, frozen at session start)",
}


# Defaults keep both blocks comfortably small even on conservative tokenisers.
DEFAULT_AGENT_LIMIT: int = 2200
DEFAULT_OPERATOR_LIMIT: int = 1375


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotebookResult:
    """Return value from every ``add`` / ``replace`` / ``remove`` call.

    Stable across the public API so the dashboard + the writer + tests
    can rely on the same shape regardless of the operation. ``ok`` is
    the single source of truth for success; ``error`` is set iff
    ``ok`` is ``False``.
    """

    ok: bool
    target: str
    entries: tuple[str, ...]
    used_chars: int
    char_limit: int
    message: str = ""
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render to a plain dict for JSON-serialisable API responses."""
        out: dict[str, Any] = {
            "ok": self.ok,
            "target": self.target,
            "entries": list(self.entries),
            "used_chars": int(self.used_chars),
            "char_limit": int(self.char_limit),
            "usage_pct": (
                int(self.used_chars / self.char_limit * 100)
                if self.char_limit > 0 else 0
            ),
        }
        if self.message:
            out["message"] = self.message
        if self.error:
            out["error"] = self.error
        if self.extra:
            out["extra"] = dict(self.extra)
        return out


# ---------------------------------------------------------------------------
# MemoryNotebook
# ---------------------------------------------------------------------------


class MemoryNotebook:
    """Bounded curated text store with frozen-snapshot semantics.

    One instance per workspace. Construct with ``MemoryNotebook(root_dir)``
    where ``root_dir`` is the workspace memory dir (typically
    ``<workspace>/memory/notebook``). Call :meth:`load` once at session
    start to populate the snapshot.
    """

    def __init__(
        self,
        root_dir: Path,
        *,
        agent_char_limit: int = DEFAULT_AGENT_LIMIT,
        operator_char_limit: int = DEFAULT_OPERATOR_LIMIT,
    ) -> None:
        self._root = Path(root_dir)
        self._limits: dict[str, int] = {
            "agent": int(agent_char_limit),
            "operator": int(operator_char_limit),
        }
        self._entries: dict[str, list[str]] = {"agent": [], "operator": []}
        self._snapshot: dict[str, str] = {"agent": "", "operator": ""}

    # -- lifecycle -----------------------------------------------------

    def load(self) -> None:
        """Read both files from disk and capture the system-prompt snapshot.

        Idempotent — calling :meth:`load` twice (e.g. on session restart)
        re-reads the disk and re-captures the snapshot.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        for target in VALID_TARGETS:
            entries = self._read_file(self._path_for(target))
            self._entries[target] = list(dict.fromkeys(entries))  # dedupe, keep order
            self._snapshot[target] = self._render_block(target, self._entries[target])

    # -- introspection -------------------------------------------------

    def entries(self, target: str) -> tuple[str, ...]:
        """Return the **live** entries for ``target`` (not the snapshot)."""
        self._require_target(target)
        return tuple(self._entries[target])

    def used_chars(self, target: str) -> int:
        """Char count of the current live entries for ``target``."""
        self._require_target(target)
        ents = self._entries[target]
        if not ents:
            return 0
        return len(ENTRY_DELIMITER.join(ents))

    def char_limit(self, target: str) -> int:
        self._require_target(target)
        return self._limits[target]

    def snapshot_block(self, target: str) -> str:
        """Return the **frozen** system-prompt block captured by :meth:`load`.

        Returns ``""`` if the notebook was empty at load time. Callers
        should concatenate the two snapshot blocks (agent + operator)
        when assembling the system prompt.
        """
        self._require_target(target)
        return self._snapshot[target]

    def snapshot_blocks(self) -> dict[str, str]:
        """Return both snapshots (``agent`` and ``operator``) as a dict."""
        return dict(self._snapshot)

    # -- mutations -----------------------------------------------------

    def add(self, target: str, content: str) -> NotebookResult:
        """Append ``content`` as a new entry on ``target``.

        Rejects empty content, exact-duplicate content, content matching
        a threat pattern, and content that would push the file past
        :meth:`char_limit`.
        """
        self._require_target(target)
        body = (content or "").strip()
        if not body:
            return self._error(target, "Content cannot be empty.")

        scan_err = scan_memory_content(body)
        if scan_err is not None:
            return self._error(target, scan_err)

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)
            entries = self._entries[target]

            if body in entries:
                return self._success(
                    target, "Entry already exists (no duplicate added).",
                )

            tentative = entries + [body]
            new_total = len(ENTRY_DELIMITER.join(tentative))
            limit = self._limits[target]
            if new_total > limit:
                return self._error(
                    target,
                    (
                        f"Notebook at {self.used_chars(target):,}/{limit:,} chars. "
                        f"Adding this entry ({len(body)} chars) would exceed the "
                        "limit. Replace or remove an older entry first."
                    ),
                )

            entries.append(body)
            self._entries[target] = entries
            self._save(target)

        return self._success(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> NotebookResult:
        """Replace the unique entry containing ``old_text`` with ``new_content``."""
        self._require_target(target)
        needle = (old_text or "").strip()
        body = (new_content or "").strip()
        if not needle:
            return self._error(target, "old_text cannot be empty.")
        if not body:
            return self._error(
                target,
                "new_content cannot be empty. Use 'remove' to delete entries.",
            )

        scan_err = scan_memory_content(body)
        if scan_err is not None:
            return self._error(target, scan_err)

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)
            entries = self._entries[target]
            matches = [(i, e) for i, e in enumerate(entries) if needle in e]
            if not matches:
                return self._error(target, f"No entry matched {needle!r}.")
            if len(matches) > 1 and len({e for _, e in matches}) > 1:
                previews = [
                    (e[:80] + ("..." if len(e) > 80 else "")) for _, e in matches
                ]
                return self._error(
                    target,
                    f"Multiple distinct entries matched {needle!r}. Be more specific.",
                    extra={"matches": previews},
                )

            idx = matches[0][0]
            tentative = list(entries)
            tentative[idx] = body
            new_total = len(ENTRY_DELIMITER.join(tentative))
            limit = self._limits[target]
            if new_total > limit:
                return self._error(
                    target,
                    (
                        f"Replacement would put notebook at {new_total:,}/{limit:,} "
                        "chars. Shorten the new content or remove other entries first."
                    ),
                )

            self._entries[target] = tentative
            self._save(target)

        return self._success(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> NotebookResult:
        """Remove the unique entry containing ``old_text``."""
        self._require_target(target)
        needle = (old_text or "").strip()
        if not needle:
            return self._error(target, "old_text cannot be empty.")

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)
            entries = self._entries[target]
            matches = [(i, e) for i, e in enumerate(entries) if needle in e]
            if not matches:
                return self._error(target, f"No entry matched {needle!r}.")
            if len(matches) > 1 and len({e for _, e in matches}) > 1:
                previews = [
                    (e[:80] + ("..." if len(e) > 80 else "")) for _, e in matches
                ]
                return self._error(
                    target,
                    f"Multiple distinct entries matched {needle!r}. Be more specific.",
                    extra={"matches": previews},
                )

            idx = matches[0][0]
            entries.pop(idx)
            self._entries[target] = entries
            self._save(target)

        return self._success(target, "Entry removed.")

    # -- internal ------------------------------------------------------

    def _path_for(self, target: str) -> Path:
        return self._root / _TARGET_FILES[target]

    def _require_target(self, target: str) -> None:
        if target not in VALID_TARGETS:
            raise ValueError(
                f"Unknown notebook target {target!r}; expected one of "
                f"{VALID_TARGETS!r}",
            )

    def _reload_target(self, target: str) -> None:
        fresh = self._read_file(self._path_for(target))
        self._entries[target] = list(dict.fromkeys(fresh))

    def _save(self, target: str) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries[target])

    def _success(self, target: str, message: str) -> NotebookResult:
        used = self.used_chars(target)
        return NotebookResult(
            ok=True,
            target=target,
            entries=tuple(self._entries[target]),
            used_chars=used,
            char_limit=self._limits[target],
            message=message,
        )

    def _error(
        self,
        target: str,
        error: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> NotebookResult:
        used = self.used_chars(target)
        return NotebookResult(
            ok=False,
            target=target,
            entries=tuple(self._entries[target]),
            used_chars=used,
            char_limit=self._limits[target],
            error=error,
            extra=dict(extra or {}),
        )

    def _render_block(self, target: str, entries: list[str]) -> str:
        if not entries:
            return ""
        content = ENTRY_DELIMITER.join(entries)
        used = len(content)
        limit = self._limits[target]
        pct = min(100, int(used / limit * 100)) if limit > 0 else 0
        header = f"{_TARGET_HEADERS[target]} [{pct}% — {used:,}/{limit:,} chars]"
        rule = "═" * 46
        return f"{rule}\n{header}\n{rule}\n{content}"

    # -- file IO -------------------------------------------------------

    @staticmethod
    @contextmanager
    def _file_lock(path: Path) -> Iterator[None]:
        """Cross-platform exclusive lock on a sibling ``.lock`` file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        if _fcntl is None and _msvcrt is None:
            yield
            return

        # ``msvcrt.locking`` requires at least one byte in the file.
        if _msvcrt is not None and (
            not lock_path.exists() or lock_path.stat().st_size == 0
        ):
            lock_path.write_text(" ", encoding="utf-8")

        mode = "r+" if _msvcrt is not None else "a+"
        fd = open(lock_path, mode, encoding="utf-8")
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_EX)
            else:
                fd.seek(0)
                _msvcrt.locking(fd.fileno(), _msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                if _fcntl is not None:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                elif _msvcrt is not None:
                    fd.seek(0)
                    _msvcrt.locking(fd.fileno(), _msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            fd.close()

    @staticmethod
    def _read_file(path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            _LOG.debug("notebook read failed", exc_info=True)
            return []
        if not raw.strip():
            return []
        return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]

    @staticmethod
    def _write_file(path: Path, entries: list[str]) -> None:
        """Atomic write — temp file + ``os.replace`` so readers never see a
        half-written notebook."""
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".notebook_", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


__all__ = [
    "MemoryNotebook",
    "NotebookResult",
    "VALID_TARGETS",
    "ENTRY_DELIMITER",
    "DEFAULT_AGENT_LIMIT",
    "DEFAULT_OPERATOR_LIMIT",
]

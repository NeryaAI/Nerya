"""Compatibility writer that delegates canonical writes to MemoryRuntime."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..agent.memory_index import MemoryIndex
from ..core.config import Config
from .activity import MemoryActivityEvent, MemoryActivityLog
from .notebook import MemoryNotebook
from .write_rules import (
    NOTEBOOK_CATEGORIES,
    load_write_rules,
)


__all__ = ["MemoryWriter", "MemoryWriteResult", "default_notebook"]


# ---------------------------------------------------------------------------
# Notebook helpers
# ---------------------------------------------------------------------------


def default_notebook(config: Config) -> MemoryNotebook:
    """Return the notebook bound to the active workspace.

    Centralised so the writer, the API routes, and any future agent
    plumbing all read from / write to the same on-disk files. The
    canonical location is ``<workspace>/memory/notebook/{AGENT,OPERATOR}.md``
    — kept under the workspace so a profile switch (different
    ``HERMES_HOME``-style env override) flows through automatically.
    """

    root = Path(config.paths.root) / "memory" / "notebook"
    nb = MemoryNotebook(root)
    nb.load()
    return nb


@dataclass
class MemoryWriteResult:
    ok: bool
    skipped: bool = False
    skip_reason: str = ""
    category: str = ""
    key: str = ""
    title: str = ""
    hash: str = ""
    fact_ts: str = ""
    target_files: list[str] = field(default_factory=list)


def _hash(category: str, content: str, key: str = "") -> str:
    h = hashlib.sha256()
    h.update(category.encode("utf-8"))
    h.update(b"::")
    h.update(key.encode("utf-8"))
    h.update(b"::")
    h.update(content.encode("utf-8"))
    return h.hexdigest()


@dataclass
class MemoryWriter:
    """Apply ``memory.write_rules`` and emit activity events on every write."""

    config: Config
    _notebook: MemoryNotebook | None = field(default=None, repr=False)

    @property
    def index(self) -> MemoryIndex:
        return MemoryIndex(paths=self.config.paths)

    @property
    def activity(self) -> MemoryActivityLog:
        return MemoryActivityLog(config=self.config)

    @property
    def notebook(self) -> MemoryNotebook:
        """Lazy-load the curated notebook bound to this workspace.

        Cached on the writer so repeated captures inside the same
        request don't re-read the on-disk files.
        """
        if self._notebook is None:
            self._notebook = default_notebook(self.config)
        return self._notebook

    def capture(
        self,
        *,
        category: str,
        content: str,
        title: str = "",
        key: str = "",
        tags: Iterable[str] | None = None,
        source: str = "",
        actor_id: str = "default",
        scope: str = "global",
        strategy_id: str = "",
        target_files: Iterable[str] | None = None,
    ) -> MemoryWriteResult:
        """Persist a memory entry honouring the write rules.

        Returns a :class:`MemoryWriteResult`. The caller can also
        inspect the activity log for the most recent event.
        """
        from .runtime import MemoryRuntime, MemoryScopeError

        rule = load_write_rules(self.config).get(category)
        result = MemoryWriteResult(
            ok=False,
            category=category,
            key=str(key or "").strip(),
            title=str(title or "").strip(),
        )
        if target_files is not None:
            result.skipped = True
            result.skip_reason = "target_override_forbidden"
            self.activity.append(MemoryActivityEvent.write_skipped(
                category=category,
                skip_reason=result.skip_reason,
                title=result.title, source=source, actor_id=actor_id,
            ))
            return result
        runtime = MemoryRuntime(
            self.config,
            actor_id=actor_id,
            strategy_id=strategy_id,
        )
        try:
            remembered = runtime.remember(
                category=category,
                content=content,
                title=result.title,
                key=result.key,
                tags=tags,
                source=source,
                writer_id="memory_writer",
                scope=scope,
            )
        except MemoryScopeError:
            result.skipped = True
            result.skip_reason = "invalid_scope"
            return result
        result.ok = remembered.ok
        result.skipped = remembered.skipped
        result.skip_reason = remembered.skip_reason
        if category in NOTEBOOK_CATEGORIES and result.skip_reason == "unsafe_content":
            result.skip_reason = "notebook_rejected"
        result.hash = _hash(category, str(content or "").strip(), key=result.key)
        record = remembered.record
        if record is not None:
            result.fact_ts = datetime.fromtimestamp(
                record.created_at, tz=timezone.utc,
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            result.target_files = (
                list(rule.target_files)
                if category in NOTEBOOK_CATEGORIES and rule is not None
                else list(record.target_files)
            )
        elif rule is not None:
            result.target_files = list(rule.target_files)
        return result

    def record_search(
        self,
        *,
        query: str,
        result_count: int,
        latency_ms: int = 0,
        source: str = "",
        actor_id: str = "default",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit a search event into the activity log.

        Called by the memsearch wrapper and the agent's recall path so
        the dashboard's "recent searches" stream reflects every query.
        """

        self.activity.append(MemoryActivityEvent.search(
            query=str(query or ""),
            result_count=int(result_count),
            latency_ms=int(latency_ms),
            source=source,
            actor_id=actor_id,
            extra=extra,
        ))

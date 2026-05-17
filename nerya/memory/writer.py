"""Single entry point for writing into Nerya's long-term memory.

Every caller (chat session, agent step, skill, evolution gate) goes
through :class:`MemoryWriter` so the write rules are honoured uniformly
and one activity event is emitted per attempt.

Sketch:

.. code-block:: python

    writer = MemoryWriter(config)

    writer.capture(
        category="learning",
        title="prefer 3-5d swing horizons",
        content="Operator says they reject scalping signals; only swing.",
        key="trading.preferred_horizon",
        tags=["preferences", "horizon"],
        source="chat:session_42",
    )

The writer:

1. Looks up the rule for ``category``. If ``enabled=false`` it logs
   a ``write_skipped`` event with ``skip_reason="disabled"`` and returns.
2. Computes a stable hash of ``content``. If ``dedupe=by_hash`` and the
   same hash already exists in the index, logs ``skip_reason="duplicate_hash"``.
3. If ``dedupe=by_key`` the supersession is handled by
   :class:`MemoryIndex.remember`.
4. Calls :meth:`MemoryIndex.remember` to append a fact (so vector
   search and the existing ``recall`` API see it).
5. Appends the same content to each ``target_files`` markdown surface
   so the optional memsearch watcher picks it up on the next change.
6. Enforces ``max_entries`` by superseding the oldest non-superseded
   record above the cap (does not physically delete).
7. Emits ``write_ok`` to the activity log.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..agent.memory_index import MemoryIndex
from ..core.config import Config
from .activity import MemoryActivityEvent, MemoryActivityLog
from .notebook import MemoryNotebook, NotebookResult
from .write_rules import (
    DEDUPE_STRATEGIES,
    MemoryWriteRule,
    NOTEBOOK_CATEGORIES,
    NOTEBOOK_TARGET_BY_CATEGORY,
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


def _find_existing_hash_record(
    index: MemoryIndex, *, category: str, content_hash: str
) -> bool:
    """Return True when an active fact with this hash exists.

    The hash is stored as a tag (``hash:<digest>``) so we don't need
    to extend the FactRecord schema. Superseded records are ignored.
    """

    tag = f"hash:{content_hash}"
    for rec in index.all_records(include_superseded=False):
        if tag in rec.tags and (not category or category in rec.tags):
            return True
    return False


def _enforce_max_entries(
    index: MemoryIndex, *, category: str, max_entries: int
) -> int:
    """Supersede oldest active records above the cap. Returns count flipped."""

    if max_entries <= 0:
        return 0
    actives = [
        r for r in index.all_records(include_superseded=False)
        if category in r.tags
    ]
    if len(actives) <= max_entries:
        return 0
    # Sort newest first; everything past max_entries is superseded.
    actives.sort(key=lambda r: r.ts, reverse=True)
    overflow = actives[max_entries:]
    flipped = 0
    if overflow:
        all_records = index._read_all()
        active_set = {(r.ts, r.value) for r in overflow}
        for rec in all_records:
            if (rec.ts, rec.value) in active_set and not rec.superseded:
                rec.superseded = True
                flipped += 1
        if flipped:
            index._rewrite(all_records)
    return flipped


def _append_to_markdown(workspace_root: Path, target: str, body: str) -> Path | None:
    """Append ``body`` to ``<workspace>/<target>``; return the file path."""

    rel = target.strip()
    if not rel:
        return None
    path = (workspace_root / rel).resolve()
    try:
        # Sandbox: target must live inside the workspace.
        path.relative_to(workspace_root.resolve())
    except ValueError:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(body)
        if not body.endswith("\n"):
            fh.write("\n")
    return path


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

        rules = load_write_rules(self.config)
        rule = rules.get(category)
        result = MemoryWriteResult(
            ok=False,
            category=category,
            key=str(key or "").strip(),
            title=str(title or "").strip(),
        )
        body = str(content or "").strip()
        if not body:
            result.skipped = True
            result.skip_reason = "empty_content"
            self.activity.append(MemoryActivityEvent.write_skipped(
                category=category, skip_reason="empty_content",
                title=result.title, source=source, actor_id=actor_id,
            ))
            return result

        if rule is None:
            result.skipped = True
            result.skip_reason = "unknown_category"
            self.activity.append(MemoryActivityEvent.write_skipped(
                category=category, skip_reason="unknown_category",
                title=result.title, source=source, actor_id=actor_id,
            ))
            return result
        if not rule.enabled:
            result.skipped = True
            result.skip_reason = "disabled"
            self.activity.append(MemoryActivityEvent.write_skipped(
                category=category, skip_reason="disabled",
                title=result.title, source=source, actor_id=actor_id,
            ))
            return result

        # ----- Notebook fast-path --------------------------------------
        # Notebook categories (``notebook_agent`` / ``notebook_operator``)
        # are stored verbatim in the curated MEMORY.md / USER.md-style
        # files. No fact-index entry, no markdown append; the notebook IS
        # the store. We still emit a write event so the dashboard
        # activity stream shows every notebook mutation.
        if category in NOTEBOOK_CATEGORIES:
            return self._capture_notebook(
                rule=rule,
                result=result,
                body=body,
                source=source,
                actor_id=actor_id,
            )

        content_hash = _hash(category, body, key=result.key)
        result.hash = content_hash

        if rule.dedupe == "by_hash" and _find_existing_hash_record(
            self.index, category=category, content_hash=content_hash
        ):
            result.skipped = True
            result.skip_reason = "duplicate_hash"
            self.activity.append(MemoryActivityEvent.write_skipped(
                category=category, skip_reason="duplicate_hash",
                title=result.title, hash=content_hash,
                source=source, actor_id=actor_id,
            ))
            return result

        # Append to the structured fact index. ``MemoryIndex.remember``
        # already supersedes prior facts on (key, scope, strategy_id)
        # so dedupe=by_key is implemented for free.
        merged_tags = list(tags or [])
        merged_tags.append(category)
        merged_tags.append(f"hash:{content_hash}")
        if rule.dedupe == "by_key" and not result.key:
            # by_key dedupe requires a key; without one we fall back to
            # by_hash semantics for this single write.
            if _find_existing_hash_record(
                self.index, category=category, content_hash=content_hash
            ):
                result.skipped = True
                result.skip_reason = "duplicate_hash"
                self.activity.append(MemoryActivityEvent.write_skipped(
                    category=category, skip_reason="duplicate_hash",
                    title=result.title, hash=content_hash,
                    source=source, actor_id=actor_id,
                ))
                return result

        target_overrides = list(target_files or rule.target_files)
        result.target_files = list(target_overrides)
        # Pick the first target file (if any) as the markdown surface
        # the fact records — the rest receive a copy of the content
        # too. ``file`` is a relative-to-workspace path; the index
        # remembers it so callers can trace facts back to the .md.
        primary_target = target_overrides[0] if target_overrides else ""

        rec = self.index.remember(
            value=body,
            scope=scope,
            file=primary_target,
            strategy_id=strategy_id,
            key=result.key,
            tags=merged_tags,
            source_turn=source,
        )
        result.ok = True
        result.fact_ts = rec.ts

        # Mirror to markdown so the existing memsearch watcher picks it
        # up. We tag each line with the category + key for traceability.
        workspace_root = self.config.paths.root
        block = _format_markdown_block(
            category=category,
            title=result.title,
            key=result.key,
            content=body,
            ts=rec.ts,
        )
        for target in target_overrides:
            _append_to_markdown(workspace_root, target, block)

        # Enforce max_entries (best-effort, supersession only).
        _enforce_max_entries(
            self.index, category=category, max_entries=rule.max_entries,
        )

        preview = body
        self.activity.append(MemoryActivityEvent.write_ok(
            category=category,
            key=result.key,
            title=result.title,
            preview=preview,
            hash=content_hash,
            source=source,
            actor_id=actor_id,
            extra={
                "target_files": list(target_overrides),
                "scope": scope,
                "strategy_id": strategy_id,
            },
        ))
        return result

    def _capture_notebook(
        self,
        *,
        rule: MemoryWriteRule,
        result: MemoryWriteResult,
        body: str,
        source: str,
        actor_id: str,
    ) -> MemoryWriteResult:
        """Route a ``notebook_*`` capture into :class:`MemoryNotebook`.

        Uses ``add()`` semantics — replace/remove on the curated store
        come through the dedicated ``/memory/notebook`` API, not the
        general capture path, because they need substring matching.
        """

        target = NOTEBOOK_TARGET_BY_CATEGORY.get(rule.category)
        if target is None:
            result.skipped = True
            result.skip_reason = "unknown_notebook_target"
            self.activity.append(MemoryActivityEvent.write_skipped(
                category=rule.category,
                skip_reason="unknown_notebook_target",
                title=result.title, source=source, actor_id=actor_id,
            ))
            return result

        nb_result: NotebookResult = self.notebook.add(target, body)
        # Same hash semantics as the markdown path so the activity log
        # stays consistent when the dashboard joins notebook + non-
        # notebook events on ``hash``.
        content_hash = _hash(rule.category, body, key=result.key)
        result.hash = content_hash
        result.target_files = list(rule.target_files)

        if not nb_result.ok:
            result.skipped = True
            # The notebook returns rich, human-readable errors; use them
            # as the skip reason so the dashboard can render verbatim.
            result.skip_reason = "notebook_rejected"
            self.activity.append(MemoryActivityEvent.write_skipped(
                category=rule.category,
                skip_reason="notebook_rejected",
                title=result.title,
                hash=content_hash,
                source=source,
                actor_id=actor_id,
                extra={
                    "notebook_target": target,
                    "notebook_error": nb_result.error,
                    "notebook_used_chars": nb_result.used_chars,
                    "notebook_char_limit": nb_result.char_limit,
                },
            ))
            return result

        result.ok = True
        result.fact_ts = ""
        self.activity.append(MemoryActivityEvent.write_ok(
            category=rule.category,
            key=result.key,
            title=result.title,
            preview=body,
            hash=content_hash,
            source=source,
            actor_id=actor_id,
            extra={
                "notebook_target": target,
                "notebook_used_chars": nb_result.used_chars,
                "notebook_char_limit": nb_result.char_limit,
                "notebook_entry_count": len(nb_result.entries),
                "target_files": list(rule.target_files),
            },
        ))
        # Auto-ingest a research-vault row so the operator can cite this
        # notebook entry later. Honors ``runtime.evidence_vault`` and
        # never raises.
        try:
            from ..evidence import autoingest as _evidence_autoingest

            class _ConfigClient:
                __slots__ = ("config",)

                def __init__(self, cfg) -> None:
                    self.config = cfg

            _evidence_autoingest.on_research_save(
                _ConfigClient(self.config),
                provider=str(rule.category),
                artifact_id=str(content_hash),
                title=str(result.title or rule.category),
                body=str(body or "")[:8000],
                tags=[
                    rule.category,
                    f"notebook_target:{target}",
                    f"source:{source}" if source else "",
                ],
            )
        except Exception:  # pragma: no cover - defensive
            pass
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


def _format_markdown_block(
    *,
    category: str,
    title: str,
    key: str,
    content: str,
    ts: str,
) -> str:
    parts = [f"\n## {title or '(memory)'}\n"]
    meta = [f"`{ts}`", f"`{category}`"]
    if key:
        meta.append(f"`key={key}`")
    parts.append(" · ".join(meta) + "\n\n")
    parts.append(content.strip() + "\n")
    return "".join(parts)

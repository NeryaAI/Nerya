"""One scoped interface for Nerya memory reads, writes, and lifecycle."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from ..core import jsonl
from ..core.config import Config
from .activity import MemoryActivityEvent, MemoryActivityLog
from .content_scanner import scan_memory_content
from .context_fence import build_memory_context_block, sanitize_context
from .notebook import MemoryNotebook
from .projection import GENERATED_PROJECTION_MARKER, MemoryProjection
from .store import MemoryRecord, MemoryScopeError, MemoryStore
from .write_rules import (
    NOTEBOOK_CATEGORIES,
    NOTEBOOK_TARGET_BY_CATEGORY,
    load_write_rules,
)


_MARKDOWN_SECTION_RE = re.compile(r"^##[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)
_METADATA_TOKEN_RE = re.compile(r"`([^`]+)`")


@dataclass(frozen=True)
class MemoryRememberResult:
    ok: bool
    skipped: bool = False
    skip_reason: str = ""
    record: MemoryRecord | None = None


@dataclass(frozen=True)
class MemoryContext:
    """Stable prompt prefix and query-dependent per-turn recall."""

    stable: str = ""
    dynamic: str = ""
    recalled: tuple[MemoryRecord, ...] = ()
    external_sources: tuple[str, ...] = ()


class MemoryRuntime:
    """Memory service bound to one trusted actor/session/strategy scope."""

    def __init__(
        self,
        config: Config,
        *,
        actor_id: str = "default",
        session_id: str = "",
        strategy_id: str = "",
    ) -> None:
        self.config = config
        self.actor_id = self._required_actor_id(actor_id)
        self._legacy_owner_actor = self._required_actor_id(
            str(config.get("memory.legacy_owner_actor", "default") or "default")
        )
        self.session_id = self._clean_id(session_id, "session_id")
        self.strategy_id = self._clean_id(strategy_id, "strategy_id")
        self._external_provider = self._build_external_provider()
        self.activity = MemoryActivityLog(config=config)
        self.store = MemoryStore(config.paths.db)
        self._import_legacy_index()
        self._import_legacy_markdown()
        self._projection = MemoryProjection(config, self.store)
        self._notebook = MemoryNotebook(config.paths.memory / "notebook")
        self._notebook.load()
        blocks = self._notebook.snapshot_blocks()
        self._stable_snapshot = "\n\n".join(
            block for block in blocks.values() if block
        ).strip()

    def remember(
        self,
        *,
        category: str,
        content: str,
        scope: str = "global",
        key: str = "",
        title: str = "",
        tags: Iterable[str] | None = None,
        source: str = "",
        source_turn_id: str = "",
        evidence_refs: Iterable[str] | None = None,
        writer_id: str = "runtime",
        confidence: float = 1.0,
        importance: float = 0.5,
    ) -> MemoryRememberResult:
        body = str(content or "").strip()
        category_name = str(category or "").strip()
        key_name = str(key or "").strip()
        title_text = str(title or "").strip()
        source_ref = str(source or "").strip()
        source_turn = str(source_turn_id or "").strip()
        writer_name = str(writer_id or "runtime").strip() or "runtime"
        tag_values = tuple(str(tag or "").strip() for tag in (tags or ()))
        evidence_values = tuple(
            str(reference or "").strip() for reference in (evidence_refs or ())
        )
        scanned_fields = (
            ("category", category_name),
            ("content", body),
            ("key", key_name),
            ("title", title_text),
            ("source", source_ref),
            ("source_turn_id", source_turn),
            ("writer_id", writer_name),
            *(("tag", value) for value in tag_values),
            *(("evidence_ref", value) for value in evidence_values),
        )
        unsafe = next(
            (
                (field, error)
                for field, value in scanned_fields
                if value and (error := scan_memory_content(value))
            ),
            None,
        )
        if unsafe is not None:
            unsafe_field, scan_error = unsafe
            return self._skipped(
                "",
                "unsafe_content",
                "",
                "",
                "[blocked unsafe memory content]",
                "",
                extra={
                    "scanner_error": scan_error,
                    "scanner_field": unsafe_field,
                },
            )
        if not body:
            return self._skipped(
                category_name,
                "empty_content",
                key_name,
                title_text,
                body,
                source_ref,
            )
        rule = load_write_rules(self.config).get(category_name)
        if rule is None:
            return self._skipped(
                category_name,
                "unknown_category",
                key_name,
                title_text,
                body,
                source_ref,
            )
        if not rule.enabled:
            return self._skipped(
                category_name,
                "disabled",
                key_name,
                title_text,
                body,
                source_ref,
            )
        if rule.category in NOTEBOOK_CATEGORIES:
            if str(scope or "").strip().lower() != "global":
                raise MemoryScopeError("notebook memory only supports global scope")
            target = NOTEBOOK_TARGET_BY_CATEGORY[rule.category]
            existing = self.store.active_by_key(
                actor_id=self.actor_id,
                scope="global",
                scope_id="",
                stable_key=key_name,
            )
            if existing is not None and existing.content == body:
                self._write_ok(
                    category_name,
                    key_name,
                    title_text,
                    body,
                    source_ref,
                    extra={"notebook_target": target, "duplicate": True},
                )
                return MemoryRememberResult(ok=True, record=existing)
            result = (
                self._notebook.replace(target, existing.content, body)
                if existing is not None
                else self._notebook.add(target, body)
            )
            if not result.ok:
                return self._skipped(
                    category_name,
                    "notebook_rejected",
                    key_name,
                    title_text,
                    body,
                    source_ref,
                    extra={"notebook_error": result.error},
                )
            try:
                stored = self.store.remember(
                    actor_id=self.actor_id,
                    writer_id=writer_name,
                    scope="global",
                    scope_id="",
                    strategy_id="",
                    session_id="",
                    category=rule.category,
                    content=body,
                    stable_key=key_name,
                    title=title_text,
                    tags=tag_values,
                    source_ref=source_ref,
                    source_turn_id=source_turn,
                    evidence_refs=evidence_values,
                    confidence=self._unit_interval(confidence, "confidence"),
                    importance=self._unit_interval(importance, "importance"),
                    retention_days=0,
                    max_entries=0,
                    dedupe=rule.dedupe,
                    target_files=(),
                )
            except BaseException:
                if existing is not None:
                    self._notebook.replace(target, body, existing.content)
                else:
                    self._notebook.remove(target, body)
                raise
            self._write_ok(
                category_name,
                key_name,
                title_text,
                body,
                source_ref,
                extra={
                    "notebook_target": target,
                    "memory_id": stored.record.memory_id,
                },
            )
            return MemoryRememberResult(ok=True, record=stored.record)
        scope_name, scope_id, strategy_id, session_id = self._resolve_scope(scope)
        if scope_name == "strategy":
            target_files = [f"strategies/{strategy_id}/learnings.md"]
        elif scope_name == "session":
            target_files = []
        else:
            target_files = rule.target_files
        stored = self.store.remember(
            actor_id=self.actor_id,
            writer_id=writer_name,
            scope=scope_name,
            scope_id=scope_id,
            strategy_id=strategy_id,
            session_id=session_id,
            category=rule.category,
            content=body,
            stable_key=key_name,
            title=title_text,
            tags=tag_values,
            source_ref=source_ref,
            source_turn_id=source_turn,
            evidence_refs=evidence_values,
            confidence=self._unit_interval(confidence, "confidence"),
            importance=self._unit_interval(importance, "importance"),
            retention_days=rule.retention_days,
            max_entries=rule.max_entries,
            dedupe=rule.dedupe,
            target_files=target_files,
        )
        if stored.created:
            projection_synced = self._sync_projection(source="runtime:remember")
            self._write_ok(
                category_name,
                key_name,
                title_text,
                body,
                source_ref,
                extra={
                    "memory_id": stored.record.memory_id,
                    "scope": scope_name,
                    "strategy_id": strategy_id,
                    "session_id": session_id,
                    "projection_synced": projection_synced,
                },
            )
        else:
            return self._skipped(
                category_name,
                stored.skip_reason,
                key_name,
                title_text,
                body,
                source_ref,
                record=stored.record,
            )
        return MemoryRememberResult(
            ok=True,
            record=stored.record,
        )

    def recall(
        self,
        query: str,
        *,
        scope: str = "visible",
        limit: int = 10,
    ) -> list[MemoryRecord]:
        scope_name = str(scope or "visible").strip().lower()
        if scope_name != "visible":
            scope_name = self._resolve_scope(scope_name)[0]
        started = time.monotonic()
        query_text = str(query or "").strip()
        recalled = self.store.recall(
            actor_id=self.actor_id,
            query=query_text,
            strategy_id=self.strategy_id,
            session_id=self.session_id,
            scope=scope_name,
            limit=limit,
        )
        records = list(recalled.records)
        if recalled.expired_count:
            self._sync_projection(source="runtime:recall_expiry")
        self._emit(
            MemoryActivityEvent.search(
                query=query_text,
                result_count=len(records),
                latency_ms=int((time.monotonic() - started) * 1000),
                source="runtime:recall",
                actor_id=self.actor_id,
                extra={
                    "scope": scope_name,
                    "strategy_id": self.strategy_id,
                    "session_id": self.session_id,
                    "expired": recalled.expired_count,
                    "query_hash": hashlib.sha256(
                        query_text.encode("utf-8")
                    ).hexdigest(),
                },
            )
        )
        return records

    def context(
        self,
        query: str,
        *,
        max_chars: int = 6000,
        limit: int = 10,
    ) -> MemoryContext:
        """Build stable and dynamic memory blocks under one hard budget."""

        budget = max(0, int(max_chars))
        if budget == 0:
            return MemoryContext()
        hits = tuple(self.recall(query, limit=limit))
        external = self._external_recall(query, limit=limit)
        dynamic_raw = "\n\n".join(
            part
            for part in (
                self._render_hits(hits),
                self._render_external(external),
            )
            if part
        )

        if self._stable_snapshot:
            stable_budget = int(budget * 0.6)
            stable = self._fenced_with_budget(self._stable_snapshot, stable_budget)
            dynamic_budget = budget - len(stable)
        else:
            stable, dynamic_budget = "", budget

        dynamic = self._fenced_with_budget(dynamic_raw, dynamic_budget)
        return MemoryContext(
            stable=stable,
            dynamic=dynamic,
            recalled=hits,
            external_sources=tuple(chunk.source for chunk in external),
        )

    def forget(
        self,
        *,
        key: str = "",
        memory_id: str = "",
        scope: str = "global",
    ) -> int:
        """Forget a memory id or every historical version of a scoped key."""

        scope_name, scope_id, _, _ = self._resolve_scope(scope)
        key_name = str(key or "").strip()
        memory_id_name = str(memory_id or "").strip()
        candidates = self.store.forget_candidates(
            actor_id=self.actor_id,
            scope=scope_name,
            scope_id=scope_id,
            stable_key=key_name,
            memory_id=memory_id_name,
        )
        removed_notebook: list[tuple[str, str]] = []
        for record in candidates:
            if record.status != "active" or record.category not in NOTEBOOK_CATEGORIES:
                continue
            target = NOTEBOOK_TARGET_BY_CATEGORY[record.category]
            removed = self._notebook.remove(target, record.content)
            if not removed.ok:
                raise OSError("failed to remove canonical notebook memory")
            removed_notebook.append((target, record.content))
        hashes = {
            self._activity_hash(record.category, record.stable_key, record.content)
            for record in candidates
        }
        try:
            self.activity.scrub(
                actor_id=self.actor_id,
                key=key_name,
                hashes=hashes,
            )
            forgotten = self.store.forget(
                actor_id=self.actor_id,
                scope=scope_name,
                scope_id=scope_id,
                stable_key=key_name,
                memory_id=memory_id_name,
            )
        except BaseException:
            for target, content in removed_notebook:
                self._notebook.add(target, content)
            raise
        if forgotten.count:
            self._sync_projection(source="runtime:forget")
            self._refresh_search_index_after_forget()
            self._emit(
                MemoryActivityEvent(
                    kind="forget",
                    key=str(key or "").strip(),
                    actor_id=self.actor_id,
                    source="runtime:forget",
                    extra={
                        "scope": scope_name,
                        "memory_id": memory_id_name,
                        "count": forgotten.count,
                    },
                )
            )
        return forgotten.count

    def _refresh_search_index_after_forget(self) -> None:
        if not bool(self.config.get("memory.vector_search.enabled", False)):
            return
        try:
            from . import memsearch_index

            result = memsearch_index.reindex(self.config, force=True)
            if isinstance(result, dict) and result.get("ok") is False:
                raise RuntimeError(str(result.get("error") or "reindex_failed"))
        except Exception as exc:
            self._emit(
                MemoryActivityEvent(
                    kind="derived_index_error",
                    source="runtime:forget",
                    actor_id=self.actor_id,
                    extra={"error_type": type(exc).__name__},
                )
            )

    def maintain(self) -> int:
        """Apply retention policy and refresh derived projections."""

        expired = self.store.maintain(actor_id=self.actor_id)
        if expired:
            self._sync_projection(source="runtime:maintain")
        return expired

    def _sync_projection(self, *, source: str) -> bool:
        try:
            return self._projection.sync(actor_id=self.actor_id)
        except Exception as exc:
            self._emit(
                MemoryActivityEvent(
                    kind="projection_error",
                    source=source,
                    actor_id=self.actor_id,
                    extra={"error_type": type(exc).__name__},
                )
            )
            return False

    def end_session(self, *, summary: str = "") -> MemoryRememberResult | None:
        """Capture an optional summary and run retention maintenance."""

        result = None
        if str(summary or "").strip():
            scope = (
                "session"
                if self.session_id
                else "strategy"
                if self.strategy_id
                else "global"
            )
            result = self.remember(
                category="session_summary",
                content=summary,
                scope=scope,
                key=f"session.summary.{self.session_id}" if self.session_id else "",
                source="runtime:end_session",
                writer_id="session_lifecycle",
            )
        self.maintain()
        return result

    def _skipped(
        self,
        category: str,
        reason: str,
        key: str,
        title: str,
        body: str,
        source: str,
        *,
        extra: dict | None = None,
        record: MemoryRecord | None = None,
    ) -> MemoryRememberResult:
        self._emit(
            MemoryActivityEvent.write_skipped(
                category=category,
                skip_reason=reason,
                title=title,
                preview=body,
                hash=self._activity_hash(category, key, body),
                source=source,
                actor_id=self.actor_id,
                extra={"key": key, **dict(extra or {})},
            )
        )
        return MemoryRememberResult(
            ok=False,
            skipped=True,
            skip_reason=reason,
            record=record,
        )

    def _write_ok(
        self,
        category: str,
        key: str,
        title: str,
        body: str,
        source: str,
        *,
        extra: dict | None = None,
    ) -> None:
        self._emit(
            MemoryActivityEvent.write_ok(
                category=category,
                key=key,
                title=title,
                preview=body,
                hash=self._activity_hash(category, key, body),
                source=source,
                actor_id=self.actor_id,
                extra=dict(extra or {}),
            )
        )

    def _emit(self, event: MemoryActivityEvent) -> None:
        try:
            self.activity.append(event)
        except OSError:
            pass

    @staticmethod
    def _activity_hash(category: str, key: str, body: str) -> str:
        raw = f"{category}::{key}::{body}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _render_hits(hits: Iterable[MemoryRecord]) -> str:
        parts: list[str] = []
        for hit in hits:
            identity = hit.stable_key or hit.memory_id
            source = f"; source={hit.source_ref}" if hit.source_ref else ""
            parts.append(
                f"[{hit.category}; scope={hit.scope}; id={identity}{source}]\n"
                f"{hit.content}"
            )
        return "\n\n".join(parts)

    @staticmethod
    def _render_external(chunks: Iterable[object]) -> str:
        parts: list[str] = []
        for chunk in chunks:
            text = str(getattr(chunk, "text", "") or "").strip()
            source = str(getattr(chunk, "source", "external") or "external")
            if text:
                parts.append(f"[external; source={source}]\n{text}")
        return "\n\n".join(parts)

    def _build_external_provider(self):
        if not self.session_id or self.strategy_id:
            return None
        try:
            from .agentmemory_provider import (
                AgentMemoryProvider,
                selected_external_provider,
            )

            if selected_external_provider(self.config) != "agentmemory":
                return None
            provider = AgentMemoryProvider(self.config)
            actor_hash = hashlib.sha256(self.actor_id.encode("utf-8")).hexdigest()[:16]
            provider.settings = replace(
                provider.settings,
                session_id=f"nerya-{actor_hash}:{self.session_id}",
            )
            return provider
        except Exception:
            return None

    def _external_recall(self, query: str, *, limit: int) -> list[object]:
        provider = self._external_provider
        query_text = str(query or "").strip()
        if provider is None or not query_text or scan_memory_content(query_text):
            return []
        started = time.monotonic()
        try:
            candidates = provider.prefetch(query_text, limit=limit) or []
        except Exception:
            candidates = []
        accepted: list[object] = []
        for chunk in candidates:
            metadata = getattr(chunk, "metadata", {})
            if not isinstance(metadata, dict):
                continue
            result_session = str(
                metadata.get("sessionId") or metadata.get("session_id") or ""
            ).strip()
            provider_session = str(
                getattr(getattr(provider, "settings", None), "session_id", "") or ""
            ).strip()
            text = str(getattr(chunk, "text", "") or "").strip()
            if result_session != provider_session or not text:
                continue
            if scan_memory_content(text):
                continue
            accepted.append(chunk)
        self._emit(
            MemoryActivityEvent.search(
                query=query_text,
                result_count=len(accepted),
                latency_ms=int((time.monotonic() - started) * 1000),
                source="external:agentmemory",
                actor_id=self.actor_id,
                extra={"session_id": self.session_id},
            )
        )
        return accepted[: max(0, int(limit))]

    @staticmethod
    def _fenced_with_budget(raw: str, budget: int) -> str:
        clean = sanitize_context(str(raw or "")).strip()
        if not clean or budget <= 0:
            return ""
        overhead = len(build_memory_context_block("x")) - 1
        if budget <= overhead:
            return ""
        content_budget = budget - overhead
        if len(clean) > content_budget:
            if content_budget <= 3:
                return ""
            clean = clean[: content_budget - 3].rstrip() + "..."
        block = build_memory_context_block(clean)
        return block if len(block) <= budget else ""

    def _resolve_scope(self, scope: str) -> tuple[str, str, str, str]:
        value = str(scope or "").strip().lower()
        if value == "global":
            return "global", "", "", ""
        if value == "strategy":
            if not self.strategy_id:
                raise MemoryScopeError("strategy memory requires an active strategy")
            return "strategy", self.strategy_id, self.strategy_id, self.session_id
        if value == "session":
            if not self.session_id:
                raise MemoryScopeError("session memory requires an active session")
            return "session", self.session_id, self.strategy_id, self.session_id
        raise MemoryScopeError(f"unknown memory scope: {scope!r}")

    def _import_legacy_index(self) -> None:
        if self.actor_id != self._legacy_owner_actor:
            return
        legacy_source = "memory/index.jsonl"
        if not self.store.begin_legacy_import(
            actor_id=self.actor_id,
            legacy_source=legacy_source,
        ):
            return
        rows = jsonl.read_all(self.config.paths.memory_index)
        if any(isinstance(row, dict) and row.get("memory_id") for row in rows):
            self.store.complete_legacy_import(
                actor_id=self.actor_id,
                legacy_source=legacy_source,
            )
            return
        known_categories = set(load_write_rules(self.config))
        rows.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
        for row in rows:
            if not isinstance(row, dict) or bool(row.get("superseded")):
                continue
            content = str(row.get("value") or "").strip()
            if not content or scan_memory_content(content):
                continue
            scope = str(row.get("scope") or "").strip().lower()
            strategy_id = str(row.get("strategy_id") or "").strip()
            if scope == "global":
                scope_id = ""
                strategy_id = ""
            elif scope == "strategy" and strategy_id:
                try:
                    strategy_id = self._clean_id(strategy_id, "strategy_id")
                except MemoryScopeError:
                    continue
                scope_id = strategy_id
            else:
                continue
            tags = [str(tag).strip().lower() for tag in row.get("tags") or []]
            category = next(
                (tag for tag in tags if tag in known_categories),
                self._legacy_category(str(row.get("file") or "")),
            )
            canonical = json.dumps(row, sort_keys=True, ensure_ascii=False)
            legacy_ref = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            self.store.import_legacy_record(
                actor_id=self.actor_id,
                scope=scope,
                scope_id=scope_id,
                strategy_id=strategy_id,
                category=category,
                content=content,
                stable_key=str(row.get("key") or "").strip(),
                title="",
                tags=tags,
                source_turn_id=str(row.get("source_turn") or "").strip(),
                target_file=str(row.get("file") or "").strip(),
                created_at=self._legacy_timestamp(str(row.get("ts") or "")),
                legacy_source=legacy_source,
                legacy_ref=legacy_ref,
            )
        self.store.complete_legacy_import(
            actor_id=self.actor_id,
            legacy_source=legacy_source,
        )

    def _import_legacy_markdown(self) -> None:
        if self.actor_id != self._legacy_owner_actor:
            return
        sources: list[tuple[Path, str, str, str]] = [
            (self.config.paths.memory / "global.md", "global", "", "learning"),
            (self.config.paths.memory / "mistakes.md", "global", "", "error"),
            (
                self.config.paths.memory / "market_regimes.md",
                "global",
                "",
                "learning",
            ),
            (
                self.config.paths.memory / "skill_learnings.md",
                "global",
                "",
                "learning",
            ),
            (self.config.paths.memory / "decisions.md", "global", "", "decision"),
            (self.config.paths.memory / "signals.md", "global", "", "signal"),
        ]
        rules = load_write_rules(self.config)
        root = self.config.paths.root.resolve()
        for category, rule in rules.items():
            if category in NOTEBOOK_CATEGORIES:
                continue
            for target in rule.target_files:
                path = (root / str(target or "")).resolve()
                try:
                    path.relative_to(root)
                except ValueError:
                    continue
                sources.append((path, "global", "", category))
        if self.strategy_id:
            sources.append(
                (
                    self.config.paths.strategies / self.strategy_id / "learnings.md",
                    "strategy",
                    self.strategy_id,
                    "learning",
                )
            )
        known_categories = set(rules)
        seen_sources: set[str] = set()
        prepared_sources: list[tuple[Path, str, str, str, str]] = []
        for raw_path, scope, scope_id, fallback_category in sources:
            path = raw_path
            try:
                source = str(path.resolve().relative_to(root))
            except ValueError:
                continue
            if source in seen_sources:
                continue
            seen_sources.add(source)
            prepared_sources.append((path, source, scope, scope_id, fallback_category))
        pending_sources = self.store.begin_legacy_imports(
            actor_id=self.actor_id,
            legacy_sources=(item[1] for item in prepared_sources),
        )
        for path, source, scope, scope_id, fallback_category in prepared_sources:
            if source not in pending_sources:
                continue
            if not path.exists():
                self.store.complete_legacy_import(
                    actor_id=self.actor_id,
                    legacy_source=source,
                )
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if (
                GENERATED_PROJECTION_MARKER in text[:1000]
                or "<!-- Generated from nerya.db;" in text[:1000]
            ):
                self.store.complete_legacy_import(
                    actor_id=self.actor_id,
                    legacy_source=source,
                )
                continue
            matches = list(_MARKDOWN_SECTION_RE.finditer(text))
            preamble_end = matches[0].start() if matches else len(text)
            preamble = text[:preamble_end].strip()
            preamble_lines = preamble.splitlines()
            if preamble_lines and preamble_lines[0].lstrip().startswith("# "):
                preamble_lines = preamble_lines[1:]
            preamble_body = "\n".join(preamble_lines).strip()
            if preamble_body and not scan_memory_content(preamble_body):
                preamble_ref = hashlib.sha256(
                    f"{source}\npreamble\n{preamble}".encode("utf-8")
                ).hexdigest()
                try:
                    created_at = path.stat().st_mtime
                except OSError:
                    created_at = 0.0
                self.store.import_legacy_record(
                    actor_id=self.actor_id,
                    scope=scope,
                    scope_id=scope_id,
                    strategy_id=scope_id if scope == "strategy" else "",
                    category=fallback_category,
                    content=preamble_body,
                    stable_key="",
                    title="legacy preamble",
                    tags=[fallback_category, "legacy"],
                    source_turn_id="",
                    target_file=source,
                    created_at=created_at,
                    legacy_source=source,
                    legacy_ref=preamble_ref,
                )
            for index, match in enumerate(matches):
                end = (
                    matches[index + 1].start()
                    if index + 1 < len(matches)
                    else len(text)
                )
                section = text[match.start() : end].strip()
                title = match.group("title").strip()
                body = text[match.end() : end].strip()
                if not body:
                    continue
                created_at = self._legacy_timestamp(title)
                stable_key = ""
                category = fallback_category
                if created_at <= 0:
                    lines = body.splitlines()
                    tokens = _METADATA_TOKEN_RE.findall(lines[0]) if lines else []
                    token_timestamp = self._legacy_timestamp(tokens[0]) if tokens else 0
                    if token_timestamp > 0:
                        created_at = token_timestamp
                        candidate = tokens[1].strip().lower() if len(tokens) > 1 else ""
                        if candidate in known_categories:
                            category = candidate
                        stable_key = next(
                            (
                                token.split("=", 1)[1].strip()
                                for token in tokens[2:]
                                if token.startswith("key=")
                            ),
                            "",
                        )
                        body = "\n".join(lines[1:]).strip()
                if not body or scan_memory_content(body):
                    continue
                legacy_ref = hashlib.sha256(
                    f"{source}\n{section}".encode("utf-8")
                ).hexdigest()
                self.store.import_legacy_record(
                    actor_id=self.actor_id,
                    scope=scope,
                    scope_id=scope_id,
                    strategy_id=scope_id if scope == "strategy" else "",
                    category=category,
                    content=body,
                    stable_key=stable_key,
                    title="" if self._legacy_timestamp(title) > 0 else title,
                    tags=[category],
                    source_turn_id="",
                    target_file=source,
                    created_at=created_at,
                    legacy_source=source,
                    legacy_ref=legacy_ref,
                )
            self.store.complete_legacy_import(
                actor_id=self.actor_id,
                legacy_source=source,
            )

    @staticmethod
    def _legacy_category(target_file: str) -> str:
        name = str(target_file or "").lower()
        if name.endswith("mistakes.md"):
            return "error"
        if name.endswith("decisions.md"):
            return "decision"
        if name.endswith("signals.md"):
            return "signal"
        return "learning"

    @staticmethod
    def _legacy_timestamp(raw: str) -> float:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _required_id(value: str, name: str) -> str:
        clean = MemoryRuntime._clean_id(value, name)
        if not clean:
            raise MemoryScopeError(f"{name} must be non-empty")
        return clean

    @staticmethod
    def _required_actor_id(value: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise MemoryScopeError("actor_id must be non-empty")
        if len(clean) > 256 or any(ord(char) < 32 for char in clean):
            raise MemoryScopeError("invalid actor_id")
        return clean

    @staticmethod
    def _clean_id(value: str, name: str) -> str:
        clean = str(value or "").strip()
        if any(part in clean for part in ("/", "\\", "..", "\x00")):
            raise MemoryScopeError(f"invalid {name}")
        return clean

    @staticmethod
    def _unit_interval(value: float, name: str) -> float:
        number = float(value)
        if not 0.0 <= number <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")
        return number


__all__ = [
    "MemoryContext",
    "MemoryRememberResult",
    "MemoryRuntime",
    "MemoryScopeError",
]

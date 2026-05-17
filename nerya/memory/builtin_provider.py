"""Built-in (always-on) memory provider for Nerya.

This provider combines the curated AGENT.md / OPERATOR.md notebook
with the local fact log. It ships with the codebase, requires no API
key, and becomes the default as soon as the agent boots. External
providers (Mem0, Honcho, Hindsight, …) layer on top of this one —
they never replace it.

Composition (so we don't grow yet another god-class):

* :class:`MemoryNotebook` for the bounded curated stores.
* :class:`MemoryWriter` for rule-driven captures + the activity log.
* ``memsearch_index`` for vector recall (when enabled by the operator).

Each delegate already knows how to do its own thing safely; the
provider just glues them onto the :class:`MemoryProvider` lifecycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..core.config import Config
from . import memsearch_index
from .notebook import MemoryNotebook, NotebookResult, VALID_TARGETS
from .provider import (
    MemoryProvider,
    MemoryProviderInfo,
    MemoryRecallChunk,
    MemoryToolDef,
    MemoryToolResult,
)
from .writer import MemoryWriter, default_notebook


__all__ = ["BuiltinMemoryProvider"]

_log = logging.getLogger("nerya.memory.builtin")


_BUILTIN_INFO = MemoryProviderInfo(
    id="builtin",
    name="Built-in (notebook + memsearch)",
    family="builtin",
    description=(
        "Curated AGENT.md / OPERATOR.md notebook injected verbatim into "
        "the system prompt, plus the optional memsearch vector index "
        "over markdown memory. Always available — no remote calls."
    ),
    requires_api_key=False,
    env_key=None,
    cost_hint="free (local files + optional embedding cost)",
)


@dataclass
class BuiltinMemoryProvider(MemoryProvider):
    """Always-on provider backed by the curated notebook + memsearch."""

    config: Config
    info: MemoryProviderInfo = field(default=_BUILTIN_INFO, init=False)
    _notebook: MemoryNotebook | None = field(default=None, init=False, repr=False)
    _writer: MemoryWriter | None = field(default=None, init=False, repr=False)
    _system_prompt_snapshot: str = field(default="", init=False, repr=False)

    # ------------------------------------------------------------- lifecycle

    def is_available(self) -> bool:
        # Always available: the notebook is on-disk only and the
        # writer never fails to construct (memsearch is optional and
        # checked separately by the dashboard).
        return True

    def initialize(self) -> None:
        """Lazy-load the notebook + take the system-prompt snapshot.

        The system-prompt block is captured once here so the LLM prefix
        cache stays byte-stable for the whole session even if the agent
        uses the memory tool mid-turn.
        """

        nb = default_notebook(self.config)
        self._notebook = nb
        # The notebook's load() already runs inside default_notebook().
        snap = nb.snapshot_blocks()
        joined = "\n\n".join(part for part in snap.values() if part)
        self._system_prompt_snapshot = joined.strip()
        # Lazy-construct the writer so MemoryIndex doesn't load until
        # the first capture / hook fires.
        self._writer = MemoryWriter(self.config)

    def shutdown(self) -> None:
        # Nothing to release; the notebook flushes on every write.
        return None

    # ----------------------------------------------------- system prompt

    def system_prompt_block(self) -> str:
        return self._system_prompt_snapshot

    # -------------------------------------------------------------- recall

    def prefetch(self, query: str, *, limit: int = 5) -> list[MemoryRecallChunk]:
        """Run memsearch over the workspace markdown if it's enabled."""

        try:
            res = memsearch_index.search(
                self.config,
                query=str(query or ""),
                top_k=int(limit or 5),
            )
        except Exception as exc:  # noqa: BLE001 — recall is best-effort
            _log.warning("builtin memory recall failed: %s", exc)
            return []
        if not isinstance(res, dict):
            return []
        rows = res.get("results")
        if not isinstance(rows, list):
            return []
        chunks: list[MemoryRecallChunk] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = str(
                row.get("content")
                or row.get("text")
                or row.get("chunk")
                or "",
            ).strip()
            if not text:
                continue
            try:
                score = float(row.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            chunks.append(MemoryRecallChunk(
                text=text,
                score=score,
                source=str(
                    row.get("source")
                    or row.get("path")
                    or row.get("file")
                    or "memory",
                ),
                metadata={k: v for k, v in row.items() if k not in {"content", "text", "chunk"}},
            ))
        return chunks

    # --------------------------------------------------------------- tools

    def get_tool_schemas(self) -> list[MemoryToolDef]:
        """Expose one action-based ``memory`` tool surface."""

        return [
            MemoryToolDef(
                name="memory",
                description=(
                    "Curate the agent / operator notebook. "
                    "Entries land verbatim in the system prompt at the "
                    "next session start. Use sparingly: this is "
                    "long-term memory, not scratch space."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "replace", "remove", "read"],
                        },
                        "target": {
                            "type": "string",
                            "enum": list(VALID_TARGETS),
                            "description": "agent = AGENT.md, operator = OPERATOR.md",
                        },
                        "content": {
                            "type": "string",
                            "description": "New entry (for add) or replacement text (for replace).",
                        },
                        "old_text": {
                            "type": "string",
                            "description": "Substring identifying the entry to replace / remove.",
                        },
                    },
                    "required": ["action", "target"],
                },
            ),
        ]

    def handle_tool_call(
        self, name: str, arguments: dict[str, Any]
    ) -> MemoryToolResult:
        if name != "memory":
            return MemoryToolResult(
                ok=False,
                error=f"builtin: unknown tool {name!r}",
            )
        nb = self._notebook
        if nb is None:
            return MemoryToolResult(
                ok=False,
                error="builtin: notebook not initialised",
            )
        action = str(arguments.get("action") or "").strip().lower()
        target = str(arguments.get("target") or "").strip().lower()
        if target not in VALID_TARGETS:
            return MemoryToolResult(
                ok=False,
                error=f"builtin: invalid target {target!r}; want one of {list(VALID_TARGETS)}",
            )
        if action == "read":
            entries = nb.entries(target)
            return MemoryToolResult(
                ok=True,
                content="\n§\n".join(entries),
                extra={
                    "target": target,
                    "entries": list(entries),
                    "used_chars": nb.used_chars(target),
                    "char_limit": nb.char_limit(target),
                },
            )
        if action == "add":
            res: NotebookResult = nb.add(target, str(arguments.get("content") or ""))
        elif action == "replace":
            res = nb.replace(
                target,
                str(arguments.get("old_text") or ""),
                str(arguments.get("content") or ""),
            )
        elif action == "remove":
            res = nb.remove(target, str(arguments.get("old_text") or ""))
        else:
            return MemoryToolResult(
                ok=False,
                error=f"builtin: unknown action {action!r}",
            )
        return MemoryToolResult(
            ok=res.ok,
            content=res.message,
            error=res.error,
            extra={
                "target": res.target,
                "used_chars": res.used_chars,
                "char_limit": res.char_limit,
                "entries": list(res.entries),
                **res.extra,
            },
        )

    # ----------------------------------------------------- session hooks

    def on_session_end(self, *, summary: str = "") -> None:
        # The notebook is already persisted on every write; nothing to flush.
        # When ``summary`` is non-empty, route it through the standard writer
        # rules so operators can disable or retarget that capture from
        # /memory/write_rules.
        if not summary:
            return
        if self._writer is None:
            return
        try:
            self._writer.capture(
                category="session_summary",
                content=summary,
                title="session summary",
                source="builtin:on_session_end",
            )
        except Exception:  # noqa: BLE001 — best-effort
            _log.exception("builtin memory: session_summary capture failed")

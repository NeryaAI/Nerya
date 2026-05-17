"""Abstract base class for pluggable Nerya memory providers.

The memory subsystem is split into two layers:

1. A **built-in** memory provider (the curated MEMORY.md / USER.md
   notebook + the embedded fact log) that is *always on*. It owns the
   data Nerya itself produces and never goes away.
2. **Optional external providers** (Mem0, Honcho, Hindsight, Supermemory,
   Letta, ...) that can be plugged in to back the agent with cloud /
   richer memory stores. At most one external provider is active per
   workspace at any time.

This module reflects that split: :class:`MemoryProvider` is the contract
both layers implement. The orchestration that enforces the *exactly
one builtin + at most one external* rule lives in
``nerya/memory/manager.py``.

Design notes:

* Methods that *might* talk to a slow remote service (``prefetch``,
  ``sync_turn``, ``handle_tool_call``) are kept narrow and should be
  cancellable. ``prefetch`` is the only call the manager fires on the
  hot path before each turn, so it must time-bound itself.
* The *system-prompt block* is split into the part injected at the
  start of each session (``system_prompt_block``) and the part that
  may be pre-fetched per-turn before the LLM call. The first is
  **frozen** for prefix-cache stability; the second is wrapped in a
  ``<memory-context>`` fence by the manager so the model can tell
  recalled content apart from fresh user input.
* Tool dispatch is opt-in: a provider returns its own JSON-Schema tool
  definitions from ``get_tool_schemas`` (e.g. a ``memory`` tool with
  ``add`` / ``replace`` / ``remove`` actions) and processes the
  resulting calls in ``handle_tool_call``. Providers that don't offer
  tools simply return ``[]``.
* ``initialize`` runs once per session; it's the right place to load
  state from disk or open a remote connection. The manager treats
  ``initialize`` failures as recoverable — the provider just gets
  marked unavailable, it doesn't take Nerya down.

The contract is intentionally Python-narrow (no MyPy ``Protocol``)
because subclasses elsewhere in the tree may extend it with provider-
specific helpers, and we want the ``isinstance`` check to be cheap.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterable


__all__ = [
    "MemoryProvider",
    "MemoryProviderInfo",
    "MemoryToolDef",
    "MemoryToolResult",
    "MemoryRecallChunk",
]


@dataclass(frozen=True)
class MemoryProviderInfo:
    """Static metadata the dashboard renders for each registered provider.

    ``id`` is the canonical, lowercase, hyphenated identifier (matches
    the catalog id when applicable). ``family`` groups providers in
    the dashboard (``"builtin"`` vs ``"external"``). ``cost_hint``
    is informational text the dashboard surfaces under the provider
    card so the operator can compare options.
    """

    id: str
    name: str
    family: str  # "builtin" | "external"
    description: str
    requires_api_key: bool = False
    env_key: str | None = None
    cost_hint: str = ""
    install_command: str = ""
    install_alternatives: tuple[str, ...] = field(default_factory=tuple)
    docs_url: str = ""


@dataclass(frozen=True)
class MemoryToolDef:
    """JSON-schema-shaped tool definition exposed to the LLM."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryToolResult:
    """Result returned to the LLM after handling a memory tool call."""

    ok: bool
    content: str = ""
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "content": self.content,
            "error": self.error,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class MemoryRecallChunk:
    """One recalled snippet returned by a provider's ``prefetch``.

    ``score`` is a normalised similarity (0.0–1.0) for sorting in the
    dashboard activity stream; providers that don't expose scores
    (for example, the built-in notebook block) should return ``1.0``.
    ``source`` is a human-readable identifier the activity log can
    show (file path, vector-store ref, mem0 memory id, etc.).
    """

    text: str
    score: float = 1.0
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryProvider(abc.ABC):
    """Common contract for builtin + external memory backends.

    A provider's lifecycle::

                 ┌──────────────┐
                 │  registered  │
                 └──────┬───────┘
                        │ initialize() — once per session
                        ▼
                 ┌──────────────┐
                 │   active     │
                 └──┬────────┬──┘
        prefetch()  │        │  handle_tool_call() (LLM-driven)
        sync_turn() │        │
        on_*()      │        │
                    │        │
                    ▼        ▼
                 ┌──────────────┐
                 │   shutdown   │
                 └──────────────┘

    Methods marked ``@abc.abstractmethod`` MUST be overridden. Hooks
    (``on_*``) have empty default implementations so a minimal
    provider only needs to override the four core methods.
    """

    info: MemoryProviderInfo

    # ------------------------------------------------------------ lifecycle

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Cheap availability probe.

        Returns ``True`` when the provider can serve calls (deps
        installed, API key resolved, optional vector index built).
        The dashboard surfaces this as the green/amber pill in the
        memory tab; the manager uses it to decide whether to dispatch
        prefetch / sync_turn calls or skip them.
        """

        raise NotImplementedError

    def initialize(self) -> None:
        """One-time initialisation (called by the manager).

        Subclasses override to load from disk or open a remote
        connection. The default no-op suits the simplest providers.
        """

        return None

    def shutdown(self) -> None:
        """Release resources held by :meth:`initialize`."""

        return None

    # --------------------------------------------------- system-prompt block

    def system_prompt_block(self) -> str:
        """Return the block injected verbatim into the system prompt.

        This block is *frozen* at session start. For
        the builtin notebook it's the bounded MEMORY.md / USER.md
        snapshot; for external providers it can be a short
        introduction (e.g. ``"You have access to a Mem0 memory store..."``).
        """

        return ""

    # ------------------------------------------------------------- recall

    def prefetch(self, query: str, *, limit: int = 5) -> list[MemoryRecallChunk]:
        """Return up to ``limit`` recalled chunks for ``query``.

        Called per-turn when the manager decides recall is warranted.
        Providers that don't offer recall (e.g. write-only sinks)
        return ``[]``. Implementations MUST be cancellable / fast —
        the manager will gather across all active providers and add
        the results into the LLM context.
        """

        return []

    # ---------------------------------------------------- writes & telemetry

    def sync_turn(self, *, turn: dict[str, Any]) -> None:
        """Called after every LLM turn so the provider can ingest it.

        ``turn`` is shaped like::

            {
              "role":      "assistant" | "user",
              "content":   "...",
              "metadata":  {"tools_used": [...], "tokens": ..., ...},
              "ts":        "2025-..."
            }

        Most providers ignore this; Mem0 / Honcho-like backends use
        it to derive longer-term memories.
        """

        return None

    # ------------------------------------------------------------ tooling

    def get_tool_schemas(self) -> list[MemoryToolDef]:
        """JSON-schema tool definitions to expose to the LLM.

        The built-in provider exposes a single ``memory`` tool
        with action ∈ {``add``, ``replace``, ``remove``, ``read``};
        Nerya keeps the same shape so prompts stay portable.
        """

        return []

    def handle_tool_call(
        self, name: str, arguments: dict[str, Any]
    ) -> MemoryToolResult:
        """Process a tool call routed back from the LLM.

        Default implementation rejects unknown calls with a clear
        error message so a provider that forgot to override sees the
        problem at the next session.
        """

        return MemoryToolResult(
            ok=False,
            error=f"{self.info.id}: tool {name!r} is not implemented",
        )

    # -------------------------------------------------- optional event hooks
    # The manager calls these best-effort; an exception in a hook does
    # not kill the agent turn (it's logged and the manager keeps going).

    def on_turn_start(self, *, query: str) -> None:
        """Right before the LLM is called for a new user turn."""
        return None

    def on_session_end(self, *, summary: str = "") -> None:
        """The session ended cleanly; flush state to disk if needed."""
        return None

    def on_pre_compress(self) -> None:
        """About to summarise & truncate the conversation; persist now."""
        return None

    def on_memory_write(self, *, category: str, payload: dict[str, Any]) -> None:
        """The writer just persisted ``payload`` under ``category``."""
        return None

    def on_delegation(self, *, target: str, payload: dict[str, Any]) -> None:
        """The agent just handed off to ``target`` (sub-agent, tool …)."""
        return None

    # ------------------------------------------------------------- helpers

    @classmethod
    def supported_actions(cls) -> Iterable[str]:
        """Names of override-able actions on this provider class.

        Used by the manager + dashboard to render a capability matrix
        without instantiating each provider.
        """

        return (
            "system_prompt_block",
            "prefetch",
            "sync_turn",
            "get_tool_schemas",
            "handle_tool_call",
            "on_turn_start",
            "on_session_end",
            "on_pre_compress",
            "on_memory_write",
            "on_delegation",
        )

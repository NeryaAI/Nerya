"""Orchestrator for Nerya memory providers.

At most **one external** :class:`MemoryProvider` plus the
**always-on builtin** can be active per workspace at any
time. The manager enforces this rule, fans out lifecycle events,
gathers recalled chunks for the LLM context, and is the single
surface the agent kernel and the dashboard talk to.

Why a separate manager (and not a free function)?

* The 1+1 rule needs a single point of truth — making it the manager's
  invariant means no caller can sidestep it.
* External providers can be heavy (network, big indexes); ``initialize``
  / ``shutdown`` need careful sequencing.
* The dashboard wants a unified ``GET /memory/providers`` view with
  availability, capability flags and current-active state. The
  manager owns that materialised view.
* Every dispatch (``prefetch``, ``sync_turn``, hooks) is best-effort:
  one provider's failure must not blow up the agent turn. The manager
  swallows exceptions, logs them, and keeps going.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..core.config import Config
from .activity import MemoryActivityEvent, MemoryActivityLog
from .context_fence import build_memory_context_block
from .provider import (
    MemoryProvider,
    MemoryProviderInfo,
    MemoryRecallChunk,
    MemoryToolDef,
    MemoryToolResult,
)


__all__ = [
    "MemoryManager",
    "MemoryManagerError",
    "MemoryManagerSnapshot",
]

_log = logging.getLogger("nerya.memory.manager")


class MemoryManagerError(Exception):
    """Raised on invariant violations (e.g. two external providers)."""


@dataclass
class _RegisteredProvider:
    """Internal bookkeeping for one provider instance."""

    provider: MemoryProvider
    initialised: bool = False
    last_error: str = ""
    last_initialised_at: float | None = None


@dataclass(frozen=True)
class MemoryManagerSnapshot:
    """Materialised view of the manager state for the dashboard."""

    builtin: dict[str, Any]
    external: dict[str, Any] | None
    available_external: list[dict[str, Any]] = field(default_factory=list)


class MemoryManager:
    """Single coordinator for Nerya memory providers.

    Typical lifecycle in the agent kernel::

        manager = MemoryManager(config)
        manager.set_builtin(builtin_provider)
        if maybe_external is not None:
            manager.set_external(maybe_external)
        manager.initialize()  # opens any externally-managed connections

        ...

        block = manager.system_prompt_block()  # frozen for the session
        chunks = manager.prefetch(user_input)
        ...
        manager.sync_turn(turn={"role": "assistant", "content": ...})
        manager.shutdown()  # at session end
    """

    def __init__(
        self,
        config: Config,
        *,
        activity_log: MemoryActivityLog | None = None,
    ) -> None:
        self.config = config
        self._activity = activity_log or MemoryActivityLog(config=config)
        self._lock = threading.RLock()
        self._builtin: _RegisteredProvider | None = None
        self._external: _RegisteredProvider | None = None
        # Available external providers the manager *knows about* but
        # has not selected yet. Used by the dashboard to render the
        # "switch provider" picker.
        self._available_external: dict[str, MemoryProvider] = {}

    # ------------------------------------------------------------- registration

    def set_builtin(self, provider: MemoryProvider) -> None:
        """Pin the builtin provider (always-on).

        Replacing the builtin is allowed (a workspace migration may
        swap stores) but it MUST be the same family — we reject
        ``family != 'builtin'`` so an external provider can't
        accidentally take over the always-on slot.
        """

        if provider.info.family != "builtin":
            raise MemoryManagerError(
                f"set_builtin: provider {provider.info.id!r} has family "
                f"{provider.info.family!r}; expected 'builtin'"
            )
        with self._lock:
            if self._builtin is not None and self._builtin.initialised:
                self._builtin.provider.shutdown()
            self._builtin = _RegisteredProvider(provider=provider)

    def register_external_provider(self, provider: MemoryProvider) -> None:
        """Make ``provider`` selectable as the active external backend.

        Registration is purely metadata — nothing is fetched or
        initialised until :meth:`set_external` picks the provider.
        """

        if provider.info.family != "external":
            raise MemoryManagerError(
                f"register_external_provider: provider {provider.info.id!r} "
                f"has family {provider.info.family!r}; expected 'external'"
            )
        with self._lock:
            self._available_external[provider.info.id] = provider

    def set_external(self, provider: MemoryProvider | None) -> None:
        """Activate ``provider`` (or ``None`` to disable external memory).

        At most one external provider may be active at a time.
        Replacing the active external first ``shutdown()``s the old
        one, *then* installs and initialises the new one. The builtin
        is unaffected.
        """

        with self._lock:
            if self._external is not None and self._external.initialised:
                try:
                    self._external.provider.shutdown()
                except Exception:  # noqa: BLE001 — shutdown must not raise
                    _log.exception("memory.manager: shutdown of previous external failed")
            if provider is None:
                self._external = None
                return
            if provider.info.family != "external":
                raise MemoryManagerError(
                    f"set_external: provider {provider.info.id!r} has family "
                    f"{provider.info.family!r}; expected 'external'"
                )
            self._available_external.setdefault(provider.info.id, provider)
            self._external = _RegisteredProvider(provider=provider)
            self._maybe_initialise(self._external)

    # ------------------------------------------------------------- lifecycle

    def initialize(self) -> None:
        """Initialise both registered providers (idempotent)."""

        with self._lock:
            if self._builtin is not None:
                self._maybe_initialise(self._builtin)
            if self._external is not None:
                self._maybe_initialise(self._external)

    def shutdown(self) -> None:
        """Tear down active providers in LIFO order."""

        with self._lock:
            for slot in (self._external, self._builtin):
                if slot is None or not slot.initialised:
                    continue
                try:
                    slot.provider.shutdown()
                except Exception:  # noqa: BLE001
                    _log.exception("memory.manager: shutdown failed for %s", slot.provider.info.id)
                slot.initialised = False

    # ----------------------------------------------- system prompt + recall

    def system_prompt_block(self) -> str:
        """Return both providers' frozen system-prompt blocks joined.

        The builtin block is emitted first (Nerya's notebook is
        higher trust), the external block second. Empty blocks are
        skipped. The whole result is wrapped in a ``<memory-context>``
        fence so the LLM treats it as background context rather than
        fresh user input.
        """

        parts: list[str] = []
        for slot in self._iter_active_slots():
            try:
                block = slot.provider.system_prompt_block()
            except Exception as exc:  # noqa: BLE001
                slot.last_error = str(exc)
                _log.exception(
                    "memory.manager: %s.system_prompt_block raised", slot.provider.info.id,
                )
                continue
            if block:
                parts.append(block.rstrip())
        if not parts:
            return ""
        return build_memory_context_block("\n\n".join(parts))

    def prefetch(self, query: str, *, limit: int = 5) -> list[MemoryRecallChunk]:
        """Aggregate prefetch results from every active provider.

        We fan out sequentially today (one provider at a time) — the
        ABC docstring promises providers are fast / cancellable, so a
        single call is acceptable. If we ever add a slow provider we
        can move this onto an executor without changing the contract.
        """

        all_chunks: list[MemoryRecallChunk] = []
        for slot in self._iter_active_slots():
            t0 = time.monotonic()
            try:
                chunks = slot.provider.prefetch(query, limit=limit) or []
            except Exception as exc:  # noqa: BLE001
                slot.last_error = str(exc)
                _log.exception(
                    "memory.manager: %s.prefetch raised", slot.provider.info.id,
                )
                continue
            latency_ms = int((time.monotonic() - t0) * 1000)
            self._record_search(
                provider_id=slot.provider.info.id,
                query=query,
                hits=len(chunks),
                latency_ms=latency_ms,
            )
            all_chunks.extend(chunks)
        return all_chunks

    def sync_turn(self, *, turn: dict[str, Any]) -> None:
        for slot in self._iter_active_slots():
            try:
                slot.provider.sync_turn(turn=turn)
            except Exception as exc:  # noqa: BLE001
                slot.last_error = str(exc)
                _log.exception(
                    "memory.manager: %s.sync_turn raised", slot.provider.info.id,
                )

    # ------------------------------------------------------------ tools

    def collect_tool_schemas(self) -> list[MemoryToolDef]:
        """Return every active provider's tool definitions, deduped by name."""

        seen: set[str] = set()
        out: list[MemoryToolDef] = []
        for slot in self._iter_active_slots():
            try:
                schemas = slot.provider.get_tool_schemas() or []
            except Exception as exc:  # noqa: BLE001
                slot.last_error = str(exc)
                continue
            for schema in schemas:
                if schema.name in seen:
                    continue
                seen.add(schema.name)
                out.append(schema)
        return out

    def handle_tool_call(
        self, name: str, arguments: dict[str, Any]
    ) -> MemoryToolResult:
        """Dispatch a tool call to the first provider that owns it.

        First-match wins; the dedupe in :meth:`collect_tool_schemas`
        prevents two providers exposing the same name simultaneously.
        """

        for slot in self._iter_active_slots():
            try:
                schemas = slot.provider.get_tool_schemas() or []
            except Exception as exc:  # noqa: BLE001
                slot.last_error = str(exc)
                continue
            if any(s.name == name for s in schemas):
                try:
                    return slot.provider.handle_tool_call(name, arguments)
                except Exception as exc:  # noqa: BLE001
                    slot.last_error = str(exc)
                    return MemoryToolResult(
                        ok=False,
                        error=f"{slot.provider.info.id}: {exc}",
                    )
        return MemoryToolResult(
            ok=False,
            error=f"no active memory provider owns tool {name!r}",
        )

    # ----------------------------------------------------- best-effort hooks

    def on_turn_start(self, *, query: str) -> None:
        self._dispatch_hook("on_turn_start", query=query)

    def on_session_end(self, *, summary: str = "") -> None:
        self._dispatch_hook("on_session_end", summary=summary)

    def on_pre_compress(self) -> None:
        self._dispatch_hook("on_pre_compress")

    def on_memory_write(self, *, category: str, payload: dict[str, Any]) -> None:
        self._dispatch_hook("on_memory_write", category=category, payload=payload)

    def on_delegation(self, *, target: str, payload: dict[str, Any]) -> None:
        self._dispatch_hook("on_delegation", target=target, payload=payload)

    # ----------------------------------------------------- introspection

    def snapshot(self) -> MemoryManagerSnapshot:
        """Return the dashboard-shaped view of the manager's state."""

        with self._lock:
            builtin_view = self._slot_view(self._builtin)
            external_view = self._slot_view(self._external)
            available = []
            for pid, prov in self._available_external.items():
                if self._external is not None and self._external.provider.info.id == pid:
                    continue
                available.append(self._info_to_dict(prov.info, available=self._safe_available(prov)))
        return MemoryManagerSnapshot(
            builtin=builtin_view or {},
            external=external_view,
            available_external=available,
        )

    @property
    def builtin(self) -> MemoryProvider | None:
        return self._builtin.provider if self._builtin else None

    @property
    def external(self) -> MemoryProvider | None:
        return self._external.provider if self._external else None

    # ------------------------------------------------------------- helpers

    def _iter_active_slots(self) -> Iterable[_RegisteredProvider]:
        with self._lock:
            slots = []
            if self._builtin is not None and self._builtin.initialised:
                slots.append(self._builtin)
            if self._external is not None and self._external.initialised:
                slots.append(self._external)
        return slots

    def _maybe_initialise(self, slot: _RegisteredProvider) -> None:
        if slot.initialised:
            return
        try:
            slot.provider.initialize()
        except Exception as exc:  # noqa: BLE001
            slot.last_error = str(exc)
            _log.exception(
                "memory.manager: initialize failed for %s", slot.provider.info.id,
            )
            return
        slot.initialised = True
        slot.last_initialised_at = time.time()
        slot.last_error = ""

    def _dispatch_hook(self, hook_name: str, **kwargs: Any) -> None:
        for slot in self._iter_active_slots():
            method = getattr(slot.provider, hook_name, None)
            if method is None:
                continue
            try:
                method(**kwargs)
            except Exception as exc:  # noqa: BLE001
                slot.last_error = str(exc)
                _log.exception(
                    "memory.manager: %s.%s raised", slot.provider.info.id, hook_name,
                )

    def _record_search(
        self,
        *,
        provider_id: str,
        query: str,
        hits: int,
        latency_ms: int,
    ) -> None:
        try:
            self._activity.append(MemoryActivityEvent.search(
                query=query,
                result_count=hits,
                latency_ms=latency_ms,
                source=f"provider:{provider_id}",
                extra={"provider_id": provider_id},
            ))
        except Exception:  # noqa: BLE001 — activity log is best-effort
            _log.exception("memory.manager: failed to record search event")

    def _slot_view(self, slot: _RegisteredProvider | None) -> dict[str, Any] | None:
        if slot is None:
            return None
        return self._info_to_dict(
            slot.provider.info,
            available=self._safe_available(slot.provider),
            initialised=slot.initialised,
            last_error=slot.last_error,
            last_initialised_at=slot.last_initialised_at,
        )

    @staticmethod
    def _safe_available(provider: MemoryProvider) -> bool:
        try:
            return bool(provider.is_available())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _info_to_dict(
        info: MemoryProviderInfo,
        *,
        available: bool,
        initialised: bool = False,
        last_error: str = "",
        last_initialised_at: float | None = None,
    ) -> dict[str, Any]:
        return {
            "id": info.id,
            "name": info.name,
            "family": info.family,
            "description": info.description,
            "requires_api_key": info.requires_api_key,
            "env_key": info.env_key,
            "cost_hint": info.cost_hint,
            "install_command": info.install_command,
            "install_alternatives": list(info.install_alternatives),
            "docs_url": info.docs_url,
            "available": available,
            "initialised": initialised,
            "last_error": last_error,
            "last_initialised_at": last_initialised_at,
        }

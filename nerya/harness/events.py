"""Interceptable pipeline events — the Nerya waterfall bus.

Nerya already has a pub/sub surface (``StreamingEventBus`` in
:mod:`nerya.agent.streaming`) but pub/sub is observe-only: listeners
cannot intercept or transform anything. The waterfall bus adds the
*pipeline* flavour of extension point, modelled on the
``waterfall`` dispatch mode popularised by composable plugin hosts
(deepseek-harness exposes ``tools/pre-execute``, ``tools/post-execute``
and friends this way):

* Producers call :meth:`WaterfallBus.waterfall` with a payload.
* Listeners registered via :meth:`WaterfallBus.on` are invoked as a
  chain ``listener(payload, next)``. A listener **must** call
  ``next()`` to delegate; returning a value without delegating
  short-circuits the rest of the chain (that is the interception
  semantics — e.g. a ``tools/post-execute`` listener may replace a
  result without letting later listeners see the original).
* If no listener is registered the producer's fallback decides the
  outcome, so adding an empty bus to a hot path is free.

Observation-only events (no interception, listener failures can never
hurt the producer) use :meth:`WaterfallBus.emit` — failures are
contained and logged, mirroring how ``HookRegistry`` treats hooks.

This module is deliberately dependency-free (stdlib only) so the
harness base stays importable from anywhere without cycles.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, TypeVar

__all__ = ["WaterfallBus"]

log = logging.getLogger("nerya.harness.events")

T = TypeVar("T")

NextFn = Callable[[], Any]
WaterfallListener = Callable[..., Any]
EventListener = Callable[[dict[str, Any]], None]


class WaterfallBus:
    """Typed-by-convention event bus with waterfall (chain) dispatch.

    Thread-safe: registrations may happen from any thread (plugin
    setup, API routes) while dispatch happens on the agent loop.
    """

    def __init__(self) -> None:
        self._waterfall: dict[str, list[WaterfallListener]] = {}
        self._events: dict[str, list[EventListener]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ waterfall

    def on(
        self,
        name: str,
        listener: WaterfallListener,
        *,
        prepend: bool = False,
    ) -> Callable[[], None]:
        """Subscribe ``listener`` to the ``name`` waterfall chain.

        Returns a disposer that removes exactly this subscription —
        the registration-as-effect contract every Nerya extension
        surface follows (see :mod:`nerya.harness.extensions`).
        """

        with self._lock:
            chain = self._waterfall.setdefault(name, [])
            if prepend:
                chain.insert(0, listener)
            else:
                chain.append(listener)

        def _dispose() -> None:
            with self._lock:
                chain = self._waterfall.get(name)
                if chain is None:
                    return
                try:
                    chain.remove(listener)
                except ValueError:
                    pass
                if not chain:
                    self._waterfall.pop(name, None)

        return _dispose

    def waterfall(
        self,
        name: str,
        payload: T,
        *,
        fallback: Callable[[T], Any] | None = None,
    ) -> Any:
        """Dispatch ``payload`` through the ``name`` chain.

        Each listener is called as ``listener(payload, next)``. The
        final ``next()`` (past the last listener) invokes ``fallback``
        when provided, otherwise returns ``payload`` unchanged.
        """

        chain = self._listeners_snapshot(self._waterfall, name)
        if not chain:
            return fallback(payload) if fallback is not None else payload

        def _run(index: int) -> Any:
            if index >= len(chain):
                return fallback(payload) if fallback is not None else payload

            def _next() -> Any:
                return _run(index + 1)

            return chain[index](payload, _next)

        return _run(0)

    # --------------------------------------------------------------- events

    def on_event(self, name: str, listener: EventListener) -> Callable[[], None]:
        """Subscribe an observation-only listener; failures contained."""

        with self._lock:
            listeners = self._events.setdefault(name, [])
            listeners.append(listener)

        def _dispose() -> None:
            with self._lock:
                bucket = self._events.get(name)
                if bucket is None:
                    return
                try:
                    bucket.remove(listener)
                except ValueError:
                    pass
                if not bucket:
                    self._events.pop(name, None)

        return _dispose

    def emit(self, name: str, **fields: Any) -> None:
        """Fire an observation event. Listener errors are logged, never raised."""

        for listener in self._listeners_snapshot(self._events, name):
            try:
                listener(fields)
            except Exception:
                log.warning("event %s listener failed", name, exc_info=True)

    # --------------------------------------------------------------- helper

    def has_listeners(self, name: str) -> bool:
        with self._lock:
            return bool(self._waterfall.get(name)) or bool(self._events.get(name))

    def _listeners_snapshot(
        self, table: dict[str, list[Any]], name: str
    ) -> list[Any]:
        with self._lock:
            return list(table.get(name, ()))

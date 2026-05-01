"""registry-driven ACP method/action declaration.

The runtime' editor-agent surface composes its method table from the live
tool/registry layer rather than a hand-coded ``dict`` in a single
file. Nerya's ACP server used to do exactly that — every supported
method was a literal in :func:`AcpServer._methods`. That made
extension awkward (every new capability needed Python edits in a
single hot file) and meant clients had no way to introspect the
method list at runtime — they had to rely on a hand-maintained
``capabilities`` blob.

This module replaces that with a typed :class:`MethodRegistry`. Each
method is a :class:`MethodSpec` with a name, handler, description,
JSON-schema-shaped params/result hints, tags, and a category bucket
(``session`` / ``tool`` / ``approvals`` / ``agent`` / ``meta``).

The server populates the registry during construction by calling
:func:`register_default_methods`, which mirrors the legacy hand-coded
table verbatim and also wires the new capabilities:

* ``session.create`` / ``session.list`` / ``session.interrupt`` /
  ``session.resume`` / ``session.branch`` — talk-track lifecycle
  helpers backed by the in-process :class:`SessionStore`;
* ``tool.list`` / ``tool.call`` / ``tool.approve`` — manifest-driven
  tool surface that funnels through the same dispatch chokepoint as
  the planner;
* ``event.subscribe`` / ``event.unsubscribe`` / ``event.poll`` —
  pub-sub style event drain so MCP/IDE clients can stream
  ``turn.start`` / ``turn.step`` / ``approval.pending`` updates over
  the same JSON-RPC pipe;
* ``meta.methods`` — introspection of every registered method,
  intended to replace the static "capabilities" blob clients used
  to consume.

The :class:`AcpServer` keeps the wire shape unchanged (line-delimited
JSON-RPC 2.0); only the routing layer becomes data-driven.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional


# --------------------------------------------------------------------- #
# Method specs + registry
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class MethodSpec:
    """One ACP JSON-RPC method.

    ``handler`` accepts the request ``params`` dict and returns the
    JSON-serialisable result body (the framing layer adds
    ``{"jsonrpc": "2.0", "id": ..., "result": ...}``). The handler may
    raise :class:`AcpError` to surface a structured JSON-RPC error.
    """

    name: str
    handler: Callable[[dict[str, Any]], Any]
    description: str = ""
    category: str = "agent"
    params_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    deprecated: bool = False

    def asdict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "params_schema": dict(self.params_schema),
            "result_schema": dict(self.result_schema),
            "tags": list(self.tags),
            "deprecated": self.deprecated,
        }


class MethodRegistry:
    """Thread-safe ACP method table.

    The legacy ``AcpServer._methods()`` returned a fresh dict each
    call. We keep the same per-server semantics but expose the table
    as a real registry so other layers (gateway, dashboard, tests)
    can introspect or extend it without monkeypatching.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_name: dict[str, MethodSpec] = {}

    def register(self, spec: MethodSpec, *, override: bool = False) -> MethodSpec:
        with self._lock:
            if not override and spec.name in self._by_name:
                raise ValueError(
                    f"acp method {spec.name!r} already registered "
                    "(pass override=True to replace)"
                )
            self._by_name[spec.name] = spec
            return spec

    def add(
        self,
        name: str,
        handler: Callable[[dict[str, Any]], Any],
        *,
        description: str = "",
        category: str = "agent",
        params_schema: dict[str, Any] | None = None,
        result_schema: dict[str, Any] | None = None,
        tags: Iterable[str] = (),
        deprecated: bool = False,
        override: bool = False,
    ) -> MethodSpec:
        spec = MethodSpec(
            name=name,
            handler=handler,
            description=description,
            category=category,
            params_schema=dict(params_schema or {}),
            result_schema=dict(result_schema or {}),
            tags=tuple(tags),
            deprecated=deprecated,
        )
        return self.register(spec, override=override)

    def get(self, name: str) -> MethodSpec | None:
        with self._lock:
            return self._by_name.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._by_name.keys())

    def specs(self) -> list[MethodSpec]:
        with self._lock:
            return [self._by_name[name] for name in sorted(self._by_name)]

    def categories(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        with self._lock:
            for spec in self._by_name.values():
                out.setdefault(spec.category, []).append(spec.name)
        for cat in out:
            out[cat] = sorted(out[cat])
        return out

    def asdict(self) -> dict[str, Any]:
        return {
            "methods": [s.asdict() for s in self.specs()],
            "categories": self.categories(),
            "total": len(self._by_name),
        }


# --------------------------------------------------------------------- #
# Session store
# --------------------------------------------------------------------- #


@dataclass
class _Session:
    id: str
    title: str
    status: str
    created_ts: str
    parent_id: str | None = None
    actor: str = ""
    tags: list[str] = field(default_factory=list)
    last_event_ts: str = ""
    interrupted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "created_ts": self.created_ts,
            "parent_id": self.parent_id,
            "actor": self.actor,
            "tags": list(self.tags),
            "last_event_ts": self.last_event_ts,
            "interrupted": self.interrupted,
            "metadata": dict(self.metadata),
        }


class SessionStore:
    """In-process session catalogue used by ``session.*`` methods.

    Sessions are intentionally lightweight — they record the lifecycle
    state (active / interrupted / resumed / branched) and act as the
    correlation key for ``event.subscribe``. The trading/turn
    journals stay the source of truth for executed work; this store
    only tracks the conversation envelope.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}

    def create(
        self,
        *,
        title: str = "",
        actor: str = "",
        parent_id: str | None = None,
        tags: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        now_iso: Callable[[], str],
    ) -> _Session:
        sid = f"sess_{uuid.uuid4().hex[:12]}"
        ts = now_iso()
        sess = _Session(
            id=sid,
            title=title or sid,
            status="active",
            created_ts=ts,
            parent_id=parent_id,
            actor=actor,
            tags=list(tags or []),
            last_event_ts=ts,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            if parent_id is not None and parent_id not in self._sessions:
                raise KeyError(f"parent session {parent_id!r} not found")
            self._sessions[sid] = sess
        return sess

    def get(self, sid: str) -> _Session | None:
        with self._lock:
            return self._sessions.get(sid)

    def require(self, sid: str) -> _Session:
        sess = self.get(sid)
        if sess is None:
            raise KeyError(f"session {sid!r} not found")
        return sess

    def list(self) -> list[_Session]:
        with self._lock:
            return [self._sessions[sid] for sid in sorted(self._sessions)]

    def update_status(
        self, sid: str, status: str, *, now_iso: Callable[[], str],
        interrupted: Optional[bool] = None,
    ) -> _Session:
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                raise KeyError(f"session {sid!r} not found")
            sess.status = status
            sess.last_event_ts = now_iso()
            if interrupted is not None:
                sess.interrupted = interrupted
            return sess


# --------------------------------------------------------------------- #
# Event bus
# --------------------------------------------------------------------- #


@dataclass
class _Subscriber:
    id: str
    kinds: tuple[str, ...]
    session_filter: str | None
    queue: list[dict[str, Any]] = field(default_factory=list)
    created_ts: str = ""

    def matches(self, event: dict[str, Any]) -> bool:
        if self.session_filter is not None:
            if event.get("session_id") != self.session_filter:
                return False
        if not self.kinds:
            return True
        kind = str(event.get("kind") or "")
        return any(_kind_matches(kind, k) for k in self.kinds)


def _kind_matches(kind: str, pattern: str) -> bool:
    """Trivial glob: ``"*"`` matches everything, ``"foo.*"`` matches the
    family, anything else is exact. We deliberately skip ``fnmatch`` to
    avoid surprising Windows path semantics."""

    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return kind.startswith(pattern[:-1])  # keep the dot
    return kind == pattern


class EventBus:
    """Pub-sub bus for ``event.subscribe`` style ACP methods.

    Backed by an in-memory queue per subscriber so the ACP layer can
    drain events without coupling to threads. Producers (turn engine,
    approvals helper, trigger emitter) call :meth:`publish`. Each
    subscriber gets at most ``max_queue`` buffered events; older ones
    are dropped with a synthetic ``event.dropped`` marker so the
    client knows it lost data.
    """

    def __init__(self, *, max_queue: int = 256) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, _Subscriber] = {}
        self._max_queue = max(8, int(max_queue))

    def subscribe(
        self,
        *,
        kinds: Iterable[str] = (),
        session_id: str | None = None,
        now_iso: Callable[[], str],
    ) -> _Subscriber:
        sid = f"sub_{uuid.uuid4().hex[:12]}"
        sub = _Subscriber(
            id=sid,
            kinds=tuple(kinds or ()),
            session_filter=session_id,
            created_ts=now_iso(),
        )
        with self._lock:
            self._subs[sid] = sub
        return sub

    def unsubscribe(self, subscription_id: str) -> bool:
        with self._lock:
            return self._subs.pop(subscription_id, None) is not None

    def publish(self, event: dict[str, Any]) -> int:
        """Fan-out an event to all matching subscribers.

        Returns the number of subscribers that received the event.
        """

        delivered = 0
        with self._lock:
            for sub in self._subs.values():
                if not sub.matches(event):
                    continue
                # Queue must stay <= max_queue. When we hit the cap we
                # drop the oldest item, append a synthetic
                # ``event.dropped`` marker, and replace the *next*
                # oldest item rather than appending — otherwise the
                # queue would grow by 1 per overflow (pop 1, push 2).
                if len(sub.queue) >= self._max_queue:
                    sub.queue.pop(0)
                    if sub.queue and sub.queue[0].get("kind") != "event.dropped":
                        sub.queue.pop(0)
                    sub.queue.append({
                        "kind": "event.dropped",
                        "ts": event.get("ts"),
                        "reason": "queue_overflow",
                    })
                sub.queue.append(dict(event))
                delivered += 1
        return delivered

    def drain(self, subscription_id: str, *, max_items: int = 64) -> list[dict[str, Any]]:
        with self._lock:
            sub = self._subs.get(subscription_id)
            if sub is None:
                raise KeyError(f"subscription {subscription_id!r} not found")
            if max_items <= 0:
                items = list(sub.queue)
                sub.queue.clear()
                return items
            head, tail = sub.queue[:max_items], sub.queue[max_items:]
            sub.queue = tail
            return head

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "subscribers": len(self._subs),
                "queued": {sid: len(s.queue) for sid, s in self._subs.items()},
                "kinds": {sid: list(s.kinds) for sid, s in self._subs.items()},
            }


# --------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------- #


class AcpError(Exception):
    """JSON-RPC compatible structured error.

    ``code`` follows the JSON-RPC 2.0 reserved ranges. Handlers raise
    these to surface ``invalid_params`` (-32602), ``not_found``
    (-32004), or domain-specific errors.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

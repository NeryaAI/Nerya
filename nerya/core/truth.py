"""Runtime truth gate.

Every data/LLM/connector response in Nerya must be explicit about whether it
originated from a real provider/connector, an explicitly-requested paper or
mock mode, or a degraded/unavailable path. This module provides the shared
types and helpers so production runtime paths never silently fabricate data.

Silent mock fallback is forbidden. A caller that truly wants mocks (tests,
demos, bootstrap) must opt in via one of:

* environment variable ``NERYA_ALLOW_MOCK_DATA=1`` (or ``=true``/``=yes``)
* passing ``allow_mock=True`` to the fetch function
* config ``runtime.mock_mode: true`` — surfaced via :func:`config_allows_mock`

Callers that do not opt in receive an empty list / explicit degraded envelope
instead of fake success data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict, field
from typing import Any, Literal, Mapping


SourceMode = Literal["live", "cached", "paper", "mock", "unavailable", "degraded"]

_TRUTHY = {"1", "true", "yes", "on"}


class MockNotAllowed(RuntimeError):
    """Raised when a runtime path tries to return mock data without opt-in."""

    def __init__(self, what: str, detail: str = "") -> None:
        msg = f"mock fallback for {what!r} is not allowed in production runtime"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)
        self.what = what
        self.detail = detail


@dataclass
class RuntimeEnvelope:
    """Explicit truth envelope attached to every data/provider result.

    Attributes:
        source:     short identifier of the upstream (``binance``, ``reddit``, ``mock``...)
        mode:       one of :data:`SourceMode` describing truthfulness
        degraded:   True when the caller explicitly got less than full data
        fallback_used: True when a mock/paper fallback replaced a real source
        error:      short error classification on degraded/unavailable results
        provider:   LLM/connector provider name if relevant
        venue:      exchange/chain venue if relevant
        connector_id: concrete connector identity if relevant
    """

    source: str
    mode: SourceMode = "live"
    degraded: bool = False
    fallback_used: bool = False
    error: str = ""
    provider: str = ""
    venue: str = ""
    connector_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        extra = d.pop("extra", None) or {}
        for k, v in extra.items():
            d.setdefault(k, v)
        return d


def env_allows_mock() -> bool:
    """True when ``NERYA_ALLOW_MOCK_DATA`` / ``NERYA_MOCK_MODE`` is set."""
    for name in ("NERYA_ALLOW_MOCK_DATA", "NERYA_MOCK_MODE"):
        if os.environ.get(name, "").lower() in _TRUTHY:
            return True
    return False


def config_allows_mock(config_like: Any | None) -> bool:
    """True when a loaded :class:`~nerya.core.config.Config` enables mock mode."""
    if config_like is None:
        return False
    getter = getattr(config_like, "get", None)
    if callable(getter):
        try:
            if bool(getter("runtime.mock_mode", False)):
                return True
            if bool(getter("runtime.paper_trading_enabled", False)) \
                    and bool(getter("runtime.mock_when_paper", False)):
                return True
        except Exception:
            return False
    return False


def resolve_allow_mock(allow_mock: bool | None = None,
                       config_like: Any | None = None) -> bool:
    """Decide whether mock fallback is authorised for this call.

    Order: explicit kwarg > env var > loaded config.
    """
    if allow_mock is not None:
        return bool(allow_mock)
    if env_allows_mock():
        return True
    if config_allows_mock(config_like):
        return True
    return False


def require_mock(what: str, *, allow_mock: bool | None = None,
                 config_like: Any | None = None, detail: str = "") -> None:
    """Raise :class:`MockNotAllowed` when mock fallback is not authorised."""
    if not resolve_allow_mock(allow_mock, config_like):
        raise MockNotAllowed(what, detail)


def degraded_envelope(source: str, *, error: str = "",
                      venue: str = "", provider: str = "") -> RuntimeEnvelope:
    """Build a ``unavailable/degraded`` envelope for empty results."""
    return RuntimeEnvelope(
        source=source or "unavailable",
        mode="unavailable",
        degraded=True,
        fallback_used=False,
        error=error,
        provider=provider,
        venue=venue,
    )


def mock_envelope(source: str = "mock", *, venue: str = "",
                  provider: str = "") -> RuntimeEnvelope:
    return RuntimeEnvelope(
        source=source,
        mode="mock",
        degraded=False,
        fallback_used=True,
        provider=provider,
        venue=venue,
    )


def live_envelope(source: str, *, venue: str = "",
                  provider: str = "",
                  connector_id: str = "") -> RuntimeEnvelope:
    return RuntimeEnvelope(
        source=source,
        mode="live",
        provider=provider,
        venue=venue,
        connector_id=connector_id,
    )


def tag_list_envelope(items: list[dict[str, Any]],
                      envelope: RuntimeEnvelope) -> list[dict[str, Any]]:
    """Annotate each item in ``items`` with ``_envelope`` metadata in-place.

    Useful when fetchers return lists of dicts — upstream consumers can either
    look at the per-item ``source`` field (backward compat) or read the
    ``_envelope`` block to see the full truth.
    """
    env = envelope.as_dict()
    for it in items:
        if isinstance(it, dict):
            it.setdefault("_envelope", env)
            it.setdefault("source", envelope.source)
    return items


__all__ = [
    "SourceMode",
    "MockNotAllowed",
    "RuntimeEnvelope",
    "env_allows_mock",
    "config_allows_mock",
    "resolve_allow_mock",
    "require_mock",
    "degraded_envelope",
    "mock_envelope",
    "live_envelope",
    "tag_list_envelope",
]

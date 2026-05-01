"""Shared helper: resolve a public, no-credential connector by venue.

The legacy ``websearch_skill`` / ``market_data_skill`` route through
``ctx.extras['_md_connectors']`` for caching. Standalone scripts have
no ``ctx``, so we instead build a lightweight connector via
:func:`nerya.connectors.registry.build_connector` with a stripped
``account_cfg`` that carries the venue alone. This works for the
public read paths (``get_ticker``, ``get_klines``, ``get_order_book``)
without a credentialed account, and matches what
``_connector_helpers.public_connector`` does internally.

Mock fallback follows the project-wide rule:
``NERYA_ALLOW_MOCK_DATA=1`` or ``runtime.mock_mode`` toggles it on.
The default is **degraded > mock** so a missing connector never
fabricates data silently.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nerya.connectors.base import Connector
from nerya.connectors.registry import build_connector
from nerya.core.truth import resolve_allow_mock


def venue_of(market: str) -> str:
    if not market:
        return ""
    if ":" in market:
        return market.split(":", 1)[0].lower()
    return ""


def public_connector(market: str, *, workspace: str | None = None) -> Connector:
    """Build a public connector for ``venue_of(market)``.

    Raises ``RuntimeError`` if the venue is unknown and mock mode is
    not authorised, so the caller can surface a clear ``degraded``
    envelope rather than fabricating data.
    """

    venue = venue_of(market)
    workspace_root = Path(workspace).expanduser().resolve() if workspace else None
    if venue in ("mock", "paper", ""):
        # Mock fallback only when explicitly authorised.
        if not resolve_allow_mock(None, None):
            raise RuntimeError(
                f"public connector for market {market!r} unavailable: "
                f"venue={venue!r} requires explicit mock authorisation "
                f"(set NERYA_ALLOW_MOCK_DATA=1)"
            )
        from nerya.connectors.mock_exchange import MockExchange

        return MockExchange()
    cfg: dict[str, Any] = {"venue": venue, "mode": "public"}
    return build_connector(cfg, workspace=workspace_root)


def workspace_root(workspace: str | None = None) -> Path:
    return Path(workspace).expanduser().resolve() if workspace else Path(os.getcwd()).resolve()


__all__ = ["public_connector", "venue_of", "workspace_root"]

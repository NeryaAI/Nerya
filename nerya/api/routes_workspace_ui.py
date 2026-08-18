"""Declarative dashboard UI routes.

The route layer is intentionally thin.  Validation, proposal construction,
and promotion live in :mod:`nerya.workspace.ui` so the same safety boundary is
available to CLI/tests and to the HTTP server.
"""

from __future__ import annotations

from typing import Any

from ..workspace import ui as workspace_ui


def _extensions(client) -> list[dict[str, Any]]:
    """Best-effort read-only skill dashboard descriptors.

    Older skill manifests do not have a ``dashboard`` field, so this helper
    deliberately treats the catalog as optional and never lets a malformed
    extension make the core UI manifest unavailable.
    """

    try:
        from .routes_capability import _dashboard_extensions

        return list(_dashboard_extensions(client) or [])
    except Exception:  # pragma: no cover - optional capability seam
        return []


def _read_handler(client, _query):
    return workspace_ui.read(client.config.paths, extensions=_extensions(client))


def _propose_handler(client, payload):
    return workspace_ui.propose(client.config.paths, payload or {})


def _apply_handler(client, payload):
    body = payload or {}
    return workspace_ui.apply(client.config.paths, body.get("proposal_id"))


def routes():
    return [
        ("GET", "/workspace/ui", _read_handler),
        ("POST", "/workspace/ui/propose", _propose_handler),
        ("POST", "/workspace/ui/apply", _apply_handler),
    ]


__all__ = ["routes"]

"""Shared utilities for every ``nerya`` subcommand.

Splitting :mod:`nerya.cli.app` into per-topic modules kept forcing the
same three tiny helpers to be re-imported everywhere — we centralise
them here so the individual command files stay flat.
"""

from __future__ import annotations

import json
from typing import Any

from ..sdk import InternalClient


def _print(data: Any) -> None:
    """Pretty-print a dict/list as indented JSON; otherwise str()."""
    if isinstance(data, (dict, list)):
        print(json.dumps(data, default=str, indent=2))
    else:
        print(data)


def _client(workspace: str | None,
            profile: str | None = None) -> InternalClient:
    """Return a configured :class:`InternalClient` for ``workspace``.

    accept an optional ``profile`` selector so commands
    can dispatch to ``$NERYA_HOME/<profile>`` without mutating env vars.
    """
    return InternalClient.boot(workspace, profile=profile)


def _add_ws(p) -> None:
    """Attach the global ``--workspace`` and ``--profile`` options to a
    subcommand. Profiles take precedence over an empty ``--workspace``
    so ``nerya skill list --profile dev`` Just Works.
    """
    p.add_argument("--workspace", default=None)
    p.add_argument("--profile", default=None,
                   help="Select a Nerya profile under $NERYA_HOME "
                        "(default: $NERYA_PROFILE / 'default').")


def _ev_args(ev: dict) -> dict:
    """Normalise a trigger-event JSON blob into kwargs for
    :meth:`InternalClient.triggers.emit`."""
    return dict(
        source=ev.get("source", "cli"),
        kind=ev["kind"],
        payload=ev.get("payload") or {},
        target=ev.get("target", "main"),
        strategy_id=ev.get("strategy_id"),
        idempotency_key=ev.get("idempotency_key"),
        dry_run=bool(ev.get("dry_run", False)),
    )


__all__ = ["_print", "_client", "_add_ws", "_ev_args"]

"""Routes mounted only when ``integrations.anet.enabled`` is true.

This module is imported by :mod:`nerya.api.local_server` solely when
the operator has explicitly opted into the AgentNetwork P2P surface.
Keep the module side-effect free and fast to import: anything that
talks to the local ``anet daemon`` (socket dial, libp2p state) must
happen inside a handler, never at module load time, so stopping the
daemon does not crash the Nerya HTTP server.

The endpoints below are intentionally minimal — enough for the anet
gateway to probe Nerya during ``svc register`` (``/anet/health``,
``/anet/meta``) and for operators to sanity-check the integration
(``/anet/status``). All business data continues to flow through the
existing Nerya routes (``/market/*``, ``/strategy_history/*``, etc.)
exposed to the peer via the whitelist.
"""

from __future__ import annotations

from typing import Any

from ..integrations.anet import whitelist as _wl
from .. import __name__ as _nerya_pkg  # noqa: F401 — keeps import graph honest


def _status(client, _params) -> dict[str, Any]:
    cfg = client.config
    block = (cfg.data.get("integrations") or {}).get("anet") or {}
    # Report the resolved whitelist so operators can verify exactly
    # which of their Nerya endpoints the anet daemon is proxying.
    extras = [p for p in (block.get("expose_paths") or []) if isinstance(p, str)]
    paths = _wl.resolve_exposed_paths(extras)
    return {
        "enabled": cfg.integration_enabled("anet"),
        "daemon_url": block.get("daemon_url") or "",
        "service_name": block.get("service_name") or "",
        "tags": list(block.get("tags") or []),
        "modes": list(block.get("modes") or []),
        "cost_model": dict(block.get("cost_model") or {}),
        "exposed_paths": paths,
        "heartbeat_seconds": int(block.get("heartbeat_seconds") or 60),
    }


def _meta(client, _params) -> dict[str, Any]:
    """Machine-readable service card consumed by ``anet svc register``.

    The anet gateway calls this at register time (CP7) and every peer
    that runs ``anet svc meta <name>`` afterwards. Keep the payload
    stable: other agents pattern-match on ``endpoints[*].path``.
    """
    cfg = client.config
    block = (cfg.data.get("integrations") or {}).get("anet") or {}
    extras = [p for p in (block.get("expose_paths") or []) if isinstance(p, str)]
    paths = _wl.resolve_exposed_paths(extras)
    endpoints = [
        {"method": m, "path": p, "description": d}
        for (m, p, d) in _wl.describe_paths(paths)
    ]
    return {
        "name": block.get("service_name") or "nerya",
        "version": "0.1.0",
        "description": block.get("description") or "",
        "tags": list(block.get("tags") or []),
        "cost_model": dict(block.get("cost_model") or {}),
        "endpoints": endpoints,
        # Hint to callers: every path here is read-only. Writes / signer
        # / wallet-touching endpoints are NEVER exposed through anet.
        "surface": "read_only",
    }


def _health(client, _params) -> dict[str, Any]:
    """Lightweight liveness probe for the anet gateway health-check.

    Separate from the global ``/health`` so operators can turn the
    integration off without flapping the Nerya service health signal.
    """
    return {"ok": True, "service": "nerya-anet"}


def routes():
    # ``/meta`` and ``/health`` are the protocol-standard paths the
    # anet gateway probes during ``svc register`` and ``svc meta``;
    # the ``/anet/...`` aliases exist so Nerya operators can curl the
    # integration-specific flavour without touching the gateway probe.
    return [
        ("GET", "/anet/health", _health),
        ("GET", "/anet/meta", _meta),
        ("GET", "/anet/status", _status),
        ("GET", "/meta", _meta),
    ]

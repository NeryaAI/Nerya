"""HTTP routes for the headless-browser engine subsystem.

Talks to :mod:`nerya.integrations.browser_engines`. Dependencies are
**only** installed when an engine is explicitly enabled by the user.

Routes:

- ``GET  /browsers/registry``       — static catalogue (no install state)
- ``GET  /browsers/status``         — install / selection state
- ``POST /browsers/select``         — set the active engine
- ``POST /browsers/configure``      — toggle engine ``enabled`` flags
- ``POST /browsers/install``        — install one engine
- ``POST /browsers/uninstall``      — uninstall one engine
- ``POST /browsers/probe``          — one-shot fetch through the engine
"""

from __future__ import annotations

from typing import Any

from ..integrations import browser_engines as be


def routes():

    def registry(_client, _payload):
        return {"ok": True, "engines": be.list_specs()}

    def status(client, _payload):
        return be.status(client.config.paths.root)

    def select(client, payload):
        body = payload or {}
        name = (body.get("name") or body.get("engine") or "").strip().lower()
        return be.configure(client.config.paths.root, selected=name)

    def configure(client, payload):
        body = payload or {}
        enabled = body.get("enabled") if isinstance(body.get("enabled"), dict) else None
        selected = body.get("selected")
        return be.configure(
            client.config.paths.root,
            selected=str(selected).strip().lower() if isinstance(selected, str) else None,
            enabled={k: bool(v) for k, v in (enabled or {}).items()} if enabled else None,
        )

    def install(client, payload):
        body = payload or {}
        name = (body.get("name") or body.get("engine") or "").strip().lower()
        if not name:
            return {"ok": False, "error": "name is required"}
        return be.install(client.config.paths.root, name)

    def uninstall(client, payload):
        body = payload or {}
        name = (body.get("name") or body.get("engine") or "").strip().lower()
        if not name:
            return {"ok": False, "error": "name is required"}
        return be.uninstall(client.config.paths.root, name)

    def probe(client, payload):
        body = payload or {}
        name = (body.get("name") or body.get("engine") or "").strip().lower() or None
        url = (body.get("url") or "https://example.com").strip()
        try:
            timeout = float(body.get("timeout_s") or 60.0)
        except Exception:
            timeout = 60.0
        result: dict[str, Any] = be.fetch(
            client.config.paths.root,
            name=name, url=url, timeout_s=timeout,
        )
        # Trim output for the dashboard — don't ship 1MB markdown blobs.
        for key in ("markdown", "text", "html"):
            value = result.get(key)
            if isinstance(value, str) and len(value) > 4000:
                result[key + "_preview"] = value[:4000] + "\n\n[truncated]"
                result.pop(key, None)
        return result

    return [
        ("GET", "/browsers/registry", registry),
        ("GET", "/browsers/status", status),
        ("POST", "/browsers/select", select),
        ("POST", "/browsers/configure", configure),
        ("POST", "/browsers/install", install),
        ("POST", "/browsers/uninstall", uninstall),
        ("POST", "/browsers/probe", probe),
    ]

"""HTTP routes for runtime feature flags.

These routes expose the in-process feature-flag registry so operators can:

- See which runtime features are currently enabled.
- Override a flag (env var or workspace JSON) without redeploying.
- Force a cache refresh after editing ``workspace/state/runtime_flags.json``.

Endpoints
~~~~~~~~~

- ``GET  /runtime/flags``          - snapshot of all known flags
- ``POST /runtime/flags/set``      - persist an override
  Body: ``{"key": "runtime.evidence_vault", "enabled": false}``
  Pass ``"enabled": null`` to clear the override.
- ``POST /runtime/flags/refresh``  - drop the in-process cache
"""

from __future__ import annotations


from ..runtime import feature_flags as ff
from ._envelope import action, debug_ref, ok


def _snapshot_handler(client, _payload):
    snap = ff.snapshot(client)
    counts = snap.get("counts", {})
    return ok(
        f"{counts.get('enabled', 0)} of {counts.get('total', 0)} runtime flag(s) enabled",
        data=snap,
        debug_refs=[debug_ref("module", "runtime.feature_flags")],
        primary_action=action(
            id="open_runtime_settings",
            label="Open runtime settings",
            href="/settings?section=runtime",
        ),
    )


def _set_handler(client, payload):
    body = payload or {}
    key = str(body.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "key_required", "_status": 400}
    enabled_raw = body.get("enabled", None)
    if enabled_raw is None:
        enabled = None
    elif isinstance(enabled_raw, bool):
        enabled = enabled_raw
    else:
        s = str(enabled_raw).strip().lower()
        if s in ("1", "true", "on", "yes"):
            enabled = True
        elif s in ("0", "false", "off", "no"):
            enabled = False
        else:
            return {"ok": False, "error": "enabled_must_be_bool", "_status": 400}
    result = ff.set_override(client, key, enabled)
    if not result.get("ok"):
        result["_status"] = 400
        return result
    return ok(
        f"flag {key} override set to {enabled!r}",
        data=result,
    )


def _refresh_handler(_client, _payload):
    ff.reset_cache()
    return ok("runtime flag cache cleared", data={"cleared": True})


def routes():
    return [
        ("GET", "/runtime/flags", _snapshot_handler),
        ("POST", "/runtime/flags/set", _set_handler),
        ("POST", "/runtime/flags/refresh", _refresh_handler),
    ]

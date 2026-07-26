"""Exchange provider HTTP endpoints.

Backs the ``Settings → Exchanges`` pane on the dashboard + lets the
agent loop discover every venue it can already talk to (so a user
asking for a missing venue can be immediately told "use ccxt:kraken"
instead of triggering ``exchange_author``). Also surfaces the
per-venue credential field schema and the optional auto-install path
used by the account-intake flow.
"""

from __future__ import annotations

from pathlib import Path

from ..connectors.ccxt_adapter import supported_exchanges
from ..connectors.registry import list_providers
from ..connectors.provider_spec import get_registry
from ..install.dep_installer import (
    DependencyInstallError,
    install as run_install,
    is_auto_install_allowed,
)


def routes():
    def list_all(client, _payload):
        ws = Path(client.config.paths.root)
        # Make sure any workspace/providers/* are hot-loaded too.
        get_registry().reload_workspace(ws)
        providers = list_providers()
        return {
            "providers": providers,
            "ccxt_supported": supported_exchanges(),
            "count": len(providers),
        }

    def ping(client, payload):
        venue = str(payload.get("venue") or "").lower().strip()
        if not venue:
            return {"ok": False, "error": "venue_required"}
        spec = get_registry().find(venue)
        if spec is None:
            return {"ok": False, "error": "unknown_venue",
                    "known": sorted({s["id"] for s in list_providers()})}
        return {"ok": True, "venue": venue, "spec": spec.to_info()}

    def credential_schema(_client, payload):
        venue = str((payload or {}).get("venue") or "").lower().strip()
        if not venue:
            return {"ok": False, "error": "venue_required"}
        spec = get_registry().find(venue)
        if spec is None:
            return {"ok": False, "error": "unknown_venue",
                    "known": sorted({s["id"] for s in list_providers()})}
        fields = [f.to_dict() for f in spec.credential_fields]
        return {
            "ok": True,
            "venue": venue,
            "provider": spec.to_info(),
            "credential_fields": fields,
            # Backward-compatible shape used by older dashboard/e2e clients.
            "schema": {"fields": fields},
            "install_command": spec.install_command,
            "install_hint": spec.install_hint,
        }

    def install_endpoint(client, payload):
        body = payload or {}
        venue = str(body.get("venue") or "").lower().strip()
        approve = bool(body.get("approve", False))
        if not venue:
            return {"ok": False, "error": "venue_required"}
        spec = get_registry().find(venue)
        if spec is None:
            return {"ok": False, "error": "unknown_venue"}
        cmd = (spec.install_command or "").strip()
        if not cmd:
            return {"ok": True, "skipped": True, "reason": "no_install_needed",
                    "venue": venue}
        allowed, reason = is_auto_install_allowed(
            client.config.data, approve=approve,
        )
        if not allowed:
            return {
                "ok": False, "error": "install_not_allowed", "reason": reason,
                "install_command": cmd,
                "install_hint": spec.install_hint,
                "venue": venue,
            }
        try:
            result = run_install(
                client.config.paths, cmd,
                config_data=client.config.data, approve=approve,
            )
        except DependencyInstallError as exc:
            return {"ok": False, "error": "install_refused", "detail": str(exc),
                    "install_command": cmd, "venue": venue}
        return {"ok": result.ok, "venue": venue, "result": result.asdict()}

    return [
        ("GET", "/exchanges/providers", list_all),
        ("POST", "/exchanges/providers", list_all),
        ("POST", "/exchanges/ping", ping),
        ("POST", "/exchanges/credential_schema", credential_schema),
        ("GET", "/exchanges/credential_schema", credential_schema),
        ("POST", "/exchanges/install", install_endpoint),
    ]

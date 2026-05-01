"""Security HTTP endpoints — SecretVault CRUD surface.

These endpoints let the dashboard Integrations tab manage encrypted API
keys (LLM / CEX / wallet / gateway) without ever exposing plaintext
values. ``list`` returns metadata + first 4 chars of a preview only, and
``reveal`` is intentionally absent — once a secret is stored Nerya never
prints it back.
"""

from __future__ import annotations

import re
from typing import Any

from ..security.secret_buffer import get_default_buffer, reset_default_buffer
from ..security.secrets import SecretVault
from ..security.web_safety import (
    WebPolicy,
    evaluate_url,
    evaluate_urls,
    make_citation,
)


_NAME_OK = re.compile(r"^[a-z][a-z0-9_\-.]{1,80}$")


def _public(meta) -> dict[str, Any]:
    return {"name": meta.name, "kind": meta.kind, "scope": meta.scope,
            "preview": meta.preview, "fingerprint": meta.fingerprint,
            "ref": f"vault://{meta.name}"}


def routes():
    def list_secrets(client, _payload):
        vault = SecretVault.open(client.config.paths.vault_enc)
        return {"refs": [_public(m) for m in vault.list()]}

    def put_secret(client, payload):
        name = str(payload.get("name") or "").strip().lower()
        value = payload.get("value")
        kind = str(payload.get("kind") or "opaque").strip().lower()
        scope = payload.get("scope")
        if not name or not _NAME_OK.match(name):
            return {"ok": False, "error": "invalid_name",
                    "detail": "use lowercase a-z0-9_-. starting with a letter"}
        if not isinstance(value, str) or not value:
            return {"ok": False, "error": "value_required"}
        if isinstance(scope, str):
            scope = [s.strip() for s in scope.split(",") if s.strip()]
        if not isinstance(scope, list):
            scope = []
        vault = SecretVault.open(client.config.paths.vault_enc)
        meta = vault.put(name=name, value=value, kind=kind,
                         scope=[str(s) for s in scope], owner="dashboard")
        return {"ok": True, "ref": _public(meta)}

    def delete_secret(client, payload):
        name = str(payload.get("name") or "").strip().lower()
        if not name:
            return {"ok": False, "error": "name_required"}
        vault = SecretVault.open(client.config.paths.vault_enc)
        try:
            vault.delete(name)
        except Exception as exc:
            return {"ok": False, "error": "delete_failed", "detail": str(exc)}
        return {"ok": True, "name": name}

    def _resolve_policy(client) -> WebPolicy:
        # read web policy from workspace if present so the
        # dashboard can pin allow/deny lists without restarting the
        # server.  We fall back to defaults silently because most
        # workspaces won't ship a custom policy file.
        paths = client.config.paths
        policy_path = paths.security / "web_policy.yml" if hasattr(paths, "security") \
            else paths.root / "security" / "web_policy.yml"
        try:
            return WebPolicy.load_from_file(policy_path)
        except Exception:
            return WebPolicy()

    def web_check(client, payload):
        url = str(payload.get("url") or "").strip()
        if not url:
            urls = payload.get("urls") or []
            if not isinstance(urls, list):
                return {"ok": False, "error": "url or urls required"}
            decisions = evaluate_urls([str(u) for u in urls],
                                      policy=_resolve_policy(client))
            return {
                "ok": True,
                "decisions": [d.to_dict() for d in decisions],
                "all_allowed": all(d.is_allowed() for d in decisions),
            }
        decision = evaluate_url(url, policy=_resolve_policy(client))
        return {"ok": True, "decision": decision.to_dict()}

    def web_citation(client, payload):
        source = str(payload.get("source") or "").strip()
        body = str(payload.get("body") or "")
        title = str(payload.get("title") or "")
        max_chars = int(payload.get("max_chars") or 800)
        fetched_at = str(payload.get("fetched_at") or "")
        if not source:
            return {"ok": False, "error": "source required"}
        try:
            citation = make_citation(
                source=source, body=body, title=title,
                max_chars=max_chars, fetched_at=fetched_at,
                policy=_resolve_policy(client),
            )
        except Exception as exc:
            return {"ok": False, "error": "rejected", "detail": str(exc)}
        return {"ok": True, "citation": citation.to_dict()}

    def buffer_list(_client, _payload):
        buffer = get_default_buffer()
        captures = buffer.list_metadata()
        return {
            "ok": True,
            "captures": [c.asdict() for c in captures],
            "count": len(captures),
        }

    def buffer_clear(_client, _payload):
        reset_default_buffer()
        return {"ok": True, "cleared": True}

    return [
        ("POST", "/security/secrets/list", list_secrets),
        ("POST", "/security/secrets/put", put_secret),
        ("POST", "/security/secrets/delete", delete_secret),
        ("GET", "/security/secrets/buffer", buffer_list),
        ("POST", "/security/secrets/buffer", buffer_list),
        ("POST", "/security/secrets/buffer/clear", buffer_clear),
        ("POST", "/security/web/check", web_check),
        ("GET", "/security/web/check", web_check),
        ("POST", "/security/web/citation", web_citation),
    ]

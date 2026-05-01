"""HTTP auth-header helper for data-source connectors.

Some upstream APIs need bespoke authentication that doesn't fit the
``api_key`` / ``api_secret`` / ``api_passphrase`` triplet — e.g. an
``Authorization: Bearer <token>`` line, an ``X-Custom-Token`` header,
or a multi-header signed request. This module lets accounts attach a
free-form ``headers`` map to their config and resolve any
``vault://<name>`` references at request time so plaintext only lives
in memory for a single call.

Header schema
-------------

Accounts store a ``provider_config.headers`` dict where each value is
either:

* A plain string (e.g. ``"X-API-Key": "raw-token"``).
* A vault reference (e.g. ``"Authorization": "Bearer vault://mytoken"``).

The token suffix can sit anywhere in the value — the resolver scans
each header for the substring ``vault://<name>``, looks the name up in
:class:`SecretVault` (scope ``exchange``), and substitutes the
plaintext just-in-time. Multiple references in a single value are
allowed.

Two callers feed into this:

1. The dashboard / agent intake form for accounts that have
   ``custom_headers_supported=True`` — those send a small
   ``[{"key", "value"}]`` array which the API converts to a dict.
2. The connector factory at runtime — it reads
   ``cfg["provider_config"]["headers"]`` and feeds it through
   :func:`resolve_headers` before issuing the request.

Vault resolution failures are logged but don't raise: a missing token
just leaves the ``vault://...`` literal in place, so the upstream API
returns a clean ``401`` instead of leaking the token name into the
error path.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..security.secrets import SecretVault


# Match ``vault://<name>`` within an arbitrary header value. Names
# follow the same rules SecretVault accepts: lowercase letters, digits,
# underscore, hyphen, dot — 2..81 chars.
_VAULT_REF_RE = re.compile(r"vault://([a-z][a-z0-9_\-.]{1,80})")


def normalize_headers_payload(raw: Any) -> dict[str, str]:
    """Coerce common UI / agent payload shapes into a dict.

    Accepts:
    * ``{"X-API-Key": "..."}`` — already a dict.
    * ``[{"key": "X-API-Key", "value": "..."}]`` — list of pairs from
      the dashboard table component.
    * ``[("X-API-Key", "...")]`` — list of tuples from CLI scripts.

    Header keys are returned as-is (HTTP headers are case-insensitive
    on the wire, but we keep the operator's casing). Empty keys or
    non-string values are dropped silently.
    """

    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()
                if isinstance(k, str) and k and isinstance(v, (str, int, float))}
    out: dict[str, str] = {}
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                k = item.get("key") or item.get("name") or item.get("header")
                v = item.get("value")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                k, v = item[0], item[1]
            else:
                continue
            if isinstance(k, str) and k and isinstance(v, (str, int, float)):
                out[k] = str(v)
    return out


def resolve_headers(
    headers: dict[str, str] | None,
    *,
    workspace: Path | None,
    vault_passphrase: str | None = None,
) -> dict[str, str]:
    """Return a copy of ``headers`` with ``vault://...`` refs expanded.

    Vault lookups go through :class:`SecretVault` with scope
    ``exchange`` (matching ``credentials`` on the same account row).
    Missing references stay literal so the resulting 4xx clearly
    points at the misconfigured header.
    """

    if not headers:
        return {}
    out: dict[str, str] = {}
    vault: SecretVault | None = None
    for key, value in headers.items():
        if not isinstance(value, str):
            value = str(value)
        if "vault://" not in value:
            out[key] = value
            continue
        if vault is None and workspace is not None:
            vault_path = Path(workspace) / "vault" / "secrets.enc"
            if vault_path.exists():
                try:
                    vault = SecretVault.open(vault_path, passphrase=vault_passphrase)
                except Exception:
                    vault = None

        def _swap(m: "re.Match[str]") -> str:
            if vault is None:
                return m.group(0)
            try:
                v = vault.resolve(m.group(1), required_scope="exchange")
                return v if v else m.group(0)
            except Exception:
                return m.group(0)

        out[key] = _VAULT_REF_RE.sub(_swap, value)
    return out


def headers_metadata(headers: dict[str, str] | None) -> list[dict[str, Any]]:
    """Return metadata-only descriptors safe to send back over HTTP.

    Strips any value that would be a vault ref to ``"vault://***"`` and
    truncates plaintext to a 4-character preview, so the dashboard can
    render a "Configured headers" table without leaking secrets.
    """

    out: list[dict[str, Any]] = []
    for key, value in (headers or {}).items():
        if not isinstance(value, str):
            value = str(value)
        if "vault://" in value:
            mask = _VAULT_REF_RE.sub(lambda m: f"vault://{m.group(1)[:4]}***", value)
            out.append({"key": key, "value": mask, "kind": "vault_ref"})
        else:
            mask = value[:4] + ("***" if len(value) > 4 else "")
            out.append({"key": key, "value": mask, "kind": "plaintext"})
    return out


__all__ = ["normalize_headers_payload", "resolve_headers", "headers_metadata"]

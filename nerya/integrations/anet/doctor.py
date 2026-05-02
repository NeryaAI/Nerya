"""Self-check for the ANET integration.

``nerya anet doctor`` invokes :func:`run_doctor` to produce a machine-
readable report: is the daemon reachable, is the optional ``anet`` pip
extra installed, does the configured token look usable, is the local
Nerya API answering on the host the daemon will proxy to. No
registration is attempted here — the doctor is pure diagnosis.
"""

from __future__ import annotations

from typing import Any

from ..anet import whitelist as _wl


def _check_sdk() -> dict[str, Any]:
    """Soft-import ``anet.svc`` without raising if missing.

    The pip extra is optional; doctor surfaces the actionable install
    hint instead of exploding the whole CLI.
    """
    try:
        import anet.svc  # noqa: F401 — import for side-effect check only
        import anet  # noqa: F401
        ver = getattr(__import__("anet"), "__version__", "unknown")
        return {"ok": True, "version": ver}
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "install_hint": "pip install 'nerya[anet]'",
        }


def _check_daemon(base_url: str, token: str | None) -> dict[str, Any]:
    """Probe ``GET /api/status`` on the anet daemon.

    The starter-template uses this path as its readiness signal; if
    the daemon is alive it returns JSON with a ``peer_id`` field.
    """
    try:
        import httpx
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "reason": f"httpx missing: {exc}"}
    if not base_url:
        return {"ok": False, "reason": "integrations.anet.daemon_url is empty"}
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.get(
            base_url.rstrip("/") + "/api/status",
            headers=headers, timeout=3.0,
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "hint": "is the anet daemon running? try `anet daemon &`",
        }
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "body": r.text[:200]}
    data: dict[str, Any] = {}
    try:
        data = r.json()
    except Exception:
        pass
    return {
        "ok": True,
        "peer_id": data.get("peer_id"),
        "did": data.get("did"),
        "peers": data.get("peers"),
    }


def _check_nerya_api(api_url: str) -> dict[str, Any]:
    """Probe the Nerya local API — the daemon will proxy to this URL."""
    try:
        import httpx
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "reason": f"httpx missing: {exc}"}
    try:
        r = httpx.get(api_url.rstrip("/") + "/anet/health", timeout=3.0)
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "hint": "start nerya (nerya serve or the local launcher)",
        }
    return {"ok": r.status_code == 200, "status": r.status_code}


def _resolve_token(config, token_ref: str) -> str:
    """Resolve ``secret:anet/api_token`` via the Nerya vault.

    Returns ``""`` when the ref is empty, the vault is unavailable, or
    the secret is missing. The doctor reports this as a warning but
    not a hard failure — registration will fail later and surface a
    clearer error.
    """
    if not token_ref:
        return ""
    # Accept raw tokens for local dev where no vault entry exists yet.
    if not token_ref.startswith("secret:"):
        return token_ref
    try:
        from ..security.vault import SecretVault  # type: ignore
    except Exception:
        # Fall back to a looser lookup via InternalClient.
        return ""
    try:
        vault = SecretVault(config.paths.root)
        name = token_ref.split(":", 1)[1]
        return vault.get_plaintext(name) or ""
    except Exception:
        return ""


def run_doctor(config) -> dict[str, Any]:
    """Build the full doctor report for the active workspace."""
    block = (config.data.get("integrations") or {}).get("anet") or {}
    enabled = bool(config.integration_enabled("anet"))
    daemon_url = str(block.get("daemon_url") or "")
    token = _resolve_token(config, str(block.get("token_ref") or ""))
    api_block = config.data.get("api") or {}
    api_host = str(api_block.get("host") or "127.0.0.1")
    api_port = int(api_block.get("port") or 18317)
    api_url = f"http://{api_host}:{api_port}"

    extras = [p for p in (block.get("expose_paths") or []) if isinstance(p, str)]
    resolved_paths = _wl.resolve_exposed_paths(extras)
    rejected = [p for p in extras if p not in resolved_paths]

    report: dict[str, Any] = {
        "enabled": enabled,
        "disable_env_set": (
            __import__("os").environ.get("NERYA_DISABLE_INTEGRATIONS", "")
        ).lower() in ("1", "true", "yes", "on"),
        "sdk": _check_sdk(),
        "daemon": _check_daemon(daemon_url, token),
        "nerya_api": _check_nerya_api(api_url),
        "token_ref": block.get("token_ref") or "",
        "token_resolved": bool(token),
        "exposed_paths": resolved_paths,
        "rejected_extras": rejected,
        "service_name": block.get("service_name") or "",
        "daemon_url": daemon_url,
        "api_url": api_url,
    }
    # Summarise so the CLI can print a one-line verdict.
    report["ready"] = (
        enabled
        and report["sdk"].get("ok")
        and report["daemon"].get("ok")
        and report["nerya_api"].get("ok")
    )
    return report


__all__ = ["run_doctor"]

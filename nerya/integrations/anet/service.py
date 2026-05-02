"""ANET register loop — publishes Nerya as a discoverable P2P service.

Run by ``nerya anet register`` (foreground) or by a host service unit
installed via ``nerya service install --unit anet-register``. Exits
cleanly with code 0 when ``integrations.anet.enabled`` is false so
composed process supervisors (systemd, launchd, NSSM) can hard-wire
this alongside the Nerya API without gating each one in shell.

Design points, each mirroring the starter-template behaviour:

* The optional ``anet`` pip extra is imported **inside** :func:`main`,
  not at module load. Missing the extra while the integration is
  disabled must never crash anything.
* The register payload is rebuilt from :data:`Nerya config` on every
  heartbeat (default 60s) so yaml edits propagate without a restart.
* SIGINT / SIGTERM unregister cleanly before exit.
* The daemon re-registration cadence is necessary because anet v1.1
  does not persist registrations across daemon restarts; without the
  heartbeat the service silently disappears after an anet crash.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time
from typing import Any

from ..anet import whitelist as _wl


class _HttpxSvcClient:
    """Minimal drop-in replacement for ``anet.svc.SvcClient``.

    PyPI's ``anet`` package is currently a 0.0.1 placeholder; the
    real SDK ships with the ``anet`` CLI but is not pip-installable
    as of v1.1.11. This shim speaks the three REST endpoints the
    register loop actually needs:

    * ``POST /api/svc/register``   — publish a service.
    * ``POST /api/svc/unregister`` — retract a service by name.
    * ``GET  /api/svc/list``       — used by callers for debugging.

    When the official pip package catches up, the top-level import
    flips back to ``from anet.svc import SvcClient`` and this shim
    is bypassed without any call-site changes.
    """

    def __init__(self, base_url: str, token: str):
        import httpx
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=15.0,
        )

    def register(self, **kwargs) -> dict:
        # Drop keys with None values so the daemon doesn't complain
        # about unexpected nulls in the cost_model.
        payload = {k: v for k, v in kwargs.items() if v is not None}
        r = self._client.post("/api/svc/register", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"register HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except Exception:
            return {}

    def unregister(self, name: str) -> dict:
        r = self._client.post("/api/svc/unregister", json={"name": name})
        if r.status_code >= 400:
            raise RuntimeError(f"unregister HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except Exception:
            return {}

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def _short_host_hash() -> str:
    """4-byte deterministic suffix so two laptops don't collide on service name."""
    import hashlib
    h = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:6]
    return h


def _resolve_token(config, token_ref: str) -> str:
    """Same resolution rules as :mod:`doctor`; kept local to avoid a cycle."""
    if not token_ref:
        return ""
    if not token_ref.startswith("secret:"):
        return token_ref
    try:
        from ..security.vault import SecretVault  # type: ignore
        vault = SecretVault(config.paths.root)
        name = token_ref.split(":", 1)[1]
        return vault.get_plaintext(name) or ""
    except Exception:
        return ""


def _wait_for_nerya_api(host: str, port: int, *, timeout: float = 30.0) -> bool:
    """Block until ``/anet/health`` responds or ``timeout`` elapses."""
    try:
        import httpx
    except Exception:
        return False
    url = f"http://{host}:{port}/anet/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _build_register_kwargs(config) -> dict[str, Any]:
    """Translate Nerya yaml into the ``SvcClient.register`` call shape."""
    block = (config.data.get("integrations") or {}).get("anet") or {}
    api_block = config.data.get("api") or {}
    host = str(api_block.get("host") or "127.0.0.1")
    port = int(api_block.get("port") or 18317)

    name = str(block.get("service_name") or "").strip()
    if not name:
        name = "nerya"
    if not name.endswith(_short_host_hash()):
        name = f"{name}-{_short_host_hash()}"

    extras = [p for p in (block.get("expose_paths") or []) if isinstance(p, str)]
    paths = _wl.resolve_exposed_paths(extras)

    cost = block.get("cost_model") or {}
    free = bool(cost.get("free", True))
    per_call = cost.get("per_call")
    per_kb = cost.get("per_kb")
    deposit = cost.get("deposit")
    # Any explicit cost beats the free flag — mirrors starter-template.
    if per_call or per_kb or deposit:
        free = False

    return {
        "name": name,
        "endpoint": f"http://{host}:{port}",
        "paths": paths,
        "modes": list(block.get("modes") or ["rr"]),
        "tags": list(block.get("tags") or []),
        "description": str(block.get("description") or "Nerya"),
        "health_check": "/anet/health",
        "meta_path": "/anet/meta",
        "free": free,
        "per_call": per_call if not free else None,
        "per_kb": per_kb if not free else None,
        "deposit": deposit,
    }


def main(config=None, *, argv: list[str] | None = None) -> int:
    """Entry point. ``argv`` is accepted for future flags; unused today."""
    del argv  # reserved

    if config is None:
        from ...core.config import load_config
        config = load_config()

    if not config.integration_enabled("anet"):
        # Exit 0 so host-service units don't flap when the operator
        # turns the integration off. A dedicated log line makes the
        # no-op observable.
        print("[anet] integration disabled — exit (config: integrations.anet.enabled=false)",
              flush=True)
        return 0

    block = (config.data.get("integrations") or {}).get("anet") or {}
    daemon_url = str(block.get("daemon_url") or "http://127.0.0.1:3998")
    token = _resolve_token(config, str(block.get("token_ref") or ""))
    if not token:
        print("[anet] ERROR: no ANET api token resolved. "
              "Set integrations.anet.token_ref to a secret: ref "
              "or export ANET_TOKEN.", file=sys.stderr, flush=True)
        # Fall back to ANET_TOKEN env so Windows dev flows still work.
        token = os.environ.get("ANET_TOKEN", "")
    if token:
        os.environ.setdefault("ANET_TOKEN", token)
    os.environ.setdefault("ANET_BASE_URL", daemon_url)

    api_block = config.data.get("api") or {}
    api_host = str(api_block.get("host") or "127.0.0.1")
    api_port = int(api_block.get("port") or 18317)

    print(f"[anet] waiting for Nerya API on {api_host}:{api_port} …", flush=True)
    if not _wait_for_nerya_api(api_host, api_port):
        print("[anet] ERROR: Nerya API did not answer /anet/health — "
              "start it first (e.g. `nerya serve`).",
              file=sys.stderr, flush=True)
        return 3

    try:
        from anet.svc import SvcClient, SvcAPIError, AuthMissingError  # type: ignore
        svc = SvcClient(base_url=daemon_url)
        use_sdk = True  # noqa: F841 — retained for future branching
    except (ImportError, ModuleNotFoundError, AttributeError) as exc:
        # PyPI's ``anet`` 0.0.1 is a placeholder; the real SDK ships
        # with the ``anet`` daemon and isn't on pip yet. Fall back to
        # a tiny httpx-based client that speaks the same two endpoints
        # we actually need.
        print(f"[anet] anet.svc SDK unavailable ({exc}); "
              "using built-in httpx fallback client",
              file=sys.stderr, flush=True)
        svc = _HttpxSvcClient(base_url=daemon_url, token=token)
        SvcAPIError = RuntimeError  # noqa: N806
    except Exception as exc:
        print(f"[anet] {exc}", file=sys.stderr, flush=True)
        return 1

    heartbeat = max(10, int(block.get("heartbeat_seconds") or 60))
    service_name_holder: dict[str, str] = {"name": ""}

    def register_once() -> None:
        kwargs = _build_register_kwargs(config)
        service_name_holder["name"] = kwargs["name"]
        try:
            resp = svc.register(**kwargs)
        except SvcAPIError as exc:
            print(f"[anet] register failed: {exc}", file=sys.stderr, flush=True)
            return
        ans = (resp or {}).get("ans") or {}
        print(
            f"[anet] ✓ registered name={kwargs['name']} "
            f"paths={len(kwargs['paths'])} free={kwargs['free']} "
            f"ans.published={ans.get('published')} uri={ans.get('uri')}",
            flush=True,
        )

    def shutdown(*_args) -> None:
        name = service_name_holder.get("name") or ""
        if name:
            try:
                svc.unregister(name)
                print(f"[anet] unregistered {name}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[anet] unregister failed (non-fatal): {exc}",
                      file=sys.stderr, flush=True)
        try:
            svc.close()
        except Exception:
            pass
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    try:
        signal.signal(signal.SIGTERM, shutdown)
    except (AttributeError, ValueError):
        # SIGTERM may not be available on Windows non-main threads.
        pass

    register_once()
    while True:
        time.sleep(heartbeat)
        register_once()


__all__ = ["main"]

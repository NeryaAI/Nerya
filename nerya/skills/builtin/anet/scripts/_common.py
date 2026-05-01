"""Shared helpers for the ``anet`` skill's scripts.

Scripts import from here rather than from
:mod:`nerya.integrations.anet.service` so the skill layer keeps its
"one action = one script" shape and does not entangle with the
register-loop module.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _load_config_from_env():
    """Resolve the active Nerya workspace without the CLI."""
    from nerya.core.config import load_config
    ws = os.environ.get("NERYA_WORKSPACE") or os.environ.get("NERYA_HOME") or None
    if ws:
        return load_config(Path(ws).expanduser())
    return load_config()


def require_anet(*, require_outbound: bool = True) -> dict[str, Any]:
    """Guard every script entry point.

    Returns the resolved ``integrations.anet`` block on success;
    raises :class:`SystemExit` with a JSON error on failure. Separating
    the guard keeps the scripts small and the refusal message uniform.
    """
    cfg = _load_config_from_env()
    if not cfg.integration_enabled("anet"):
        err = {
            "ok": False,
            "error": "anet_disabled",
            "reason": "integrations.anet.enabled is false (or "
                      "NERYA_DISABLE_INTEGRATIONS is set); enable it "
                      "in the workspace yaml before using this skill.",
        }
        print(err)
        raise SystemExit(2)
    block = (cfg.data.get("integrations") or {}).get("anet") or {}
    if require_outbound:
        out = (block.get("outbound") or {})
        if not out.get("skill_enabled"):
            err = {
                "ok": False,
                "error": "anet_outbound_disabled",
                "reason": "integrations.anet.outbound.skill_enabled is "
                          "false; only inbound (register) is active.",
            }
            print(err)
            raise SystemExit(2)
    return block


def get_client(block: dict[str, Any]):
    """Return a configured ``SvcClient`` or exit with an install hint."""
    try:
        from anet.svc import SvcClient  # type: ignore
    except Exception as exc:
        err = {
            "ok": False,
            "error": "anet_sdk_missing",
            "reason": f"{type(exc).__name__}: {exc}",
            "install_hint": "pip install 'nerya[anet]'",
        }
        print(err)
        raise SystemExit(3)
    base_url = str(block.get("daemon_url") or "http://127.0.0.1:13921")
    # SvcClient reads ANET_TOKEN / ANET_BASE_URL from env; service.py
    # already populates them when running in-process. Set them here too
    # so ad-hoc ``python -m ... scripts.discover`` works.
    os.environ.setdefault("ANET_BASE_URL", base_url)
    token_ref = str(block.get("token_ref") or "")
    if token_ref and not os.environ.get("ANET_TOKEN"):
        # Best-effort vault lookup; falls through to env if unavailable.
        if token_ref.startswith("secret:"):
            try:
                from nerya.core.config import load_config
                cfg = load_config()
                from nerya.security.vault import SecretVault  # type: ignore
                vault = SecretVault(cfg.paths.root)
                name = token_ref.split(":", 1)[1]
                tok = vault.get_plaintext(name) or ""
                if tok:
                    os.environ["ANET_TOKEN"] = tok
            except Exception:
                pass
        else:
            os.environ["ANET_TOKEN"] = token_ref
    return SvcClient(base_url=base_url)


__all__ = ["require_anet", "get_client"]

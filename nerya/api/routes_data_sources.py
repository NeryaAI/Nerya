"""HTTP routes for managing data-source API keys.

Currently scoped to the **Financial Datasets API** (used by
``nerya/data/equities.py`` and the equity research / DCF / SEC filing
skills). Mirrors the multi-key-rotation pattern used by
``routes_search.py``: keys are stored in the SecretVault as a
comma-separated string under ``vault://financial_datasets.keys``.

Routes:

- ``GET  /data/financial_datasets/status``  — readiness, per-source key counts
- ``POST /data/financial_datasets/keys``    — patch key list (vault by default)
"""

from __future__ import annotations

import os
from typing import Any

from ..security.secret_buffer import get_default_buffer
from ..security.secret_scanner import expand_placeholders
from ..security.secrets import SecretVault


_VAULT_NAME = "financial_datasets.keys"
_LEGACY_VAULT_SINGLE = "financial_datasets_api_key"


def _split_keys(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            text = str(v or "").strip()
            if text:
                out.append(text)
        return out
    return [p.strip() for p in str(value).replace("\n", ",").split(",") if p and p.strip()]


def _open_vault(client) -> SecretVault | None:
    path = client.config.paths.vault_enc
    try:
        return SecretVault.open(path)
    except Exception:
        return None


def _vault_keys(vault: SecretVault | None) -> list[str]:
    if vault is None:
        return []
    keys: list[str] = []
    seen: set[str] = set()
    for name in (_VAULT_NAME, _LEGACY_VAULT_SINGLE):
        try:
            raw = vault.resolve(name)
        except Exception:
            continue
        for k in _split_keys(raw):
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys


def _env_keys() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    legacy = _split_keys(os.environ.get("FINANCIAL_DATASETS_API_KEY"))
    multi = _split_keys(os.environ.get("NERYA_FINANCIAL_DATASETS_KEYS"))
    if legacy:
        out["FINANCIAL_DATASETS_API_KEY"] = legacy
    if multi:
        out["NERYA_FINANCIAL_DATASETS_KEYS"] = multi
    return out


def _expand_key_secret_placeholders(values: Any) -> tuple[list[str], str | None]:
    """Expand one or more placeholder tokens in key payload."""
    keys: list[str] = []
    if values is None:
        return keys, None

    expanded_values: list[str] = []
    buffer = get_default_buffer()

    def _expand_value(raw: Any) -> tuple[str, str | None]:
        text = str(raw or "").strip()
        if not text:
            return "", None
        if "<<NERYA_SECRET:" not in text:
            return text, None
        expanded, _resolved = expand_placeholders(
            text, buffer=buffer, consume=True,
        )
        if "<<NERYA_SECRET:" in expanded:
            return expanded, "unresolved_secret_token"
        return expanded, None

    if isinstance(values, list):
        for item in values:
            expanded, error = _expand_value(item)
            if error:
                return [], error
            if expanded:
                expanded_values.append(expanded)
    else:
        expanded, error = _expand_value(values)
        if error:
            return [], error
        if expanded:
            expanded_values.append(expanded)

    return _split_keys(expanded_values), None


def _build_status(client) -> dict[str, Any]:
    vault = _open_vault(client)
    vault_list = _vault_keys(vault)
    env_blob = _env_keys()
    env_total = sum(len(v) for v in env_blob.values())
    total = len(vault_list) + env_total
    preview = [k[:4] + "…" + k[-2:] for k in vault_list]
    return {
        "ok": True,
        "name": "financial_datasets",
        "ready": total > 0,
        "total_keys": total,
        "vault_count": len(vault_list),
        "env_count": env_total,
        "env_sources": list(env_blob.keys()),
        "vault_ref": f"vault://{_VAULT_NAME}",
        "key_preview": preview,
        "endpoints": [
            "GET  /data/financial_datasets/status",
            "POST /data/financial_datasets/keys",
        ],
        "documentation": "https://docs.financialdatasets.ai",
    }


def routes():

    def status(client, _payload):
        return _build_status(client)

    def set_keys(client, payload):
        body = payload or {}
        store = (body.get("store") or "vault").strip().lower()
        raw_keys = body.get("keys")
        keys, expand_error = _expand_key_secret_placeholders(raw_keys)
        if expand_error is not None:
            return {"ok": False, "error": "invalid_secret_token",
                    "detail": "secret token was missing/expired; please re-enter the key"}

        if store == "workspace":
            # Pure plaintext path — write keys to a JSON file the
            # equities client also reads (env-style fallback).
            from pathlib import Path
            import json
            target = Path(client.config.paths.root) / "financial_datasets.json"
            if not keys:
                if target.exists():
                    try:
                        target.unlink()
                    except Exception:
                        pass
                return _build_status(client)
            target.write_text(
                json.dumps({"keys": keys}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # Also propagate to env for current process.
            os.environ["NERYA_FINANCIAL_DATASETS_KEYS"] = ",".join(keys)
            return _build_status(client)

        vault = _open_vault(client)
        if vault is None:
            return {
                "ok": False,
                "error": "vault_unavailable",
                "detail": "could not open the workspace SecretVault",
            }
        if not keys:
            try:
                vault.delete(_VAULT_NAME)
            except Exception:
                pass
            return _build_status(client)
        try:
            vault.put(
                name=_VAULT_NAME,
                value=",".join(keys),
                kind="api_keys",
                scope=["data", "financial_datasets"],
                owner="dashboard",
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return _build_status(client)

    return [
        ("GET", "/data/financial_datasets/status", status),
        ("POST", "/data/financial_datasets/keys", set_keys),
    ]

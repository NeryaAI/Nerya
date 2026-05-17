"""Vault-backed runtime environment variables.

The dashboard lets operators configure environment variables that must
be visible to shell-launched skill scripts and stdio MCP subprocesses.
Values live only in :class:`SecretVault`; this module exposes metadata
for management and plaintext values only to process-spawn boundaries.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

from ..core.paths import WorkspacePaths
from ..core.proxy import proxy_env_for_workspace
from .secrets import SecretMeta, SecretVault


ENV_SECRET_PREFIX = "env."
DEFAULT_ENV_SCOPES = ("env", "shell", "mcp.read")
_ENV_NAME_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _paths(value: WorkspacePaths | Path | str) -> WorkspacePaths:
    if isinstance(value, WorkspacePaths):
        return value
    return WorkspacePaths(root=Path(value))


def normalize_env_name(name: str) -> str:
    env_name = str(name or "").strip()
    if not _ENV_NAME_OK.match(env_name):
        raise ValueError("invalid env var name; use A-Z, 0-9 and _; do not start with a digit")
    return env_name.upper()


def secret_name_for_env(name: str) -> str:
    return f"{ENV_SECRET_PREFIX}{normalize_env_name(name).lower()}"


def env_name_from_secret_name(secret_name: str) -> str:
    if not secret_name.startswith(ENV_SECRET_PREFIX):
        return ""
    raw = secret_name[len(ENV_SECRET_PREFIX):].strip()
    if not raw:
        return ""
    env_name = raw.upper()
    return env_name if _ENV_NAME_OK.match(env_name) else ""


def is_runtime_env_meta(meta: SecretMeta) -> bool:
    return meta.kind == "env" and bool(env_name_from_secret_name(meta.name))


def public_env_row(meta: SecretMeta) -> dict[str, Any]:
    env_name = env_name_from_secret_name(meta.name)
    return {
        "name": env_name,
        "secret_name": meta.name,
        "kind": meta.kind,
        "scope": list(meta.scope),
        "owner": meta.owner,
        "created_at": meta.created_at,
        "preview": meta.preview,
        "fingerprint": meta.fingerprint,
        "ref": meta.ref(),
    }


def list_runtime_env(workspace: WorkspacePaths | Path | str) -> list[dict[str, Any]]:
    paths = _paths(workspace)
    vault = SecretVault.open(paths.vault_enc)
    rows = [
        public_env_row(meta)
        for meta in vault.list()
        if is_runtime_env_meta(meta)
    ]
    rows.sort(key=lambda row: row["name"])
    return rows


def put_runtime_env(
    workspace: WorkspacePaths | Path | str,
    *,
    name: str,
    value: str,
    owner: str = "settings",
) -> dict[str, Any]:
    if not isinstance(value, str):
        raise ValueError("env value must be a string")
    env_name = normalize_env_name(name)
    vault = SecretVault.open(_paths(workspace).vault_enc)
    meta = vault.put(
        name=secret_name_for_env(env_name),
        value=value,
        kind="env",
        scope=list(DEFAULT_ENV_SCOPES),
        owner=owner,
    )
    return public_env_row(meta)


def delete_runtime_env(workspace: WorkspacePaths | Path | str, *, name: str) -> str:
    env_name = normalize_env_name(name)
    vault = SecretVault.open(_paths(workspace).vault_enc)
    vault.delete(secret_name_for_env(env_name))
    return env_name


def runtime_env_values(workspace: WorkspacePaths | Path | str) -> dict[str, str]:
    paths = _paths(workspace)
    vault = SecretVault.open(paths.vault_enc)
    out: dict[str, str] = {}
    for meta in vault.list():
        if not is_runtime_env_meta(meta):
            continue
        env_name = env_name_from_secret_name(meta.name)
        if not env_name:
            continue
        out[env_name] = vault.resolve(meta.name, required_scope="env")
    return out


def build_process_env(
    base: Mapping[str, str] | None,
    workspace: WorkspacePaths | Path | str,
) -> dict[str, str]:
    paths = _paths(workspace)
    env = dict(base or os.environ)
    env.setdefault("NERYA_WORKSPACE", str(paths.root))
    env.update(runtime_env_values(paths))
    try:
        env.update(proxy_env_for_workspace(paths))
    except Exception:
        pass
    return env


__all__ = [
    "DEFAULT_ENV_SCOPES",
    "ENV_SECRET_PREFIX",
    "build_process_env",
    "delete_runtime_env",
    "env_name_from_secret_name",
    "is_runtime_env_meta",
    "list_runtime_env",
    "normalize_env_name",
    "public_env_row",
    "put_runtime_env",
    "runtime_env_values",
    "secret_name_for_env",
]

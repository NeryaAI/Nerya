"""SecretVault — the only place in Nerya that sees raw secret values."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.atomic_write import atomic_write_bytes
from ..core.errors import SecretAccessDenied, SecretNotFoundError
from ..core.redaction import fingerprint, preview
from ..core.time import now_iso
from . import encryption


@dataclass
class SecretMeta:
    name: str
    kind: str
    scope: list[str]
    owner: str
    created_at: str
    fingerprint: str
    preview: str

    def ref(self) -> str: return f"vault://{self.name}"

    def as_public(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "scope": self.scope,
            "owner": self.owner, "ref": self.ref(),
            "preview": self.preview, "sha12": self.fingerprint,
        }


@dataclass
class SecretVault:
    path: Path
    passphrase: str
    _cache: dict[str, str] = field(default_factory=dict, init=False)
    _meta: dict[str, SecretMeta] = field(default_factory=dict, init=False)
    _loaded: bool = field(default=False, init=False)

    @classmethod
    def open(cls, workspace_vault_file: Path, passphrase: str | None = None) -> "SecretVault":
        pp = passphrase or os.environ.get("NERYA_VAULT_PASSPHRASE") or "nerya-default-passphrase"
        v = cls(path=Path(workspace_vault_file), passphrase=pp)
        v._load()
        return v

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._cache = {}
        self._meta = {}
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        try:
            env = encryption.Envelope.from_dict(json.loads(self.path.read_bytes()))
            raw = encryption.unseal(env, self.passphrase)
            doc = json.loads(raw.decode("utf-8"))
        except Exception:
            return
        for item in doc.get("secrets", []):
            name = item["name"]
            self._cache[name] = item["value"]
            self._meta[name] = SecretMeta(
                name=name, kind=item.get("kind", "opaque"),
                scope=item.get("scope", []), owner=item.get("owner", "runtime"),
                created_at=item.get("created_at", now_iso()),
                fingerprint=fingerprint(item["value"]),
                preview=preview(item["value"]),
            )

    def _flush(self) -> None:
        doc = {
            "secrets": [
                {
                    "name": name,
                    "value": self._cache[name],
                    "kind": m.kind,
                    "scope": m.scope,
                    "owner": m.owner,
                    "created_at": m.created_at,
                }
                for name, m in self._meta.items()
            ]
        }
        raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        env = encryption.seal(raw, self.passphrase)
        atomic_write_bytes(self.path, json.dumps(env.to_dict()).encode("utf-8"))

    # ---------- public API ----------
    def put(self, *, name: str, value: str, kind: str, scope: list[str],
            owner: str = "runtime") -> SecretMeta:
        self._cache[name] = value
        meta = SecretMeta(
            name=name, kind=kind, scope=scope, owner=owner,
            created_at=now_iso(),
            fingerprint=fingerprint(value),
            preview=preview(value),
        )
        self._meta[name] = meta
        self._flush()
        return meta

    def list(self) -> list[SecretMeta]:
        return list(self._meta.values())

    def meta(self, name: str) -> SecretMeta:
        if name not in self._meta:
            raise SecretNotFoundError(name)
        return self._meta[name]

    def resolve(self, name: str, *, required_scope: str | None = None) -> str:
        if name not in self._cache:
            raise SecretNotFoundError(name)
        if required_scope and required_scope not in self._meta[name].scope:
            raise SecretAccessDenied(f"secret {name} lacks scope {required_scope}")
        return self._cache[name]

    def public_ref(self, name: str) -> dict[str, Any]:
        return self.meta(name).as_public()

    def delete(self, name: str) -> None:
        self._cache.pop(name, None)
        self._meta.pop(name, None)
        self._flush()

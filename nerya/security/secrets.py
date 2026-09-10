"""SecretVault — the only place in Nerya that sees raw secret values."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.atomic_write import atomic_write_bytes
from ..core.errors import SecretAccessDenied, SecretNotFoundError
from ..core.redaction import fingerprint, preview
from ..core.time import now_iso
from . import encryption

log = logging.getLogger(__name__)

# Published-in-source fallback used only so dev/test workspaces without a
# configured passphrase keep working. Live trading paths independently
# refuse to run without NERYA_VAULT_PASSPHRASE (see trading/submit.py),
# so this default only ever protects at-rest credential storage — which
# is why falling back to it is loud, never silent.
_DEFAULT_PASSPHRASE = "nerya-default-passphrase"
_default_pp_warned = False


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
    #: Set when the on-disk vault existed but could not be decrypted /
    #: parsed. Empty vault + non-empty load_error means "unreadable",
    #: never "nothing stored".
    load_error: str = field(default="", init=False)
    _cache: dict[str, str] = field(default_factory=dict, init=False)
    _meta: dict[str, SecretMeta] = field(default_factory=dict, init=False)
    _loaded: bool = field(default=False, init=False)

    @classmethod
    def open(cls, workspace_vault_file: Path, passphrase: str | None = None) -> "SecretVault":
        global _default_pp_warned
        pp = passphrase or os.environ.get("NERYA_VAULT_PASSPHRASE") or ""
        if not pp:
            pp = _DEFAULT_PASSPHRASE
            if not _default_pp_warned:
                _default_pp_warned = True
                log.warning(
                    "SecretVault: NERYA_VAULT_PASSPHRASE is not set — falling "
                    "back to the built-in default passphrase. At-rest "
                    "encryption is NOT protection until you set a real "
                    "passphrase (live trading is blocked without one)."
                )
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
        except Exception as exc:
            # A vault that exists but cannot be read (wrong passphrase,
            # corrupt/truncated file) must not silently masquerade as an
            # empty vault: every later resolve() would look like "not
            # configured" instead of "vault unreadable".
            self.load_error = f"{type(exc).__name__}: {exc}"
            log.error(
                "SecretVault at %s could not be loaded (%s) — treating as "
                "empty. Secrets stored in it are NOT gone; fix the "
                "passphrase or restore the file.",
                self.path, self.load_error,
            )
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
        if self.passphrase == _DEFAULT_PASSPHRASE:
            # At-rest encryption under a source-published constant is not
            # protection: anyone with the vault file gets every credential
            # in it. Reading back what a previous version stored stays
            # possible, but storing NEW secrets requires a real passphrase.
            raise SecretAccessDenied(
                "refusing to store secrets under the built-in default vault "
                "passphrase. Set NERYA_VAULT_PASSPHRASE (or pass "
                "passphrase= to SecretVault.open) before storing secrets."
            )
        if self.load_error and self.path.exists():
            # The on-disk vault exists but could not be decrypted. _flush()
            # rewrites the file from the in-memory cache only, so storing
            # anything now would silently DESTROY every credential already
            # in the vault. Fail loudly instead.
            raise SecretAccessDenied(
                f"vault at {self.path} is unreadable ({self.load_error}); "
                "refusing to overwrite it. Fix NERYA_VAULT_PASSPHRASE or "
                "restore/resolve the file before storing new secrets."
            )
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

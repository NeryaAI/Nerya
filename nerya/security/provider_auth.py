"""OAuth provider auth scaffolding.

This module treats provider credentials as first-class records with
refresh, revocation, and health metadata. Nerya historically went
straight to vault refs or env vars; this layer adds explicit auth state
without forcing every caller to switch overnight.

Concepts
--------

``ProviderAuthRecord``
    One credential record for a provider. Captures the credential
    ``kind`` (``api_key``, ``oauth``, ``device_code``), the secret(s)
    backing it (token, refresh token, expiry, scopes), the issuing
    provider id, and runtime metadata (``last_used``, ``health``,
    ``revoked_at``).

``ProviderAuthStore``
    JSON file persisted under ``workspace/security/provider_auth.json``.
    Reads/writes are atomic via :func:`nerya.core.atomic_write`.  The
    store itself never holds raw secrets in clear text on disk — long
    secrets (tokens, refresh tokens) are written into the existing
    :class:`SecretVault` by deterministic name and only the vault
    pointer is persisted in the JSON record.

``ProviderAuthManager``
    Higher-level façade that resolves a runtime credential for a
    provider.  Chooses the freshest non-revoked record, returns
    ``NeedsReauth`` when no usable record exists, and refreshes OAuth
    tokens via injected callbacks.

This file is intentionally small: it provides the scaffolding,
persistence, and structured ``needs_reauth`` semantics. Live OAuth
providers (Google, OKX, MCP servers) plug into this via
:class:`ProviderConfig` registrations.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..core.atomic_write import atomic_write_bytes
from ..core.errors import NeryaError
from ..core.redaction import fingerprint, preview
from ..core.time import now_iso
from .secrets import SecretVault


# --------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------- #


class ProviderAuthError(NeryaError):
    """Base class for OAuth/provider-auth failures."""


class ProviderNotConfigured(ProviderAuthError):
    """Caller asked for credentials for an unknown provider."""


class NeedsReauth(ProviderAuthError):
    """Structured ``needs_reauth`` error.

    Callers use this to short-circuit retry loops and surface a
    re-authentication prompt to the operator. Nerya callers should catch
    :class:`NeedsReauth` and emit an approval/event rather than retrying
    the request.
    """

    def __init__(self, provider: str, reason: str = "no_credentials") -> None:
        super().__init__(f"needs_reauth:{provider}:{reason}")
        self.provider = provider
        self.reason = reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "needs_reauth",
            "provider": self.provider,
            "reason": self.reason,
        }


# --------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------- #


VALID_KINDS = ("api_key", "bearer", "oauth", "device_code")


@dataclass
class ProviderConfig:
    """Static description of a provider's auth shape.

    Real providers register one of these at startup; the manager uses
    the entry to know what scopes to ask for, how to refresh, and which
    secret names to persist.

    ``refresh_fn`` is intentionally optional and synchronous — the
    scaffolding does *not* perform network IO itself; live refresh is
    plumbed in by the caller (e.g. an HTTP-bearing OAuth helper).
    """

    provider: str
    kind: str = "api_key"
    scopes: List[str] = field(default_factory=list)
    description: str = ""
    refresh_fn: Optional[Callable[["ProviderAuthRecord"], Dict[str, Any]]] = None
    needs_secret_name: str = ""

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise ProviderAuthError(
                f"invalid provider kind {self.kind!r} (allowed: {VALID_KINDS})"
            )


@dataclass
class ProviderAuthRecord:
    """A single stored credential for a provider.

    Long secrets (token / refresh_token) live in the vault by
    deterministic name; this record only tracks the *pointer* and
    metadata.
    """

    provider: str
    kind: str
    actor_id: str = "default"
    scopes: List[str] = field(default_factory=list)
    token_secret: str = ""
    refresh_secret: str = ""
    expires_at: Optional[float] = None
    created_at: str = field(default_factory=now_iso)
    last_used_at: Optional[str] = None
    refreshed_at: Optional[str] = None
    revoked_at: Optional[str] = None
    health: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_active(self) -> bool:
        return self.revoked_at is None

    def is_expired(self, *, leeway: float = 30.0) -> bool:
        if self.expires_at is None:
            return False
        return time.time() + leeway >= float(self.expires_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "kind": self.kind,
            "actor_id": self.actor_id,
            "scopes": list(self.scopes),
            "token_secret": self.token_secret,
            "refresh_secret": self.refresh_secret,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "refreshed_at": self.refreshed_at,
            "revoked_at": self.revoked_at,
            "health": self.health,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderAuthRecord":
        return cls(
            provider=str(data["provider"]),
            kind=str(data.get("kind", "api_key")),
            actor_id=str(data.get("actor_id", "default")),
            scopes=list(data.get("scopes") or []),
            token_secret=str(data.get("token_secret") or ""),
            refresh_secret=str(data.get("refresh_secret") or ""),
            expires_at=data.get("expires_at"),
            created_at=str(data.get("created_at") or now_iso()),
            last_used_at=data.get("last_used_at"),
            refreshed_at=data.get("refreshed_at"),
            revoked_at=data.get("revoked_at"),
            health=str(data.get("health") or "unknown"),
            metadata=dict(data.get("metadata") or {}),
        )

    def public_view(self) -> Dict[str, Any]:
        """Return a redacted record safe for dashboard/API exposure."""
        return {
            "provider": self.provider,
            "kind": self.kind,
            "actor_id": self.actor_id,
            "scopes": list(self.scopes),
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "refreshed_at": self.refreshed_at,
            "revoked_at": self.revoked_at,
            "health": self.health,
            "active": self.is_active(),
            "expired": self.is_expired(),
            "token_ref": self.token_secret,
            "refresh_ref": self.refresh_secret,
        }


# --------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------- #


def _record_key(provider: str, actor_id: str) -> str:
    return f"{provider}::{actor_id}"


def _vault_secret_name(provider: str, actor_id: str, slot: str) -> str:
    """Generate a deterministic secret name for a token/refresh entry.

    The name is short enough to fit our redaction previews but unique
    enough that two actors don't collide.  We don't include raw secret
    material in the name itself; a sha1 of the (provider, actor)
    tuple is enough for collision avoidance.
    """
    digest = hashlib.sha1(f"{provider}::{actor_id}".encode("utf-8")).hexdigest()[:12]
    return f"oauth_{slot}_{digest}"


@dataclass
class ProviderAuthStore:
    """Atomic JSON store for :class:`ProviderAuthRecord` rows.

    Vault interaction is optional — when ``vault`` is ``None`` the store
    treats ``token_secret`` / ``refresh_secret`` as opaque strings (used
    by tests + offline fixtures).
    """

    path: Path
    vault: Optional[SecretVault] = None
    _records: Dict[str, ProviderAuthRecord] = field(default_factory=dict, init=False)
    _loaded: bool = field(default=False, init=False)

    @classmethod
    def open(cls, path: Path | str, vault: Optional[SecretVault] = None) -> "ProviderAuthStore":
        store = cls(path=Path(path), vault=vault)
        store._load()
        return store

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        try:
            doc = json.loads(self.path.read_bytes() or b"{}")
        except Exception:
            return
        for row in doc.get("records") or []:
            try:
                rec = ProviderAuthRecord.from_dict(row)
            except Exception:
                continue
            self._records[_record_key(rec.provider, rec.actor_id)] = rec

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "version": 1,
            "records": [r.to_dict() for r in self._records.values()],
            "updated_at": now_iso(),
        }
        atomic_write_bytes(self.path, json.dumps(doc, ensure_ascii=False).encode("utf-8"))

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #

    def register(
        self,
        *,
        provider: str,
        kind: str,
        actor_id: str = "default",
        scopes: Iterable[str] | None = None,
        token: str = "",
        refresh_token: str = "",
        expires_at: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ProviderAuthRecord:
        """Persist a credential for ``(provider, actor_id)``.

        When a vault is attached, ``token`` and ``refresh_token`` are
        stored *in the vault* under deterministic names; the JSON
        record only keeps the vault key.  Without a vault the values
        are written into the JSON record directly (test mode).
        """
        if kind not in VALID_KINDS:
            raise ProviderAuthError(f"invalid kind {kind!r}")
        scopes_list = list(scopes or [])
        token_secret_ref = ""
        refresh_secret_ref = ""
        if self.vault is not None:
            if token:
                token_name = _vault_secret_name(provider, actor_id, "token")
                self.vault.put(
                    name=token_name,
                    value=token,
                    kind="provider_token",
                    scope=[f"provider:{provider}"],
                    owner=f"oauth/{actor_id}",
                )
                token_secret_ref = f"vault://{token_name}"
            if refresh_token:
                refresh_name = _vault_secret_name(provider, actor_id, "refresh")
                self.vault.put(
                    name=refresh_name,
                    value=refresh_token,
                    kind="provider_refresh",
                    scope=[f"provider:{provider}"],
                    owner=f"oauth/{actor_id}",
                )
                refresh_secret_ref = f"vault://{refresh_name}"
        else:
            token_secret_ref = token
            refresh_secret_ref = refresh_token
        rec = ProviderAuthRecord(
            provider=provider,
            kind=kind,
            actor_id=actor_id,
            scopes=scopes_list,
            token_secret=token_secret_ref,
            refresh_secret=refresh_secret_ref,
            expires_at=expires_at,
            metadata=dict(metadata or {}),
            health="active",
        )
        self._records[_record_key(provider, actor_id)] = rec
        self._flush()
        return rec

    def revoke(self, provider: str, actor_id: str = "default", *, reason: str = "manual") -> None:
        key = _record_key(provider, actor_id)
        if key not in self._records:
            return
        rec = self._records[key]
        rec.revoked_at = now_iso()
        rec.health = f"revoked:{reason}"
        self._flush()

    def remove(self, provider: str, actor_id: str = "default") -> bool:
        key = _record_key(provider, actor_id)
        if key not in self._records:
            return False
        del self._records[key]
        self._flush()
        return True

    def mark_used(self, provider: str, actor_id: str = "default") -> None:
        key = _record_key(provider, actor_id)
        if key in self._records:
            self._records[key].last_used_at = now_iso()
            self._flush()

    def update_token(
        self,
        *,
        provider: str,
        actor_id: str,
        token: str,
        refresh_token: str = "",
        expires_at: Optional[float] = None,
    ) -> ProviderAuthRecord:
        """Update token material on an existing record (used by refresh)."""
        key = _record_key(provider, actor_id)
        if key not in self._records:
            raise ProviderNotConfigured(f"no record for {key}")
        rec = self._records[key]
        if self.vault is not None:
            token_name = _vault_secret_name(provider, actor_id, "token")
            self.vault.put(
                name=token_name,
                value=token,
                kind="provider_token",
                scope=[f"provider:{provider}"],
                owner=f"oauth/{actor_id}",
            )
            rec.token_secret = f"vault://{token_name}"
            if refresh_token:
                refresh_name = _vault_secret_name(provider, actor_id, "refresh")
                self.vault.put(
                    name=refresh_name,
                    value=refresh_token,
                    kind="provider_refresh",
                    scope=[f"provider:{provider}"],
                    owner=f"oauth/{actor_id}",
                )
                rec.refresh_secret = f"vault://{refresh_name}"
        else:
            rec.token_secret = token
            if refresh_token:
                rec.refresh_secret = refresh_token
        rec.expires_at = expires_at
        rec.refreshed_at = now_iso()
        rec.health = "active"
        self._flush()
        return rec

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def get(self, provider: str, actor_id: str = "default") -> Optional[ProviderAuthRecord]:
        return self._records.get(_record_key(provider, actor_id))

    def list(self) -> List[ProviderAuthRecord]:
        return list(self._records.values())

    def list_for_provider(self, provider: str) -> List[ProviderAuthRecord]:
        return [r for r in self._records.values() if r.provider == provider]

    def resolve_token(self, provider: str, actor_id: str = "default") -> str:
        rec = self.get(provider, actor_id)
        if rec is None:
            raise NeedsReauth(provider, "missing")
        if not rec.is_active():
            raise NeedsReauth(provider, "revoked")
        ref = rec.token_secret or ""
        if ref.startswith("vault://") and self.vault is not None:
            name = ref[len("vault://") :]
            return self.vault.resolve(name, required_scope=f"provider:{provider}")
        return ref

    def fingerprint_for(self, provider: str, actor_id: str = "default") -> Dict[str, Any]:
        token = self.resolve_token(provider, actor_id)
        return {"sha12": fingerprint(token), "preview": preview(token)}


# --------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------- #


@dataclass
class ProviderAuthManager:
    """High-level façade combining configs + records + refresh."""

    store: ProviderAuthStore
    configs: Dict[str, ProviderConfig] = field(default_factory=dict)

    def register_config(self, cfg: ProviderConfig) -> None:
        self.configs[cfg.provider] = cfg

    def known_providers(self) -> List[str]:
        return sorted(self.configs.keys())

    def has_credentials(self, provider: str, actor_id: str = "default") -> bool:
        rec = self.store.get(provider, actor_id)
        return rec is not None and rec.is_active() and not rec.is_expired()

    def get_token(self, provider: str, *, actor_id: str = "default") -> str:
        if provider not in self.configs:
            raise ProviderNotConfigured(provider)
        rec = self.store.get(provider, actor_id)
        if rec is None or not rec.is_active():
            raise NeedsReauth(provider, "missing" if rec is None else "revoked")
        if rec.is_expired():
            self._refresh(rec)
            rec = self.store.get(provider, actor_id)
            if rec is None or rec.is_expired():
                raise NeedsReauth(provider, "expired")
        token = self.store.resolve_token(provider, actor_id)
        self.store.mark_used(provider, actor_id)
        return token

    def _refresh(self, rec: ProviderAuthRecord) -> None:
        cfg = self.configs.get(rec.provider)
        if cfg is None or cfg.refresh_fn is None:
            raise NeedsReauth(rec.provider, "no_refresh")
        try:
            payload = cfg.refresh_fn(rec) or {}
        except Exception as exc:  # noqa: BLE001 - bubble up as needs_reauth
            raise NeedsReauth(rec.provider, f"refresh_failed:{exc.__class__.__name__}") from exc
        new_token = str(payload.get("token") or "")
        if not new_token:
            raise NeedsReauth(rec.provider, "refresh_returned_empty")
        self.store.update_token(
            provider=rec.provider,
            actor_id=rec.actor_id,
            token=new_token,
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=payload.get("expires_at"),
        )

    def public_view(self) -> List[Dict[str, Any]]:
        return [r.public_view() for r in self.store.list()]

    def reauth_payload(self, provider: str, *, actor_id: str = "default") -> Dict[str, Any]:
        cfg = self.configs.get(provider)
        return {
            "kind": "needs_reauth",
            "provider": provider,
            "actor_id": actor_id,
            "scopes": list(cfg.scopes) if cfg else [],
            "auth_kind": cfg.kind if cfg else "api_key",
            "description": cfg.description if cfg else "",
        }


__all__ = [
    "NeedsReauth",
    "ProviderAuthError",
    "ProviderAuthManager",
    "ProviderAuthRecord",
    "ProviderAuthStore",
    "ProviderConfig",
    "ProviderNotConfigured",
    "VALID_KINDS",
]

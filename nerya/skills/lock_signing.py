"""supply-chain trust polish: signed skill lockfiles.

The :mod:`nerya.skills.lockfile` module records ``sha256`` for every
promoted skill.  The next step on the supply-chain hardening road is
to *attest* those hashes with a signature so that:

* a tampered ``skills.lock.yml`` (e.g. someone editing a hash by
  hand) can be detected even before we re-walk the skill tree;
* a workspace can require a known signing key before loading skills
  (matches The runtime' "trusted publisher" gate);
* the dashboard / CLI can show "lock verified by `<key fingerprint>`"
  next to each skill.

The signing is deliberately HMAC-SHA256 first (stdlib only).  We keep
the door open for Ed25519 by treating the algorithm as a tagged value
in the signature envelope; when ``cryptography`` is available we
delegate to it transparently.

Signature envelope shape (stored alongside the lock under
``skills/skills.lock.sig``)::

    version: 1
    algorithm: "hmac-sha256"
    key_id: "operator-default"
    fingerprint: "abcd1234..."
    signed_at: "2026-04-25T..."
    digest: "sha256-of-canonical-lock-bytes"
    signature: "hex-encoded-mac"
    canonical_bytes: 1473  # informational

Canonicalisation
----------------

We use a strict, sorted, JSON serialisation of the lock contents so
the signature is independent of YAML formatting and dict order.  The
canonical bytes also drive the per-entry content hash that
:func:`fingerprint_lock` exposes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core import yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.errors import SecurityError
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from . import lockfile as _lockfile


SIG_VERSION = 1
ALGO_HMAC_SHA256 = "hmac-sha256"
ALGO_ED25519 = "ed25519"


class LockSignatureError(SecurityError):
    """Raised when verifying / loading a signed lock fails."""


# --------------------------------------------------------------------- #
# Canonical bytes
# --------------------------------------------------------------------- #


def _canonical_lock_payload(entries: dict[str, "_lockfile.LockEntry"]) -> bytes:
    """Return canonical bytes for ``entries`` (sorted, JSON, utf-8).

    The canonical form *excludes* the ``signature`` field of each
    entry (a signature should never co-sign itself).  Everything else
    is included so a hash drift, version drift, source drift, or
    publisher swap all change the canonical bytes and therefore
    invalidate the existing signature.
    """
    body = {
        "version": _lockfile.LOCK_VERSION,
        "skills": {
            sid: {
                "version": e.version,
                "sha256": e.sha256,
                "source_kind": e.source_kind,
                "source": e.source,
                "installed_at": e.installed_at,
                "publisher": e.publisher,
            }
            for sid, e in sorted(entries.items())
        },
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def fingerprint_lock(paths: WorkspacePaths) -> dict[str, Any]:
    """Return ``{digest, byte_count, entries}`` for the current lock.

    ``digest`` is the sha256 hex of the canonical lock bytes.  This is
    what gets signed and what we re-verify on boot.
    """
    entries = _lockfile.load_lock(paths)
    canon = _canonical_lock_payload(entries)
    return {
        "digest": hashlib.sha256(canon).hexdigest(),
        "byte_count": len(canon),
        "entries": len(entries),
    }


# --------------------------------------------------------------------- #
# Key resolution
# --------------------------------------------------------------------- #


@dataclass
class SigningKey:
    """In-memory signing key.

    For ``hmac-sha256`` we treat ``material`` as the raw secret bytes.
    For ``ed25519`` we treat ``material`` as the seed/private key
    bytes; verification only needs the public part, which we derive
    on demand from ``cryptography`` when it's available.
    """

    key_id: str
    material: bytes
    algorithm: str = ALGO_HMAC_SHA256

    def fingerprint(self) -> str:
        return hashlib.sha256(self.material).hexdigest()[:32]


def resolve_signing_key(
    *,
    explicit: Optional[bytes] = None,
    env_var: str = "NERYA_LOCK_SIGNING_KEY",
    key_id: str = "operator-default",
    algorithm: str = ALGO_HMAC_SHA256,
) -> Optional[SigningKey]:
    """Return a :class:`SigningKey` from explicit bytes or env, else
    ``None``.

    Resolution order:

    1. ``explicit`` parameter (used by tests and operator CLIs).
    2. ``NERYA_LOCK_SIGNING_KEY`` env var (raw bytes; we accept any
       length ≥ 16 for hmac to discourage trivial keys).
    """
    if explicit:
        return SigningKey(key_id=key_id, material=explicit, algorithm=algorithm)
    raw = os.environ.get(env_var)
    if raw and len(raw.encode("utf-8")) >= 16:
        return SigningKey(key_id=key_id, material=raw.encode("utf-8"),
                          algorithm=algorithm)
    return None


# --------------------------------------------------------------------- #
# Sign / verify primitives
# --------------------------------------------------------------------- #


def _hmac_sign(key: SigningKey, payload: bytes) -> str:
    return hmac.new(key.material, payload, hashlib.sha256).hexdigest()


def _hmac_verify(key: SigningKey, payload: bytes, signature: str) -> bool:
    return hmac.compare_digest(_hmac_sign(key, payload), signature)


def _ed25519_sign(key: SigningKey, payload: bytes) -> str:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except Exception as exc:  # pragma: no cover - cryptography not installed
        raise LockSignatureError(
            "ed25519 requires the 'cryptography' package"
        ) from exc
    seed = key.material
    if len(seed) != 32:
        raise LockSignatureError("ed25519 seed must be 32 bytes")
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    return priv.sign(payload).hex()


def _ed25519_verify(key: SigningKey, payload: bytes, signature: str) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except Exception:  # pragma: no cover - cryptography not installed
        return False
    seed = key.material
    if len(seed) != 32:
        return False
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    try:
        pub.verify(bytes.fromhex(signature), payload)
        return True
    except Exception:
        return False


def sign_payload(key: SigningKey, payload: bytes) -> str:
    if key.algorithm == ALGO_HMAC_SHA256:
        return _hmac_sign(key, payload)
    if key.algorithm == ALGO_ED25519:
        return _ed25519_sign(key, payload)
    raise LockSignatureError(f"unknown algorithm: {key.algorithm!r}")


def verify_payload(key: SigningKey, payload: bytes, signature: str) -> bool:
    if key.algorithm == ALGO_HMAC_SHA256:
        return _hmac_verify(key, payload, signature)
    if key.algorithm == ALGO_ED25519:
        return _ed25519_verify(key, payload, signature)
    return False


# --------------------------------------------------------------------- #
# Sign / verify the lock file as a whole
# --------------------------------------------------------------------- #


@dataclass
class SignedLock:
    version: int = SIG_VERSION
    algorithm: str = ALGO_HMAC_SHA256
    key_id: str = "operator-default"
    fingerprint: str = ""
    signed_at: str = ""
    digest: str = ""
    signature: str = ""
    canonical_bytes: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "fingerprint": self.fingerprint,
            "signed_at": self.signed_at,
            "digest": self.digest,
            "signature": self.signature,
            "canonical_bytes": self.canonical_bytes,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SignedLock":
        return cls(
            version=int(data.get("version") or SIG_VERSION),
            algorithm=str(data.get("algorithm") or ALGO_HMAC_SHA256),
            key_id=str(data.get("key_id") or "operator-default"),
            fingerprint=str(data.get("fingerprint") or ""),
            signed_at=str(data.get("signed_at") or ""),
            digest=str(data.get("digest") or ""),
            signature=str(data.get("signature") or ""),
            canonical_bytes=int(data.get("canonical_bytes") or 0),
            extra=dict(data.get("extra") or {}),
        )


def _signature_path(paths: WorkspacePaths) -> Path:
    return paths.skills_lock.with_suffix(".lock.sig")


def sign_lock(paths: WorkspacePaths, *, key: SigningKey,
              extra: dict[str, Any] | None = None) -> SignedLock:
    """Sign the current lock and persist the signature envelope.

    The envelope lives at ``skills/skills.lock.sig`` (next to the
    YAML lock).  Re-signing simply overwrites the file atomically.
    """
    if not paths.skills_lock.exists():
        raise LockSignatureError("no skills.lock.yml to sign")
    entries = _lockfile.load_lock(paths)
    canon = _canonical_lock_payload(entries)
    digest = hashlib.sha256(canon).hexdigest()
    sig = sign_payload(key, canon)
    envelope = SignedLock(
        version=SIG_VERSION,
        algorithm=key.algorithm,
        key_id=key.key_id,
        fingerprint=key.fingerprint(),
        signed_at=now_iso(),
        digest=digest,
        signature=sig,
        canonical_bytes=len(canon),
        extra=dict(extra or {}),
    )
    out_path = _signature_path(paths)
    atomic_write_text(out_path, yaml_io.dumps(envelope.to_dict()))
    return envelope


def load_signature(paths: WorkspacePaths) -> Optional[SignedLock]:
    p = _signature_path(paths)
    if not p.exists():
        return None
    try:
        doc = yaml_io.load(p, default={}) or {}
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    return SignedLock.from_dict(doc)


@dataclass
class VerifyReport:
    ok: bool
    reason: str = ""
    digest: str = ""
    expected_digest: str = ""
    fingerprint: str = ""
    key_id: str = ""
    algorithm: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "digest": self.digest,
            "expected_digest": self.expected_digest,
            "fingerprint": self.fingerprint,
            "key_id": self.key_id,
            "algorithm": self.algorithm,
        }


def verify_lock_signature(paths: WorkspacePaths, *,
                          key: Optional[SigningKey]) -> VerifyReport:
    """Verify ``skills.lock.sig`` against the on-disk lock and key.

    Returns a :class:`VerifyReport` with structured failure reasons:

    * ``no_lock`` — no ``skills.lock.yml`` to compare against.
    * ``no_signature`` — no signature envelope on disk.
    * ``no_key`` — the caller passed ``None``.
    * ``digest_mismatch`` — the lock was edited after signing.
    * ``signature_mismatch`` — the key/algorithm doesn't verify.
    * ``unsupported_algorithm`` — envelope algorithm not recognised.
    """
    if not paths.skills_lock.exists():
        return VerifyReport(ok=False, reason="no_lock")
    envelope = load_signature(paths)
    if envelope is None:
        return VerifyReport(ok=False, reason="no_signature")
    entries = _lockfile.load_lock(paths)
    canon = _canonical_lock_payload(entries)
    expected_digest = hashlib.sha256(canon).hexdigest()
    if envelope.digest and envelope.digest != expected_digest:
        return VerifyReport(
            ok=False, reason="digest_mismatch",
            digest=envelope.digest,
            expected_digest=expected_digest,
            fingerprint=envelope.fingerprint,
            key_id=envelope.key_id,
            algorithm=envelope.algorithm,
        )
    if key is None:
        return VerifyReport(
            ok=False, reason="no_key",
            digest=envelope.digest,
            expected_digest=expected_digest,
            fingerprint=envelope.fingerprint,
            key_id=envelope.key_id,
            algorithm=envelope.algorithm,
        )
    if key.algorithm != envelope.algorithm:
        return VerifyReport(
            ok=False, reason="unsupported_algorithm",
            digest=envelope.digest,
            expected_digest=expected_digest,
            fingerprint=envelope.fingerprint,
            key_id=envelope.key_id,
            algorithm=envelope.algorithm,
        )
    valid = verify_payload(key, canon, envelope.signature)
    if not valid:
        return VerifyReport(
            ok=False, reason="signature_mismatch",
            digest=envelope.digest,
            expected_digest=expected_digest,
            fingerprint=envelope.fingerprint,
            key_id=envelope.key_id,
            algorithm=envelope.algorithm,
        )
    return VerifyReport(
        ok=True, reason="verified",
        digest=envelope.digest,
        expected_digest=expected_digest,
        fingerprint=envelope.fingerprint,
        key_id=envelope.key_id,
        algorithm=envelope.algorithm,
    )


def remove_signature(paths: WorkspacePaths) -> bool:
    p = _signature_path(paths)
    if not p.exists():
        return False
    p.unlink()
    return True


__all__ = [
    "ALGO_ED25519",
    "ALGO_HMAC_SHA256",
    "LockSignatureError",
    "SIG_VERSION",
    "SignedLock",
    "SigningKey",
    "VerifyReport",
    "fingerprint_lock",
    "load_signature",
    "remove_signature",
    "resolve_signing_key",
    "sign_lock",
    "sign_payload",
    "verify_lock_signature",
    "verify_payload",
]

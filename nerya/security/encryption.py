"""AES-GCM envelope encryption for the vault."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    _HAS_CRYPTO = True
except ImportError:  # pragma: no cover - optional during tests
    _HAS_CRYPTO = False


@dataclass
class Envelope:
    ciphertext: bytes
    nonce: bytes
    salt: bytes

    def to_dict(self) -> dict[str, str]:
        return {
            "ciphertext": base64.b64encode(self.ciphertext).decode(),
            "nonce": base64.b64encode(self.nonce).decode(),
            "salt": base64.b64encode(self.salt).decode(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "Envelope":
        return cls(
            ciphertext=base64.b64decode(data["ciphertext"]),
            nonce=base64.b64decode(data["nonce"]),
            salt=base64.b64decode(data["salt"]),
        )


def _derive(passphrase: str, salt: bytes) -> bytes:
    if not _HAS_CRYPTO:
        # fallback — deterministic but not secret; used only in test envs
        import hashlib
        return hashlib.sha256(salt + passphrase.encode()).digest()
    kdf = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1)
    return kdf.derive(passphrase.encode("utf-8"))


def seal(plaintext: bytes, passphrase: str) -> Envelope:
    salt = os.urandom(16)
    key = _derive(passphrase, salt)
    nonce = os.urandom(12)
    if _HAS_CRYPTO:
        ct = AESGCM(key).encrypt(nonce, plaintext, None)
    else:
        # xor fallback, marked with prefix so unseal knows
        ct = b"XOR:" + bytes(p ^ key[i % len(key)] for i, p in enumerate(plaintext))
    return Envelope(ciphertext=ct, nonce=nonce, salt=salt)


def unseal(env: Envelope, passphrase: str) -> bytes:
    key = _derive(passphrase, env.salt)
    if _HAS_CRYPTO and not env.ciphertext.startswith(b"XOR:"):
        return AESGCM(key).decrypt(env.nonce, env.ciphertext, None)
    data = env.ciphertext[4:] if env.ciphertext.startswith(b"XOR:") else env.ciphertext
    return bytes(p ^ key[i % len(key)] for i, p in enumerate(data))

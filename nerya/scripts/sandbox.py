"""In-process script sandbox.

The real sandbox would use a subprocess with seccomp / pledge /
AppContainer; here we do best-effort monkey-patch guards. Scripts are
therefore only trusted as much as their static analysis — the runtime
layer exists to turn drive-by attempts into explicit
``PermissionError``s that are picked up by the journal.
"""

from __future__ import annotations

import builtins
import os
from contextlib import contextmanager


_BLOCKED_PATH_NEEDLES = (
    # auth material
    ".ssh", ".env", "wallet.json", "id_rsa", "id_ed25519",
    "keystore.json", "mnemonic", "seed_phrase",
    # browser data
    "Login Data", "Cookies", "Web Data",
    # Nerya sensitive files - scripts have no legitimate reason to
    # open these. They go through the skills API instead.
    "nerya.yml", "limits.yml", "accounts.yml", "exchanges.yml",
    "secrets.refs.yml", "secrets.enc", "keyring.ref",
)

_BLOCKED_ENV_KEYWORDS = (
    "API_KEY", "APIKEY", "SECRET", "TOKEN",
    "PRIVATE_KEY", "PASSWORD", "PASSPHRASE",
    "MNEMONIC", "SEED",
)


@contextmanager
def sandbox():
    orig_open = builtins.open
    orig_env_get = os.environ.get
    orig_env_getitem = os.environ.__getitem__

    def guarded_open(file, *a, **kw):  # noqa: D401
        s = str(file)
        low = s.lower()
        for needle in _BLOCKED_PATH_NEEDLES:
            if needle.lower() in low:
                raise PermissionError(f"sandbox blocked read/write: {s}")
        return orig_open(file, *a, **kw)

    def _blocks_env(key: str) -> bool:
        up = str(key).upper()
        return any(k in up for k in _BLOCKED_ENV_KEYWORDS)

    def guarded_env_get(key, default=None):
        if _blocks_env(key):
            raise PermissionError(f"sandbox blocked env read: {key}")
        return orig_env_get(key, default)

    def guarded_env_getitem(key):
        if _blocks_env(key):
            raise PermissionError(f"sandbox blocked env read: {key}")
        return orig_env_getitem(key)

    builtins.open = guarded_open
    os.environ.get = guarded_env_get  # type: ignore[assignment]
    try:
        yield
    finally:
        builtins.open = orig_open
        os.environ.get = orig_env_get  # type: ignore[assignment]
        _ = orig_env_getitem  # reserved for future subclass override

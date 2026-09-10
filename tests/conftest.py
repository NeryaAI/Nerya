"""Shared pytest configuration.

SecretVault.put() refuses to store secrets under the built-in default
passphrase, so any test whose flow vaults credentials needs a real one.
Set it here once; tests that exercise the default-passphrase fallback
delete the variable explicitly (monkeypatch.delenv).
"""

import os

os.environ.setdefault("NERYA_VAULT_PASSPHRASE", "test-vault-passphrase")

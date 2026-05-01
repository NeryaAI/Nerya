"""Credential pool for LLM providers.

When a single provider key hits a 429 / 5xx or burns through its daily
quota, the pool transparently rotates to the next healthy key in its set.
All keys come from :class:`SecretVault`; plaintext never leaves this module.

Key naming convention (operator controls it):
    vault://openai/primary
    vault://openai/backup
    vault://openai/cheap

The caller asks ``pool.checkout(provider_id="openai")``. Each call returns
the least-recently-used key that is currently healthy; callers report back
via :meth:`report_success` / :meth:`report_failure`. Failures with status
codes in :data:`TEMP_FAIL_STATUSES` cooldown the key for ``cooldown_s``
seconds before it is eligible again. 401 / 403 permanently disables it
until the operator removes + re-adds it.

The pool is process-local and thread-safe; for multi-process setups each
worker keeps its own state but all agree on the same underlying secrets.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

from ..security.secrets import SecretVault


# HTTP statuses that put a key on temporary cooldown (vs permanent disable).
TEMP_FAIL_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
PERMA_FAIL_STATUSES = frozenset({401, 403})


@dataclass
class KeyState:
    name: str                 # secret name in the vault, e.g. "openai/primary"
    last_used: float = 0.0
    last_success: float = 0.0
    total_calls: int = 0
    total_failures: int = 0
    cooldown_until: float = 0.0
    disabled: bool = False
    disabled_reason: str = ""

    def is_healthy(self, now: float) -> bool:
        return (not self.disabled) and (now >= self.cooldown_until)


@dataclass
class CredentialPool:
    """Rotating pool of provider keys, backed by a :class:`SecretVault`."""

    vault: SecretVault
    cooldown_s: float = 60.0
    _provider_keys: dict[str, list[KeyState]] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # ---------------------------------------------------------------- setup
    def register_keys(self, provider_id: str, secret_names: Iterable[str]) -> None:
        """Declare the key names the pool should cycle through for ``provider_id``.

        Names that do not exist in the vault are skipped silently so the
        pool can be partially configured.
        """
        states: list[KeyState] = []
        for name in secret_names:
            try:
                self.vault.meta(name)
            except Exception:
                continue
            states.append(KeyState(name=name))
        with self._lock:
            self._provider_keys[provider_id] = states

    def autodiscover(self, provider_id: str) -> list[str]:
        """Pick up any vault secret whose ``name`` starts with ``<provider_id>/``."""
        names = [m.name for m in self.vault.list()
                 if m.name.startswith(f"{provider_id}/")]
        if names:
            self.register_keys(provider_id, names)
        return names

    # ---------------------------------------------------------------- use
    def checkout(self, provider_id: str) -> tuple[str, str] | None:
        """Return (secret_name, api_key) for the next healthy key, or None."""
        with self._lock:
            states = self._provider_keys.get(provider_id) or []
            if not states:
                return None
            now = time.time()
            healthy = [s for s in states if s.is_healthy(now)]
            if not healthy:
                return None
            # LRU by last_used
            healthy.sort(key=lambda s: s.last_used)
            choice = healthy[0]
            choice.last_used = now
            choice.total_calls += 1
        try:
            value = self.vault.resolve(choice.name)
        except Exception:
            with self._lock:
                choice.disabled = True
                choice.disabled_reason = "vault_resolve_failed"
            return None
        return choice.name, value

    def report_success(self, provider_id: str, secret_name: str) -> None:
        with self._lock:
            state = self._find(provider_id, secret_name)
            if state is None:
                return
            state.last_success = time.time()
            state.cooldown_until = 0.0

    def report_failure(self, provider_id: str, secret_name: str, *,
                       status: int | None = None, reason: str = "") -> None:
        now = time.time()
        with self._lock:
            state = self._find(provider_id, secret_name)
            if state is None:
                return
            state.total_failures += 1
            if status in PERMA_FAIL_STATUSES:
                state.disabled = True
                state.disabled_reason = reason or f"status_{status}"
                return
            if status in TEMP_FAIL_STATUSES or status is None:
                state.cooldown_until = now + self.cooldown_s

    # ---------------------------------------------------------------- view
    def snapshot(self) -> list[dict]:
        with self._lock:
            out = []
            now = time.time()
            for provider, states in self._provider_keys.items():
                for s in states:
                    out.append({
                        "provider": provider,
                        "secret_name": s.name,
                        "healthy": s.is_healthy(now),
                        "disabled": s.disabled,
                        "disabled_reason": s.disabled_reason,
                        "cooldown_remaining_s": max(0.0, s.cooldown_until - now),
                        "last_used_age_s": max(0.0, now - s.last_used) if s.last_used else None,
                        "last_success_age_s": max(0.0, now - s.last_success) if s.last_success else None,
                        "total_calls": s.total_calls,
                        "total_failures": s.total_failures,
                    })
            return out

    def _find(self, provider_id: str, secret_name: str) -> KeyState | None:
        for s in self._provider_keys.get(provider_id) or []:
            if s.name == secret_name:
                return s
        return None

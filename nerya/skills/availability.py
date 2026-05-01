"""Plan 31 P1 — action availability probes.

Hermes filters tools the LLM can see by running cheap availability
probes at planner-build time:

* ``requires_env`` — environment variables that must be set,
* ``requires_secret`` — vault keys that must be present, and
* ``check_fn``      — a dotted path to ``(config, manifest, action) ->
                      (bool, str)`` for connector-health style checks.

Without this, the planner advertises actions the agent cannot run, the
model picks them, and the call dies at dispatch with a vague
``KeyError`` / ``ConnectorUnavailable``.

This module exposes a single public function, :func:`probe_action`,
plus a cached :func:`build_availability_table` helper used by the
capability matrix and the planner. Probes are best-effort — when a
``check_fn`` raises, we mark the action as **unavailable** and log the
reason rather than crashing the planner.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AvailabilityVerdict:
    """Outcome of an availability probe for a single action."""

    available: bool
    reason: str = ""
    missing_env: tuple[str, ...] = ()
    missing_secrets: tuple[str, ...] = ()
    check_fn: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "missing_env": list(self.missing_env),
            "missing_secrets": list(self.missing_secrets),
            "check_fn": self.check_fn,
        }


def _missing_env_vars(names: list[str]) -> tuple[str, ...]:
    return tuple(n for n in (names or []) if not (os.environ.get(n) or "").strip())


def _missing_secrets(config, names: list[str]) -> tuple[str, ...]:
    if not names:
        return ()
    try:
        from ..security.secrets import SecretVault
    except Exception:
        return tuple(names)
    try:
        vault_path = getattr(config.paths, "vault_enc", None) or (
            getattr(config.paths, "vault", None) and (config.paths.vault / "vault.enc")
        )
        if vault_path is None:
            return tuple(names)
        vault = SecretVault.open(vault_path)
    except Exception:
        return tuple(names)
    try:
        present_names = {meta.name for meta in vault.list()}
    except Exception:
        return tuple(names)
    return tuple(n for n in names if n not in present_names)


def _resolve_check_fn(dotted: str) -> Callable | None:
    """Import ``dotted`` lazily and return the callable (or ``None``)."""

    if not dotted:
        return None
    module_name, _, attr = dotted.rpartition(".")
    if not module_name or not attr:
        log.warning("availability: invalid check_fn %r", dotted)
        return None
    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:
        log.warning("availability: cannot import %s (%s)", module_name, exc)
        return None
    fn = getattr(mod, attr, None)
    if not callable(fn):
        log.warning("availability: %s is not callable", dotted)
        return None
    return fn


def probe_action(config, manifest, action) -> AvailabilityVerdict:
    """Probe a single action and return an :class:`AvailabilityVerdict`."""

    requires_env = list(getattr(action, "requires_env", []) or [])
    requires_secret = list(getattr(action, "requires_secret", []) or [])
    check_fn_path = str(getattr(action, "check_fn", "") or "").strip()

    missing_env = _missing_env_vars(requires_env)
    if missing_env:
        return AvailabilityVerdict(
            available=False,
            reason=f"missing env: {', '.join(missing_env)}",
            missing_env=missing_env,
            check_fn=check_fn_path,
        )

    missing_secrets = _missing_secrets(config, requires_secret)
    if missing_secrets:
        return AvailabilityVerdict(
            available=False,
            reason=f"missing secret: {', '.join(missing_secrets)}",
            missing_secrets=missing_secrets,
            check_fn=check_fn_path,
        )

    fn = _resolve_check_fn(check_fn_path)
    if fn is not None:
        try:
            outcome = fn(config, manifest, action)
        except Exception as exc:
            return AvailabilityVerdict(
                available=False,
                reason=f"check_fn error: {type(exc).__name__}: {exc}",
                check_fn=check_fn_path,
            )
        if isinstance(outcome, tuple) and len(outcome) >= 1:
            ok = bool(outcome[0])
            reason = str(outcome[1]) if len(outcome) > 1 else ""
        else:
            ok = bool(outcome)
            reason = ""
        if not ok:
            return AvailabilityVerdict(
                available=False,
                reason=reason or "check_fn returned False",
                check_fn=check_fn_path,
            )

    return AvailabilityVerdict(available=True, check_fn=check_fn_path)


def build_availability_table(config, registry) -> dict[str, dict[str, AvailabilityVerdict]]:
    """Run :func:`probe_action` for every action in ``registry``.

    Returns ``{skill_id: {action_name: AvailabilityVerdict}}``.
    """
    table: dict[str, dict[str, AvailabilityVerdict]] = {}
    if registry is None:
        return table
    try:
        entries = list(registry.list())
    except Exception:
        return table
    for entry in entries:
        manifest = getattr(entry, "manifest", None)
        if manifest is None:
            continue
        actions = getattr(manifest, "actions", {}) or {}
        verdicts: dict[str, AvailabilityVerdict] = {}
        for name, spec in actions.items():
            try:
                verdicts[name] = probe_action(config, manifest, spec)
            except Exception as exc:
                verdicts[name] = AvailabilityVerdict(
                    available=False,
                    reason=f"probe failure: {type(exc).__name__}: {exc}",
                )
        table[manifest.id] = verdicts
    return table

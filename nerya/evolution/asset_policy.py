"""Safety policy for evolution assets and validation commands."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

from .patch_proposal import is_protected


ALLOWED_VALIDATION_PREFIXES = (
    ("python", "-m", "pytest"),
    ("pytest",),
    ("npx", "tsc", "--noEmit"),
    ("npm", "test"),
    ("python", "-m", "nerya.cli.app", "doctor"),
)


@dataclass(frozen=True)
class PolicyResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": list(self.reasons)}


def validate_mutation_scope(
    targets: list[str],
    *,
    forbidden_scopes: list[str] | None = None,
    max_files: int | None = None,
) -> PolicyResult:
    reasons: list[str] = []
    if max_files is not None and len(targets) > int(max_files):
        reasons.append(f"too_many_files:{len(targets)}>{int(max_files)}")
    for target in targets:
        if is_protected(target):
            reasons.append(f"protected_scope:{target}")
        for rule in forbidden_scopes or []:
            if _matches(rule, target):
                reasons.append(f"forbidden_scope:{target}")
                break
    return PolicyResult(ok=not reasons, reasons=reasons)


def validate_validation_command(command: str) -> PolicyResult:
    parts = shlex.split(command, posix=False)
    if not parts:
        return PolicyResult(ok=False, reasons=["empty_command"])
    normalized = tuple(p.strip("\"'") for p in parts)
    for prefix in ALLOWED_VALIDATION_PREFIXES:
        if normalized[: len(prefix)] == prefix:
            return PolicyResult(ok=True)
    return PolicyResult(ok=False, reasons=[f"command_not_allowed:{command}"])


def validate_validation_commands(commands: list[str]) -> PolicyResult:
    reasons: list[str] = []
    for command in commands:
        res = validate_validation_command(command)
        reasons.extend(res.reasons)
    return PolicyResult(ok=not reasons, reasons=reasons)


def _matches(rule: str, target: str) -> bool:
    if "*" in rule:
        prefix, suffix = rule.split("*", 1)
        return target.startswith(prefix) and target.endswith(suffix)
    return rule == target


__all__ = [
    "ALLOWED_VALIDATION_PREFIXES",
    "PolicyResult",
    "validate_mutation_scope",
    "validate_validation_command",
    "validate_validation_commands",
]

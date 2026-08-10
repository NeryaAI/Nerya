"""Safety policy for evolution assets and validation commands."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .patch_proposal import is_protected


ALLOWED_VALIDATION_PREFIXES = (
    ("python", "-m", "pytest"),
    ("pytest",),
    ("npx", "tsc", "--noEmit"),
    ("npm", "test"),
    ("python", "-m", "nerya.cli.app", "doctor"),
    # Agent-loop regression evals (see nerya/evals/cli.py) — the
    # executable surface behind ``eval_scenario`` validation steps.
    ("python", "-m", "nerya.evals"),
)

# ``python -m nerya.evals`` is a code-executing validation step.  The command
# itself is useful for the sealed baseline, but accepting an arbitrary
# ``--module`` would let a proposal import and run attacker-controlled Python
# before any scenario result is produced.  Keep this registry deliberately
# small and package-owned; expanding it requires a reviewed code change.
REGISTERED_EVAL_MODULES = frozenset({"nerya.evals.scenarios"})
_EVAL_COMMAND_PREFIX = ("python", "-m", "nerya.evals")


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


def validate_validation_command(
    command: str,
    *,
    workspace: Path | None = None,
) -> PolicyResult:
    try:
        parts = shlex.split(command, posix=False)
    except ValueError as exc:
        return PolicyResult(ok=False, reasons=[f"command_parse_error:{exc}"])
    if not parts:
        return PolicyResult(ok=False, reasons=["empty_command"])
    normalized = tuple(p.strip("\"'") for p in parts)
    if normalized[: len(_EVAL_COMMAND_PREFIX)] == _EVAL_COMMAND_PREFIX:
        return _validate_eval_command(normalized, workspace=workspace)
    dangerous_flags = {
        "-p", "--plugin", "--pyargs", "--confcutdir", "--rootdir",
        "--basetemp", "--override-ini", "--import-mode=importlib",
        "--plugin", "--project", "--config", "--config-file",
    }
    if any(
        token in dangerous_flags
        or any(token.startswith(flag + "=") for flag in dangerous_flags)
        for token in normalized
    ):
        return PolicyResult(ok=False, reasons=["validation_flag_not_allowed"])
    if any(token in {";", "&&", "||", "|", ">", ">>"} for token in normalized):
        return PolicyResult(ok=False, reasons=["validation_shell_syntax_not_allowed"])
    for prefix in ALLOWED_VALIDATION_PREFIXES:
        if normalized[: len(prefix)] == prefix:
            path_check = _validate_workspace_arguments(normalized, workspace)
            if not path_check.ok:
                return path_check
            return PolicyResult(ok=True)
    return PolicyResult(ok=False, reasons=[f"command_not_allowed:{command}"])


def _validate_eval_command(
    parts: tuple[str, ...],
    *,
    workspace: Path | None = None,
) -> PolicyResult:
    """Validate the narrow argument surface of the eval executable.

    ``subprocess.run`` does not invoke a shell, but importing a module is still
    arbitrary code execution.  Parse the handful of supported flags instead
    of treating the executable prefix as sufficient authorization.
    """

    module: str | None = None
    seen_workspace = False
    seen_stop = False
    idx = len(_EVAL_COMMAND_PREFIX)
    while idx < len(parts):
        token = parts[idx]
        if token == "--module":
            if module is not None or idx + 1 >= len(parts):
                return PolicyResult(ok=False, reasons=["eval_module_argument_invalid"])
            module = parts[idx + 1]
            idx += 2
            continue
        if token.startswith("--module="):
            if module is not None:
                return PolicyResult(ok=False, reasons=["eval_module_argument_invalid"])
            module = token.split("=", 1)[1]
            idx += 1
            continue
        if token == "--stop-on-failure":
            if seen_stop:
                return PolicyResult(ok=False, reasons=["eval_flag_repeated:--stop-on-failure"])
            seen_stop = True
            idx += 1
            continue
        if token == "--workspace":
            if seen_workspace or idx + 1 >= len(parts):
                return PolicyResult(ok=False, reasons=["eval_workspace_argument_invalid"])
            workspace_arg = parts[idx + 1]
            # Validation subprocesses run with the proposal workspace as cwd;
            # only a relative child path can remain inside that boundary.
            workspace_path = Path(workspace_arg)
            if workspace_path.is_absolute() or ".." in workspace_path.parts:
                return PolicyResult(ok=False, reasons=["eval_workspace_outside_workspace"])
            if workspace is not None:
                try:
                    (Path(workspace) / workspace_path).resolve().relative_to(
                        Path(workspace).resolve()
                    )
                except (OSError, ValueError):
                    return PolicyResult(ok=False, reasons=["eval_workspace_outside_workspace"])
            seen_workspace = True
            idx += 2
            continue
        return PolicyResult(ok=False, reasons=[f"eval_flag_not_allowed:{token}"])

    if module not in REGISTERED_EVAL_MODULES:
        return PolicyResult(
            ok=False,
            reasons=[f"eval_module_not_registered:{module or '<missing>'}"],
        )
    return PolicyResult(ok=True)


def _validate_workspace_arguments(
    parts: tuple[str, ...], workspace: Path | None,
) -> PolicyResult:
    """Keep executable validation inputs inside the proposal workspace."""

    for token in parts:
        if ".." in Path(token).parts:
            return PolicyResult(ok=False, reasons=["validation_path_outside_workspace"])
        if workspace is None or not Path(token).is_absolute():
            continue
        try:
            Path(token).resolve().relative_to(Path(workspace).resolve())
        except (OSError, ValueError):
            return PolicyResult(ok=False, reasons=["validation_path_outside_workspace"])
    return PolicyResult(ok=True)


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
    "REGISTERED_EVAL_MODULES",
    "PolicyResult",
    "validate_mutation_scope",
    "validate_validation_command",
    "validate_validation_commands",
]

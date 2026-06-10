"""Process execution boundary for shell-class tools.

The current desktop harness cannot rely on a platform sandbox everywhere, so
this wrapper enforces the invariant Nerya can prove locally: commands execute
from an explicit workspace cwd, with timeouts and captured output under one
auditable chokepoint. OS-specific hardening can be added here without changing
tool handlers.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class SandboxViolation(RuntimeError):
    """Raised when a process request escapes the declared workspace."""


@dataclass(frozen=True)
class SandboxExecResult:
    args: str | Sequence[str]
    returncode: int | None
    stdout: str
    stderr: str
    cwd: Path
    elapsed_ms: int
    pid: int | None = None
    background: bool = False


def _resolve_cwd(cwd: str | Path, root: str | Path | None) -> Path:
    cwd_path = Path(cwd).expanduser().resolve()
    if root is None:
        return cwd_path
    root_path = Path(root).expanduser().resolve()
    try:
        cwd_path.relative_to(root_path)
    except ValueError as exc:
        raise SandboxViolation(
            f"sandbox_exec cwd is outside workspace: {cwd_path}"
        ) from exc
    return cwd_path


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def sandbox_exec(
    args: str | Sequence[str],
    *,
    cwd: str | Path,
    root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    shell: bool = False,
    capture_output: bool = True,
    text: bool = True,
    check: bool = False,
    background: bool = False,
) -> SandboxExecResult:
    """Run a process from the workspace sandbox chokepoint.

    `subprocess.run` is intentionally centralized here so tool modules do not
    grow their own process-launch semantics.
    """

    cwd_path = _resolve_cwd(cwd, root)
    started = time.monotonic()
    if background:
        proc = subprocess.Popen(
            args,
            shell=shell,
            cwd=str(cwd_path),
            env=dict(env) if env is not None else None,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=text,
        )
        return SandboxExecResult(
            args=args,
            returncode=None,
            stdout="",
            stderr="",
            cwd=cwd_path,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            pid=proc.pid,
            background=True,
        )

    completed = subprocess.run(
        args,
        shell=shell,
        cwd=str(cwd_path),
        env=dict(env) if env is not None else None,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        check=check,
    )
    return SandboxExecResult(
        args=args,
        returncode=completed.returncode,
        stdout=_text(completed.stdout),
        stderr=_text(completed.stderr),
        cwd=cwd_path,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


__all__ = ["SandboxExecResult", "SandboxViolation", "sandbox_exec"]

"""Native ``run_shell`` tool with risk classification + sandbox enforcement.

References:
* docs/agent-harness-comparison-and-refactor-todo.md Phase 5 (BashTool risk).
* Claude Code's ``BashTool/bashPermissions.ts`` — we approximate the
  *intent* (per-call risk classification, prefix rules, destructive
  patterns) without taking a runtime dep on a JS shell parser.

Behaviour:

* Commands run with ``shell=True`` so chained pipelines work as the
  model expects (``ls | head``).
* ``cwd`` is resolved under the workspace root via
  :func:`resolve_workspace_path`. Escapes are denied.
* ``timeout`` defaults to 30s, hard cap 300s.
* ``stdout`` / ``stderr`` are truncated at 64 KiB each by default
  (configurable up to 256 KiB).
* The *per-call risk classifier* upgrades known-destructive commands
  (``rm -rf``, ``chmod -R 0``, ``mkfs``, fork bomb, etc.) to
  :class:`RiskLevel.DANGEROUS` so the permission engine asks before
  running them.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from ..types import (
    ContextModifier,
    RiskLevel,
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultPart,
)
from .paths import (
    WorkspaceEscapeError,
    resolve_workspace_path,
    to_workspace_relative,
)


_DEFAULT_TIMEOUT_S = 30
_MAX_TIMEOUT_S = 300
_DEFAULT_OUTPUT_BYTES = 64 * 1024
_MAX_OUTPUT_BYTES = 256 * 1024


# Destructive patterns mirror the operator skill heuristics but are
# centralised here so the registry-side risk classifier can read them
# without importing from ``skills.builtin``.
_DESTRUCTIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f", re.IGNORECASE), "rm -rf"),
    (re.compile(r"\brm\s+-[a-z]*f[a-z]*r", re.IGNORECASE), "rm -fr"),
    (re.compile(r"\bdd\s+if=", re.IGNORECASE), "dd if="),
    (re.compile(r"\bmkfs(\.|\s)", re.IGNORECASE), "mkfs"),
    (re.compile(r"\bshred\b", re.IGNORECASE), "shred"),
    (re.compile(r":\(\)\s*\{.*?:.*?\}\s*;\s*:", re.DOTALL), "fork bomb"),
    (re.compile(r">\s*/dev/sd[a-z]"), "raw block device write"),
    (re.compile(r"\bchmod\s+-R\s+0?00\b"), "chmod -R 000"),
    (re.compile(r"\bgit\s+clean\s+-[a-z]*f", re.IGNORECASE), "git clean -f"),
    (re.compile(r"\bgit\s+reset\s+--hard", re.IGNORECASE), "git reset --hard"),
    (re.compile(r"\bgit\s+push\s+--force", re.IGNORECASE), "git push --force"),
    (re.compile(r"\bsudo\b", re.IGNORECASE), "sudo"),
)

_NETWORK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcurl\b"),
    re.compile(r"\bwget\b"),
    re.compile(r"\bnpm\s+publish\b"),
    re.compile(r"\bpip\s+install\b"),
    re.compile(r"\bnpm\s+install\b"),
    re.compile(r"\byarn\s+add\b"),
    re.compile(r"\bgit\s+push\b"),
)

_SHELL_SEGMENT_RE = re.compile(r"(?:^|[;&|]\s*)([A-Za-z0-9_.\\/-]+)")
_DELETE_HEADS = {
    "rm", "del", "erase", "rmdir", "rd", "remove-item", "ri",
}
_WRITE_HEADS = {
    "cp", "copy", "mv", "move", "mkdir", "md", "touch", "tee",
    "sed", "perl", "npm", "pnpm", "yarn", "pip", "python", "python3",
}
_READ_HEADS = {
    "ls", "dir", "cat", "type", "head", "tail", "echo", "pwd", "cd",
    "wc", "stat", "file", "grep", "rg", "find", "tree", "diff",
    "git", "where", "which", "Get-ChildItem".lower(), "Get-Content".lower(),
    "Select-String".lower(),
}
_SAFE_GIT_SUBCOMMANDS = {
    "status", "log", "diff", "show", "branch", "remote", "config",
    "rev-parse", "ls-files", "grep", "describe", "tag", "blame",
}
_CONFIG_PATH_RE = re.compile(
    r"(?ix)"
    r"(^|[\s\"'])"
    r"("
    r"\.env(?:\.[^\s\"']+)?|"
    r"nerya\.ya?ml|"
    r"accounts[/\\][^\s\"']+|"
    r"exchanges\.ya?ml|"
    r"triggers[/\\]schedules\.yml|"
    r"strategies[/\\][^/\\\s\"']+[/\\](strategy|limits)\.ya?ml|"
    r"providers[/\\][^\s\"']+|"
    r"(vault|secrets)[/\\][^\s\"']+"
    r")"
)


def _command_heads(cmd: str) -> list[str]:
    heads: list[str] = []
    for match in _SHELL_SEGMENT_RE.finditer(cmd):
        raw = match.group(1).strip().strip("\"'")
        if not raw:
            continue
        heads.append(Path(raw).name.lower())
    return heads


def _looks_like_config_write(cmd: str, heads: list[str]) -> bool:
    if not _CONFIG_PATH_RE.search(cmd):
        return False
    if ">" in cmd or ">>" in cmd:
        return True
    if any(head in _WRITE_HEADS or head in _DELETE_HEADS for head in heads):
        return True
    return False


def classify_shell_risk(arguments: dict[str, Any]) -> RiskLevel:
    """Map ``run_shell`` arguments -> :class:`RiskLevel`.

    Matches Claude Code's *spirit* (allow read commands without prompt,
    ask on writes/network, hard-block destructive without explicit
    approval) without forking ``mvdan/sh``.
    """

    cmd = str((arguments or {}).get("command") or "")
    if not cmd:
        return RiskLevel.READ
    heads = _command_heads(cmd)
    if any(head in _DELETE_HEADS for head in heads):
        return RiskLevel.DANGEROUS
    if _looks_like_config_write(cmd, heads):
        return RiskLevel.DANGEROUS
    if any(head in {"powershell", "pwsh"} for head in heads) and re.search(
        r"\b(Remove-Item|Set-Content|Add-Content|Clear-Content)\b",
        cmd,
        re.IGNORECASE,
    ):
        return RiskLevel.DANGEROUS
    for rx, _label in _DESTRUCTIVE_PATTERNS:
        if rx.search(cmd):
            return RiskLevel.DANGEROUS
    if re.search(r"\bfind\b.*\s-delete\b", cmd, re.IGNORECASE):
        return RiskLevel.DANGEROUS
    for rx in _NETWORK_PATTERNS:
        if rx.search(cmd):
            return RiskLevel.EXEC
    parts = cmd.strip().split()
    head = Path(parts[0]).name.lower() if parts else ""
    if head in _READ_HEADS and ">" not in cmd and ">>" not in cmd:
        if head != "git":
            return RiskLevel.READ
    if head == "git":
        sub = parts[1] if len(parts) > 1 else ""
        if sub in _SAFE_GIT_SUBCOMMANDS:
            return RiskLevel.READ
        return RiskLevel.EXEC
    if head in _WRITE_HEADS:
        return RiskLevel.WRITE
    return RiskLevel.EXEC


def _truncate(s: str, *, limit: int) -> tuple[str, bool]:
    data = s.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return s, False
    head = data[: limit // 2]
    tail = data[-limit // 2 :]
    return (
        head.decode("utf-8", errors="ignore")
        + f"\n\n... truncated {len(data) - limit} bytes ...\n\n"
        + tail.decode("utf-8", errors="ignore"),
        True,
    )


def run_shell_handler(call: ToolCall, *, root: Path) -> ToolResult:
    args = call.arguments or {}
    cmd = args.get("command")
    description = args.get("description") or ""
    cwd_arg = args.get("cwd") or "."
    timeout_s = args.get("timeout_s") or args.get("timeout") or _DEFAULT_TIMEOUT_S
    background = bool(args.get("background", False))
    output_limit = int(args.get("output_limit") or _DEFAULT_OUTPUT_BYTES)
    output_limit = max(1024, min(output_limit, _MAX_OUTPUT_BYTES))

    if not isinstance(cmd, str) or not cmd.strip():
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="run_shell requires a non-empty 'command' string",
            ),
        )
    try:
        timeout_s = float(timeout_s)
    except Exception:
        timeout_s = _DEFAULT_TIMEOUT_S
    timeout_s = max(1.0, min(timeout_s, float(_MAX_TIMEOUT_S)))

    try:
        cwd = resolve_workspace_path(str(cwd_arg), root=root, default=".")
    except WorkspaceEscapeError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PERMISSION_DENIED,
                message=str(exc),
            ),
        )
    if not cwd.is_dir():
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message=f"cwd is not a directory: {cwd}",
            ),
        )

    env = os.environ.copy()
    started = time.monotonic()
    proc: Optional[subprocess.Popen[str]] = None
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if background:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return ToolResult(
                tool_use_id=call.id,
                name=call.name,
                elapsed_ms=elapsed_ms,
                content=[
                    ToolResultPart.text_part(
                        f"$ {cmd}\n[backgrounded; pid={proc.pid}]"
                    ),
                    ToolResultPart.json_part(
                        {
                            "command": cmd,
                            "cwd": to_workspace_relative(cwd, root),
                            "pid": proc.pid,
                            "background": True,
                            "description": description,
                        }
                    ),
                ],
                metadata={"pid": proc.pid, "background": True},
            )

        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except Exception:
                stdout, stderr = "", ""
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.TIMEOUT,
                    message=f"command exceeded timeout of {timeout_s}s",
                    detail={
                        "command": cmd,
                        "cwd": to_workspace_relative(cwd, root),
                        "stdout_preview": (stdout or "")[:1024],
                        "stderr_preview": (stderr or "")[:1024],
                    },
                ),
            )
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"failed to launch command: {exc}",
            ),
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    exit_code = proc.returncode if proc else -1
    stdout, stdout_truncated = _truncate(stdout or "", limit=output_limit)
    stderr, stderr_truncated = _truncate(stderr or "", limit=output_limit)

    text = (
        f"$ {cmd}\n"
        f"[exit={exit_code}, took {elapsed_ms}ms"
        + (f", cwd={to_workspace_relative(cwd, root)}" if str(cwd_arg) != "." else "")
        + "]\n\n"
    )
    if stdout:
        text += f"## stdout\n{stdout}\n"
    if stderr:
        text += f"\n## stderr\n{stderr}\n"

    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        elapsed_ms=elapsed_ms,
        is_error=(exit_code != 0),
        content=[
            ToolResultPart.shell_part(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=elapsed_ms,
                truncated=stdout_truncated or stderr_truncated,
            ),
            ToolResultPart.text_part(text),
        ],
        metadata={
            "command": cmd,
            "exit_code": exit_code,
            "cwd": to_workspace_relative(cwd, root),
            "description": description,
        },
        context_modifiers=[
            ContextModifier(
                kind="artifact_index",
                payload={
                    "kind": "shell_command",
                    "command": cmd,
                    "exit_code": exit_code,
                    "elapsed_ms": elapsed_ms,
                },
            )
        ],
    )


__all__ = ["classify_shell_risk", "run_shell_handler"]

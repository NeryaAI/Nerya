"""Native ``run_shell`` tool with risk classification + sandbox enforcement.

Implementation notes:
* Use per-call risk classification, prefix rules, and destructive-pattern
  detection without taking a runtime dependency on a shell parser.

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
import subprocess
import time
from pathlib import Path
from typing import Any

from ...core.sandbox import sandbox_exec
from ...security.runtime_env import build_process_env
from ..tool_errors import schema_validation_result
from ..types import (
    ContextModifier,
    RiskLevel,
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultPart,
)
from .conversation_files import conversation_files_dir
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
_NETWORK_FETCH_HEADS = {"curl", "wget"}
_READ_ONLY_PIPE_HEADS = {
    "awk",
    "cat",
    "cut",
    "grep",
    "head",
    "jq",
    "python",
    "python3",
    "py",
    "sed",
    "sort",
    "tail",
    "tr",
    "uniq",
    "wc",
}
_NETWORK_WRITE_FLAGS_RE = re.compile(
    r"(?ix)"
    r"("
    r"\b(?:curl|wget)\b[^\n|;&]*\s(?:"
    r"-o|--output|-O|--remote-name|--output-document|"
    r"-T|--upload-file|--ftp-create-dirs"
    r")(?:\s|=|$)|"
    r"\b(?:curl|wget)\b[^\n|;&]*\s(?:"
    r"-d|--data(?:-[a-z-]+)?|-F|--form|--form-string|"
    r"--post-data|--method\s+(?:POST|PUT|PATCH|DELETE)"
    r")(?:\s|=|$)"
    r")"
)
_NETWORK_MUTATING_METHOD_RE = re.compile(
    r"(?i)\b(?:curl|wget)\b[^\n|;&]*(?:-X|--request)\s*=?\s*(POST|PUT|PATCH|DELETE)\b"
)
_NETWORK_SECRET_SEND_RE = re.compile(
    r"(?i)\b(authorization|cookie|x-api-key|api[_-]?key|bearer|secret|token)\b"
)

_NATIVE_STRATEGY_DISCOVERY_TERMS = (
    "capability_catalog",
    "meme_strategy_guide",
    "data_api",
    "connector_list",
    "connector_view",
    "market_data",
    "onchainos",
    "okx_onchain",
    "byreal",
    "wallet provider",
    "wallet capability",
    "token_hot_tokens",
    "memepump",
)
_SHELL_TEST_OR_BUILD_RE = re.compile(
    r"\b(pytest|ruff|mypy|pyright|npm\s+test|pnpm\s+test|yarn\s+test)\b",
    re.IGNORECASE,
)
_SHELL_PROJECT_COMMAND_RE = re.compile(
    r"\b("
    r"pytest|mypy|pyright|"
    r"ruff(?![^\n]*--fix)|"
    r"(?:npm|pnpm|yarn)\s+(?:(?:run|run-script)\s+)?"
    r"(?:test|build|lint|typecheck|check)"
    r")\b",
    re.IGNORECASE,
)
_PARENT_PATH_RE = re.compile(r"(^|[\s\"'=:(])\.\.(?:[/\\]|$)")
_WORKSPACE_ENUM_RE = re.compile(
    r"(?i)(list\s+workspace|workspace\s+files|workspace\s+root|os\.walk|"
    r"get-childitem|dir\s+.*[/\\]s|ls\s+-la|find\s+.+-name)"
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
_FS_PATH_ACCESS_HEADS = {
    "cat", "type", "head", "tail", "more", "less", "ls", "dir",
    "stat", "file", "get-content", "gc",
}
_SAFE_GIT_SUBCOMMANDS = {
    "status", "log", "diff", "show", "branch", "remote", "config",
    "rev-parse", "ls-files", "grep", "describe", "tag", "blame",
}
_URL_LIKE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_ABSOLUTE_PATH_FLAG_RE = re.compile(r"^/[A-Za-z](?::.*)?$")
_CONFIG_PATH_RE = re.compile(
    r"(?ix)"
    r"(^|[\s\"'])"
    r"("
    r"\.env(?:\.[^\s\"']+)?|"
    r"nerya\.ya?ml|"
    r"news_feeds\.ya?ml|"
    r"messages[/\\]channels\.ya?ml|"
    r"accounts[/\\][^\s\"']+|"
    r"exchanges\.ya?ml|"
    r"triggers[/\\]schedules\.yml|"
    r"strategies[/\\][^/\\\s\"']+[/\\](strategy|limits)\.ya?ml|"
    r"providers[/\\][^\s\"']+|"
    r"(vault|secrets)[/\\][^\s\"']+"
    r")"
)
_PROPOSAL_ONLY_CONFIG_PATH_RE = re.compile(
    r"(?ix)(^|[\s\"'])"
    r"(nerya\.ya?ml|agents\.ya?ml|workspace\.ya?ml|"
    r"news_feeds\.ya?ml|"
    r"messages[/\\]channels\.ya?ml|"
    r"policies[/\\](planner|tier_policy)\.ya?ml)"
)
_LIVE_STRATEGY_PATH_RE = re.compile(
    r"(?ix)(^|[\s\"'])strategies[/\\][^/\\\s\"']+[/\\][^\s\"']+"
)
_SHELL_WRITE_TEXT_RE = re.compile(
    r"(?ix)"
    r"\b(Set-Content|Add-Content|Clear-Content|Out-File)\b|"
    r"WriteAllText|write_text|open\s*\([^)]*[\"']w[\"']|"
    r"\bsed\b[^\n|;&]*\s-i\b|"
    r"\bperl\b[^\n|;&]*\s-pi\b"
)


def _mask_quoted_shell_text(cmd: str) -> str:
    out: list[str] = []
    quote = ""
    escaped = False
    for ch in cmd:
        if quote:
            if escaped:
                out.append(" ")
                escaped = False
                continue
            if ch == "\\" and quote == '"':
                out.append(" ")
                escaped = True
                continue
            if ch == quote:
                quote = ""
                out.append(ch)
            else:
                out.append(" ")
            continue
        if ch in {"'", '"'}:
            quote = ch
            out.append(ch)
            continue
        out.append(ch)
    return "".join(out)


def _command_heads(cmd: str) -> list[str]:
    heads: list[str] = []
    for match in _SHELL_SEGMENT_RE.finditer(_mask_quoted_shell_text(cmd)):
        raw = match.group(1).strip().strip("\"'")
        if not raw:
            continue
        heads.append(Path(raw).name.lower())
    return heads


def _shell_segments(cmd: str) -> list[list[str]]:
    segments: list[list[str]] = []
    for raw in re.split(r"[;&|]+", cmd):
        raw = raw.strip()
        if not raw:
            continue
        try:
            tokens = shlex.split(raw, posix=False)
        except ValueError:
            tokens = raw.split()
        cleaned = [t.strip().strip("\"'") for t in tokens if t.strip().strip("\"'")]
        if cleaned:
            segments.append(cleaned)
    return segments


def _looks_like_absolute_path_arg(token: str) -> bool:
    text = str(token or "").strip().strip("\"'")
    text = re.sub(r"^(?:\d*(?:>>?|<<?)|&>)", "", text).strip()
    if not text or text.startswith("-") or _URL_LIKE_RE.match(text):
        return False
    # Windows command switches such as /s or /b are not paths.
    if _ABSOLUTE_PATH_FLAG_RE.match(text):
        return False
    return (
        text.startswith("/")
        or text.startswith("\\")
        or text.startswith("~")
        or bool(_WINDOWS_DRIVE_RE.match(text))
    )


def _absolute_path_escape(cmd: str, *, root: Path) -> str:
    """Return the first absolute filesystem argument outside workspace.

    ``cwd`` sandboxing is not sufficient for shell tools: commands such as
    ``cat /etc/passwd`` can still target host paths. Keep this check in the
    native shell layer so every provider/model gets the same hard verifier.
    """

    root_resolved = Path(root).expanduser().resolve()
    mutation_context = _shell_may_mutate_files(cmd, _command_heads(cmd))

    def escaped(raw: str) -> bool:
        text = re.sub(
            r"^(?:\d*(?:>>?|<<?)|&>)",
            "",
            str(raw or "").strip().strip("\"'"),
        ).strip()
        if text.lower() in {"/dev/null", "nul"}:
            return False
        try:
            Path(text).expanduser().resolve().relative_to(root_resolved)
        except ValueError:
            return True
        except Exception:
            return True
        return False

    for segment in _shell_segments(cmd):
        head = Path(segment[0]).name.lower()
        if head not in _FS_PATH_ACCESS_HEADS and not mutation_context:
            continue
        for token in segment[1:]:
            if not _looks_like_absolute_path_arg(token):
                continue
            if escaped(token):
                return token

    if mutation_context:
        quoted_absolute_path = re.compile(
            r"(?P<quote>[\"'])(?P<path>(?:~[/\\]|/|[A-Za-z]:[/\\])"
            r"[^\"'\r\n]+)(?P=quote)"
        )
        for match in quoted_absolute_path.finditer(cmd):
            candidate = match.group("path")
            if escaped(candidate):
                return candidate
    return ""


def _looks_like_config_write(cmd: str, heads: list[str]) -> bool:
    if not _CONFIG_PATH_RE.search(cmd):
        return False
    if ">" in cmd or ">>" in cmd:
        return True
    if any(head in _WRITE_HEADS or head in _DELETE_HEADS for head in heads):
        return True
    return False


def _proposal_only_shell_tool(cmd: str, heads: list[str]) -> str:
    if not (
        _PROPOSAL_ONLY_CONFIG_PATH_RE.search(cmd)
        or _LIVE_STRATEGY_PATH_RE.search(cmd)
    ):
        return ""
    writes = (
        ">" in cmd
        or ">>" in cmd
        or _SHELL_WRITE_TEXT_RE.search(cmd)
        or any(head in _WRITE_HEADS or head in _DELETE_HEADS for head in heads)
    )
    if not writes:
        return ""
    if _PROPOSAL_ONLY_CONFIG_PATH_RE.search(cmd):
        return "evolve_core_config_patch"
    return "strategy_draft_proposal"


def _looks_like_read_only_network_fetch(cmd: str, heads: list[str]) -> bool:
    if not any(head in _NETWORK_FETCH_HEADS for head in heads):
        return False
    if ">" in cmd or ">>" in cmd:
        return False
    if _NETWORK_WRITE_FLAGS_RE.search(cmd):
        return False
    if _NETWORK_MUTATING_METHOD_RE.search(cmd):
        return False
    if _NETWORK_SECRET_SEND_RE.search(cmd):
        return False
    for head in heads:
        if head in _NETWORK_FETCH_HEADS:
            continue
        if head in {"python", "python3", "py"}:
            if re.search(
                rf"\b{re.escape(head)}(?:\.exe)?\b\s+-m\s+json\.tool\b",
                cmd,
                re.IGNORECASE,
            ):
                continue
            return False
        if head == "sed" and re.search(r"\bsed\b[^\n|;&]*\s-i\b", cmd, re.IGNORECASE):
            return False
        if head not in _READ_ONLY_PIPE_HEADS:
            return False
    return True


def _looks_like_native_strategy_discovery(cmd: str, description: str) -> bool:
    haystack = f"{description}\n{cmd}".lower()
    heads = _command_heads(cmd)
    if any(head in _DELETE_HEADS for head in heads):
        return False
    if re.search(r"\bfind\b.*\s-delete\b", cmd, re.IGNORECASE):
        return False
    if _WORKSPACE_ENUM_RE.search(haystack) and any(
        head in _READ_HEADS or head in {"python", "python3", "py"} for head in heads
    ):
        return True
    if not any(term in haystack for term in _NATIVE_STRATEGY_DISCOVERY_TERMS):
        return False
    if _SHELL_TEST_OR_BUILD_RE.search(cmd):
        return False

    uses_python_probe = any(head in {"python", "python3", "py"} for head in heads) and (
        re.search(r"\s-c\b", cmd)
        or "from nerya.data" in haystack
        or "data_api(" in haystack
    )
    if uses_python_probe:
        return True

    searches_workspace_for_native_sources = any(
        head in _READ_HEADS for head in heads
    ) and any(
        term in haystack
        for term in (
            "connector",
            "data source",
            "wallet provider",
            "onchainos",
            "meme_strategy_guide",
            "capability_catalog",
        )
    )
    return searches_workspace_for_native_sources


def classify_shell_risk(arguments: dict[str, Any]) -> RiskLevel:
    """Map ``run_shell`` arguments -> :class:`RiskLevel`.

    Allow read commands without prompt, ask on writes/network, and
    hard-block destructive commands without explicit approval.
    """

    arguments = arguments or {}
    if bool(arguments.get("allow_outside_conversation")):
        return RiskLevel.DANGEROUS
    cmd = str(arguments.get("command") or "")
    if not cmd:
        return RiskLevel.READ
    cmd = cmd.strip()
    if _looks_like_native_strategy_discovery(
        cmd,
        str(arguments.get("description") or ""),
    ):
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
    if _looks_like_read_only_network_fetch(cmd, heads):
        return RiskLevel.READ
    for rx in _NETWORK_PATTERNS:
        if rx.search(cmd):
            return RiskLevel.EXEC
    parts = cmd.strip().split()
    head = Path(parts[0]).name.lower() if parts else ""
    if head in _DELETE_HEADS:
        return RiskLevel.DANGEROUS
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


def _shell_may_mutate_files(cmd: str, heads: list[str]) -> bool:
    """Conservatively identify shell commands that can change files."""

    without_dev_null = re.sub(
        r"(?:\d?>|&>)\s*(?:/dev/null|NUL)\b",
        "",
        cmd,
        flags=re.IGNORECASE,
    )
    if ">" in without_dev_null or _NETWORK_WRITE_FLAGS_RE.search(cmd):
        return True
    if _SHELL_WRITE_TEXT_RE.search(cmd):
        return True
    if re.search(r"\b(?:ruff)\b[^\n|;&]*--fix\b", cmd, re.IGNORECASE):
        return True
    if _SHELL_PROJECT_COMMAND_RE.search(cmd):
        return False
    return any(head in _WRITE_HEADS or head in _DELETE_HEADS for head in heads)


def _conversation_cwd_required_result(
    call: ToolCall,
    *,
    conversation_dir: Path,
    reason: str,
) -> ToolResult:
    relative = conversation_dir.as_posix()
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=ToolErrorKind.PERMISSION_DENIED,
            message=(
                "run_shell was not executed: a file-mutating command in an "
                "Agent conversation must stay in the conversation directory "
                f"{relative!r}. {reason} To write elsewhere, set "
                "allow_outside_conversation=true and provide "
                "outside_conversation_reason; that exception requires approval."
            ),
            retryable=False,
            detail={
                "reason": "conversation_file_placement",
                "conversation_dir": relative,
            },
            recovery_hint={
                "next_required_action": {
                    "tool": "run_shell",
                    "cwd": relative,
                    "reason": "conversation_file_placement",
                }
            },
        ),
    )


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


def run_shell_handler(
    call: ToolCall,
    *,
    root: Path,
    session_id: str | None = None,
) -> ToolResult:
    args = call.arguments or {}
    cmd = args.get("command")
    description = args.get("description") or ""
    cwd_was_explicit = bool(str(args.get("cwd") or "").strip())
    cwd_arg = args.get("cwd") or "."
    allow_outside = bool(args.get("allow_outside_conversation", False))
    outside_reason = str(args.get("outside_conversation_reason") or "").strip()
    timeout_s = args.get("timeout_s") or args.get("timeout") or _DEFAULT_TIMEOUT_S
    background = bool(args.get("background", False))
    output_limit = int(args.get("output_limit") or _DEFAULT_OUTPUT_BYTES)
    output_limit = max(1024, min(output_limit, _MAX_OUTPUT_BYTES))

    if not isinstance(cmd, str) or not cmd.strip():
        return schema_validation_result(
            call, "run_shell requires a non-empty 'command' string",
        )
    if allow_outside and not outside_reason:
        return schema_validation_result(
            call,
            "outside_conversation_reason is required when "
            "allow_outside_conversation=true",
        )
    heads = _command_heads(cmd)
    proposal_tool = _proposal_only_shell_tool(cmd, heads)
    if proposal_tool:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PERMISSION_DENIED,
                message=(
                    "run_shell was not executed: the command appears to mutate "
                    "a proposal-only live strategy or runtime config path. "
                    f"Call {proposal_tool} and stage the change under "
                    "evolution/proposals/ for operator review instead."
                ),
                retryable=False,
                recovery_hint={
                    "next_required_action": {
                        "tool": proposal_tool,
                        "reason": "proposal_only_shell_mutation",
                    }
                },
            ),
        )
    if _looks_like_native_strategy_discovery(cmd, str(description)):
        preferred_tools = ["glob", "list_dir", "read_file"]
        if any(
            term in f"{description}\n{cmd}".lower()
            for term in _NATIVE_STRATEGY_DISCOVERY_TERMS
        ):
            preferred_tools.extend(["role_list", "subagent_list", "strategy_backtest"])
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PERMISSION_DENIED,
                message=(
                    "run_shell was not executed: this command is trying to "
                    "rediscover strategy, connector, wallet, or on-chain data "
                    "that native tools already expose. Read "
                    "strategy_author with skill_view, use connector_list / "
                    "connector_view / data_api / market_data for bounded "
                    "evidence, then call strategy_draft_proposal and edit_file "
                    "/ write_file on the staged proposal files with Nerya SDK "
                    "code when custom strategy logic is needed. For workspace "
                    "file listing use glob, list_dir, or read_file; to "
                    "enumerate roles or subagents use role_list / "
                    "subagent_list; to run a backtest use strategy_backtest. "
                    "Reserve run_shell for explicit "
                    "operator commands, tests, builds, or cases with no "
                    "native tool."
                ),
                retryable=False,
                detail={
                    "reason": "tool_redirect",
                    "preferred_tools": preferred_tools,
                },
                recovery_hint={
                    "reason": "tool_redirect",
                    "preferred_tools": preferred_tools,
                },
            ),
        )
    escaped_path = _absolute_path_escape(cmd, root=root)
    if escaped_path:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PERMISSION_DENIED,
                message=(
                    "permission_denied: workspace sandbox blocked an "
                    f"absolute path outside the workspace: {escaped_path!r}. "
                    "权限不足，已拒绝访问 workspace 沙箱之外的路径。"
                    "run_shell cannot read host/system files; use read_file "
                    "or list_dir with workspace-relative paths only. Tell the "
                    "operator this command was refused (拒绝) by the sandbox."
                ),
                retryable=False,
                detail={
                    "path": escaped_path,
                    "reason": "workspace_sandbox_escape",
                },
                recovery_hint={
                    "action": "use_workspace_relative_path",
                    "reason": "workspace_sandbox_escape",
                },
            ),
        )
    conversation_dir: Path | None = None
    shell_placement = "workspace"
    mutates_files = _shell_may_mutate_files(cmd, heads)
    if mutates_files and _PARENT_PATH_RE.search(cmd):
        if session_id and not allow_outside:
            return _conversation_cwd_required_result(
                call,
                conversation_dir=conversation_files_dir(root, session_id),
                reason="Parent-directory traversal is not allowed for this command.",
            )
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PERMISSION_DENIED,
                message=(
                    "run_shell was not executed: parent-directory traversal "
                    "in a file-mutating command cannot be proven to stay "
                    "inside the workspace. Use a workspace-rooted path instead."
                ),
                retryable=False,
                detail={"reason": "workspace_sandbox_escape"},
            ),
        )
    if session_id:
        conversation_dir = conversation_files_dir(root, session_id)
        if mutates_files and not allow_outside:
            escaped_conversation_path = _absolute_path_escape(
                cmd,
                root=conversation_dir,
            )
            if escaped_conversation_path:
                return _conversation_cwd_required_result(
                    call,
                    conversation_dir=conversation_dir,
                    reason=(
                        "The command names an absolute path outside the "
                        f"conversation directory: {escaped_conversation_path!r}."
                    ),
                )
            if not cwd_was_explicit:
                try:
                    conversation_dir.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    return ToolResult.from_error(
                        tool_use_id=call.id,
                        name=call.name,
                        error=ToolError(
                            kind=ToolErrorKind.EXECUTION_ERROR,
                            message=f"failed to create conversation directory: {exc}",
                        ),
                    )
                cwd_arg = str(conversation_dir)
                shell_placement = "conversation_reroute"
            else:
                shell_placement = "conversation"
        elif mutates_files and allow_outside:
            shell_placement = "explicit_exception"
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
        return schema_validation_result(call, f"cwd is not a directory: {cwd}")
    if (
        conversation_dir is not None
        and mutates_files
        and not allow_outside
        and cwd_was_explicit
    ):
        try:
            cwd.relative_to(conversation_dir.resolve())
        except ValueError:
            return _conversation_cwd_required_result(
                call,
                conversation_dir=conversation_dir,
                reason=f"The requested cwd {to_workspace_relative(cwd, root)!r} is outside it.",
            )

    try:
        env = build_process_env(os.environ, root)
    except Exception:
        env = os.environ.copy()
    if conversation_dir is not None:
        env["NERYA_CONVERSATION_DIR"] = str(conversation_dir)
    started = time.monotonic()
    try:
        if background:
            proc = sandbox_exec(
                cmd,
                shell=True,
                cwd=cwd,
                root=root,
                env=env,
                capture_output=True,
                text=True,
                background=True,
            )
            elapsed_ms = proc.elapsed_ms
            pid = proc.pid
            return ToolResult(
                tool_use_id=call.id,
                name=call.name,
                elapsed_ms=elapsed_ms,
                content=[
                    ToolResultPart.text_part(
                        f"$ {cmd}\n[backgrounded; pid={pid}]"
                    ),
                    ToolResultPart.json_part(
                        {
                            "command": cmd,
                            "cwd": to_workspace_relative(cwd, root),
                            "pid": pid,
                            "background": True,
                            "description": description,
                            "placement": shell_placement,
                            **(
                                {"outside_conversation_reason": outside_reason}
                                if outside_reason
                                else {}
                            ),
                        }
                    ),
                ],
                metadata={
                    "pid": pid,
                    "background": True,
                    "placement": shell_placement,
                    **(
                        {"outside_conversation_reason": outside_reason}
                        if outside_reason
                        else {}
                    ),
                },
            )

        proc = sandbox_exec(
            cmd,
            shell=True,
            cwd=cwd,
            root=root,
            env=env,
            timeout=timeout_s,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
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
                    "stdout_preview": str(stdout)[:1024],
                    "stderr_preview": str(stderr)[:1024],
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

    elapsed_ms = proc.elapsed_ms
    exit_code = int(proc.returncode or 0)
    stdout, stdout_truncated = _truncate(proc.stdout or "", limit=output_limit)
    stderr, stderr_truncated = _truncate(proc.stderr or "", limit=output_limit)

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
            "placement": shell_placement,
            **(
                {"outside_conversation_reason": outside_reason}
                if outside_reason
                else {}
            ),
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

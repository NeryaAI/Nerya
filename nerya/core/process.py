"""Small Windows spawn adaptations shared by native tools and stdio MCP.

POSIX argv, environment and executable lookup are deliberately unchanged.
Commands are never split with shlex or globally rewritten: backslashes in
JSON, regular expressions and script arguments are data, not path separators.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


def prepare_process_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    if os.name != "nt":
        return dict(env) if env is not None else None
    # Windows names are case-insensitive even when an ordinary Python dict
    # contains both Path and PATH. The most recent override must win.
    result = {key.upper(): value for key, value in (os.environ if env is None else env).items()}
    result.setdefault("PYTHONUTF8", "1")
    result.setdefault("PYTHONIOENCODING", "utf-8")
    return result


def resolve_process_args(
    args: str | Sequence[str], *, env: Mapping[str, str] | None,
    cwd: str | Path | None, shell: bool = False,
) -> str | Sequence[str]:
    if os.name != "nt" or shell or isinstance(args, str) or not args:
        return args
    command = list(args)
    executable = os.fspath(command[0])
    if len(executable) >= 2 and executable[0] == executable[-1] == '"':
        executable = executable[1:-1]
    executable = os.path.expanduser(os.path.expandvars(executable))
    # Popen on Windows does not use the child env's PATH or cwd for executable
    # lookup. Resolve first, including npm.cmd/npx.cmd and relative executables.
    if os.path.dirname(executable):
        if not os.path.isabs(executable):
            executable = os.path.abspath(os.path.join(cwd or os.getcwd(), executable))
    else:
        executable = shutil.which(executable, path=(env or os.environ).get("PATH")) or executable
    command[0] = executable
    return command


def terminate_windows_tree(proc: subprocess.Popen) -> bool:
    """Stop a running child tree; report whether taskkill confirmed success.

    A parent that already exited cannot prove ownership of all descendants.
    Never report process_group_stopped=True merely because poll() returned.
    """
    confirmed = False
    if proc.poll() is not None:
        return confirmed
    taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
    try:
        result = subprocess.run(
            [str(taskkill), "/PID", str(proc.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        confirmed = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    return confirmed

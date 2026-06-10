from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nerya.core.sandbox import SandboxViolation, sandbox_exec

pytestmark = pytest.mark.smoke


def test_sandbox_exec_runs_command_inside_workspace(tmp_path: Path) -> None:
    result = sandbox_exec(
        [sys.executable, "-c", "print('sandbox-ok')"],
        cwd=tmp_path,
        root=tmp_path,
        timeout=5,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "sandbox-ok"
    assert result.cwd == tmp_path.resolve()


def test_sandbox_exec_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent

    with pytest.raises(SandboxViolation, match="outside workspace"):
        sandbox_exec(
            [sys.executable, "-c", "print('escape')"],
            cwd=outside,
            root=tmp_path,
            timeout=5,
            capture_output=True,
            text=True,
        )


def test_shell_class_tools_route_subprocess_through_sandbox_exec() -> None:
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "nerya" / "tools" / "native" / "shell.py",
        root / "nerya" / "tools" / "native" / "search.py",
        root / "nerya" / "tools" / "native" / "skill.py",
        root / "nerya" / "skills" / "installer.py",
    ]

    for path in files:
        source = path.read_text(encoding="utf-8")
        assert "sandbox_exec" in source, path
        assert "subprocess.run(" not in source, path
        assert "subprocess.Popen(" not in source, path

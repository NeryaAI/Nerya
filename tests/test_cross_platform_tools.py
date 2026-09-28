"""Offline contracts run unchanged on Windows, macOS and Linux runners."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import ntpath
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from nerya.agent.file_state import FileStateCache, compute_file_hash
from nerya.core import process as process_module
from nerya.core.sandbox import ManagedProcess, sandbox_exec
from nerya.mcp.transports.stdio import StdioMCPClient, StdioTransportError
from nerya.tools.native.file_ops import edit_file_handler, read_file_handler, write_file_handler
from nerya.tools.native.paths import WorkspaceEscapeError, resolve_workspace_path, to_workspace_relative
from nerya.tools.native.search import glob_handler, grep_handler
from nerya.tools.native.shell import (
    _absolute_path_escape, _shell_invocation, _shell_segments,
    run_shell_handler, shell_runtime_description,
)
from nerya.tools.native.skill import SkillIndex, script_run_handler
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "Nerya workspace 中文"
    root.mkdir()
    return root


def test_relative_cwd_uses_workspace_not_server_cwd(workspace):
    child = workspace / "nested dir"
    child.mkdir()
    result = sandbox_exec([sys.executable, "-c", "import os; print(os.getcwd())"],
                          cwd="nested dir", root=workspace, timeout=5)
    assert result.returncode == 0
    assert Path(result.stdout.strip()) == child.resolve()


@pytest.mark.parametrize("threaded", [False, True])
def test_pipe_readers_drain_both_streams_without_deadlock(workspace, monkeypatch, threaded):
    if threaded and os.name != "nt":
        # Exercise the Windows reader implementation on POSIX too, without
        # monkeypatching os.name globally (which corrupts pathlib's host flavour).
        monkeypatch.setattr(ManagedProcess, "_drain", ManagedProcess._drain_windows)
    result = sandbox_exec([sys.executable, "-c",
        "import os; [(os.write(1,b'x'*65536),os.write(2,b'y'*65536)) for _ in range(16)]"],
        cwd=workspace, root=workspace, timeout=5, output_limit=4096)
    assert result.returncode == 0 and result.process_exited and result.truncated
    assert result.stdout == "x" * 4096 and result.stderr == "y" * 4096


def test_argv_preserves_paths_unicode_json_and_empty_args(workspace):
    script = workspace / "echo args.py"
    script.write_text("import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=True))", encoding="utf-8")
    arguments = [r"C:\Users\中文 用户\file.txt", "C:\\trailing\\", "", "a b", 'a"b',
                 json.dumps({"path": r"C:\test\file", "regex": r"\d+\s"}), "a&b|c;d"]
    result = sandbox_exec([sys.executable, str(script), *arguments], cwd=workspace, root=workspace, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == arguments


@pytest.mark.parametrize("action", ["timeout", "stop"])
def test_process_termination_is_bounded_and_reaped(workspace, action):
    child = sandbox_exec([sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(30)"],
                         cwd=workspace, root=workspace, background=True, timeout=0.4 if action == "timeout" else 5)
    until = time.monotonic() + 3
    while "ready" not in child.process.snapshot().stdout and time.monotonic() < until:
        time.sleep(0.02)
    if action == "stop":
        child.process.stop()
    result = child.process.wait()
    assert result.process_exited and result.returncode is not None
    assert result.timed_out if action == "timeout" else result.cancelled


def test_child_only_path_and_executable_with_spaces(workspace):
    folder = workspace / "custom bin"
    folder.mkdir()
    if os.name == "nt":
        executable = folder / "nerya-probe.cmd"
        executable.write_bytes(b"@echo off\r\necho child-path-ok\r\n")
    else:
        executable = folder / "nerya-probe"
        executable.write_bytes(b"#!/bin/sh\nprintf child-path-ok\n")
        executable.chmod(0o755)
    env = dict(os.environ, PATH=str(folder))
    result = sandbox_exec(["nerya-probe"], cwd=workspace, root=workspace, env=env, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "child-path-ok"


def test_windows_env_keys_and_argv_lookup_are_platform_specific(monkeypatch):
    monkeypatch.setattr(process_module, "os", SimpleNamespace(name="nt", path=ntpath,
        environ={"SystemRoot": r"C:\Windows"}, fspath=os.fspath, getcwd=lambda: r"C:\workspace"))
    env = process_module.prepare_process_env({"Path": "old", "PATH": "new", "pythonioencoding": "utf-8"})
    assert env == {"PATH": "new", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    monkeypatch.setattr(process_module.shutil, "which", lambda name, path=None: r"C:\Program Files\node\npx.cmd")
    args = ["npx", "-y", "pkg", r"C:\my data\input.json", ""]
    resolved = process_module.resolve_process_args(args, env=env, cwd=r"C:\workspace")
    assert resolved == [r"C:\Program Files\node\npx.cmd", *args[1:]]
    assert args[0] == "npx"


@pytest.mark.parametrize("path", ["C:relative.txt", "D:folder/file", "../outside", "a\x00b"])
def test_ambiguous_or_escaping_paths_are_rejected(workspace, path):
    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(path, root=workspace)


def test_paths_use_native_host_semantics(workspace):
    path = workspace / "folder" / "数据.txt"
    path.parent.mkdir()
    path.write_bytes(b"test")
    for raw in [str(path), path.as_posix(), "folder/数据.txt"]:
        assert resolve_workspace_path(raw, root=workspace, must_exist=True) == path.resolve()
    assert to_workspace_relative(path, workspace) == "folder/数据.txt"
    if os.name == "nt":
        assert resolve_workspace_path(r"folder\数据.txt", root=workspace) == path.resolve()
        assert FileStateCache._key(str(path).upper()) == FileStateCache._key(path)
    else:
        # Backslash is a legal POSIX filename character, not a separator.
        literal = workspace / r"literal\name.txt"
        literal.write_bytes(b"literal")
        assert resolve_workspace_path(r"literal\name.txt", root=workspace) == literal.resolve()
        with pytest.raises(WorkspaceEscapeError):
            resolve_workspace_path(r"C:\Users\other\file.txt", root=workspace)


@pytest.mark.skipif(os.name != "nt", reason="native Windows filename rules")
@pytest.mark.parametrize("path", ["NUL", "folder/CON.txt", "notes.txt:stream", "nerya.yml.", "notes /file"])
def test_windows_invalid_filename_aliases_are_rejected(workspace, path):
    with pytest.raises(WorkspaceEscapeError, match="invalid workspace path"):
        resolve_workspace_path(path, root=workspace)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_file_write_read_edit_preserves_eol_and_hash(workspace, newline):
    cache = FileStateCache()
    content = newline.join(["first 中文", "second", "third", ""])
    path = "notes/test file.txt"
    created = write_file_handler(ToolCall(name="write_file", arguments={"path": path, "content": content}),
                                root=workspace, file_state=cache)
    assert not created.is_error, created.text()
    assert (workspace / path).read_bytes() == content.encode("utf-8")
    read = read_file_handler(ToolCall(name="read_file", arguments={"path": path}), root=workspace, file_state=cache)
    assert not read.is_error
    edited = edit_file_handler(ToolCall(name="edit_file", arguments={
        "path": path, "old_string": "first 中文\nsecond", "new_string": "changed 中文\nsecond"}),
        root=workspace, file_state=cache)
    assert not edited.is_error, edited.text()
    raw = (workspace / path).read_bytes()
    assert raw == content.replace("first", "changed").encode("utf-8")
    payload = next(part.data for part in edited.content if part.type == "json")
    assert payload["content_hash"] == compute_file_hash(raw)


def test_skill_script_relative_prefix_and_arguments(workspace):
    skill = workspace / "skills" / "demo"
    scripts = skill / "scripts" / "nested folder"
    scripts.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nid: demo\ndescription: test\n---\n# Demo\n", encoding="utf-8")
    (scripts / "echo args.py").write_text(
        "import json,sys; print(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    args = [r"C:\Users\中文\input file.csv", "", "a b"]
    result = script_run_handler(ToolCall(name="script_run", arguments={"skill_id": "demo",
        "name": "scripts/nested folder/echo args.py", "args": args}),
        skill_index=SkillIndex([workspace / "skills"]), cwd=workspace)
    assert not result.is_error, result.text()
    assert next(part.data for part in result.content if part.type == "json")["stdout_json"] == args


def test_glob_and_grep_handle_native_paths_and_leading_dash(workspace):
    directory = workspace / "folder with spaces"
    directory.mkdir()
    (directory / "test.txt").write_text("--needle\n", encoding="utf-8")
    result = glob_handler(ToolCall(name="glob", arguments={"pattern": str(directory / "*.txt")}), root=workspace)
    assert not result.is_error and result.metadata["count"] == 1
    result = grep_handler(ToolCall(name="grep", arguments={"pattern": "--needle", "path": str(directory)}), root=workspace)
    assert not result.is_error and result.metadata["count"] == 1


def test_symlink_targets_outside_workspace_not_read(workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-needle", encoding="utf-8")
    try:
        (workspace / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("host does not permit symlink creation")
    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path("link.txt", root=workspace)
    result = glob_handler(ToolCall(name="glob", arguments={"pattern": "*.txt"}), root=workspace)
    assert result.metadata["count"] == 0
    result = grep_handler(ToolCall(name="grep", arguments={"pattern": "outside-needle"}), root=workspace)
    assert result.metadata["count"] == 0


def test_shell_default_and_powershell_encoding(workspace):
    raw = 'Write-Output "中文 C:\\Users\\some folder"'
    args, use_shell, name = _shell_invocation(raw, "powershell", workspace)
    decoded = base64.b64decode(args[-1]).decode("utf-16-le")
    assert raw in decoded and not use_shell and name == "powershell"
    assert _shell_invocation("echo ok", "default", workspace) == (
        "echo ok", True, "cmd" if os.name == "nt" else "sh")
    assert ("Windows" if os.name == "nt" else "POSIX") in shell_runtime_description()


@pytest.mark.skipif(os.name != "nt", reason="native Windows PowerShell contract")
def test_windows_powershell_unicode_and_exit_code(workspace):
    result = run_shell_handler(ToolCall(name="run_shell", arguments={"command": "Write-Output '中文输出'",
                               "shell": "powershell"}), root=workspace)
    assert not result.is_error, result.text()
    assert "中文输出" in result.text()
    result = run_shell_handler(ToolCall(name="run_shell", arguments={"command": "exit 7", "shell": "powershell"}), root=workspace)
    assert result.is_error and result.metadata["exit_code"] == 7


def test_default_shell_quoted_python_command(workspace):
    args = [sys.executable, "-c", "print('shell-ok')"]
    command = subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
    result = run_shell_handler(ToolCall(name="run_shell", arguments={"command": command}), root=workspace)
    assert not result.is_error, result.text()
    assert "shell-ok" in result.text()


def test_quoted_shell_paths_are_not_split_at_metacharacters(workspace):
    path = workspace / "R & D; research" / "input.txt"
    command = f'cat "{path}" | head'
    assert _shell_segments(command) == [["cat", str(path)], ["head"]]
    assert _absolute_path_escape(command, root=workspace) == ""
    assert _absolute_path_escape(f'cat "{workspace.parent / "outside & private.txt"}"', root=workspace)


@pytest.mark.skipif(os.name != "posix", reason="POSIX group probe")
def test_inaccessible_exited_group_is_not_reported_stopped(monkeypatch):
    process = ManagedProcess.__new__(ManagedProcess)
    process.proc = SimpleNamespace(pid=123456, poll=lambda: 0)
    def denied(*args):
        raise PermissionError("group is not accessible")
    monkeypatch.setattr(os, "killpg", denied)
    assert process._group_alive() is True
    process._signal(15)  # no unhandled exception after the parent already exited


@pytest.fixture
def mcp_script(workspace):
    script = workspace / "mcp fixture.py"
    script.write_text(
        "import json,os,sys,time\n"
        "for line in sys.stdin:\n"
        "    msg=json.loads(line)\n"
        "    if 'id' not in msg: continue\n"
        "    if msg['method']=='initialize': result={}\n"
        "    elif msg['params'].get('name')=='silent': time.sleep(30); continue\n"
        "    else:\n"
        "        os.write(2, b'x'*200000)\n"
        "        result={'echo':msg['params'].get('arguments',{})}\n"
        "    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}),flush=True)\n",
        encoding="utf-8")
    return script


def test_stdio_drains_stderr_and_serializes_concurrent_calls(workspace, mcp_script):
    with StdioMCPClient(server_id="fixture", command=[sys.executable, "-u", str(mcp_script)],
                        cwd=str(workspace), read_timeout=5) as client:
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda i: client.call_tool("echo", {"index": i}), range(6)))
        assert results == [{"echo": {"index": i}} for i in range(6)]


def test_stdio_read_timeout_closes_child_and_does_not_reuse_late_reply(workspace, mcp_script):
    with StdioMCPClient(server_id="fixture", command=[sys.executable, "-u", str(mcp_script)],
                        cwd=str(workspace), read_timeout=0.2) as client:
        client._ensure_started()
        proc = client._proc
        started = time.monotonic()
        with pytest.raises(StdioTransportError, match="timed out"):
            client.call_tool("silent", {})
        assert time.monotonic() - started < 8
        assert proc.poll() is not None and client._proc is None
        assert client.call_tool("echo", {"after": "timeout"}) == {"echo": {"after": "timeout"}}


def test_stdio_startup_timeout_is_real(workspace):
    client = StdioMCPClient(server_id="silent", command=[sys.executable, "-c", "import time;time.sleep(30)"],
                            cwd=str(workspace), startup_timeout=0.2)
    started = time.monotonic()
    with pytest.raises(StdioTransportError, match="timed out"):
        client.list_tools()
    assert time.monotonic() - started < 8
    assert client._proc is None

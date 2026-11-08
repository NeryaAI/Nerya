"""Real OS-boundary checks; candidate test strings deliberately attempt abuse."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from nerya.strategies import candidate_validation as runner
from nerya.strategies.package import load_package_from_dir

pytestmark = pytest.mark.smoke
macos = pytest.mark.skipif(sys.platform != "darwin", reason="real macOS Seatbelt acceptance")


def package(tmp_path, tests=None, main="def run(ctx): return None\n", extra=None):
    root = tmp_path / "candidate"
    root.mkdir()
    files = {
        "strategy.yml": "id: sandbox_test\nmode: paper\nschedule: {type: cron, cron: '0 * * * *'}\n",
        "main.py": main, **(extra or {}),
    }
    if tests is not None:
        files["tests/test_candidate.py"] = tests
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return load_package_from_dir(root)


def test_disabled_never_spawns_or_copies(tmp_path, monkeypatch):
    candidate = package(tmp_path)
    monkeypatch.setattr(runner, "sandbox_exec", lambda *a, **kw: pytest.fail("spawned"))
    monkeypatch.setattr(runner, "_snapshot", lambda *a: pytest.fail("copied"))
    result = runner.run_candidate_validation(candidate, smoke_test=False, run_tests=False)
    assert result["smoke_issues"] == []
    assert result["behavioral"]["status"] == "not_run"
    assert result["behavioral"]["exit_code"] is None


def test_unsupported_fails_closed(tmp_path, monkeypatch):
    candidate = package(tmp_path)
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "sandbox_exec", lambda *a, **kw: pytest.fail("spawned"))
    result = runner.run_candidate_validation(candidate, smoke_test=True)
    assert result["behavioral"]["status"] == "unavailable"
    assert result["smoke_issues"][0]["severity"] == "blocker"


def test_rss_esrch_waits_for_owned_child_exit_code(tmp_path, monkeypatch):
    from types import SimpleNamespace

    class Proc:
        pid = 12345

        def poll(self):
            return None

    class Managed:
        def __init__(self):
            self.proc = Proc()
            self.wait_calls = 0
            self.stop_calls = 0

        def stop(self):
            self.stop_calls += 1

        def wait(self):
            self.wait_calls += 1
            return SimpleNamespace(returncode=0, stdout="", stderr="", elapsed_ms=1,
                                   truncated=False, timed_out=False, process_exited=True,
                                   process_group_stopped=True)

    managed = Managed()
    sandbox = tmp_path / "sandbox-exec"
    sandbox.touch()
    monkeypatch.setattr(runner, "_SANDBOX", sandbox)
    monkeypatch.setattr(runner, "_memory_reader",
                        lambda: lambda pid: (_ for _ in ()).throw(ProcessLookupError(3, "gone")))
    monkeypatch.setattr(runner, "_snapshot", lambda *args: None)
    monkeypatch.setattr(runner, "_profile", lambda *args: "")
    monkeypatch.setattr(runner, "_python_executable", lambda: tmp_path / "python")
    monkeypatch.setattr(runner, "_timezone_roots", lambda: ())
    monkeypatch.setattr(runner, "sandbox_exec",
                        lambda *args, **kwargs: SimpleNamespace(process=managed))
    monkeypatch.setattr(runner, "_read_report", lambda path: {
        "exit_code": 0, "status": "passed",
        "summary": {key: 0 for key in runner._COUNTS} | {"collected": 1, "passed": 1},
        "failures": [], "smoke_issues": [],
    })

    result = runner.run_candidate_validation(package(tmp_path), smoke_test=True)

    assert result["behavioral"]["status"] == "passed", result
    assert result["behavioral"]["exit_code"] == 0
    assert managed.wait_calls == 1
    assert managed.stop_calls == 0


def test_runtime_libraries_resolve_rpath_from_native_load_commands(tmp_path, monkeypatch):
    from types import SimpleNamespace

    python = tmp_path / "bin/python"
    extension = tmp_path / "lib/python3.12/lib-dynload/_tkinter.so"
    tcl = tmp_path / "lib/libtcl9.0.dylib"
    tk = tmp_path / "lib/libtcl9tk9.0.dylib"
    for path in (python, extension, tcl, tk):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(runner.sys, "base_prefix", str(tmp_path))
    monkeypatch.setattr(runner.sysconfig, "get_config_var", lambda name: str(extension.parent))
    monkeypatch.setattr(runner, "_framework_library", lambda: None)

    def otool(command, **kwargs):
        output = []
        for name in command[2:]:
            path = Path(name)
            output.append(f"{path}:")
            if command[1] == "-L" and path == extension:
                output.extend(f"\t@rpath/{lib.name} (compatibility version 9.0.0, current version 9.0.3)"
                              for lib in (tcl, tk))
            if command[1] == "-l" and path in (python, extension):
                rpath = "@executable_path/../lib" if path == python else "@loader_path/../.."
                output.extend(("          cmd LC_RPATH", "      cmdsize 40",
                               f"         path {rpath} (offset 12)"))
        return SimpleNamespace(stdout="\n".join(output))

    monkeypatch.setattr(runner.subprocess, "run", otool)
    assert runner._runtime_libraries(python) == tuple(sorted((tcl, tk)))


@macos
def test_real_framework_runtime_and_stdlib_dependencies_start_in_sandbox(tmp_path):
    candidate = package(tmp_path, '''
import bz2, lzma, sqlite3, ssl, zlib
def test_native_runtime():
    assert sqlite3.connect(':memory:').execute('select 42').fetchone() == (42,)
    assert ssl.OPENSSL_VERSION
    for module in (bz2, lzma, zlib):
        assert module.decompress(module.compress(b'runtime fixture')) == b'runtime fixture'
''')
    result = runner.run_candidate_validation(candidate, smoke_test=True)
    assert result["smoke_issues"] == [], result["behavioral"]
    assert result["behavioral"]["status"] == "passed", result["behavioral"]
    assert result["behavioral"]["summary"]["passed"] == 1
    assert result["behavioral"]["safety"]["os_isolated"]


@macos
def test_runtime_library_grants_are_literals_and_adjacent_host_files_stay_denied(tmp_path):
    python = runner._python_executable()
    libraries = runner._runtime_libraries(python)
    policy = runner._profile(tmp_path, python, [], ())
    for path in libraries:
        assert f'(allow file-read* (literal {json.dumps(str(path))}))' in policy
        # Standalone Python bundles Tcl directly in the already allowed lib/ tree.
        if path.parent != Path(sys.base_prefix).resolve() / "lib":
            assert f'(allow file-read* (subpath {json.dumps(str(path.parent))}))' not in policy
    for root in ("/opt/homebrew", str(Path.home()), str(Path(sys.base_prefix).resolve())):
        assert f'(allow file-read* (subpath {json.dumps(root)}))' not in policy
    assert policy.count("(allow process-exec ") == 1
    # These adjacent paths must remain inaccessible even while loader files
    # are readable. The sentinel is synthetic and never uses the live workspace.
    sentinel = tmp_path / "private-sentinel"
    sentinel.write_text("host-only")
    denied_paths = [str(sentinel), "/opt/homebrew/etc"]
    framework = runner._framework_library()
    if framework:
        denied_paths.append(str(framework.parent / "Resources/Info.plist"))
    candidate = package(tmp_path, f'''
from pathlib import Path
import pytest
def test_adjacent_paths():
    for path in {denied_paths!r}:
        with pytest.raises(PermissionError): Path(path).read_bytes()
''')
    result = runner.run_candidate_validation(candidate, smoke_test=False)
    assert result["behavioral"]["status"] == "passed", result["behavioral"]


@macos
def test_missing_framework_receipt_preserves_bounded_loader_diagnostic(tmp_path, monkeypatch):
    if not runner._framework_library():
        pytest.skip("framework runtime required")
    monkeypatch.setattr(runner, "_runtime_libraries", lambda python: ())
    result = runner.run_candidate_validation(package(tmp_path), smoke_test=True)
    behavior = result["behavioral"]
    assert behavior["status"] == "error"
    assert "no worker receipt" in behavior["diagnostic"]
    assert "Library not loaded" in behavior["diagnostic"]
    assert "blocked by sandbox" in behavior["diagnostic"]
    assert len(behavior["diagnostic"]) <= runner._TEXT_LIMIT
    assert behavior["summary"]["collected"] == 0
    assert not behavior["safety"]["os_isolated"]


@macos
def test_runtime_dependency_inspection_failure_never_launches_candidate(tmp_path, monkeypatch):
    def failed_inspection(*args, **kwargs):
        raise runner.subprocess.TimeoutExpired("otool", 5)
    monkeypatch.setattr(runner.subprocess, "run", failed_inspection)
    monkeypatch.setattr(runner, "sandbox_exec", lambda *a, **kw: pytest.fail("spawned"))
    result = runner.run_candidate_validation(package(tmp_path), smoke_test=True)
    assert result["behavioral"]["status"] == "error"
    assert "dependency inspection failed" in result["behavioral"]["diagnostic"]


@macos
def test_real_sdk_conftest_and_exact_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-m impossible -p nonexistent_plugin")
    monkeypatch.setenv("PYTEST_PLUGINS", "nonexistent_plugin")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "host_injection"))
    candidate = package(tmp_path, '''
from nerya.strategies import StrategyResult, ResultBuilder, StrategyAgentTask
def test_real_semantics(answer):
    assert answer == 42
    assert StrategyResult.hold(reason='hold').asdict()['status'] == 'hold'
    assert ResultBuilder().ok(signals=[1]).metadata == {'signals': [1]}
    assert StrategyAgentTask.skip('empty').reason == 'empty'
def test_wrong_value():
    assert 23/9 == 11/9
def test_wrong_cross():
    assert None == 'death'
''', extra={
        "conftest.py": "import pytest\n@pytest.fixture\ndef answer(): return 42\n",
        "pytest.ini": "[pytest]\naddopts = -m impossible -p nonexistent_plugin\n",
        "sitecustomize.py": "raise RuntimeError('candidate sitecustomize loaded')\n",
    })
    result = runner.run_candidate_validation(candidate, smoke_test=True)
    behavior = result["behavioral"]
    assert result["smoke_issues"] == [], result
    assert behavior["status"] == "failed", behavior
    assert behavior["exit_code"] == 1
    assert behavior["summary"]["failed"] == 2
    assert behavior["summary"]["passed"] == 1
    assert {f["nodeid"].split("::")[-1] for f in behavior["failures"]} == {"test_wrong_value", "test_wrong_cross"}
    assert "11/9" in behavior["stdout"] and "None" in behavior["stdout"]
    assert not list(candidate.root.rglob("__pycache__"))
    assert not list(candidate.root.rglob(".pytest_cache"))
    assert behavior["safety"]["os_isolated"]


@macos
def test_real_zoneinfo_in_smoke_and_tests_without_host_read_access(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONTZPATH", "/etc")
    candidate = package(tmp_path, '''
from datetime import datetime, timedelta
from pathlib import Path
import os
import pytest
import zoneinfo
from main import SHANGHAI
def test_timezone():
    assert SHANGHAI.key == 'Asia/Shanghai'
    assert datetime(2026, 10, 22, tzinfo=SHANGHAI).utcoffset() == timedelta(hours=8)
    assert datetime(2026, 1, 1, tzinfo=zoneinfo.ZoneInfo('UTC')).utcoffset() == timedelta(0)
    assert 'PYTHONTZPATH' not in os.environ
    assert all(Path(root).name == 'zoneinfo' for root in zoneinfo.TZPATH)
    for path in ['/etc/passwd', '/private/etc/passwd']:
        with pytest.raises(PermissionError): Path(path).read_bytes()
''', main="from zoneinfo import ZoneInfo\nSHANGHAI = ZoneInfo('Asia/Shanghai')\ndef run(ctx): return None\n")
    result = runner.run_candidate_validation(candidate, smoke_test=True)
    assert result["smoke_issues"] == [], result
    assert result["behavioral"]["status"] == "passed", result
    assert result["behavioral"]["summary"]["passed"] == 1
    assert result["behavioral"]["safety"]["timezone_roots"] == [str(p) for p in runner._timezone_roots()]


def test_timezone_roots_reject_broad_or_environment_supplied_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONTZPATH", str(tmp_path))
    monkeypatch.setattr(runner.sysconfig, "get_config_var", lambda key: f"/etc:/usr/share:{tmp_path}")
    assert runner._timezone_roots() == ()


@macos
def test_real_numpy_and_uname_without_broad_sysctl_access(tmp_path):
    candidate = package(tmp_path, '''
import ctypes
import errno
import os
import numpy as np
from main import HOST, VALUES
def test_numpy_and_uname():
    assert HOST == os.uname()
    assert HOST.sysname == 'Darwin' and HOST.machine
    np.testing.assert_array_equal(VALUES, [1., 2., 3.])
    assert float(VALUES.mean()) == 2.
    # Adjacent kernel metadata and process listings stay outside the allowlist.
    sysctl = ctypes.CDLL(None, use_errno=True).sysctlbyname
    sysctl.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    sysctl.restype = ctypes.c_int
    for name in (b'kern.boottime', b'kern.proc.all'):
        size = ctypes.c_size_t()
        ctypes.set_errno(0)
        assert sysctl(name, None, ctypes.byref(size), None, 0) == -1
        assert ctypes.get_errno() in (errno.EPERM, errno.EACCES)
''', main='''import os
import numpy as np
HOST = os.uname()
VALUES = np.array([1., 2., 3.])
def run(ctx): return None
''')
    result = runner.run_candidate_validation(candidate, smoke_test=True)
    assert result["smoke_issues"] == [], result
    behavior = result["behavioral"]
    assert behavior["status"] == "passed", result
    assert behavior["summary"]["passed"] == 1
    assert behavior["safety"]["os_isolated"]


@macos
def test_real_os_denies_host_access_network_and_processes(tmp_path, monkeypatch):
    secret = tmp_path / "host-secret"
    secret.write_text("must stay on host")
    destination = tmp_path / "host-write"
    monkeypatch.setenv("CANDIDATE_SECRET_TOKEN", "never-in-child")
    repo = Path(runner.__file__).resolve().parents[2]
    candidate = package(tmp_path, f'''
import os, socket, subprocess, sys, pathlib, resource
import pytest
def test_boundary(tmp_path):
    assert 'CANDIDATE_SECRET_TOKEN' not in os.environ
    assert 'NERYA_VAULT_PASSPHRASE' not in os.environ
    assert sys.flags.isolated and sys.dont_write_bytecode and sys.flags.no_site
    for path in [{str(secret)!r}, {str(repo / 'pyproject.toml')!r}, '/etc/passwd']:
        with pytest.raises(PermissionError): pathlib.Path(path).read_bytes()
    with pytest.raises(PermissionError): pathlib.Path({str(destination)!r}).write_text('bad')
    with pytest.raises(PermissionError): pathlib.Path('main.py').write_text('bad')
    with pytest.raises(PermissionError): pathlib.Path('tests/evil.pyc').write_bytes(b'bad')
    with pytest.raises(OSError): socket.create_connection(('127.0.0.1', 9), timeout=0.1)
    with pytest.raises(OSError): socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'bad', ('127.0.0.1', 9))
    with pytest.raises(OSError): subprocess.run(['/usr/bin/true'], check=True)
    with pytest.raises(OSError): os.posix_spawn('/usr/bin/true', ['true'], {{}})
    with pytest.raises(OSError): os.fork()
    with pytest.raises(OSError): os.execv('/usr/bin/true', ['true'])
    with pytest.raises(OSError): os.kill({os.getpid()}, 0)
    assert resource.getrlimit(resource.RLIMIT_FSIZE)[1] == {runner._FILE_BYTES}
    (tmp_path / 'allowed').write_text('scratch')
''')
    result = runner.run_candidate_validation(candidate, smoke_test=False)
    assert result["behavioral"]["status"] == "passed", result
    assert not destination.exists()
    assert secret.read_text() == "must stay on host"


@macos
@pytest.mark.parametrize("main,code", [("raise RuntimeError('smoke evidence')\n", "import_failed"),
                                       ("answer = 42\n", "entrypoint_attr_missing")])
def test_smoke_stays_in_child_and_preserves_codes(tmp_path, main, code):
    result = runner.run_candidate_validation(package(tmp_path, main=main), smoke_test=True, run_tests=False)
    assert result["smoke_issues"][0]["code"] == code, result
    assert result["behavioral"]["status"] == "not_run"


@macos
@pytest.mark.parametrize("test_code,status", [
    (None, "not_present"),
    ("import pytest\ndef test_skip(): pytest.skip('not proof')\n", "not_run"),
    ("import pytest\n@pytest.mark.xfail\ndef test_xfail(): assert False\n", "not_run"),
    ("raise RuntimeError('collection evidence')\n", "failed"),
])
def test_empty_skipped_and_collection_outcomes(tmp_path, test_code, status):
    result = runner.run_candidate_validation(package(tmp_path, test_code), smoke_test=False)
    assert result["behavioral"]["status"] == status, result


@macos
def test_printed_json_and_success_exit_cannot_forge_receipt(tmp_path):
    candidate = package(tmp_path, main="import os\nprint({'version':1,'status':'passed'}, flush=True)\nos._exit(0)\n")
    result = runner.run_candidate_validation(candidate, smoke_test=True)
    assert result["behavioral"]["status"] == "error", result
    assert result["smoke_issues"][0]["severity"] == "blocker"


@macos
def test_timeout_is_bounded_and_reaped(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_WALL_SECONDS", 2)
    candidate = package(tmp_path, main="while True: pass\n")
    result = runner.run_candidate_validation(candidate, smoke_test=True)
    behavior = result["behavioral"]
    assert behavior["status"] == "timeout", result
    assert behavior["elapsed_ms"] < 6000
    assert behavior["safety"]["process_exited"]
    assert behavior["safety"]["process_group_stopped"]


@macos
def test_output_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_OUTPUT_BYTES", 2048)
    candidate = package(tmp_path, main="print('x' * 50000)\ndef run(ctx): pass\n")
    behavior = runner.run_candidate_validation(candidate, smoke_test=True, run_tests=False)["behavioral"]
    assert behavior["status"] == "not_run", behavior
    assert behavior["output_truncated"]
    assert len(behavior["stdout"].encode()) <= 2048


@macos
@pytest.mark.parametrize("resource_kind", ["memory", "scratch"])
def test_sampled_resource_limits_stop_and_reap_child(tmp_path, monkeypatch, resource_kind):
    if resource_kind == "memory":
        monkeypatch.setattr(runner, "_MEMORY_BYTES", 64 * 1024 * 1024)
        main = "import time\nallocation = bytearray(96 * 1024 * 1024)\ntime.sleep(5)\n"
    else:
        monkeypatch.setattr(runner, "_SCRATCH_BYTES", 512 * 1024)
        main = "import os, pathlib, time\n(pathlib.Path(os.environ['TMPDIR']) / 'large').write_bytes(b'x' * 1048576)\ntime.sleep(5)\n"
    result = runner.run_candidate_validation(package(tmp_path, main=main), smoke_test=True)
    behavior = result["behavioral"]
    assert behavior["status"] == "error", result
    assert resource_kind in behavior["diagnostic"]
    assert behavior["safety"]["process_exited"]
    assert behavior["safety"]["process_group_stopped"]


@macos
def test_sandbox_startup_error_never_falls_back(tmp_path, monkeypatch):
    candidate = package(tmp_path)
    calls = []
    def failed_launch(*args, **kwargs):
        calls.append(args)
        raise OSError("deliberate sandbox startup failure")
    monkeypatch.setattr(runner, "sandbox_exec", failed_launch)
    result = runner.run_candidate_validation(candidate, smoke_test=True)
    assert result["behavioral"]["status"] == "error"
    assert len(calls) == 1
    assert result["smoke_issues"][0]["severity"] == "blocker"


@macos
@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "hardlink", "traversal", "entrypoint"])
def test_snapshot_rejects_escapes_before_spawn(tmp_path, monkeypatch, kind):
    candidate = package(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("raise AssertionError('executed outside')\n")
    if kind == "symlink":
        (candidate.root / "evil.py").symlink_to(outside)
        candidate.files += ("evil.py",)
    elif kind == "directory_symlink":
        (candidate.root / "evil").symlink_to(tmp_path, target_is_directory=True)
        candidate.files += ("evil/outside.py",)
    elif kind == "hardlink":
        os.link(outside, candidate.root / "evil.py")
        candidate.files += ("evil.py",)
    elif kind == "traversal":
        candidate.files += ("../outside.py",)
    else:
        object.__setattr__(candidate.manifest, "entrypoint", str(outside) + ":run")
    monkeypatch.setattr(runner, "sandbox_exec", lambda *a, **kw: pytest.fail("spawned"))
    result = runner.run_candidate_validation(candidate, smoke_test=True)
    assert result["behavioral"]["status"] == "error", result


def test_report_parser_is_bounded_and_rejects_symlinks(tmp_path):
    path = tmp_path / "result.json"
    path.write_text(json.dumps({"version": 1, "status": "passed"}))
    with pytest.raises(ValueError):
        runner._read_report(path)
    path.write_bytes(b' ' * (runner._REPORT_BYTES + 1))
    with pytest.raises(ValueError):
        runner._read_report(path)
    path.unlink()
    path.symlink_to(tmp_path / "other")
    with pytest.raises(OSError):
        runner._read_report(path)


@macos
def test_worktree_path_is_literal_and_grants_only_sdk_source(tmp_path, monkeypatch):
    import subprocess
    root = (tmp_path / 'checkout.[review]-v1').resolve()
    src = root / 'nerya'
    (src / 'strategies').mkdir(parents=True)
    monkeypatch.setattr(runner, '__file__', str(src / 'strategies/candidate_validation.py'))
    good, secret, sibling = src / '__init__.py', src / 'local.json', root / 'other.py'
    for file in (good, secret, sibling):
        file.write_text('sentinel', encoding='utf-8')
    python = Path(sys.executable).resolve()
    policy = tmp_path / 'rules.sb'
    policy.write_text(runner._profile(tmp_path.resolve(), python, []), encoding='utf-8')
    script = """import sys
assert open(sys.argv[1]).read() == 'sentinel'
for path in sys.argv[2:]:
    try: open(path).read()
    except PermissionError: pass
    else: raise AssertionError('out-of-scope read was allowed')
"""
    subprocess.run([str(runner._SANDBOX), '-f', str(policy), str(python), '-I', '-B', '-S',
                    '-c', script, str(good), str(secret), str(sibling)], check=True, timeout=15)

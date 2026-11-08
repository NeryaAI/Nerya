"""Bounded, fail-closed execution of untrusted strategy validation.

Seatbelt is the security boundary; core.sandbox only manages the process. The
parent copies bytes and validates JSON, and never imports candidate modules.
Only macOS is supported. In-process pytest receipts are diagnostic evidence,
not an attestation against deliberately dishonest tests/conftest hooks.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path

from ..core.sandbox import sandbox_exec
from .package import StrategyPackage

_WALL_SECONDS = 30
_CPU_SECONDS = 15
_MEMORY_BYTES = 768 * 1024 * 1024
_SCRATCH_BYTES = 32 * 1024 * 1024
_FILE_BYTES = 4 * 1024 * 1024
_MAX_FILES = 512
_SNAPSHOT_BYTES = 16 * 1024 * 1024
_OUTPUT_BYTES = 128 * 1024
_REPORT_BYTES = 256 * 1024
_MAX_ITEMS = 100
_TEXT_LIMIT = 8000
_SANDBOX = Path("/usr/bin/sandbox-exec")
_RUNNER = "macos-seatbelt-pytest"
_COUNTS = ("collected", "passed", "failed", "skipped", "errors", "xfailed", "xpassed", "deselected")


def _receipt() -> dict:
    return {
        "smoke_issues": [],
        "behavioral": {
            "status": "not_run", "runner": _RUNNER, "command": [],
            "exit_code": None, "stdout": "", "stderr": "",
            "summary": dict.fromkeys(_COUNTS, 0), "failures": [],
            "safety": {
                "backend": "macos-seatbelt", "os_isolated": False,
                "candidate_executed_in_parent": False,
                "network": "denied", "process_spawn": "denied",
                "environment": "allowlist", "candidate": "read-only copy",
                "writes": "scratch only (plus /dev/null)",
                "pytest_plugin_autoload": False, "pytest_config": "trusted empty config",
                "wall_seconds": _WALL_SECONDS, "cpu_seconds": _CPU_SECONDS,
                "memory_bytes": _MEMORY_BYTES, "scratch_bytes": _SCRATCH_BYTES,
                "file_size_hard_limit": _FILE_BYTES, "open_files_hard_limit": 128,
                "snapshot_files": _MAX_FILES, "snapshot_bytes": _SNAPSHOT_BYTES,
                "output_bytes_per_stream": _OUTPUT_BYTES, "report_bytes": _REPORT_BYTES,
                "limitations": [
                    "macOS only; no unsafe fallback",
                    "RSS and aggregate scratch limits are sampled every 50ms, not kernel quotas",
                    "Only the initial Python executable may be re-execed; fork/spawn are denied",
                    "Candidate conftest runs inside the sandbox and can influence pytest results",
                ],
            },
        },
    }


def _fail(result: dict, status: str, message: str, smoke: bool) -> dict:
    behavior = result["behavioral"]
    behavior["status"] = status
    behavior["diagnostic"] = message[:_TEXT_LIMIT]
    if smoke:
        result["smoke_issues"].append({
            "severity": "blocker", "code": f"candidate_validation_{status}",
            "message": message[:_TEXT_LIMIT], "where": "sandbox",
        })
    return result


def _snapshot(package: StrategyPackage, destination: Path) -> None:
    """Copy declared source with no symlink traversal, including during races."""
    entrypoint = Path(package.manifest.entrypoint_module)
    if entrypoint.is_absolute() or ".." in entrypoint.parts:
        raise ValueError("candidate entrypoint must stay inside the package")
    if len(package.files) > _MAX_FILES:
        raise ValueError("candidate exceeds snapshot file limit")
    destination.mkdir()
    total = 0
    root_fd = os.open(package.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in package.files:
            path = Path(name)
            if (not path.parts or path.is_absolute() or ".." in path.parts
                    or len(path.parts) > 16 or str(path) != name):
                raise ValueError(f"unsafe candidate path: {name!r}")
            directory = os.dup(root_fd)
            try:
                for part in path.parts[:-1]:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = next_fd
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError(f"candidate source must be a regular, unlinked file: {name}")
                    content = source.read(_FILE_BYTES + 1)
                total += len(content)
                if len(content) > _FILE_BYTES or total > _SNAPSHOT_BYTES:
                    raise ValueError("candidate exceeds snapshot byte limit")
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            finally:
                os.close(directory)
    finally:
        os.close(root_fd)


def _timezone_roots() -> tuple[Path, ...]:
    """Use compiled system TZPATH, never the parent's PYTHONTZPATH override."""
    trusted = {"/usr/share/zoneinfo", "/usr/lib/zoneinfo",
               "/usr/share/lib/zoneinfo", "/etc/zoneinfo"}
    roots = set()
    for value in (sysconfig.get_config_var("TZPATH") or "").split(os.pathsep):
        if value not in trusted:
            continue
        path = Path(value)
        resolved = path.resolve()
        # macOS updates the versioned database behind /usr/share/zoneinfo.
        # Never follow a replacement link to /etc, home, or another host tree.
        if not (resolved == path or resolved == Path("/private/etc/zoneinfo")
                or (value == "/usr/share/zoneinfo" and resolved.name == "zoneinfo"
                    and resolved.is_relative_to("/private/var/db/timezone/tz"))):
            continue
        if not resolved.is_dir():
            continue
        info = resolved.stat()
        if info.st_uid == 0 and not info.st_mode & 0o022:
            roots.add(resolved)
    return tuple(sorted(roots))


def _framework_library() -> Path | None:
    framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
    if not framework:
        return None
    prefix = Path(sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX") or "")
    library = Path(sysconfig.get_config_var("LDLIBRARY") or "")
    if not prefix.is_absolute() or library.is_absolute() or ".." in library.parts or library.name != framework:
        raise ValueError("invalid Python framework configuration")
    return (prefix / library).resolve(strict=True)


def _python_executable() -> Path:
    framework = _framework_library()
    if framework:
        # bin/python is a launcher that re-execs this binary. Start it directly
        # so Seatbelt still permits exactly one executable, with fork denied.
        return (framework.parent / "Resources/Python.app/Contents/MacOS/Python").resolve(strict=True)
    return Path(sys.executable).resolve(strict=True)


def _runtime_libraries(python: Path) -> tuple[Path, ...]:
    """Discover loader dependencies of the trusted interpreter, never candidate files.

    Framework Python lives beside lib/, not inside it. Native stdlib modules
    can also link to individually installed libraries (SQLite, OpenSSL, etc.).
    Grant only their exact files; unknown loader forms fail closed.
    """
    runtime = Path(sys.base_prefix).resolve()
    roots = {python.resolve(strict=True)}
    framework = _framework_library()
    if framework:
        roots.add(framework)
    extensions = Path(sysconfig.get_config_var("DESTSHARED") or "").resolve(strict=True)
    if not extensions.is_relative_to(runtime / "lib"):
        raise ValueError("native stdlib directory is outside the Python runtime")
    roots.update(path.resolve(strict=True) for path in extensions.glob("*.so"))
    pending, inspected, libraries = roots, set(), {framework} if framework else set()
    deadline = time.monotonic() + 5

    def inspect(path_args: list[str]) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Python loader dependency inspection timed out")
        try:
            output = subprocess.run(["/usr/bin/otool", *path_args],
                                    capture_output=True, text=True, check=True, timeout=remaining,
                                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout
        except subprocess.SubprocessError as exc:
            raise RuntimeError("Python loader dependency inspection failed") from exc
        if len(output) > 256 * 1024:
            raise ValueError("Python loader dependency output limit exceeded")
        return output

    def anchored(name: str, owner: Path) -> Path:
        if name.startswith("@loader_path/"):
            return owner.parent / name.removeprefix("@loader_path/")
        if name.startswith("@executable_path/"):
            return python.parent / name.removeprefix("@executable_path/")
        return Path(name)

    rpaths: dict[Path, list[Path]] = {}

    def search_paths(owner: Path) -> list[Path]:
        if owner not in rpaths:
            names = re.findall(r"\bcmd LC_RPATH\s+cmdsize \d+\s+path (.+?) \(offset \d+\)",
                               inspect(["-l", str(owner)]))
            rpaths[owner] = [path for name in names if (path := anchored(name, owner)).is_absolute()]
        return rpaths[owner]

    while pending:
        if len(inspected | pending) > 256:
            raise ValueError("Python loader dependency limit exceeded")
        batch = sorted(pending)
        if any(not path.is_file() for path in batch):
            raise ValueError("Python loader dependency is not a regular file")
        output = inspect(["-L", *map(str, batch)])
        inspected.update(batch)
        pending = set()
        owner = None
        for line in output.splitlines():
            if not line.startswith("\t"):
                owner = next((path for path in batch if line == f"{path}:"), None)
                continue
            if owner is None or " (compatibility version " not in line:
                raise ValueError("unrecognized Python loader dependency output")
            name = line.strip().split(" (compatibility version ", 1)[0]
            path = anchored(name, owner)
            if name.startswith("@rpath/"):
                suffix = name.removeprefix("@rpath/")
                path = next((root / suffix for root in search_paths(owner) + search_paths(python)
                             if (root / suffix).is_file()), path)
            if not path.is_absolute():
                raise ValueError("unresolved Python loader dependency")
            path = Path(os.path.normpath(path))
            # System libraries can exist only in dyld's shared cache. These
            # trees are already explicitly granted by the profile.
            if path.is_relative_to("/usr/lib") or path.is_relative_to("/System/Library"):
                continue
            resolved = path.resolve(strict=True)
            # dyld checks the install-name symlink as well as the Cellar file.
            libraries.update((path, path.parent.resolve(strict=True) / path.name, resolved))
            if resolved not in inspected:
                pending.add(resolved)
    return tuple(sorted(libraries))


def _profile(base: Path, python: Path, dependencies: list[str],
             timezone_roots: tuple[Path, ...] = ()) -> str:
    # Never allow the repository, home, /tmp, or the venv prefix wholesale.
    source = Path(__file__).resolve().parents[1]
    runtime = Path(sys.base_prefix).resolve() / "lib"
    read_trees = [base / "candidate", runtime, *(Path(p) for p in dependencies), *timezone_roots,
                  source / "workspace" / "_prompt_bundles" / "default",
                  Path("/usr/lib"), Path("/System/Library/Frameworks"),
                  Path("/System/Library/PrivateFrameworks"), Path("/System/Library/dyld")]
    literals = [python, *_runtime_libraries(python), base / "worker.py", base / "request.json", base / "pytest.ini",
                Path("/dev/null"), Path("/dev/urandom"), Path("/dev/random")]
    def q(path):
        return json.dumps(str(path))
    metadata = set()
    for path in [*read_trees, *literals, source]:
        metadata.update(path.parents)
    rules = [
        "(version 1)", "(deny default)",
        # dyld opens the root directory during launch; this literal does not
        # grant access to any child path.
        '(allow file-read-data (literal "/"))',
        f"(allow process-exec (literal {q(python)}))",
        "(deny process-fork)",
        '(allow sysctl-read (sysctl-name-regex #"^hw\\."))',
        '(allow sysctl-read (sysctl-name "kern.osrelease") (sysctl-name "kern.ostype") (sysctl-name "kern.osversion"))',
        # Darwin uname() also reads these two fields; NumPy calls it on import.
        # Keep the grant read-only and exact, without opening other kern.* data.
        '(allow sysctl-read (sysctl-name "kern.hostname") (sysctl-name "kern.version"))',
        f"(allow file-read* file-write* (subpath {q(base / 'scratch')}))",
        '(allow file-write-data (literal "/dev/null"))',
    ]
    rules.extend(f"(allow file-read* (subpath {q(p)}))" for p in read_trees)
    rules.extend(f"(allow file-read* (literal {q(p)}))" for p in literals)
    rules.extend(f"(allow file-read-metadata (literal {q(p)}))" for p in sorted(metadata))
    # The public SDK eagerly imports many Nerya modules. Grant source files,
    # never workspace/config/DB files adjacent to them.
    # Use a literal path boundary and a static extension predicate. Python's
    # re.escape + JSON quoting does not have Seatbelt regex semantics (notably
    # under .worktrees or a checkout name containing a hyphen).
    rules.append(f'(allow file-read* (require-all (subpath {q(source)}) (regex #"[.]py$")))')
    rules.append(f"(allow file-read-metadata (subpath {q(source)}))")
    rules.append(f"(allow file-read-data (require-all (subpath {q(source)}) (vnode-type DIRECTORY)))")
    return "\n".join(rules)


class _Usage(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_ubyte * 16)] + [
        (name, ctypes.c_uint64) for name in (
            "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups",
            "pageins", "wired_size", "resident_size", "phys_footprint",
            "proc_start_abstime", "proc_exit_abstime",
        )
    ]


def _memory_reader():
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    call = library.proc_pid_rusage
    call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_Usage)]
    call.restype = ctypes.c_int

    def resident(pid: int) -> int:
        usage = _Usage()
        if call(pid, 0, ctypes.byref(usage)) != 0:
            error = ctypes.get_errno()
            if error == 3:  # ESRCH: the owned child exited during this sample.
                raise ProcessLookupError(error, "proc_pid_rusage failed")
            raise OSError(error, "proc_pid_rusage failed")
        return max(usage.resident_size, usage.phys_footprint)

    resident(os.getpid())  # Fail before starting a candidate if unavailable.
    return resident


def _scratch_size(root: Path) -> tuple[int, int]:
    total = count = 0
    def visit(fd):
        nonlocal total, count
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_FILES or total > _SCRATCH_BYTES:
                    return
                try:
                    info = entry.stat(follow_symlinks=False)
                    total += info.st_size
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            visit(child)
                        finally:
                            os.close(child)
                except FileNotFoundError:
                    continue
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(directory)
    finally:
        os.close(directory)
    return total, count


def _read_report(path: Path) -> dict:
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("worker report is not a regular file")
        data = stream.read(_REPORT_BYTES + 1)
    if len(data) > _REPORT_BYTES:
        raise ValueError("worker report exceeds byte limit")
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("invalid worker report envelope")
    for key, fields in (("smoke_issues", ("severity", "code", "message", "where")),
                        ("failures", ("nodeid", "phase", "message"))):
        items = value.get(key)
        if not isinstance(items, list) or len(items) > _MAX_ITEMS:
            raise ValueError(f"invalid worker {key}")
        for item in items:
            if not isinstance(item, dict) or any(
                not isinstance(item.get(field), str) or len(item[field]) > _TEXT_LIMIT for field in fields
            ):
                raise ValueError(f"invalid worker {key} item")
        value[key] = [{field: item[field] for field in fields} for item in items]
    if any(i["severity"] not in {"blocker", "warning"} for i in value["smoke_issues"]):
        raise ValueError("invalid smoke severity")
    counts = value.get("summary")
    if not isinstance(counts, dict) or any(
        type(counts.get(key)) is not int or not 0 <= counts[key] <= 1_000_000 for key in _COUNTS
    ):
        raise ValueError("invalid worker summary")
    value["summary"] = {key: counts[key] for key in _COUNTS}
    if value.get("status") not in {"passed", "failed", "not_run", "not_present", "error"}:
        raise ValueError("invalid worker status")
    if type(value.get("exit_code")) is not int or value["exit_code"] not in range(6):
        raise ValueError("invalid worker exit code")
    if value["status"] == "passed" and (value["exit_code"] != 0 or counts["failed"]
            or counts["errors"] or not counts["collected"] or not counts["passed"]):
        raise ValueError("inconsistent passing worker report")
    if value["status"] == "not_present" and (value["exit_code"] != 5 or counts["collected"]):
        raise ValueError("inconsistent empty worker report")
    return value


def run_candidate_validation(package: StrategyPackage, *, smoke_test: bool, run_tests: bool = True) -> dict:
    result = _receipt()
    behavior = result["behavioral"]
    if not smoke_test and not run_tests:
        return result
    if sys.platform != "darwin" or not _SANDBOX.is_file():
        return _fail(result, "unavailable", "macOS sandbox-exec is required; candidate was not executed", smoke_test)
    try:
        resident = _memory_reader()
        with tempfile.TemporaryDirectory(prefix="nerya-candidate-") as temporary:
            base = Path(temporary).resolve()
            _snapshot(package, base / "candidate")
            scratch = base / "scratch"
            scratch.mkdir()
            (base / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            worker = Path(__file__).with_name("_candidate_validation_worker.py")
            (base / "worker.py").write_bytes(worker.read_bytes())
            dependencies = sorted({str(Path(sysconfig.get_path(k)).resolve()) for k in ("purelib", "platlib")})
            timezone_roots = _timezone_roots()
            request = {
                "base": str(base), "source": str(Path(__file__).resolve().parents[2]),
                "dependencies": dependencies, "smoke_test": smoke_test, "run_tests": run_tests,
                "timezone_roots": [str(path) for path in timezone_roots],
                "cpu_seconds": _CPU_SECONDS, "file_bytes": _FILE_BYTES,
                "max_items": _MAX_ITEMS, "text_limit": _TEXT_LIMIT, "report_bytes": _REPORT_BYTES,
            }
            (base / "request.json").write_text(json.dumps(request), encoding="utf-8")
            python = _python_executable()
            profile = base / "sandbox.sb"
            policy = _profile(base, python, dependencies, timezone_roots)
            profile.write_text(policy, encoding="utf-8")
            behavior["safety"]["profile_sha256"] = hashlib.sha256(policy.encode()).hexdigest()
            behavior["safety"]["timezone_roots"] = request["timezone_roots"]
            behavior["safety"]["read_scope"] = [
                "copied candidate", "Python runtime and explicit dependency directories",
                "system loader/frameworks", "nerya Python source and default prompt bundle",
                "trusted worker/config", "literal root directory for dyld (no descendant grant)",
            ]
            command = [str(_SANDBOX), "-f", str(profile), str(python), "-I", "-B", "-S",
                       str(base / "worker.py"), str(base / "request.json")]
            behavior["command"] = command
            env = {
                "HOME": str(scratch), "TMPDIR": str(scratch), "PATH": "/usr/bin:/bin",
                "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
                "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1",
            }
            behavior["safety"]["environment_keys"] = sorted(env)
            process = sandbox_exec(command, cwd=base / "candidate", root=base, env=env,
                                   timeout=_WALL_SECONDS, output_limit=_OUTPUT_BYTES, background=True).process
            if process is None:
                raise RuntimeError("sandbox process manager returned no handle")
            limit = None
            peak = 0
            try:
                while process.proc.poll() is None:
                    try:
                        peak = max(peak, resident(process.proc.pid))
                    except ProcessLookupError:
                        # The child can exit before Popen.poll() observes it.
                        # process.wait() below reaps it and supplies the exit code.
                        break
                    except OSError:
                        if process.proc.poll() is None:
                            raise
                    size, count = _scratch_size(scratch)
                    if peak > _MEMORY_BYTES:
                        limit = "candidate exceeded sampled memory limit"
                    elif size > _SCRATCH_BYTES or count > _MAX_FILES:
                        limit = "candidate exceeded sampled scratch limit"
                    if limit:
                        process.stop()
                        break
                    time.sleep(0.05)
            finally:
                if sys.exc_info()[0] is not None:
                    process.stop()
                execution = process.wait()
            behavior.update(exit_code=execution.returncode, stdout=execution.stdout, stderr=execution.stderr,
                            elapsed_ms=execution.elapsed_ms, output_truncated=execution.truncated)
            behavior["safety"].update(peak_memory_bytes=peak, process_exited=execution.process_exited,
                                       process_group_stopped=execution.process_group_stopped)
            if execution.timed_out:
                return _fail(result, "timeout", "candidate validation exceeded wall-clock timeout", smoke_test)
            if limit:
                return _fail(result, "error", limit, smoke_test)
            try:
                report = _read_report(scratch / "result.json")
            except FileNotFoundError:
                detail = execution.stderr.strip()[:_TEXT_LIMIT // 2]
                return _fail(result, "error",
                             f"candidate validation produced no worker receipt (exit {execution.returncode}); "
                             f"stderr: {detail or '(empty)'}", smoke_test)
            if report["exit_code"] != execution.returncode:
                raise ValueError("worker receipt does not match process exit code")
            if not smoke_test and report["smoke_issues"]:
                raise ValueError("unexpected smoke results")
            if not run_tests and report["status"] != "not_run":
                raise ValueError("unexpected behavioral execution")
            result["smoke_issues"] = report["smoke_issues"]
            behavior.update({key: report[key] for key in ("status", "summary", "failures")})
            behavior["safety"]["os_isolated"] = True
            return result
    except (OSError, ValueError, RuntimeError, RecursionError) as exc:
        return _fail(result, "error", f"candidate validation failed closed: {type(exc).__name__}: {exc}", smoke_test)

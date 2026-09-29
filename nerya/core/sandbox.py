"""Process execution boundary for shell-class tools.

The current desktop harness cannot rely on a platform sandbox everywhere, so
this wrapper enforces the invariant Nerya can prove locally: commands execute
from an explicit workspace cwd, with timeouts and captured output under one
auditable chokepoint. OS-specific hardening can be added here without changing
tool handlers.
"""

from __future__ import annotations

import os
import locale
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from ..harness.cancellation import CancelledError, is_cancelled
from .process import prepare_process_env, resolve_process_args, terminate_windows_tree


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
    cancelled: bool = False
    timed_out: bool = False
    process_exited: bool = False
    process_group_stopped: bool = False
    truncated: bool = False
    process: ManagedProcess | None = None


class ManagedProcess:
    """Own the child, drain both pipes, and reap it before confirming exit."""

    def __init__(self, proc, *, args, cwd, timeout, cancel_token, output_limit):
        self.proc, self.args, self.cwd = proc, args, cwd
        self.started = time.monotonic()
        self.timeout, self.cancel_token = timeout, cancel_token
        self.output_limit = output_limit
        self._output = {"stdout": b"", "stderr": b""}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._done = threading.Event()
        self.cancelled = self.timed_out = self.truncated = False
        self.group_stopped = False
        self.error = None
        drain = self._drain_windows if os.name == "nt" else self._drain
        self._thread = threading.Thread(target=drain, name=f"shell-{proc.pid}", daemon=False)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _group_alive(self):
        if os.name != "posix":
            return self.proc.poll() is None
        try:
            os.killpg(self.proc.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # An inaccessible group is not proof that the group has stopped.
            # In particular, do not turn a post-exit probe race into a crashed
            # reader thread or a false stop confirmation.
            return True

    def _signal(self, sig):
        try:
            if os.name == "posix":
                os.killpg(self.proc.pid, sig)
            elif sig == signal.SIGTERM:
                self.proc.terminate()
            else:
                self.proc.kill()
        except ProcessLookupError:
            pass
        except PermissionError:
            if self.proc.poll() is None:
                raise
            # The owned parent is already gone. Do not signal a potentially
            # reused/inaccessible process group; keep group_stopped false.

    def _append_output(self, name, chunk):
        with self._lock:
            value = self._output[name] + chunk
            if self.output_limit is not None and len(value) > self.output_limit:
                self.truncated = True
                value = value[-self.output_limit:]
            self._output[name] = value

    def _drain_windows(self):
        """Windows selectors accept sockets, not anonymous subprocess pipes.

        Drain each pipe independently while the supervisor enforces the same
        timeout/cancellation contract as POSIX. Readers own their pipes: closing
        a BufferedReader from another thread can deadlock on a blocked read.
        """
        readers = []

        def read_pipe(name, pipe):
            try:
                while chunk := os.read(pipe.fileno(), 65536):
                    self._append_output(name, chunk)
            except (OSError, ValueError):
                pass  # teardown can close the underlying Windows handle
            finally:
                pipe.close()

        try:
            for name in ("stdout", "stderr"):
                pipe = getattr(self.proc, name)
                if pipe is not None:
                    reader = threading.Thread(target=read_pipe, args=(name, pipe),
                                              name=f"shell-{self.proc.pid}-{name}", daemon=True)
                    reader.start()
                    readers.append(reader)
            while self.proc.poll() is None:
                self.cancelled = self._stop.is_set() or is_cancelled(self.cancel_token)
                self.timed_out = (self.timeout is not None
                                  and time.monotonic() - self.started >= self.timeout)
                if self.cancelled or self.timed_out:
                    self.group_stopped = terminate_windows_tree(self.proc)
                    break
                self._stop.wait(0.02)
            self.proc.wait()
            # An independently detached descendant may retain a pipe. Do not
            # hang the tool (or claim its whole tree stopped) in that case.
            deadline = time.monotonic() + 2.0
            for reader in readers:
                reader.join(max(0.0, deadline - time.monotonic()))
        except BaseException as exc:
            self.error = exc
            terminate_windows_tree(self.proc)
            self.proc.wait()
        finally:
            self._done.set()

    def _drain(self):
        stopping_at = None
        try:
            with selectors.DefaultSelector() as selector:
                for name in ("stdout", "stderr"):
                    pipe = getattr(self.proc, name)
                    if pipe is not None:
                        os.set_blocking(pipe.fileno(), False)
                        selector.register(pipe, selectors.EVENT_READ, name)
                while True:
                    now = time.monotonic()
                    cancelled = self._stop.is_set() or is_cancelled(self.cancel_token)
                    timed_out = self.timeout is not None and now - self.started >= self.timeout
                    exited = self.proc.poll() is not None
                    if stopping_at is None and (cancelled or timed_out or exited):
                        self.cancelled, self.timed_out = cancelled, timed_out
                        stopping_at = now
                        if self._group_alive():
                            self._signal(signal.SIGTERM)
                    if stopping_at is not None and now - stopping_at >= 0.3:
                        self._signal(signal.SIGKILL)
                    for key, _ in selector.select(0.05):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        self._append_output(key.data, chunk)
                    if self.proc.poll() is not None and not selector.get_map():
                        break
                    # Escaped descendants may retain a pipe. Do not equate
                    # their inherited fd with ownership of their new session.
                    if stopping_at is not None and now - stopping_at > 2:
                        self._signal(signal.SIGKILL)
                        break
            self.proc.wait()
            if self._group_alive():
                self._signal(signal.SIGKILL)
                until = time.monotonic() + 0.3
                while self._group_alive() and time.monotonic() < until:
                    time.sleep(0.02)
            self.group_stopped = os.name == "posix" and not self._group_alive()
        except BaseException as exc:
            self.error = exc
            self._signal(signal.SIGKILL)
            self.proc.wait()
        finally:
            for pipe in (self.proc.stdout, self.proc.stderr):
                if pipe is not None:
                    pipe.close()
            self._done.set()

    def snapshot(self):
        with self._lock:
            return SandboxExecResult(
                args=self.args, cwd=self.cwd, pid=self.proc.pid,
                returncode=self.proc.poll(),
                stdout=_text(self._output["stdout"]), stderr=_text(self._output["stderr"]),
                elapsed_ms=int((time.monotonic() - self.started) * 1000),
                cancelled=self.cancelled, timed_out=self.timed_out,
                process_exited=self._done.is_set() and self.proc.poll() is not None,
                process_group_stopped=self.group_stopped, truncated=self.truncated,
            )

    def wait(self):
        self._thread.join()
        if self.error is not None:
            raise self.error
        return self.snapshot()


def _resolve_cwd(cwd: str | Path, root: str | Path | None) -> Path:
    cwd_path = Path(cwd).expanduser()
    if root is None:
        return cwd_path.resolve()
    root_path = Path(root).expanduser().resolve()
    if not cwd_path.is_absolute():
        cwd_path = root_path / cwd_path
    cwd_path = cwd_path.resolve()
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
        if os.name == "nt":
            try:
                return value.decode("utf-8-sig")
            except UnicodeDecodeError:
                # Python children use UTF-8; legacy native Windows programs
                # can still emit the machine's code page.
                return value.decode(locale.getpreferredencoding(False), errors="replace")
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
    cancel_token: object | None = None,
    output_limit: int | None = None,
) -> SandboxExecResult:
    """Run a process from the workspace sandbox chokepoint.

    Background callers receive a managed handle; its pipes are drained from
    launch. Foreground cancellation waits for process termination.
    """

    cwd_path = _resolve_cwd(cwd, root)
    started = time.monotonic()
    if is_cancelled(cancel_token):
        raise CancelledError("cancelled before process launch")
    process_env = prepare_process_env(env)
    launch_args = resolve_process_args(args, env=process_env, cwd=cwd_path, shell=shell)
    proc = subprocess.Popen(
        launch_args, shell=shell, cwd=str(cwd_path),
        env=process_env,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=False, start_new_session=(os.name == "posix"),
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        if os.name == "nt" else 0,
    )
    managed = ManagedProcess(proc, args=args, cwd=cwd_path, timeout=timeout,
                             cancel_token=cancel_token, output_limit=output_limit)
    if background:
        return SandboxExecResult(
            args=args,
            returncode=None,
            stdout="",
            stderr="",
            cwd=cwd_path,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            pid=proc.pid,
            background=True,
            process=managed,
        )
    result = managed.wait()
    if result.cancelled:
        exc = CancelledError("process cancelled")
        exc.result = result
        raise exc
    if result.timed_out:
        exc = subprocess.TimeoutExpired(args, timeout, output=result.stdout, stderr=result.stderr)
        exc.result = result
        raise exc
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
    return result


__all__ = ["SandboxExecResult", "SandboxViolation", "sandbox_exec"]

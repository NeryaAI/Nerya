"""Persistent Seatbelt strategy worker with a bounded, host-owned SDK bridge.

The host supplies only public, current-prefix snapshots. dispatch(method, args,
kwargs) must target the current historical MockCtx/tape, never a live SDK. It
runs synchronously and must itself be bounded/cancellation-aware: the watchdog
can terminate the worker, but cannot safely interrupt arbitrary host Python.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import uuid

from nerya.harness.cancellation import CancelledError, is_cancelled
from nerya.strategies import candidate_validation as boundary
from nerya.strategies.package import _hash_files, load_package_from_dir


ALLOWED_METHODS = frozenset({
    "state.get", "state.set", "state.delete", "state.compare_and_set",
    "market.candles", "market.get_candles", "market.ohlcv", "market.get_ohlcv", "market.klines",
    "market.features", "market.ticker", "market.get_ticker", "market.mark_price",
    "portfolio.position", "portfolio.positions", "portfolio.summary",
    "portfolio.equity_usd", "portfolio.cash_usd", "portfolio.nav_usd", "state.update",
    "trading.open_position", "trading.close_position", "trading.reduce_position",
    "trading.execution_status", "trading.signal", "subagents.run", "audit.log", "audit.record",
})
_SNAPSHOT_FIELDS = frozenset({"strategy_id", "run_id", "session_id", "config", "clockts", "clock",
    "trigger", "policy", "mode", "runmode", "symbol", "timeframe", "market_name", "current_bar", "current_prefix"})
_PRIVATE_DATA = frozenset({"workspace", "workspace_root", "strategy_root", "credentials", "password",
    "api_key", "private_key", "secret", "secrets", "token", "full_tape", "tape", "future_candles"})


class IsolatedStrategyError(RuntimeError):
    """The worker/bridge failed closed; the replay must not continue."""


def _json_bytes(value, limit):
    # Strict JSON only. Never execute a host object's asdict/default hook.
    def check(item, depth=0):
        if depth > 32:
            raise IsolatedStrategyError("JSON nesting limit exceeded")
        if type(item) in (str, int, bool, type(None)):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        raise IsolatedStrategyError("bridge requires finite JSON values")
    check(value)
    raw = json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    if len(raw) > limit:
        raise IsolatedStrategyError("JSON message byte limit exceeded")
    return raw


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise IsolatedStrategyError("duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IsolatedStrategyError("invalid worker JSON") from exc
    if type(value) is not dict:
        raise IsolatedStrategyError("worker frame must be an object")
    return value


class IsolatedStrategy:
    """Load one candidate once; run(snapshot, dispatch) executes one bar.

    callback is an optional public function name in the manifest entrypoint
    module, not a host function. All returned data is JSON, never executable.
    Any exception closes the worker permanently. No unsandboxed fallback.

    frozen_files is a host-captured source mapping, copied at construction.
    When supplied, no candidate bytes or entrypoint metadata are read from the
    original package path. safety['source_revision'] binds the full mapping;
    only Python source and strategy.yml are exposed to the worker.
    """
    def __init__(self, package, callback=None, *, frozen_files: dict[str, str] | None = None,
                 cancel_token=None, request_timeout=5.0,
                 lifecycle_timeout=300.0, max_calls=128, max_total_calls=100_000,
                 max_message_bytes=256 * 1024, max_total_bytes=64 * 1024 * 1024):
        for name, value, ceiling in (("request_timeout", request_timeout, 5.0),
                                     ("lifecycle_timeout", lifecycle_timeout, 1800.0)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= ceiling:
                raise ValueError(f"{name} must be positive and <= {ceiling}")
        for name, value, ceiling in (("max_calls", max_calls, 256), ("max_total_calls", max_total_calls, 100_000),
                                     ("max_message_bytes", max_message_bytes, 1024 * 1024),
                                     ("max_total_bytes", max_total_bytes, 128 * 1024 * 1024)):
            if type(value) is not int or not 0 < value <= ceiling:
                raise ValueError(f"{name} must be a positive integer <= {ceiling}")
        self._callback_override = callback
        callback = callback or package.manifest.entrypoint_func
        if not isinstance(callback, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", callback):
            raise ValueError("callback must be a public entrypoint function name")
        self.package, self.callback, self.cancel_token = package, callback, cancel_token
        self.request_timeout, self.lifecycle_timeout = request_timeout, lifecycle_timeout
        self.max_calls, self.max_total_calls = max_calls, max_total_calls
        self.max_message_bytes, self.max_total_bytes = max_message_bytes, max_total_bytes
        self.total_calls = self.total_bytes = 0
        self.process = self.base = self._temporary = self._selector = self._watcher = None
        self._buffer, self._stderr = bytearray(), bytearray()
        self._done, self._lock = threading.Event(), threading.Lock()
        self._signal_lock = threading.Lock()
        self._failure = None
        self._started = self._deadline = 0.0
        self.closed = False
        self.safety = {"backend": "macos-seatbelt", "network": "denied", "process_spawn": "denied",
                       "candidate": "read-only source snapshot", "writes": "scratch only",
                       "process_exited": False, "process_group_stopped": False}
        self._frozen_files = None
        if frozen_files is not None:
            if type(frozen_files) is not dict or not frozen_files or len(frozen_files) > boundary._MAX_FILES:
                raise IsolatedStrategyError("invalid frozen source mapping/file limit")
            captured = dict(frozen_files)
            total = 0
            for name, text in captured.items():
                if type(name) is not str or type(text) is not str:
                    raise IsolatedStrategyError("frozen source requires string paths and text")
                path = Path(name)
                if (not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name
                        or len(path.parts) > 16 or str(path) != name or "\0" in name):
                    raise IsolatedStrategyError("unsafe frozen source path")
                size = len(text.encode("utf-8"))
                total += size
                if size > boundary._FILE_BYTES or total > boundary._SNAPSHOT_BYTES:
                    raise IsolatedStrategyError("frozen source byte limit exceeded")
            if "strategy.yml" not in captured:
                raise IsolatedStrategyError("frozen source requires strategy.yml")
            from nerya.strategies.verification import source_revision
            self.safety["source_revision"] = source_revision(captured)
            # Tuple + immutable strings prevent later caller mapping changes.
            self._frozen_files = tuple(sorted(captured.items()))

    def __enter__(self):
        if self.closed or self.process:
            raise IsolatedStrategyError("runner cannot be reopened")
        try:
            if sys.platform != "darwin" or not boundary._SANDBOX.is_file():
                raise IsolatedStrategyError("macOS Seatbelt is required; no fallback")
            self._check()
            resident = boundary._memory_reader()
            self._temporary = tempfile.TemporaryDirectory(prefix="nerya-isolated-strategy-")
            self.base = Path(self._temporary.name).resolve()
            candidate = self.base / "candidate"
            entrypoint = self.package.manifest.entrypoint_module
            if self._frozen_files is None:
                # Legacy callers retain bounded, no-follow filesystem copying.
                files = tuple(name for name in self.package.files if name.endswith(".py") or name == "strategy.yml")
                boundary._snapshot(replace(self.package, files=files), candidate)
            else:
                from nerya.strategies.verification import source_revision
                captured = dict(self._frozen_files)
                files = tuple(name for name in captured if name.endswith(".py") or name == "strategy.yml")
                candidate.mkdir()
                for name in files:
                    target = candidate / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(captured[name].encode("utf-8"))
                # Compare the actual materialized bytes, including manifest,
                # before importing any candidate or permitting host callbacks.
                materialized = {**captured, **{name: (candidate / name).read_bytes().decode("utf-8") for name in files}}
                if source_revision(materialized) != self.safety["source_revision"]:
                    raise IsolatedStrategyError("frozen source snapshot revision mismatch")
                frozen_package = load_package_from_dir(candidate)
                entrypoint = frozen_package.manifest.entrypoint_module
                if frozen_package.strategy_id != self.package.strategy_id or entrypoint not in files or not entrypoint.endswith(".py"):
                    raise IsolatedStrategyError("frozen source manifest/entrypoint mismatch")
                self.callback = self._callback_override or frozen_package.manifest.entrypoint_func
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", self.callback):
                    raise IsolatedStrategyError("frozen callback must be a public function name")
            self.safety["snapshot_sha256"] = _hash_files(self.base / "candidate", files)
            (self.base / "scratch").mkdir()
            worker = Path(__file__).with_name("isolated_strategy_worker.py")
            (self.base / "worker.py").write_bytes(worker.read_bytes())
            dependencies = sorted({str(Path(sysconfig.get_path(key)).resolve()) for key in ("purelib", "platlib")})
            timezone_roots = boundary._timezone_roots()
            request = {"base": str(self.base), "source": str(Path(boundary.__file__).resolve().parents[2]),
                       "dependencies": dependencies, "timezone_roots": list(map(str, timezone_roots)),
                       "entrypoint": entrypoint, "callback": self.callback,
                       "methods": sorted(ALLOWED_METHODS), "max_message_bytes": self.max_message_bytes,
                       "cpu_seconds": max(1, math.ceil(self.lifecycle_timeout)), "file_bytes": boundary._FILE_BYTES}
            (self.base / "request.json").write_bytes(_json_bytes(request, self.max_message_bytes))
            python = boundary._python_executable()
            policy = boundary._profile(self.base, python, dependencies, timezone_roots)
            (self.base / "sandbox.sb").write_text(policy)
            self.safety["profile_sha256"] = hashlib.sha256(policy.encode()).hexdigest()
            env = {"HOME": str(self.base / "scratch"), "TMPDIR": str(self.base / "scratch"), "PATH": "/usr/bin:/bin",
                   "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "OPENBLAS_NUM_THREADS": "1",
                   "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"}
            # Interactive pipes cannot use sandbox_exec's output-draining owner.
            # This class owns the child/session, pipe limits and reaping instead.
            self.process = subprocess.Popen([str(boundary._SANDBOX), "-f", str(self.base / "sandbox.sb"),
                str(python), "-I", "-B", "-S", str(self.base / "worker.py"), str(self.base / "request.json")],
                cwd=self.base / "candidate", env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
            self._selector = selectors.DefaultSelector()
            for name in ("stdout", "stderr"):
                pipe = getattr(self.process, name)
                os.set_blocking(pipe.fileno(), False)
                self._selector.register(pipe, selectors.EVENT_READ, name)
            os.set_blocking(self.process.stdin.fileno(), False)
            self._started = time.monotonic()
            self._deadline = self._started + 5.0
            self._watcher = threading.Thread(target=self._watch, args=(resident,), daemon=True)
            self._watcher.start()
            if self._receive() != {"type": "ready"}:
                raise IsolatedStrategyError("unexpected worker startup frame")
            self._deadline = 0.0
            return self
        except BaseException:
            self.close()
            raise

    def _kill(self):
        with self._signal_lock:
            if self.process is not None and self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Darwin can report EPERM while a signalled child exits.
                    # Reap that owned child; never target a reused group.
                    self.process.wait(timeout=.2)

    def _watch(self, resident):
        while not self._done.wait(0.025):
            try:
                self._check()
                if self.process.poll() is not None:
                    return
                try:
                    memory = resident(self.process.pid)
                except OSError:
                    if self.process.poll() is not None:
                        return
                    raise
                size, count = boundary._scratch_size(self.base / "scratch")
                if memory > boundary._MEMORY_BYTES or size > boundary._SCRATCH_BYTES or count > boundary._MAX_FILES:
                    raise IsolatedStrategyError("worker memory/scratch limit exceeded")
            except BaseException as exc:
                self._failure = exc
                self._kill()
                return

    def _check(self):
        if self._failure:
            raise self._failure
        if is_cancelled(self.cancel_token):
            raise CancelledError("isolated strategy cancelled")
        now = time.monotonic()
        if self._started and now - self._started >= self.lifecycle_timeout:
            raise IsolatedStrategyError("worker lifecycle timeout")
        if self._deadline and now >= self._deadline:
            raise IsolatedStrategyError("worker bar timeout")

    def _account(self, size):
        self.total_bytes += size
        if self.total_bytes > self.max_total_bytes:
            raise IsolatedStrategyError("worker total byte limit exceeded")

    def _pump(self):
        self._check()
        for key, _ in self._selector.select(0.025):
            chunk = os.read(key.fd, min(65536, self.max_message_bytes + 1))
            if not chunk:
                self._selector.unregister(key.fileobj)
                continue
            self._account(len(chunk))
            target = self._buffer if key.data == "stdout" else self._stderr
            target.extend(chunk)
            if key.data == "stderr":
                if len(target) > min(self.max_message_bytes, boundary._OUTPUT_BYTES):
                    raise IsolatedStrategyError("worker diagnostic byte limit exceeded")
            elif len(target) > self.max_message_bytes:
                raise IsolatedStrategyError("worker frame byte limit exceeded")
        self._check()

    def _receive(self):
        while b"\n" not in self._buffer:
            self._pump()
            if self.process.poll() is not None and b"\n" not in self._buffer:
                detail = self._stderr.decode("utf-8", errors="replace")[-2000:]
                raise IsolatedStrategyError(f"worker exited before result ({self.process.returncode}): {detail}")
        raw, _, rest = self._buffer.partition(b"\n")
        self._buffer[:] = rest
        value = _decode(raw)
        _json_bytes(value, self.max_message_bytes)
        return value

    def _send(self, value):
        raw = _json_bytes(value, self.max_message_bytes)
        self._account(len(raw))
        view = memoryview(raw)
        while view:
            self._check()
            try:
                size = os.write(self.process.stdin.fileno(), view)
                view = view[size:]
            except BlockingIOError:
                self._pump()
            except BrokenPipeError as exc:
                raise IsolatedStrategyError("worker input pipe closed") from exc

    def run(self, snapshot, dispatch):
        if self.closed or self.process is None:
            raise IsolatedStrategyError("use runner as a context manager")
        if not self._lock.acquire(blocking=False):
            raise IsolatedStrategyError("concurrent bar calls are not supported")
        try:
            self._check()
            if not callable(dispatch) or type(snapshot) is not dict or set(snapshot) - _SNAPSHOT_FIELDS:
                raise IsolatedStrategyError("invalid public replay snapshot/dispatcher")
            if snapshot.get("mode", "backtest") != "backtest":
                raise IsolatedStrategyError("only historical backtest snapshots are allowed")
            def public(value):
                if isinstance(value, dict):
                    if any(key.lower() in _PRIVATE_DATA for key in value):
                        raise IsolatedStrategyError("private/tape data is not a public snapshot")
                    for child in value.values():
                        public(child)
                elif isinstance(value, list):
                    for child in value:
                        public(child)
            _json_bytes(snapshot, self.max_message_bytes)
            public(snapshot)
            self._deadline = time.monotonic() + self.request_timeout
            frame = uuid.uuid4().hex
            self._send({"type": "run", "frame": frame, "snapshot": snapshot})
            calls = 0
            while True:
                row = self._receive()
                if row.get("frame") != frame:
                    raise IsolatedStrategyError("worker frame mismatch")
                if row.get("type") == "result" and set(row) == {"type", "frame", "value"}:
                    value = row["value"]
                    if value is not None and type(value) is not dict:
                        raise IsolatedStrategyError("strategy result must be an object or null")
                    self._deadline = 0.0
                    return value
                if row.get("type") == "error" and set(row) == {"type", "frame", "message"}:
                    raise IsolatedStrategyError("candidate failed: " + str(row["message"])[:2000])
                if set(row) != {"type", "frame", "method", "args", "kwargs"} or row.get("type") != "rpc":
                    raise IsolatedStrategyError("invalid worker RPC frame")
                method, args, kwargs = row["method"], row["args"], row["kwargs"]
                if (not isinstance(method, str) or method not in ALLOWED_METHODS or type(args) is not list
                        or type(kwargs) is not dict or any(not key.isidentifier() or key.startswith("_") for key in kwargs)):
                    raise IsolatedStrategyError("worker RPC method/arguments denied")
                if method in {"state.set", "state.delete", "state.compare_and_set", "state.update"}:
                    keys = kwargs if method == "state.update" else [args[0] if args else kwargs.get("key")]
                    if method == "state.update" and args:
                        raise IsolatedStrategyError("state.update requires keyword keys")
                    if any(not isinstance(key, str) or key.startswith(("position:", "__")) for key in keys):
                        raise IsolatedStrategyError("historical accounting state is read-only")
                calls += 1
                self.total_calls += 1
                if calls > self.max_calls or self.total_calls > self.max_total_calls:
                    raise IsolatedStrategyError("worker RPC call limit exceeded")
                self._check()
                value = dispatch(method, args, kwargs)
                self._check()
                self._send({"type": "reply", "frame": frame, "value": value})
        except BaseException:
            self.close()
            raise
        finally:
            self._lock.release()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._done.set()
        self._kill()
        if self.process:
            self.process.wait(timeout=2)
            self.safety["process_exited"] = self.process.poll() is not None
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                self.safety["process_group_stopped"] = True
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                pipe.close()
        if self._watcher and self._watcher is not threading.current_thread():
            self._watcher.join(timeout=1)
        if self._selector:
            self._selector.close()
        if self._temporary:
            self._temporary.cleanup()

    def __exit__(self, *_):
        self.close()

"""Bounded worker lifecycle for native OHLCV replay; not an Agent Loop."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import fields
from pathlib import Path

from .engine import BacktestResult
from .run_state import atomic_json


_RUNTIME_VERSION_CHANGED_MESSAGE = (
    "runtime version changed since the API started; restart the service "
    "before replaying, rather than mixing SDK versions"
)


class ReplayWorkerError(RuntimeError):
    def __init__(self, error):
        self.receipt = error
        self.surface = error.get("surface")
        self.module = error.get("module")
        self.market = error.get("market")
        self.bar_ts = error.get("bar_ts")
        message = str(error.get("message", "replay worker failed"))
        self.reason = (
            "runtime_version_changed"
            if message == _RUNTIME_VERSION_CHANGED_MESSAGE
            else "backtest_sdk_unsupported" if self.surface else "backtest_execution_failed"
        )
        super().__init__(f"{error.get('type', 'ReplayWorkerError')}: {message}")


def _stop(process):
    if process.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)


def execute_replay(strategy_root, cfg, *, candles_by_market,
                   timeframe_candles_by_market, artefacts_dir,
                   strategy_config, check_cancel=None, progress=None):
    root = Path(artefacts_dir)
    request_path = root / "replay_input.json"
    from .....core.runtime_identity import BUILD_ID, SDK_BUILD_ID
    # A run retains its exact immutable input rows in addition to reusable data.
    # Updating the shared store later cannot change this historical run.
    atomic_json(request_path, {"version": 1, "strategy_root": str(strategy_root), "runtime_build_id": BUILD_ID, "sdk_build_id": SDK_BUILD_ID,
        "config": cfg.asdict(), "strategy_config": strategy_config,
        "candles_by_market": candles_by_market,
        "timeframe_candles_by_market": timeframe_candles_by_market})
    environment = dict(os.environ)
    # A fresh worker must resolve the same checked-out runtime, independent of
    # the service's cwd or the strategy snapshot directory.
    runtime_root = Path(__file__).resolve().parents[5]
    environment["PYTHONPATH"] = os.pathsep.join([str(runtime_root), environment.get("PYTHONPATH", "")])
    deadline = time.monotonic() + cfg.max_run_seconds
    if check_cancel:
        check_cancel()
    with (root / "worker.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m",
            "nerya.skills.builtin.backtest.scripts.replay_worker", str(request_path)],
            cwd=runtime_root, env=environment, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
        try:
            while process.poll() is None:
                if check_cancel:
                    check_cancel()
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"isolated replay exceeded max_run_seconds={cfg.max_run_seconds}; worker stopped, inputs retained")
                if progress:
                    try:
                        progress(json.loads((root / "worker_progress.json").read_text()))
                    except (OSError, ValueError):
                        pass
                time.sleep(0.1)
        finally:
            _stop(process)
    path = root / "worker_result.json"
    if not path.is_file():
        raise ReplayWorkerError({"type": "worker_crashed", "message": f"worker exited {process.returncode}; inspect worker.log"})
    if path.stat().st_size > 512 * 1024 * 1024:
        raise ReplayWorkerError({"type": "result_too_large", "message": "worker result exceeds 512 MiB limit"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("ok") is not True or process.returncode != 0:
        raise ReplayWorkerError(payload.get("error") or {"message": f"worker exited {process.returncode}"})
    raw = payload.get("result")
    if not isinstance(raw, dict):
        raise ReplayWorkerError({"message": "invalid worker result schema"})
    expected = {field.name for field in fields(BacktestResult)} - {"config"}
    if set(raw) != expected:
        raise ReplayWorkerError({"message": "worker result fields do not match the runtime contract"})
    raw["strategy_root"] = Path(raw["strategy_root"]) if raw["strategy_root"] else None
    return BacktestResult(config=cfg, **raw)

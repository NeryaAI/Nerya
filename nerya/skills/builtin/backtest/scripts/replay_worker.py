"""Single-run replay worker. Strategy exceptions cannot kill the API process.

The parent owns lifecycle, deadlines and cancellation. This process performs no
data downloading and uses JSON (never pickle) for its result contract.
"""
from __future__ import annotations

import argparse
import json
import socket
from dataclasses import fields
from pathlib import Path

from .config import BacktestConfig
from .engine import run_backtest
from .run_state import atomic_json
from .....core.runtime_identity import SDK_BUILD_ID


_RUNTIME_VERSION_CHANGED_MESSAGE = (
    "runtime version changed since the API started; restart the service "
    "before replaying, rather than mixing SDK versions"
)


def _no_network(*args, **kwargs):
    raise RuntimeError("Network access is disabled during historical replay; prepare local data before executing the strategy")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    args = parser.parse_args(argv)
    request_path = Path(args.request).resolve()
    root = request_path.parent
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if request.get("sdk_build_id") not in (None, SDK_BUILD_ID):
            raise RuntimeError(_RUNTIME_VERSION_CHANGED_MESSAGE)
        cfg = BacktestConfig.from_raw(request["config"])
        # Ordinary Python/HTTP clients cannot fetch today's data during replay.
        # This guard is not an operating-system security sandbox.
        socket.socket.connect = _no_network
        socket.socket.connect_ex = _no_network
        socket.create_connection = _no_network
        result = run_backtest(request["strategy_root"], cfg,
            candles_by_market=request["candles_by_market"],
            timeframe_candles_by_market=request["timeframe_candles_by_market"],
            artefacts_dir=root, strategy_config=request["strategy_config"],
            progress=lambda state: atomic_json(root / "worker_progress.json", state))
        payload = {field.name: getattr(result, field.name) for field in fields(result)
                   if field.name not in {"config", "strategy_root"}}
        payload["strategy_root"] = str(result.strategy_root) if result.strategy_root else None
        atomic_json(root / "worker_result.json", {"ok": True, "result": payload})
        return 0
    except BaseException as exc:
        atomic_json(root / "worker_result.json", {"ok": False, "error": {
            "type": type(exc).__name__, "message": str(exc)[:3000],
            "surface": getattr(exc, "surface", None), "module": getattr(exc, "name", None),
            "market": getattr(exc, "market", None), "bar_ts": getattr(exc, "bar_ts", None),
            "details": getattr(exc, "receipt", None)}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

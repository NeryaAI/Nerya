"""Time-boxed UI fixture: real API + listener + local WS, no model or orders."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time

from websockets.sync.server import serve
from nerya.api.auth import set_admin_password
from nerya.api.local_server import build_server
from nerya.cli.commands.core import _configure_dashboard_channel, _stop_dashboard
from nerya.core import yaml_io
from nerya.core.config import load_config
from nerya.workspace.manager import WorkspaceManager


def main():
    project = Path(__file__).resolve().parents[1]
    import re
    name = os.environ.get("REVIEW_ROOT", "continuous-ui-0919")
    if not re.fullmatch(r"continuous-ui-[a-z0-9-]+", name):
        raise SystemExit("invalid review name")
    out = project / "dashboard/test-results" / name
    root = out / "workspace"
    if root.exists():
        raise SystemExit("review already exists; preserve earlier artifacts")
    WorkspaceManager.init(root)
    config = load_config(root)
    config.data.setdefault("runtime", {})["live_trading_enabled"] = False
    config.data["runtime"]["mock_mode"] = True
    yaml_io.dump(root / "nerya.yml", config.data)
    password = secrets.token_urlsafe(30)
    set_admin_password(config, password)
    out.mkdir(parents=True, exist_ok=True)
    (out / ".browser-password").write_text(password)
    (out / ".browser-password").chmod(0o600)
    yaml_io.dump(root / "security/web_policy.yml", {"allow_hosts": ["127.0.0.1"]})
    done = threading.Event()
    def feed(ws):
        while not done.is_set():
            try:
                ws.send(json.dumps({"price": 50000, "source": "controlled_ui_test"}))
            except Exception:
                return
            done.wait(0.3)
    with serve(feed, "127.0.0.1", 0) as socket_server:
        threading.Thread(target=socket_server.serve_forever, daemon=True).start()
        sid = "continuous_ui_observer"
        strategy = config.paths.strategy(sid)
        strategy.mkdir(parents=True, exist_ok=True)
        yaml_io.dump(strategy / "strategy.yml", {
            "version": 1, "strategy_id": sid, "title": "WebSocket 常驻监听验收", "mode": "paper",
            "entrypoint": "main.py:run", "execution_mode": "script", "agent_task": {"enabled": False},
            "accounts": ["paper_main"], "markets": ["mock:BTC/USDT"],
            "runtime": {"mode": "continuous", "streams": {"prices": {"url": f"ws://127.0.0.1:{socket_server.socket.getsockname()[1]}"}}},
            "evaluation": {"mode": "observation"}, "policy": {"allow_direct_order": False},
        })
        (strategy / "main.py").write_text('def run(ctx):\n    for message in ctx.stream.websocket("prices"):\n        ctx.inputs.publish("price", message["data"])\n', encoding="utf-8")
        _configure_dashboard_channel(config)
        server = build_server(config, host="127.0.0.1", port=0, start_cron=False, start_continuous=False)
        api = f"http://127.0.0.1:{server.server_address[1]}"
        child_env = dict(os.environ, NERYA_API=api, NERYA_UI_DIST_DIR=".next-continuous-review", NERYA_UI_TSCONFIG="tsconfig.chat-review.json", NERYA_E2E="1")
        with (out / "dashboard.log").open("w") as log:
            proc = subprocess.Popen(["node", str(project / "node_modules/next/dist/bin/next"), "dev", "-p", "18385", "-H", "127.0.0.1"], cwd=project / "dashboard", env=child_env, stdout=log, stderr=log, start_new_session=True)
            meta = {"api": api, "ui": "http://127.0.0.1:18385", "strategy_id": sid, "workspace": str(root), "scope": "isolated UI fixture; no model or orders"}
            (out / "server-info.json").write_text(json.dumps(meta))
            print(json.dumps(meta), flush=True)
            def terminate(*_):
                done.set()
            signal.signal(signal.SIGTERM, terminate)
            signal.signal(signal.SIGINT, terminate)
            server.timeout = 0.5
            try:
                deadline = time.monotonic() + 420
                while not done.is_set() and time.monotonic() < deadline and not (out / "review-complete").exists():
                    server.handle_request()
            finally:
                done.set()
                server.server_close()
                socket_server.shutdown()
                _stop_dashboard(proc)
                print("UI review services stopped", flush=True)


if __name__ == "__main__":
    main()

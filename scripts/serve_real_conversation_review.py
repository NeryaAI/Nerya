"""Opt-in, isolated Workspace for real browser/main-Agent acceptance.

Only the normal chat creates strategies. No stub provider, canned tools,
strategy seed specific to tests, bypassed permission or repaired production DB.
LLM secrets are copied in-memory into an encrypted test vault; never printed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys

from nerya.core import yaml_io
from nerya.core.config import load_config
from nerya.security.secrets import SecretVault
from nerya.workspace.manager import WorkspaceManager
from nerya.api.local_server import build_server


def vault_refs(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from vault_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from vault_refs(item)
    elif isinstance(value, str) and value.startswith("vault://"):
        yield value.removeprefix("vault://")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-workspace", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-real-model", action="store_true")
    parser.add_argument("--ui-port", type=int, default=18381)
    parser.add_argument("--ui-dist", default=".next-chat-review")
    args = parser.parse_args()
    if not args.allow_real_model:
        parser.error("Real model calls incur costs; explicit opt-in is required")
    import re
    if not 1024 <= args.ui_port <= 65535 or not re.fullmatch(r"\.next-[a-z0-9-]+", args.ui_dist):
        parser.error("Choose a local nonprivileged port and a .next-* build directory")
    source = load_config(args.source_workspace)
    tier = source.get("llm.default_tier", "medium")
    if source.live_trading_enabled() or source.get("runtime.mock_mode") or source.get(f"llm.tiers.{tier}.provider") == "mock":
        parser.error("Requires an existing real-model configuration with live trading and mock mode off")
    out = args.out.resolve()
    project = Path(__file__).resolve().parents[1]
    if not out.is_relative_to(project / "dashboard/test-results"):
        parser.error("Output must be under this project's dashboard/test-results")
    workspace = out / "workspace"
    if workspace.exists():
        parser.error("Use a fresh directory; do not overwrite an existing review")
    old_vault = SecretVault.open(source.paths.vault_enc)
    values = {}
    for name in sorted(set(vault_refs(source.get("llm", {})))):
        values[name] = old_vault.resolve(name, required_scope="llm")
    WorkspaceManager.init(workspace)
    copied = deepcopy(source.data)
    # The effective policy/model settings stay identical. There is no active
    # ledger, user state, provider cache or production database in this root.
    yaml_io.dump(workspace / "nerya.yml", copied)
    (workspace / "nerya.yml").chmod(0o600)
    os.environ["NERYA_VAULT_PASSPHRASE"] = secrets.token_urlsafe(48)
    vault = SecretVault.open(workspace / "vault/secrets.enc")
    for name, value in values.items():
        vault.put(name=name, value=value, kind="llm_provider_key", scope=["llm"], owner="authorized_real_chat_review")
    values.clear()
    # Same installed runtime interpreter as launching Nerya from an activated
    # virtual environment. No package installation or permission change.
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    cfg = load_config(workspace)
    assert cfg.get("llm") == source.get("llm")
    assert cfg.get("runtime.permission_mode") == source.get("runtime.permission_mode")
    assert not cfg.live_trading_enabled() and not cfg.get("runtime.mock_mode")
    from nerya.cli.commands.core import _configure_dashboard_channel, _stop_dashboard
    _configure_dashboard_channel(cfg)
    server = build_server(cfg, host="127.0.0.1", port=0)
    child_env = dict(os.environ, NERYA_API=f"http://127.0.0.1:{server.server_address[1]}",
        NERYA_UI_DIST_DIR=args.ui_dist, NERYA_UI_TSCONFIG="tsconfig.chat-review.json", NERYA_E2E="1")
    ui_log = (out / "dashboard.log").open("w", encoding="utf-8")
    dashboard = subprocess.Popen(["node", str(project / "node_modules/next/dist/bin/next"), "dev", "-p", str(args.ui_port), "-H", "127.0.0.1"],
        cwd=project / "dashboard", env=child_env, stdout=ui_log, stderr=subprocess.STDOUT)
    def terminated(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminated)
    signal.signal(signal.SIGHUP, terminated)
    meta = {"api": f"http://127.0.0.1:{server.server_address[1]}", "ui": f"http://127.0.0.1:{args.ui_port}", "workspace": str(workspace),
        "source_workspace": str(source.paths.root), "provider": cfg.get(f"llm.tiers.{tier}.provider"),
        "model": cfg.get(f"llm.tiers.{tier}.model"), "permission_mode": cfg.get("runtime.permission_mode"),
        "skill_sha256": hashlib.sha256((project / "nerya/skills/builtin/strategy_author/SKILL.md").read_bytes()).hexdigest(),
        "scope": "Fresh Workspace, same real model and permissions, actual browser chat and tools. No model/data stubs. Production database unchanged.",
        "python_environment": "Installed Nerya virtual environment; PATH activated in this review process"}
    (out / "server-info.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(json.dumps(meta, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        _stop_dashboard(dashboard)
        ui_log.close()


if __name__ == "__main__":
    main()

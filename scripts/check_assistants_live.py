"""Opt-in real-model acceptance in an isolated workspace, never the source DB.

Run from the repo: python scripts/check_assistants_live.py --source-workspace
<configured workspace> --output-dir <empty test directory> [--port 18517 --serve]
Only LLM configuration, catalog and its explicitly referenced vault entries are
copied. No gateway credentials, accounts, conversations or schedules are copied.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nerya.core.config import Config, DEFAULT_CONFIG, load_config
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.security.secrets import SecretVault
from nerya.llm.gateway import LLMGateway


def prepare_workspace(source_root: Path, target: Path) -> Config:
    if target.exists() and any(target.iterdir()):
        raise ValueError("Use an empty acceptance workspace.")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)
    source = load_config(source_root)
    data = copy.deepcopy(DEFAULT_CONFIG)
    data["llm"] = copy.deepcopy(source.data["llm"])
    data["runtime"].update(mock_mode=False, live_trading_enabled=False, permission_mode="default", api_auth_mode="local")
    data["agent"]["native"].update(tier=source.get("agent.native.tier", "high"), max_iterations=8, max_wall_seconds=150)
    config = Config(paths=WorkspacePaths(target), data=data)
    catalog = source.paths.root / "llm" / "model_catalog.json"
    refs = set()
    def visit(value):
        if isinstance(value, dict):
            for item in value.values(): visit(item)
        elif isinstance(value, list):
            for item in value: visit(item)
        elif isinstance(value, str) and value.startswith("vault://"):
            refs.add(value[8:])
    visit(data["llm"])
    if catalog.exists():
        visit(json.loads(catalog.read_text()))
        destination = target / "llm" / catalog.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(catalog, destination)
    if refs:
        source_vault = SecretVault.open(source.paths.vault_enc)
        target_vault = SecretVault.open(config.paths.vault_enc)
        for name in refs:
            value = source_vault.resolve(name, required_scope="llm")
            target_vault.put(name=name, value=value, kind="api_key", scope=["llm"], owner="assistant-acceptance")
    yaml_io.dump(config.paths.config, data)
    notes = target / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "assistant-proof.txt").write_text("ASSISTANT_FILE_PROOF_63a7", encoding="utf-8")
    return config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-workspace", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18517)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    config = prepare_workspace(args.source_workspace.resolve(), args.output_dir.resolve())
    from nerya.api.auth import issue_admin_jwt
    auth_path = config.paths.root / "browser-auth.json"
    auth_path.write_text(json.dumps(issue_admin_jwt(config, actor="assistant-acceptance")))
    auth_path.chmod(0o600)
    from nerya.api.local_server import build_server
    token = os.environ.get("NERYA_DASHBOARD_INTERNAL_TOKEN") or "assistants-acceptance-" + uuid.uuid4().hex
    os.environ["NERYA_DASHBOARD_INTERNAL_TOKEN"] = token
    server = build_server(config, host="127.0.0.1", port=args.port, start_cron=False, start_continuous=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:" + str(server.server_address[1])
    def http(path, body=None):
        request = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
            headers={"content-type": "application/json", "x-nerya-dashboard-internal": token})
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)
    def command(sid, text):
        cid = "assistant-live-" + uuid.uuid4().hex
        http("/agent/commands", {"command_id": cid, "session_id": sid, "request": {
            "source": "dashboard", "kind": "user.chat", "payload": {"text": text},
            "max_iterations": 6, "max_wall_seconds": 150}})
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            row = http("/agent/commands?session_id=" + sid + "&command_id=" + cid)["command"]
            if row["state"] not in ("queued", "running", "stopping", "delivering"):
                return row
            time.sleep(1)
        raise TimeoutError("Assistant command did not finish within 180 seconds.")
    def answer(row):
        result = row.get("result") or {}
        return str(result.get("answer") or result.get("message") or result.get("final_text") or result.get("reply_text") or "")
    report = {"workspace": str(config.paths.root), "source_workspace": str(args.source_workspace), "runtime": base, "mock_mode": False, "checks": {}}
    report["provider"], report["model"], _metadata = LLMGateway(config).effective_model_metadata(config.get("agent.native.tier"))
    try:
        scout = http("/assistants/save", {"profile": {"name": "验收研究助理", "role": "QA researcher", "instructions": "Begin every answer with QA_SCOUT. Remember conversation details. Follow requests for read-only file inspection.", "shell_allowed": False}})["assistant"]
        writer = http("/assistants/save", {"profile": {"name": "验收写作助理", "role": "QA writer", "instructions": "Begin every answer with QA_WRITER. If a detail is not in this conversation, answer UNKNOWN.", "shell_allowed": False}})["assistant"]
        sid = http("/assistants/conversations", {"assistant_id": scout["id"]})["session_id"]
        secret = "conversation-" + uuid.uuid4().hex[:12]
        first = command(sid, "Keep this private conversation nonce for the next turn: " + secret + ". Use read_file to read notes/assistant-proof.txt, and include its exact contents in your answer.")
        text = answer(first)
        checks = report["checks"]
        checks["web_identity"] = "QA_SCOUT" in text and first["state"] == "succeeded"
        checks["real_tool_read"] = "ASSISTANT_FILE_PROOF_63a7" in text
        second = command(sid, "What was my private conversation nonce in the previous turn? Include it exactly.")
        checks["conversation_context"] = secret in answer(second) and "QA_SCOUT" in answer(second)
        writer_sid = http("/assistants/conversations", {"assistant_id": writer["id"]})["session_id"]
        separate = command(writer_sid, "What is the private conversation nonce from the other assistant's conversation?")
        checks["assistant_isolation"] = "QA_WRITER" in answer(separate) and secret not in answer(separate) and "UNKNOWN" in answer(separate)
        from nerya.sdk.internal_client import InternalClient
        from nerya.api.routes_gateway import _run_gateway_turn
        gateway = _run_gateway_turn(InternalClient.from_config(config), platform="webhook", channel="acceptance_webhook", chat_id="qa-room",
            text="Introduce yourself using your configured identity. No tools are needed.", progress_cfg={"assistant_id": scout["id"], "auto_reply": False})
        checks["real_gateway_identity"] = gateway.get("assistant_id") == scout["id"] and "QA_SCOUT" in gateway.get("reply_text", "")
        events = http("/agent/commands/events?session_id=" + sid + "&command_id=" + first["command_id"])
        checks["durable_events"] = bool(events.get("events"))
        from nerya.db.sqlite import connect
        con = connect(config.paths.db)
        usage = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%llm%'").fetchall()
        con.close()
        report.update(assistant_id=scout["id"], session_id=sid, writer_id=writer["id"], gateway_session=gateway.get("session_id"),
                      command_states=[first["state"], second["state"], separate["state"]], llm_usage_tables=[r[0] for r in usage])
        # Full turn outputs are local evidence only and never include credential configuration.
        (config.paths.root / "acceptance-turns.json").write_text(json.dumps([first, second, separate, gateway], ensure_ascii=False, indent=2, default=str))
        report["ok"] = all(checks.values())
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if not report["ok"]:
            raise AssertionError("Live assistant acceptance failed; inspect acceptance-report.json.")
    except Exception as exc:
        report.update(ok=False, error=type(exc).__name__)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        raise
    finally:
        (config.paths.root / "acceptance-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        if args.serve and report.get("ok"):
            env_path = config.paths.root / "dashboard-env.json"
            env_path.write_text(json.dumps({"NERYA_API": base, "NERYA_DASHBOARD_INTERNAL_TOKEN": token}))
            env_path.chmod(0o600)
            print("Acceptance runtime remains available for browser QA.", flush=True)
            while True: time.sleep(30)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

"""Print public outcomes/receipts of this review, not reasoning or credentials."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("directory", type=Path)
args = parser.parse_args()
root = args.directory.resolve()
base = Path(__file__).resolve().parents[1] / "dashboard/test-results"
if not root.is_relative_to(base.resolve()):
    raise ValueError("Only conversation acceptance outputs are readable")
for path in sorted(root.glob("*-result.json")):
    record = json.loads(path.read_text())
    result = record.get("result") or {}
    trace = []
    for item in result.get("tool_trace") or []:
        name = item.get("action") or item.get("name")
        payload = item.get("payload") or {}
        receipt = {"action": name, "ok": item.get("ok"),
            "payload": {k: payload[k] for k in ("proposal_id", "skill", "file", "path", "config_path", "allow_mock") if k in payload}}
        raw = item.get("result")
        if name in {"strategy_validate", "strategy_submit_proposal", "strategy_backtest", "run_shell", "script_run"}:
            if isinstance(raw, str):
                try: raw = json.loads(raw)
                except ValueError: raw = {"message": raw[:1200]}
            if isinstance(raw, dict):
                receipt["result"] = {k: raw[k] for k in ("ok", "error", "reason", "status", "proposal_id", "verdict", "replay", "backtest_ts", "metrics", "out_dir", "blockers", "exit_code", "stdout", "message") if k in raw}
        if item.get("error"):
            receipt["error"] = str(item["error"])[:700]
        trace.append(receipt)
    public = {"case": path.stem, "elapsed_s": record.get("elapsed_s"), "session_id": result.get("session_id"),
        "turn_id": result.get("turn_id"), "stop": result.get("stopped_reason"),
        "final_text": result.get("final_text"), "budget": result.get("budget"), "trace": trace}
    output = root / (path.stem + "-public.json")
    output.write_text(json.dumps(public, ensure_ascii=False, indent=2))
    print(json.dumps(public, ensure_ascii=False))

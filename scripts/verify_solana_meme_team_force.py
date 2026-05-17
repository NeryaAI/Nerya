"""Force-fire ``solana_meme_team_30min`` via TriggerRuntime to verify the
freshly-generated Solana memecoin Agent Team strategy:

  * routes via ``skill:strategy.agent_task``
  * builds a 5-role team prompt with all 6 memecoin markets
  * sub-agents respond + Master decision is captured
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from nerya.core.config import load_config
from nerya.triggers.event import TriggerEvent
from nerya.triggers.runtime import TriggerRuntime


def main() -> int:
    cfg = load_config()
    runtime = TriggerRuntime.boot(cfg)

    sid = "solana_meme_team_30min"
    print(f"=== Force-fire {sid} via skill:strategy.agent_task ===\n")

    task_dir = cfg.paths.strategies / sid / "agent_tasks"
    before = set(p.name for p in task_dir.glob("agent_task_*")) if task_dir.exists() else set()
    print(f"  pre  agent_task dirs: {len(before)}")

    event = TriggerEvent.new(
        source="manual",
        kind="strategy.scheduled",
        target="skill:strategy.agent_task",
        strategy_id=sid,
        payload={
            "strategy_id": sid,
            "kind": "manual",
            "trigger_payload": {"note": "verify_solana_meme_team_force", "force": True},
        },
    )
    print(f"  event_id: {event.event_id}\n")

    t0 = time.time()
    result = runtime.emit(event)
    duration = time.time() - t0

    print(f"=== TriggerRuntime.emit -> {duration:.1f}s ===")
    if hasattr(result, "asdict"):
        out = result.asdict()
    elif hasattr(result, "__dict__"):
        out = dict(result.__dict__)
    else:
        out = {"raw": repr(result)}

    payload = out.get("payload") or out
    aggregated = (payload.get("aggregated") if isinstance(payload, dict) else None) or {}
    print(f"\n  outcome     : {payload.get('outcome') if isinstance(payload, dict) else '?'}")
    print(f"  decision    : {payload.get('decision') if isinstance(payload, dict) else '?'}")
    print(f"  selected    : {payload.get('selected_market') if isinstance(payload, dict) else '?'}")
    if aggregated:
        print(f"  roles_succeeded : {aggregated.get('roles_succeeded')}")
        print(f"  roles_failed    : {aggregated.get('roles_failed')}")
        print(f"  tokens_total    : {aggregated.get('tokens_total')}")
        print(f"  usd_total       : {aggregated.get('usd_total')}")

    if task_dir.exists():
        after = set(p.name for p in task_dir.glob("agent_task_*"))
        new = sorted(after - before)
        print(f"\n  new agent_task dirs: {new}")
        for name in new:
            d = task_dir / name
            prompt_path = d / "prompt.md"
            if prompt_path.exists():
                content = prompt_path.read_text(encoding="utf-8")
                print(f"\n  === {name}/prompt.md ({len(content):,} chars) ===")
                for marker in (
                    "okx:BONK",
                    "okx:WIF",
                    "okx:POPCAT",
                    "Technical snapshots",
                    "team_run",
                    "ranked_candidates",
                ):
                    idx = content.find(marker)
                    if idx >= 0:
                        snippet = content[idx : idx + 240].replace("\n", " | ")
                        print(f"    [{marker}] @ {idx}: {snippet[:240]}")

    sub_path = cfg.paths.strategies / sid / "history" / "subagents.jsonl"
    if sub_path.exists():
        lines = sub_path.read_text(encoding="utf-8").strip().splitlines()
        print("\n  --- latest 6 subagent rows ---")
        for line in lines[-6:]:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            out_d = rec.get("output") or {}
            keys = sorted(out_d.keys())[:6] if isinstance(out_d, dict) else []
            raw_len = len(out_d.get("raw") or "") if isinstance(out_d, dict) else 0
            degraded = out_d.get("degraded") if isinstance(out_d, dict) else "?"
            print(
                f"    {rec.get('name','?'):25} "
                f"degraded={str(degraded):>5} raw_len={raw_len:>6} keys={keys}"
            )
    else:
        print(f"\n  (no subagents.jsonl at {sub_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

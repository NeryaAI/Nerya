"""Force-fire ``bsc_meme_team_30min`` via TriggerRuntime."""

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

    sid = "bsc_meme_team_30min"
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
            "trigger_payload": {"note": "verify_bsc_meme_team_force", "force": True},
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
    if isinstance(payload, dict):
        agg = payload.get("aggregated") or {}
        print(f"  outcome  : {payload.get('outcome')}")
        print(f"  decision : {payload.get('decision')}")
        print(f"  selected : {payload.get('selected_market')}")
        if agg:
            print(f"  roles_succeeded={agg.get('roles_succeeded')} failed={agg.get('roles_failed')}")
            print(f"  tokens_total={agg.get('tokens_total')} usd_total={agg.get('usd_total')}")

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
                for marker in ("okx:CAKE", "okx:FLOKI", "okx:BABYDOGE",
                               "okx:SHIB", "okx:BANANA", "team_run", "ranked_candidates"):
                    idx = content.find(marker)
                    if idx >= 0:
                        snippet = content[idx : idx + 220].replace("\n", " | ")
                        print(f"    [{marker}] @ {idx}: {snippet[:220]}")

    sub_path = cfg.paths.strategies / sid / "history" / "subagents.jsonl"
    if sub_path.exists():
        lines = sub_path.read_text(encoding="utf-8").strip().splitlines()
        print(f"\n  --- latest 6 subagent rows ({len(lines)} total) ---")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())

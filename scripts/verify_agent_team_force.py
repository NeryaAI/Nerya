"""Force-fire v6_team_us_basket_daily through TriggerRuntime so the Agent Team
path runs without waiting for the Mon-Fri 10:00 cron.

Verifies:
  * the cron target ``skill:strategy.agent_task`` routes correctly
  * StrategyAgentTaskExecutor builds a team task (5 sub-roles)
  * equity news fetcher attaches per-ticker headlines
  * the team_run gate fires before any trade_intent_submit
  * the prompt contains kline + indicators + news for each market
"""

from __future__ import annotations

import json
import os
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

    sid = "v6_team_us_basket_daily"
    print(f"=== Force-fire {sid} via skill:strategy.agent_task ===\n")

    # Snapshot agent_task dir before
    task_dir = cfg.paths.strategies / sid / "agent_tasks"
    before = set(p.name for p in task_dir.glob("agent_task_*")) if task_dir.exists() else set()
    print(f"  pre agent_task dirs: {len(before)}")

    event = TriggerEvent.new(
        source="manual",
        kind="strategy.scheduled",
        target="skill:strategy.agent_task",
        strategy_id=sid,
        payload={
            "strategy_id": sid,
            "kind": "manual",
            "trigger_payload": {"note": "verify_agent_team_force", "force": True},
        },
    )
    print(f"  event_id: {event.event_id}")
    print(f"  target  : {event.target}")
    print()

    t0 = time.time()
    result = runtime.emit(event)
    duration = time.time() - t0

    print(f"=== TriggerRuntime.emit → {duration:.1f}s ===")
    if hasattr(result, "asdict"):
        out = result.asdict()
    elif hasattr(result, "__dict__"):
        out = dict(result.__dict__)
    else:
        out = {"raw": repr(result)}
    print(json.dumps(out, indent=2, default=str, ensure_ascii=False)[:2500])

    # Inspect any newly-created task dir
    if task_dir.exists():
        after = set(p.name for p in task_dir.glob("agent_task_*"))
        new = sorted(after - before)
        print(f"\n  new agent_task dirs: {new}")
        for name in new:
            d = task_dir / name
            prompt_path = d / "prompt.md"
            if prompt_path.exists():
                content = prompt_path.read_text(encoding="utf-8")
                print(f"\n  === {name}/prompt.md ({len(content)} chars) ===")
                # show news + first part of agent role
                for marker in [
                    "Recent news JSON:",
                    "Indicator/features",
                    "Recent K-line",
                    "agent_team",
                    "team_run",
                    "Agent Team",
                ]:
                    if marker in content:
                        idx = content.find(marker)
                        snippet = content[idx:idx + 400].replace("\n", "\n      ")
                        print(f"\n  [{marker}] @ char {idx}:")
                        print(f"      {snippet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

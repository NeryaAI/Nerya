"""Force-fire ``us_swing_qqq_team_daily`` via TriggerRuntime so we don't
have to wait for the Mon-Fri 09:30 NY cron.

Verifies the freshly-generated US-equity Agent Team strategy:
  * routes via ``skill:strategy.agent_task``
  * builds a 5-role team prompt with all 4 markets
  * fetches per-ticker equity news via ``yahoo_finance_rss``
  * 5/5 sub-agents respond (post step-3.6 / max_tokens fix)
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

    sid = "us_swing_qqq_team_daily"
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
            "trigger_payload": {"note": "verify_us_swing_force", "force": True},
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

    # Compact summary
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
                    "Recent news JSON:",
                    "Recent K-line",
                    "Indicator/features",
                    "yahoo:QQQ",
                    "yahoo:SPY",
                ):
                    idx = content.find(marker)
                    if idx >= 0:
                        snippet = content[idx : idx + 240].replace("\n", " | ")
                        print(f"    [{marker}] @ {idx}: {snippet[:240]}")

    # Show the latest 5 sub-agent rows so we can see role outputs.
    sub_path = cfg.paths.strategies / sid / "history" / "subagents.jsonl"
    if sub_path.exists():
        lines = sub_path.read_text(encoding="utf-8").strip().splitlines()
        print("\n  --- latest 5 subagent rows ---")
        for line in lines[-5:]:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            out_d = rec.get("output") or {}
            keys = sorted(out_d.keys())[:6] if isinstance(out_d, dict) else []
            print(
                f"    {rec.get('name','?'):25} "
                f"degraded={out_d.get('degraded') if isinstance(out_d, dict) else '?':>5} "
                f"raw_len={len(out_d.get('raw') or '') if isinstance(out_d, dict) else '?':>5} "
                f"keys={keys}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

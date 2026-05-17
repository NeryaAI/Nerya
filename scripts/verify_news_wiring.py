"""Verify default news fetchers are now wired into every StrategyContext."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from nerya.core.config import load_config
from nerya.strategies.context import build_strategy_context
from nerya.strategies.package import load_package


def main() -> int:
    cfg = load_config()

    for sid, expected_source in [
        ("v6_macross_then_agent", "crypto"),
        ("v6_team_us_basket_daily", "equity"),
    ]:
        print(f"=== {sid} (expects '{expected_source}' fetcher) ===")
        pkg = load_package(cfg.paths, sid)
        ctx = build_strategy_context(config=cfg, package=pkg)
        registered = list(ctx.news._fetchers.keys())
        rows = ctx.news.fetch(limit=5)
        print(f"  news.sources         = {ctx.news.sources}")
        print(f"  registered fetchers  = {registered}")
        print(f"  fetched rows         = {len(rows)}")
        ok = expected_source in registered and len(rows) > 0
        for r in rows[:3]:
            src = r.get("source") or "?"
            title = (r.get("title") or "")[:80]
            print(f"    - [{src}] {title}")
        print(f"  result: {'PASS' if ok else 'FAIL'}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

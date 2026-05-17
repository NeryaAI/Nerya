"""Force build_agent_task(ctx) on v6_macross_then_agent and inspect the
resulting Agent prompt to confirm the news section is no longer empty.

This goes through the same _build_task path as
StrategyAgentTaskExecutor._build_task does at cron-fire time:

* build_strategy_context(...) — registers the default crypto/equity
  fetchers introduced in nerya/strategies/news_fetchers.py.
* import the strategy module
* call build_agent_task(ctx)
* render the resulting StrategyAgentTask and check the prompt
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from nerya.core.config import load_config
from nerya.strategies.context import build_strategy_context
from nerya.strategies.package import load_package


def _import_strategy_main(pkg_root: Path):
    main_path = pkg_root / "main.py"
    spec = importlib.util.spec_from_file_location(
        f"_strat_main_{pkg_root.name}", str(main_path)
    )
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot import {main_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    cfg = load_config()
    sid = "v6_macross_then_agent"
    pkg = load_package(cfg.paths, sid)
    ctx = build_strategy_context(config=cfg, package=pkg)

    print(f"=== {sid}: build_agent_task with patched ctx ===")
    print(f"  news.sources         = {ctx.news.sources}")
    print(f"  registered fetchers  = {list(ctx.news._fetchers.keys())}")
    rows = ctx.news.fetch(limit=10)
    print(f"  ctx.news.fetch rows  = {len(rows)}")
    if not rows:
        print("  ! still 0 rows — fetcher not actually firing")
        return 1

    module = _import_strategy_main(pkg.root)
    fn = getattr(module, "build_agent_task", None)
    if not callable(fn):
        print("  ! build_agent_task missing on main.py")
        return 1

    task = fn(ctx)
    prompt = getattr(task, "prompt", "")
    metadata = dict(getattr(task, "metadata", {}) or {})

    news_idx = prompt.find("Recent news JSON:")
    quality_idx = prompt.find("Data quality JSON:")
    news_block = prompt[news_idx:quality_idx] if news_idx >= 0 and quality_idx > news_idx else "<missing>"
    print(f"  prompt total chars   = {len(prompt)}")
    print(f"  news block size      = {len(news_block)} chars")
    print(f"  data_quality.news_available = {metadata.get('data_quality',{}).get('news_available') if metadata.get('data_quality') else '?'}")
    print()
    print("  news block excerpt (first 1500 chars):")
    print("  " + "-" * 60)
    for line in news_block[:1500].splitlines():
        print("  " + line)
    print("  " + "-" * 60)

    if "[]" in news_block.split("\n", 2)[1] if news_block else True:
        # Heuristic: empty array right after "Recent news JSON:" header
        pass
    has_real_news = any(
        marker in news_block
        for marker in ('"title":', '"link":', "coindesk", "yahoo")
    )
    print()
    print(f"  result: {'PASS — agent prompt has live news' if has_real_news else 'FAIL — news block still empty'}")
    return 0 if has_real_news else 1


if __name__ == "__main__":
    sys.exit(main())

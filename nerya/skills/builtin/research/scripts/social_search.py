"""Search posts on social platforms (X, Reddit, Discord) via DuckDuckGo.

Standalone CLI usage::

    python -m nerya.skills.builtin.research.scripts.social_search \\
        --json '{"query": "USDC depeg", "platform": "x", "max_results": 5}'

We deliberately keep this script stdlib-only by issuing a site-restricted
search through DuckDuckGo (e.g. ``site:x.com USDC depeg``) rather than
hitting each platform's API. That avoids credentials at the cost of
freshness — for streaming-grade reads, the agent should fall back to
``run_shell`` with the platform's first-party tooling and document its
choice in the journal.

Supported platforms:

* ``x``       — ``site:x.com OR site:twitter.com``
* ``reddit``  — ``site:reddit.com``
* ``discord`` — ``site:discord.com OR site:discord.gg``

Output schema mirrors :mod:`web_search`.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .web_search import run as web_run


_PLATFORM_FILTERS = {
    "x":       "(site:x.com OR site:twitter.com)",
    "twitter": "(site:x.com OR site:twitter.com)",
    "reddit":  "site:reddit.com",
    "discord": "(site:discord.com OR site:discord.gg)",
}


def run(
    *,
    query: str,
    platform: str = "x",
    max_results: int = 8,
) -> dict[str, Any]:
    if not query:
        return {"ok": False, "error": "query is required", "results": []}
    site_filter = _PLATFORM_FILTERS.get((platform or "x").lower())
    if site_filter is None:
        return {
            "ok": False,
            "error": (
                f"unsupported platform {platform!r}; "
                f"have: {sorted(_PLATFORM_FILTERS)}"
            ),
            "results": [],
        }
    full_query = f"{site_filter} {query}".strip()
    raw = web_run(query=full_query, max_results=max_results)
    if not raw.get("ok"):
        return {
            "ok": False,
            "platform": platform,
            "query": query,
            "site_query": full_query,
            **{k: v for k, v in raw.items() if k != "ok"},
        }
    return {
        "ok": True,
        "platform": platform,
        "query": query,
        "site_query": full_query,
        "engine": raw.get("engine"),
        "elapsed_ms": raw.get("elapsed_ms"),
        "count": raw.get("count", 0),
        "results": raw.get("results", []),
    }


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    parser.add_argument("--query", dest="query", default=None)
    parser.add_argument("--platform", dest="platform", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    query = args.query or payload.get("query") or ""
    platform = args.platform or payload.get("platform") or "x"
    try:
        result = run(
            query=query,
            platform=platform,
            max_results=int(payload.get("max_results") or 8),
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

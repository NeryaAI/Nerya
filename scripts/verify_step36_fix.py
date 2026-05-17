"""Smoke test: confirm step-3.6 still answers after the adapter recognises
it as a reasoning model AND the medium tier max_tokens is bumped to 16384.

Targets the same scenario that produced empty `macro_strategist` /
`technical_analyst` output: a 40k+ char subagent-style prompt with
``reasoning_effort: high``.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from nerya.core.config import load_config
from nerya.llm.gateway import LLMGateway


LARGE_PROMPT = """You are a senior macro strategist.

Below is a long structured payload describing four US equity markets (AAPL,
MSFT, NVDA, AMZN), their daily indicators, news, and the basket-level
context. Read it carefully, then output ONE strict JSON object with keys:
recommendation, confidence, time_horizon, market, thesis, invalidation,
risk_flags, evidence.
Do not produce any prose outside the JSON.
""" + ("\n\nFiller context line " + ("X" * 200)) * 200 + """

Output JSON only, no prose.
"""


def main() -> int:
    cfg = load_config()
    print(f"Loaded config; medium tier max_tokens = "
          f"{(cfg.get('llm.tiers') or {}).get('medium', {}).get('max_tokens')}")
    gw = LLMGateway(cfg)
    print(f"Prompt size: {len(LARGE_PROMPT):,} chars")
    res = gw.call(
        task="subagent_analysis",
        caller="subagent:smoke_test",
        prompt=LARGE_PROMPT,
        tier="medium",
    )
    raw = (res.raw or "")[:600]
    parsed = res.parsed or {}
    print(f"\nresponse_len = {len(res.raw or '')}")
    print(f"parsed_keys = {sorted(parsed.keys())[:8]}")
    print(f"reasoning_text_len = {len(getattr(res, 'reasoning_text', '') or '')}")
    print(f"reasoning_tokens = {getattr(res, 'reasoning_tokens', 0)}")
    print(f"tokens = {res.tokens}, usd = {res.usd}")
    print(f"first 600 raw chars:\n{raw}\n")
    return 0 if (res.raw and len(res.raw) > 0) else 1


if __name__ == "__main__":
    sys.exit(main())

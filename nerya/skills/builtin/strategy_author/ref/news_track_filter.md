# Archetype: News-tracking with LLM filter + team vote

Goal: monitor a curated news / social / on-chain feed; for each item,
have a *light* LLM model triage relevance, then route the survivors to
a small Agent Team that decides whether to trade. The team is the
qualitative committee; the light filter is the cost gate.

## When this archetype fits

- Asymmetric, headline-driven markets: prediction markets (Polymarket),
  earnings reactions, regulatory announcements, on-chain depeg/exploit
  events.
- The decision is *qualitative*: rules cannot encode "is this a real
  hack vs FUD"; you need an LLM jury.
- Items arrive irregularly (event-driven). Cron is wrong here — use
  the trigger router.

## strategy.yml shape

```yaml
strategy_id: polymarket_news_election
title: Polymarket — election headline tracker
mode: paper
accounts: [paper_polymarket]
markets: ["polymarket:US-PRES-2028"]
trigger_kinds: [event]
triggers:
  - kind: event
    event:
      match:
        kind: "news.headline"
        tags_any: ["election", "polling", "campaign-finance"]
        sources_any: ["reuters", "bloomberg", "nytimes", "ap"]
    payload:
      lookback_minutes: 30
subagents: []                     # team_run replaces single subagent
tuning:
  enabled: false
```

## main.py shape

```python
"""Polymarket election headline tracker.

Pipeline:
  1. ctx.trigger.payload carries the headline (id, source, text, ts).
  2. Light LLM filter — is this even relevant? cheap, fast.
  3. Pull the surrounding 30m of headlines + market YES/NO ladder.
  4. Run a 3-role team (market_analyst, news_interpreter, risk_critic)
     and aggregate.
  5. Submit only when the aggregate confidence >= 0.55 AND no role
     vetoed.
"""

from __future__ import annotations

import json


_FILTER_SYSTEM = (
    "You are a relevance gate for an election-market trading strategy. "
    "Reply ONLY with JSON: {\"relevant\": bool, \"reason\": \"...\"}. "
    "Mark relevant=true only when the headline plausibly moves the "
    "Polymarket US-PRES-2028 contract within 24h."
)


def run(ctx) -> dict:
    market = "polymarket:US-PRES-2028"
    payload = ctx.trigger.payload
    headline = payload.get("headline") or {}
    if not headline:
        return {"decision": "HOLD", "reason": "trigger had no headline"}

    headline_id = str(headline.get("id") or "")
    if headline_id and ctx.dedupe.seen(f"hl:{headline_id}"):
        return {"decision": "HOLD",
                "reason": f"already processed {headline_id}"}

    relevance = ctx.llm.complete(
        tier="light",
        system=_FILTER_SYSTEM,
        user=json.dumps({
            "headline": headline,
            "market": market,
            "now": ctx.clock.now_iso(),
        }),
        max_tokens=120,
        response_format="json",
    )
    if headline_id:
        ctx.dedupe.mark(f"hl:{headline_id}")
    if not relevance.get("relevant"):
        return {"decision": "HOLD",
                "reason": f"filter dropped: {relevance.get('reason')}",
                "filter": relevance}

    recent = ctx.news.fetch(
        sources=["reuters", "bloomberg", "nytimes", "ap"],
        since=payload.get("since"),
        limit=20,
    )
    book = ctx.market.orderbook(market, depth=10)
    yes_mid = (book["yes"]["bid"] + book["yes"]["ask"]) / 2

    team = ctx.subagents.run(
        "team",
        payload={
            "task": (
                f"New headline arrived: {headline.get('text')!r}. "
                f"Should we move on Polymarket {market} (YES mid={yes_mid:.3f})?"
            ),
            "roles": [
                {"name": "market_analyst",
                 "payload": {"market": market, "yes_mid": yes_mid,
                             "book": book, "headline": headline}},
                {"name": "news_interpreter",
                 "payload": {"headline": headline, "recent": recent}},
                {"name": "risk_critic",
                 "payload": {"strategy_id": ctx.strategy_id,
                             "headline": headline}},
            ],
            "shared_payload": {"window_hours": 24,
                               "kind": "polymarket-headline"},
            "max_parallel": 3,
            "usd_budget": 0.40,
        },
    )

    failures = team.get("roles_failed") or []
    if failures:
        return {"decision": "HOLD",
                "reason": f"team incomplete: failed={failures}",
                "team": team}

    agg = team.get("aggregated") or {}
    avg_conf = float(agg.get("avg_confidence") or 0.0)
    decisions = [r.get("output", {}).get("decision") for r in team.get("results") or []]
    has_veto = any(d == "EXIT" or d == "VETO" for d in decisions)

    if has_veto or avg_conf < 0.55:
        return {"decision": "HOLD",
                "reason": f"team gate: veto={has_veto} avg_conf={avg_conf:.2f}",
                "team": team}

    side = "yes" if any(d == "ENTRY_YES" for d in decisions) else "no"
    qty_usd = 50.0
    intent = ctx.trading.submit_intent(
        market=market,
        side=side,
        size=qty_usd,
        size_unit="usd",
        order_type="market",
        reasoning=f"polymkt:{headline_id or ctx.clock.now_iso()}",
    )
    return {
        "decision": "ENTRY",
        "reason": f"team agreed (conf={avg_conf:.2f}) side={side}",
        "intent_id": intent.get("intent_id"),
        "team": team,
        "filter": relevance,
    }
```

## Custom replay / backtest plan

Polymarket headline backtests are lossy: news timestamps slip and team
verdicts are LLM-stochastic. Do not skip replay entirely. Ship a custom
fixture replay that pins LLM/team responses, then add a paper-trade plan for
the parts the replay cannot prove.

```python
# tests/test_main.py
from main import run

def test_filter_rejects_irrelevant(make_ctx):
    ctx = make_ctx(
        trigger_payload={"headline": {"id": "h1", "text": "weather report"}},
        llm_stub={"light": {"relevant": False, "reason": "weather"}},
    )
    out = run(ctx)
    assert out["decision"] == "HOLD"
    assert out["reason"].startswith("filter dropped")

def test_team_veto_blocks_entry(make_ctx):
    ctx = make_ctx(
        trigger_payload={"headline": {"id": "h2",
                                      "text": "Senate vote on campaign-finance bill"}},
        llm_stub={"light": {"relevant": True, "reason": "campaign-finance"}},
        team_stub={"results": [
            {"output": {"decision": "VETO", "confidence": 0.9}},
            {"output": {"decision": "ENTRY_YES", "confidence": 0.7}},
            {"output": {"decision": "VETO", "confidence": 0.8}},
        ], "aggregated": {"avg_confidence": 0.8}, "roles_failed": []},
    )
    out = run(ctx)
    assert out["decision"] == "HOLD"

def test_team_consensus_enters(make_ctx):
    ctx = make_ctx(
        trigger_payload={"headline": {"id": "h3", "text": "polling shock"}},
        llm_stub={"light": {"relevant": True, "reason": "polling"}},
        team_stub={"results": [
            {"output": {"decision": "ENTRY_YES", "confidence": 0.7}},
            {"output": {"decision": "ENTRY_YES", "confidence": 0.65}},
            {"output": {"decision": "ENTRY_YES", "confidence": 0.6}},
        ], "aggregated": {"avg_confidence": 0.65}, "roles_failed": []},
    )
    out = run(ctx)
    assert out["decision"] == "ENTRY"
```

In `strategy.md`, document under `## Backtesting`:

> Historical headline replay is unreliable for Polymarket because the
> contract liquidity profile shifts mid-cycle. We rely on (a) the
> stub-LLM/team replay above, (b) a custom replay report under
> `backtests/custom_replay_report.md`, and (c) a 30-day paper-trade window
> with daily review before any `live_trading_enabled: true` flip.

For OHLCV-only smoke tests, the same engine can be invoked with stubbed
surfaces:

```python
stats = ctx.backtest_replay(
    run,
    mock_surfaces={
        "news": {"mode": "stub", "payload": []},
        "llm": {"mode": "stub", "payload": {"relevant": False, "reason": "fixture"}},
        "subagents": {"mode": "stub", "payload": {"results": [], "aggregated": {"avg_confidence": 0}, "roles_failed": []}},
    },
)
```

For a richer custom replay, add `backtests/custom_replay.py` that reads
`backtests/headline_fixture.jsonl`, calls `run(ctx)` once per headline, and
writes:

- `backtests/custom_replay_result.json`
- `backtests/custom_replay_report.md`
- `backtests/custom_replay_signals.csv`

The report must list what was replayed, what was stubbed, every ENTRY/HOLD,
and why this replay is still weaker than live paper trading.

## limits.yml

```yaml
max_single_order_usd: 50
max_total_exposure_usd: 250
daily_loss_usd: 25
max_drawdown_pct: 15.0
min_confidence: 0.55
max_slippage_bps: 100
max_stale_seconds: 90
approval_threshold_usd: 50
kill_switch: false
```

## Common gotchas

- **Skipping the light filter.** Every headline goes to the team =
  cost explodes. The filter must be tier=light and JSON-shaped.
- **Forgetting `mark_seen`.** Triggers can fire twice for the same
  headline; without dedup you'll vote twice.
- **Letting the team disagree silently.** If `roles_failed` is non-
  empty or any role vetoes, fall back to HOLD — partial team verdicts
  are not a "majority wins" situation.
- **Hard-coding `usd_budget`.** Set it on `team.run` so a runaway
  team can't drain the strategy budget.
- **Polymarket-specific:** `side` is `"yes"`/`"no"` and `qty` is
  notional USD, not contracts. Don't reuse the spot-equity intent
  shape verbatim.

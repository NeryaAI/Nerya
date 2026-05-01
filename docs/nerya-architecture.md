# Nerya architecture

Nerya is organized as **runtime + workspace**. Code is static and owned by
the repository; everything runtime (strategies, journals, sessions,
approvals, vault, proposals) lives in a workspace directory that the
operator owns.

Nerya runs its own kernel, skill runtime, LLM gateway, trigger router,
trading kernel, messaging pipeline and security services. There is **no
attached external agent runtime** — all of the following boxes are
Nerya-native code under `nerya/*`.

```
┌──────────────────────────── Nerya runtime ────────────────────────────┐
│                                                                        │
│   ┌──────────────────┐   ┌─────────────┐   ┌──────────────────────┐   │
│   │ Trigger router   │──►│ Nerya kernel│──►│ Skills runtime        │   │
│   │ (ingress plane)  │   │ (agent loop │   │ (registry + dispatch  │   │
│   │  schedule / user │   │  + planner) │   │  + permissions +      │   │
│   │  / webhook / …)  │   │             │   │  manifest checks)     │   │
│   └──────────────────┘   └──────┬──────┘   └───────────┬──────────┘   │
│                                 │                      │               │
│                                 ▼                      ▼               │
│   ┌─────────────┐   ┌─────────────┐   ┌──────────────────────────┐    │
│   │ LLM gateway │   │ Subagent    │   │  Trading kernel           │    │
│   │ (tiers +    │   │ runtime     │   │  (intents → Risk Gate →   │    │
│   │  budget +   │   │ + dispatch  │   │   Approval Gate →         │    │
│   │  adapters)  │   │             │   │   paper/live execution)   │    │
│   └─────────────┘   └─────────────┘   └──────────────┬──────────┘    │
│                                                       │               │
│   ┌─────────────┐   ┌─────────────┐   ┌──────────┐   │               │
│   │ Messaging   │   │ Security    │   │ External │   │               │
│   │ pipeline    │   │ (vault,     │   │ bridges  │   │               │
│   │             │   │  signer)    │   │ (MCP,    │   │               │
│   │             │   │             │   │  ACP)    │   │               │
│   │             │   │             │   │          │   │               │
│   │             │   │             │   │          │   │               │
│   └─────────────┘   └─────────────┘   └──────────┘   ▼               │
│                                         Strategy history + journals    │
└────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌───────────────────────── Nerya workspace (files only) ────────────────┐
│  state/ journals/ inbox/ outbox/ memory/ vault/ approvals/            │
│  strategies/<id>/{strategy.yml, limits.yml, history/, sessions/}      │
│  skills/{enabled.yml, installed/, pending/}                           │
│  scripts/{pending/, approved/, rejected/}                             │
│  evolution/proposals/                                                 │
└────────────────────────────────────────────────────────────────────────┘
```

The "External bridges" box above is implemented entirely by
`nerya/mcp/` (Model Context Protocol) and `nerya/acp/` (Agent
Communication Protocol). There is no Hermes-specific adapter: Hermes is
a reference capability source only and never runs inside Nerya. Kernel,
provider, skill, tool, cron, delegation, gateway and session
responsibilities all live in the Nerya-native modules shown in the
other boxes.

## Data flow for a single trading decision

1. **Source** — Trigger SDK or scheduler writes a `TriggerEvent` into `workspace/inbox/triggers/`.
2. **Route** — `triggers/router.py` matches the event to a target (main agent, subagent, or direct skill action) and writes `strategies/{id}/history/triggers.jsonl`.
3. **Context build** — For agent/subagent targets, `agent/context_builder.py` assembles the prompt under a **prompt firewall** that marks external strings as untrusted.
4. **Reasoning** — `nerya/agent/kernel.py` (the Nerya `AgentKernel`) runs the turn loop. Every LLM call is routed through `llm/gateway.py` (tier + budget) and logged to `journals/llm.jsonl`.
5. **Decision** — The agent emits a `TradeIntent` by calling `skill:trading.submit_trade_intent`. The skill writes `history/decisions.jsonl` and forwards to the trading kernel.
6. **Risk Gate** — `trading/risk.py` validates the intent against live status, limits, virtual ledger, confidence, slippage, staleness, duplicates and conflicts.
7. **Approval Gate** — If threshold breached, writes to `approvals/pending.jsonl` and stops.
8. **Execution** — `trading/execution.py` routes to paper (default) or live connector. Fills come back as `orders.jsonl` / `fills.jsonl`.
9. **Message** — `messaging/pipeline.py` pushes human-readable updates (tokens pulled from vault).
10. **Review & reflection** — `strategy_review_skill` and `agent/reflection.py` write `reviews.jsonl` and memory files.
11. **Evolution** — `evolution/*` can draft proposals. Proposals are files; applying them requires operator approval.

## Process boundaries

- **Agent context** — sees `secret_ref`, skill actions, redacted preview.
  Never sees provider keys, exchange keys, bot tokens, or raw `.env`.
- **Skill action** — can use `connectors/*`, `security/signer`,
  `messaging/pipeline`, but only within its declared `permissions`.
- **Script** — runs in `scripts/sandbox.py` with a whitelisted import set.
  Can only reach the outside world through the Nerya SDK.
- **Connector** — only reachable from skills and trading kernel, never from
  scripts or agent context.
- **Signer / vault** — only reachable from security-scoped code paths.

See the companion docs for more detail:

- `skill-first-trading.md`
- `trigger-sdk.md`
- `trading-sdk.md`
- `llm-gateway.md`
- `risk-gate.md`
- `strategy-history.md`
- `strategy-review.md`
- `memory-reflection-evolution.md`
- `script-system.md`
- `security.md`
- `runbook.md`

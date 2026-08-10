<!-- nerya-skill-frontmatter-start -->
---
name: team
description: "Use whenever the user asks to start an Agent Team, run a committee, do a deep multi-perspective research pass, or have several roles vote on a low-frequency strategy decision. Triggers on \"start an agent team\", \"form a committee\", \"deep research X\", \"have N roles analyse this\", \"low-frequency strategy decision\", \"multi-role analysis\". Tells the model how to pick roles, when to spawn parallel vs sequential, and how to manage persistent custom roles via the `role_save` / `role_delete` tools."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Agent Team playbook

A *team* is two or more roles working in parallel on a shared mission.
The dispatcher already handles bounded parallelism and the live-trading
denylist; your job is to pick the roles, write a single mission line,
and consume the aggregated finding.

## When to start a team

Start a team when **all** of these hold:

1. The decision genuinely benefits from multiple perspectives
   (research, risk, execution) — not when one role would suffice.
2. The answer is small enough to summarise per-role in a paragraph;
   if each role needs to write a chapter, you wanted a multi-turn
   investigation, not a team.
3. The mission is the same for every role — only the *lens* differs.

Do **not** start a team when:

- the user wants a fast scalp / single trade decision (use one
  subagent or no subagent at all),
- the work is mostly file edits (use the coding lane),
- the roles would each need a different mission (run them
  sequentially).

## Choosing roles

You have two pools:

- **Default roles** (always available — `market_analyst`,
  `risk_critic`, `execution_planner`, `onchain_watcher`,
  `news_interpreter`, `portfolio_manager`, `strategy_reviewer`,
  `coding_agent`, `code_critic`, `verification_lane`, `plan_lane`,
  `explore_lane`, `strategy_tuner`).
- **Persistent workspace roles** the operator (or you) authored via
  `role_save` and stored under
  `<workspace>/subagents/<name>.agent.md`.

For an explicit Agent Team request, call `team_run` directly with the smallest
useful inline role array. Use `role_list` or `role_get` only when a requested
role is ambiguous; discovery must not delay the team launch. Typical teams are
2–4 roles.

## Running a team

Use the native `team_run` tool. `roles` must be a real JSON array, not
a quoted/stringified JSON array:

```json
{
  "task": "Decide whether to enter NVDA at the current level — paper trading.",
  "roles": [
    {"name": "market_analyst", "payload": {"symbol": "NVDA", "tf": "1h"}},
    {"name": "risk_critic",    "payload": {"strategy_id": "ashare_test"}},
    {"name": "news_interpreter"}
  ],
  "shared_payload": {"window_hours": 12},
  "max_parallel": 3
}
```

Each role sees `task` under `=== team task ===`, then its merged
payload (`shared_payload` + per-role `payload`). The harness returns
a single envelope:

```json
{
  "task": "...",
  "roles_succeeded": ["market_analyst", "risk_critic"],
  "roles_failed":    ["news_interpreter"],
  "tokens_total":    8421,
  "usd_total":       0.18,
  "results":   [{...}, {...}],
  "failures":  [{...}],
  "aggregated": {"subagents": {...}, "avg_confidence": 0.74}
}
```

Read the **aggregated** block first; fall back to individual
`results[*].output` when a role's contribution matters on its own.

## Persistent roles (custom team members)

Operators and the model can both author durable roles. Use
`role_save` whenever the same brief will be reused across turns:

```json
{
  "name": "ashare_quant_lead",
  "prompt": "# A-Share Quant Lead\nYou lead a research squad covering CN A-shares. Output JSON: {decision: ENTRY|HOLD|EXIT, drivers: string[], confidence: 0..1}.",
  "allowed_skills": ["market_data", "news_social", "web_search_fetch", "coding", "llm"],
  "tier": "medium"
}
```

Rules:

- Names match `[A-Za-z0-9_]+`.
- The prompt must spell out the deliverable schema — vague prompts
  produce vague output.
- The dispatcher denylist still blocks live-trading skills regardless
  of `allowed_skills`. Don't add `trading_write` / `wallet` /
  `script_runtime` — they will be filtered.
- `role_delete` removes a workspace role. Default roles can't be
  deleted (they live in the dispatcher).

Inspect with `role_list` (every role) or `role_get` (one role
including the prompt body).

## Running long / deep teams

For a deep-research team that should *not* block your turn, hand it
off to the orchestrator-driven `/teams/run` (HTTP) or use the
`team_orchestrator` skill instead — that surface persists tasks /
blackboard state to disk and survives kernel restarts. `team_run`
(this skill) is for in-loop, one-shot multi-role decisions; the
orchestrator is for hours-long shared workspaces.

## Failure modes

- **Picking 7 roles when 3 would do.** Each extra role adds latency and
  tokens; cap at the smallest viable set.
- **Letting one role dictate the conclusion.** Read every successful
  role's output before deciding — that's the *point* of a team.
- **Role definitions drift.** When `role_save` rewrites a prompt,
  ongoing async tasks already running with the old prompt aren't
  retroactively updated; restart them if necessary.

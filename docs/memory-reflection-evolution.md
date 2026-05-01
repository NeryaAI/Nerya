# Memory, reflection and evolution

Nerya has three distinct long-memory surfaces that serve different
purposes.

## Memory

`workspace/memory/`:

| File | Owner | Read/write |
|---|---|---|
| `global.md` | Runtime-wide | Agent reads, reflection writes |
| `mistakes.md` | Runtime-wide | Reflection writes, agent reads |
| `market_regimes.md` | Reflection + news_social skill | Reflection + weekly review write |
| `skill_learnings.md` | Reflection | Reflection writes |
| `strategy_learnings/<strategy_id>.md` | Per strategy | Reflection + strategy_review write |

Memory is always plain markdown. Agents read it into the prompt through
`agent/context_builder.py`, with a size budget and the prompt firewall.

## Reflection

`nerya/agent/reflection.py` is invoked:

- After every session that has a closed outcome
- Before a planning step in the main agent loop, reading the last N relevant entries
- By the `nerya reflect` CLI, which refreshes memory files from the last N sessions

Reflection never writes to `limits.yml`, `accounts.yml`, `vault/`, or
`signer.policy`. It only writes to the memory markdowns and optionally
enqueues an evolution proposal.

## Evolution

`nerya/evolution/` turns reflection + strategy_review output into
**proposals**. Seven proposal kinds:

- `learning_update` — markdown patch to a memory file
- `prompt_patch` — unified diff of an agent/subagent prompt
- `script_proposal` — new script + manifest in `workspace/scripts/pending/`
- `skill_proposal` — new skill directory in `workspace/skills/pending/`
- `trigger_route_patch` — unified diff to `workspace/triggers/routes.yml`
- `strategy_config_patch` — unified diff to `strategies/<id>/strategy.yml` (**not** `limits.yml`)
- `risk_limit_suggestion` — advisory only — writes `proposals/<id>/suggested_limits.yml` but cannot mutate `limits.yml`

Proposal directory:

```
workspace/evolution/proposals/<proposal_id>/
  proposal.yml
  rationale.md
  diff.patch            (if applicable)
  test_plan.md
  rollback.md
  new_script.py         (if script_proposal)
  new_skill/            (if skill_proposal)
```

Proposal lifecycle states: `draft`, `pending_review`, `approved`,
`applied`, `rejected`, `rolled_back`.

## Immutable boundaries

`evolution/promotion.py` refuses to apply any diff that touches:

- `accounts/accounts.yml`
- `accounts/secrets.refs.yml`
- `strategies/*/limits.yml` (except when advisory — suggestion only)
- `security/**`, `vault/**`
- `nerya.yml > security`, `nerya.yml > live_trading_enabled`

A proposal that claims to modify any of those immediately transitions to
`rejected` with `reason: protected_scope`.

## CLI

- `nerya reflect` — run reflection on recent sessions
- `nerya evolve` — run evolution over reflection outputs
- `nerya proposals list`
- `nerya proposals show <id>`
- `nerya proposals approve <id>`
- `nerya proposals apply <id>`
- `nerya proposals rollback <id>`

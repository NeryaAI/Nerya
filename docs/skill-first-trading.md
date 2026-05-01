# Skill-first trading (superseded)

> **Status (2026-04-28): superseded.**
>
> - The `skill.yml + actions.py` layout described below is no longer
> supported. All shipped skills now follow the Anthropic Skill spec
> (`SKILL.md` frontmatter + standalone scripts under `scripts/`); the
> agent reads the markdown body and invokes any helper scripts via
> the native `run_shell` tool. See `docs/plans/2026-04-25-skill-md-only-migration-plan.md`
> and `nerya/skills/manifest.py` for the live format.
> - Trading-kernel actions (submit / cancel / portfolio / risk_check /
> kill_switch) are now native tools registered in
> `nerya/tools/native/trading.py`, not skill actions. Generated
> strategies invoke them through the Trading SDK / `StrategyContext`
> (see `docs/plans/2026-04-28-agent-generated-strategy-runtime-refactor.md`),
> which still routes every order through Risk Gate, Approval Gate,
> and the existing journal/strategy-history pipeline.

The original document is preserved below for historical context only.
Do not use it as a guide for new code.

---

## Skill anatomy (legacy, do not follow)

```
nerya/skills/builtin/trading_skill/
  skill.yml     # manifest: id, version, actions, permissions, schemas, gates
  README.md     # human description + examples
  actions.py    # Python impl (actions are async functions)
  tests/        # skill-local tests
```

`skill.yml` keys:

- `id`, `version`, `title`, `description`
- `permissions`: free-form list like `trading.submit`, `accounts.read`, `portfolio.read`.
- `actions[]`: each has `name`, `input_schema`, `output_schema`, `risk_gate`, `approval_gate`, `journal`, `context_policy`.
- `context_policy`: controls what slice of context the skill can see (`scoped_strategy`, `global`, `subagent_only`). Used by the subagent dispatcher to prevent cross-strategy leakage.
- `risk_gate`: `required | optional | n/a`. When `required`, `skills/runtime.py` refuses to dispatch without an active Risk Gate verdict.
- `approval_gate`: `n/a | threshold | always`.

## Built-in skills (the 12 mandated)


| Skill             | Key actions                                                                                                                                                                              | Risk                          | Approval                 | Notes                                                                                                                           |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| `market_data`     | `fetch_price`, `fetch_candles`, `summarize_market`, `calculate_features`, `compress_context`                                                                                             | n/a                           | n/a                      | Resolves real public connectors by default (ccxt + native). Mock candles only when `NERYA_ALLOW_MOCK_DATA=1` is explicitly set. |
| `trading`         | `submit_trade_intent`, `cancel_order`, `get_order_status`, `get_strategy_history`                                                                                                        | **required** on submit/cancel | threshold                | Only entrypoint that reaches the trading kernel.                                                                                |
| `portfolio`       | `get_portfolio_summary`, `get_virtual_ledger`, `get_positions`, `get_pnl`                                                                                                                | n/a                           | n/a                      | Read-only.                                                                                                                      |
| `risk`            | `check_intent`, `explain_risk_decision`, `enable_kill_switch`, `disable_kill_switch`                                                                                                     | n/a                           | always on disable        | Kill switch is always-approval.                                                                                                 |
| `trigger`         | `emit_trigger`, `dry_run_route`, `list_routes`, `manage_routes`                                                                                                                          | n/a                           | threshold on manage      | Emits flow through router.                                                                                                      |
| `llm`             | `classify`, `compress`, `extract_json`, `analyze_signal`, `generate_script_proposal`                                                                                                     | n/a                           | threshold on `high` tier | Enforces tier + budget.                                                                                                         |
| `script`          | `generate_script_proposal`, `static_analyze_script`, `approve_script`, `run_script`, `schedule_script`                                                                                   | optional                      | always on approve/run    | Running a pending script is rejected.                                                                                           |
| `message`         | `create_message_request`, `send_message`, `list_messages`                                                                                                                                | n/a                           | threshold                | Tokens pulled from vault inside pipeline.                                                                                       |
| `strategy_review` | `review_trade`, `review_trade_after_delay`, `review_strategy_history`, `explain_trade`, `find_bad_triggers`, `find_subagent_mistakes`, `generate_learning_proposal`                      | n/a                           | n/a                      | Read/reason only.                                                                                                               |
| `evolution`       | `generate_learning_update`, `generate_prompt_patch`, `generate_script_proposal`, `generate_skill_proposal`, `generate_trigger_route_patch`, `generate_strategy_config_patch`, `rollback` | n/a                           | always on apply/rollback | Writes proposal files, does not apply live.                                                                                     |
| `onchain`         | `get_balance`, `simulate_swap`, `prepare_signed_tx`, `broadcast_tx`                                                                                                                      | n/a                           | always on broadcast      | Signing goes through `security/signer.py`.                                                                                      |
| `news_social`     | `fetch_news`, `fetch_social`, `classify_news`, `extract_signal`                                                                                                                          | n/a                           | n/a                      | External strings are marked untrusted.                                                                                          |


## What skill-first buys us

- **Deny-by-default tool surface** — the agent only sees `skill.<id>.<action>` with a typed `input_schema`. Anything else is rejected by `nerya/skills/runtime.py` (manifest + permission check). There is no separate tool-middleware layer — the skill runtime itself is the gate.
- **Uniform journal** — every skill call writes to `journals/skills.jsonl`, `skill_calls.jsonl` per strategy, plus any domain journal (`orders.jsonl`, `messages.jsonl`, ...).
- **Uniform permission check** — `skills/permissions.py` enforces declared permissions before dispatch.
- **Uniform review surface** — `strategy_review_skill` can consume a skill call the same way regardless of whether it's a trade, a message or a risk check.


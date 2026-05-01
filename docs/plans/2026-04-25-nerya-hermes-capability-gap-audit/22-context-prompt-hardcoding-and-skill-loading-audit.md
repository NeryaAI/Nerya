# Context / Prompt Hardcoding And Skill-Loading Audit

This audit focuses on the user's specific concern: Nerya still embeds too much tool/action capability directly into context prompts and Python fallback maps. Tool capability should be loaded from built-in skill manifests and rendered dynamically from the selected skills for the current turn, not hardcoded into `context_builder.py`.

## Status: COMPLETED (2026-04-25)

The hardcoded action catalog inside `context_builder.py` has been removed. The main-agent prompt now sources action names, hints, payload shapes, gates, and read-only flags from the live skill manifests via a new `build_action_catalog()` helper.

Verification:
- `Nerya/nerya/skills/manifest.py` lines 13–69: `ActionSpec` now carries `agent_hint`, `agent_payload_hint`, `agent_visibility`, and the new `agent_query_only` boolean (used by the kernel safety net to distinguish read-only queries from mutations).
- `Nerya/nerya/agent/kernel.py` `build_action_catalog()` (search for the function): walks every loaded skill manifest, returns a list of `{alias, skill_id, action, hint, payload_hint, visibility, risk_gate, approval_gate, query_only}` dicts. The static `ACTION_MAP` only survives as a backwards-compatibility fallback (`build_action_map(base=ACTION_MAP, skills=…)`).
- `Nerya/nerya/agent/context_builder.py` `_render_action_catalog()` and `build_context(..., action_catalog=…)`: the entire `### supported action names …` block is now generated at runtime from the catalog. The legacy hardcoded prose (40+ static action descriptions) was deleted; `_legacy_build_context_removed` is a sentinel that raises a clear error if anyone tries to import the old block.
- `Nerya/tests/test_strategy_skill.py::test_context_builder_advertises_create_strategy` no longer scrapes `context_builder.py` source; it boots a `SkillKernel`, builds the catalog, and asserts `create_strategy` and `set_strategy_status` end up in the rendered prompt.
- All built-in skills now carry `agent_hint` + `agent_payload_hint` (and read-only ones carry `agent_query_only: true`): see `Nerya/nerya/skills/builtin/*/skill.yml`.

## Status (2026-04-25, second pass): reply-text + payload extraction hardening

After the catalog refactor we ran a 19-case natural-language prompt battery against the running service (see `tmp/run_prompt_suite.py`). The runs surfaced three downstream issues that all traced back to "the new dynamic catalog renders payload shapes inside `payload={...}` blocks, but the kernel was still assuming the LLM emitted inline fields". Without these follow-up fixes the operator-visible reply was either empty, contained the LLM's internal chain-of-thought, or leaked memory snapshots.

What was fixed:

1. **Three accepted shapes for `send_message`**: `nerya/agent/kernel.py` `_message_payload` now unwraps `action_dict["payload"]` (the catalog-natural shape) in addition to the legacy inline / `message:` shapes, strips the inner stale `payload` key, and stops falling back to `decision.reasoning` when `text` is missing. Reasoning is the model's chain-of-thought and must never become the user-visible reply.
2. **Passthrough builder accepts inline + nested**: `nerya/agent/payload_builders.py` `passthrough_builder` no longer returns `{}` when the LLM emits inline fields like `{"action":"get_ticker","market":"BINANCE:BTCUSDT"}` — it now uses inline keys (minus reserved envelope keys `action`/`reasoning`/`continue`/`replan`) when no `payload` block is present. This was the root cause of the `IntentValidationError: market_data.get_ticker input invalid` storm during the trading-signal case.
3. **Dotted action-name aliasing**: `nerya/agent/kernel.py` action dispatch now treats `"market_data.get_ticker"` as `"get_ticker"` when the prefix matches a known skill_id, so a model that copied the catalog's `[skill_id]` group header is auto-corrected instead of failing.
4. **Catalog renderer makes the rule explicit**: `nerya/agent/context_builder.py` `_render_action_catalog` now spells out "the action NAME is the value in quotes; `[skill_id]` lines are visual grouping only — DO NOT emit the dotted form".
5. **Reply-text extractor is `send_message`-only**: `nerya/api/routes_agent.py` `agent_reply_text` no longer mines `result.text` from arbitrary actions (recall, list_messages, get_social_signals, ...) — that fallback was leaking raw memory dumps as the chat reply when the LLM forgot to wrap its answer in `send_message`. The same module's `_payload_text` now also descends into `payload.payload.text`.
6. **Default `max_iterations` raised to 5** in `nerya/agent/kernel.py` (was 3). 3 was too tight for "fetch → fetch → summarise → reply" turns: the model spent every iteration on data collection and never reached `send_message`. Test contract in `tests/test_agent_loop.py::test_kernel_max_iterations_is_bounded` updated to mirror.
7. **Explicit `stopped_reason`**: when the chat safety net would have re-planned but we are at the cap, the kernel now surfaces `stopped_reason="needs_summarisation"` instead of returning silently with `None`. When every read-only call in the final batch errored we surface `stopped_reason="tool_errors_no_reply"`.
8. **`agent.harness.max_iterations: 6`** added to the running config at `~/.nerya/nerya.yml` so the live service uses the bumped budget without waiting for redeploys.

Verification:
- 19/19 prompt-battery cases now produce on-topic Chinese/English replies; see `tmp/prompt_suite_final2.json`. Notable transitions:
  - `trading_signal_cn`: previously 12 read-only calls + no reply → now 6 calls + full BTC/ETH signal write-up.
  - `memory_remember_cn`: previously leaked reasoning ("The operator asked to remember…") → now Chinese confirmation.
  - `memory_recall_cn`: previously 6 looped `recall` calls + max-iterations + leaked memory dump → now 1 recall + Chinese summary.
- Targeted unit tests pass: `tests/test_agent_actions.py`, `tests/test_agent_loop.py`, `tests/test_agent_prompt_driven_e2e.py`, `tests/test_skill_manifest.py`, `tests/test_strategy_skill.py`, `tests/test_memory_skill.py`, `tests/test_architecture_audit.py`, `tests/test_scheduled_session_delivery.py`, `tests/test_agent_readonly_actions.py`, `tests/test_skill_schema_phase6.py`, `tests/test_capability_developer_skill.py`, `tests/test_exchange_skill.py`, `tests/test_api_auth.py` — 143 passed.

## Verdict

Nerya has started moving in the right direction with `agent_action` and `agent_payload_builder` in skill manifests, but the main-agent context is still heavily hardcoded. The worst offender is `nerya/agent/context_builder.py`, which enumerates supported action names, required fields, read-only query actions, natural-language heuristics, and trace-tool requirements directly in prompt text. That makes the runtime brittle: adding/changing a skill requires changing Python prompt text, and the prompt can advertise actions that are not actually selected, installed, or enabled.

## Evidence

### Existing registry-driven foundations

- `nerya/skills/manifest.py:35` defines `agent_action` and `agent_payload_builder` as manifest-level action metadata.
- `nerya/agent/payload_builders.py:4` explicitly says this was introduced to move routing out of `ACTION_MAP`.
- `nerya/agent/kernel.py:366` builds a live action map by merging static fallback with manifest-declared actions.
- `nerya/agent/kernel.py:371` says manifest actions take precedence over static fallback.
- `nerya/skills/kernel.py:15` boots skills through `SkillRegistry.load_builtin` and `SkillRuntime`.
- Some built-in skills already declare `agent_action`, for example `market_data`, `exchange`, `portfolio`, `strategy` read actions, `subagent` read actions, and `capability_developer` proposal actions.

### Main context hardcoding

- `nerya/agent/context_builder.py:191` starts the static instruction block.
- `nerya/agent/context_builder.py:201` hardcodes “Supported action names and their required fields”.
- `nerya/agent/context_builder.py:202` through `nerya/agent/context_builder.py:229` hardcode many specific actions such as `submit_trade_intent`, `send_message`, `propose_script`, `create_subagent`, `add_schedule`, `propose_prompt_patch`, `append_learning`, `create_strategy`, `set_strategy_status`, `explain_turn`, and `list_recent_turns`.
- `nerya/agent/context_builder.py:231` starts another hardcoded read-only action section.
- `nerya/agent/context_builder.py:233` through roughly `nerya/agent/context_builder.py:280` hardcode query actions like `get_ticker`, `get_mark_price`, `get_candles`, `summarize_market`, `get_klines`, strategy/subagent reads, etc.
- `nerya/agent/context_builder.py:312` through `nerya/agent/context_builder.py:336` hardcode natural-language action-selection heuristics.
- `nerya/agent/context_builder.py:338` through `nerya/agent/context_builder.py:353` hardcode trace-specific mandatory behavior and claim the trace skill is always available.
- `nerya/agent/context_builder.py:357` says only use actions whose backing skill is enabled, but the prompt itself is not generated from the selected enabled skill set.

### Action routing hardcoding

- `nerya/agent/kernel.py:141` documents “Canonical action name -> (skill_id, action_name, payload_builder)”.
- `nerya/agent/kernel.py:314` defines `ACTION_MAP` statically.
- Static entries include `submit_trade_intent`, `send_message`, `propose_script`, `create_subagent`, `add_schedule`, `create_strategy`, `set_strategy_status`, and `explain_turn` at `nerya/agent/kernel.py:315` through `nerya/agent/kernel.py:330`.
- `nerya/agent/kernel.py:343` through `nerya/agent/kernel.py:358` registers Python payload builders in code.
- `nerya/agent/kernel.py:375` still starts from `dict(base or ACTION_MAP)`, so static fallback remains active even if manifests are incomplete.

### Planner and subagent defaults

- `nerya/agent/planner.py:105` has a hardcoded emergency fallback skills list: `market_data`, `trading`, `message`.
- `nerya/agent/planner.py:190` repeats a similar fallback in `explain_plan`.
- `nerya/core/config.py:138` stores default planner routes in Python `DEFAULT_CONFIG`; it is configurable, but still not skill-discovery-driven.
- `nerya/core/config.py:223` through `nerya/core/config.py:230` define a broad `user_chat` skill allowlist in default Python config.
- `nerya/subagents/registry.py:29` defines `DEFAULT_SUBAGENT_SKILLS` in Python for named subagent roles.

### Subagent context is partially fixed but still has fallbacks

- `nerya/subagents/context_policy.py:1` documents that historical hardcoding was removed.
- `nerya/subagents/context_policy.py:74` enumerates context providers by manifest capability tags.
- However, `nerya/subagents/context_policy.py:111` falls back to `market_data.compress_context` if no `context.market` provider exists.
- `nerya/subagents/context_policy.py:145` falls back to `news_social.get_recent_news` if no `context.news` provider exists.
- These are acceptable compatibility fallbacks short-term, but they should become migration warnings and eventually be removed.

### Subagent prompt hardcoding

- `nerya/subagents/runtime.py:262` through `nerya/subagents/runtime.py:266` hardcode the JSON skill-call format and allowed-skill wording.
- `nerya/subagents/runtime.py:270` hardcodes `You are the {spec.name} subagent`.
- This is less severe than main-agent context hardcoding because it is generic and uses allowed skill IDs, but the prompt should still render tool schemas/descriptions from manifests instead of only listing skill IDs.

## Concrete Problems

### P0 Problems

1. **Prompt advertises actions independently from selected skills.** The model sees a long static list of actions even when selected skills for the turn do not include those actions. The kernel later rejects disallowed skills, but user experience degrades because the prompt taught the model wrong affordances.
2. **Adding a new built-in skill is not enough.** A skill can declare `agent_action`, but unless the prompt dynamically renders that action, the main agent may not discover it reliably.
3. **Tool descriptions are duplicated.** Action names and field descriptions live in both skill manifests and `context_builder.py` prompt text. Drift is guaranteed.
4. **Trace skill is claimed as always available.** `context_builder.py` says trace is always available/read-only, but skill selection still controls `selected`; this should be derived from selected skill manifest state.
5. **ACTION_MAP remains a silent fallback.** It keeps old actions alive even if their skill manifest does not declare `agent_action`, making it hard to know which capabilities are real manifest-backed APIs.

### P1 Problems

1. **Planner routes are configured but not capability-driven.** Default route skill lists are static config, not computed by tags like `capability.market`, `capability.message`, `capability.trace`, or `agent.default_chat`.
2. **Subagent roles have hardcoded default allowed skills.** `DEFAULT_SUBAGENT_SKILLS` should move to role manifests or workspace config.
3. **Context snapshots bypass skill capability tags.** Main context directly pulls recent strategies, portfolio, trade defaults, market snapshot from helper functions instead of a general “context providers” skill tag system.
4. **Natural-language heuristics are hardcoded in prompt.** “Current price -> get_ticker”, “news -> get_recent_news”, etc. should come from skill examples/use-cases in manifests or a routing policy registry.
5. **Payload builders are Python-only.** `agent_payload_builder` names map to Python functions; this is fine for complex transforms but lacks schema-level declarative field mapping for simple cases.

### P2 Problems

1. **Prompt sections are not modular.** There is no prompt assembly pipeline that separately loads role instructions, selected tool schemas, context providers, policy constraints, memory, and examples.
2. **No manifest-generated action help in dashboard/gateway.** The same dynamic manifest data should power `/help`, dashboard command palette, and model prompt.
3. **No “prompt contract test” proving prompt only contains selected tools.** Tests should assert that disabled/unselected skills do not appear in the model prompt.

## Required Architecture Change

### 1. Split context into data context and capability context

`build_context` should not know action names. It should only assemble:

- trigger/user message,
- selected contextual data,
- memory preview,
- policy/safety constraints,
- references/artifacts.

A new component should render capability context from selected skill manifests:

- `SkillKernel.registry` -> selected skill IDs -> action specs,
- include `agent_action`, action description, input schema, risk/approval flags, tags, examples,
- exclude actions without `agent_action` unless explicitly requested for raw skill-call mode,
- render only actions actually enabled for this turn.

### 2. Make skill manifests the source of truth

Each built-in skill action that the main agent may call should declare:

```yaml
actions:
  - name: send_message
    description: Send an operator-visible message.
    agent_action: send_message
    agent_payload_builder: message
    agent_examples:
      - when: User asks for a final answer after tool results.
        action: {action: send_message, text: "...", channel: chat}
    agent_readonly: false
    risk_gate: false
    approval_gate: false
```

For read-only query tools:

```yaml
agent_action: get_ticker
agent_payload_builder: passthrough
agent_readonly: true
agent_use_when:
  - current price
  - quote
  - spread
```

### 3. Replace static prompt action list with generated action catalog

Main prompt should say:

- Reply with strict JSON.
- You may choose only one of the actions listed in “Available actions for this turn”.
- Then render a generated list:
  - action alias,
  - backing skill/action,
  - schema summary,
  - risk/approval/read-only flags,
  - short use-when hints from manifest.

No action should appear if its skill is not selected and enabled.

### 4. Convert static `ACTION_MAP` into migration fallback only

Target state:

- `ACTION_MAP` should be removed or gated behind `agent.allow_static_action_fallback: false` by default.
- Missing manifest metadata should fail tests for built-in skills.
- During transition, static entries can exist only for backward compatibility with warnings in dev logs.

### 5. Move default subagent capabilities to manifests/config

Replace `DEFAULT_SUBAGENT_SKILLS` with:

- `workspace/subagents/<name>.agent.yml`, or
- built-in role manifests under `nerya/subagents/builtin/*.yml`, or
- skill tags such as `subagent.market_analyst.default`.

### 6. Generalize context providers

Subagent context already does this partially with `context.market` and `context.news`. Main context should use the same model:

- `context.strategy_roster`,
- `context.portfolio`,
- `context.trade_defaults`,
- `context.memory`,
- `context.market`,
- `context.gateway_session`,
- `context.artifacts`.

Each provider should come from a skill action or a dedicated context provider registry, not ad-hoc helper calls inside `context_builder.py`.

## Specific Built-In Skill Metadata Gaps

These static `ACTION_MAP` actions should be moved into skill manifests or completed where missing:


| Agent action                    | Static map target                          | Current manifest status                         | Required fix                                              |
| ------------------------------- | ------------------------------------------ | ----------------------------------------------- | --------------------------------------------------------- |
| `submit_trade_intent`           | `trading.submit_trade_intent`              | not observed in manifest scan as `agent_action` | add `agent_action` + builder to `trading_skill/skill.yml` |
| `send_message`                  | `message.send_message`                     | not observed as `agent_action`                  | add `agent_action` + builder to `message_skill/skill.yml` |
| `propose_script`                | `evolution.generate_script_proposal`       | not observed as `agent_action`                  | add to `evolution_skill/skill.yml`                        |
| `create_subagent`               | `subagent.create_subagent`                 | action exists but no `agent_action` observed    | add `agent_action` + builder                              |
| `add_schedule`                  | `trigger.add_schedule`                     | not observed as `agent_action`                  | add `agent_action` + builder                              |
| `propose_prompt_patch`          | `evolution.generate_prompt_patch`          | not observed as `agent_action`                  | add metadata                                              |
| `append_learning`               | `evolution.append_learning`                | not observed as `agent_action`                  | add metadata                                              |
| `propose_strategy_config_patch` | `evolution.generate_strategy_config_patch` | not observed as `agent_action`                  | add metadata                                              |
| `create_strategy`               | `strategy.create`                          | action exists but no `agent_action` observed    | add `agent_action` + builder                              |
| `set_strategy_status`           | `strategy.set_status`                      | action exists but no `agent_action` observed    | add `agent_action` + builder                              |
| `explain_turn`                  | `trace.explain`                            | not observed as `agent_action`                  | add to `trace_skill/skill.yml`                            |
| `list_recent_turns`             | likely `trace.list_recent_turns`           | not observed in scan                            | add metadata if action exists                             |


## Tests To Add

1. Disabled skill is not listed in main prompt available actions.
2. Unselected but installed skill is not listed in main prompt for a route.
3. Every built-in `agent_action` resolves to an installed skill action.
4. Every static compatibility action has an equivalent manifest `agent_action` before static fallback can be disabled.
5. Main prompt action catalog is generated from selected skill manifests, not hardcoded strings.
6. Adding a new built-in skill with `agent_action` makes it available without editing `context_builder.py`.
7. Removing `agent_action` from a skill removes it from prompt and action map.
8. Trace action is only advertised when trace skill is selected.
9. Subagent context providers are discovered by `context.*` tags and fallback emits migration warning.
10. Strategy/portfolio/trade-default context providers can be disabled by policy/config and disappear from context.

## Suggested Implementation Order

1. Add `agent_action` metadata to missing built-in skill manifests.
2. Create `nerya/agent/action_catalog.py` that builds selected action catalog from `SkillKernel` + selected skills.
3. Modify `build_context` signature to accept `available_actions` or a rendered action catalog; remove hardcoded action list from `context_builder.py`.
4. Update `AgentKernel.run_turn` to select skills before building context and pass generated action catalog into context builder.
5. Make `build_action_map` manifest-first and emit warnings for static fallback use.
6. Add tests proving prompt/action map are manifest-driven.
7. Move subagent default skill maps out of Python into role/config manifests.
8. Replace main-context direct snapshots with context provider tags.

## Bottom Line

The user's concern is valid. Nerya still hardcodes too much tool capability into prompt/context, especially in `context_builder.py`. The correct architecture is: built-in skill manifests declare callable agent actions and context-provider tags; the planner selects skill IDs; the prompt renders only selected manifest-backed actions; the kernel dispatches through `SkillRuntime`; static Python maps become migration fallbacks only.
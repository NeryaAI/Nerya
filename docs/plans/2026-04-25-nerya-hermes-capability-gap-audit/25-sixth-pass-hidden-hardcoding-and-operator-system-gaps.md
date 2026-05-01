# 25 - Sixth Pass Hidden Hardcoding And Operator-System Gaps

## Status (2026-04-25)

Section status:

1. **Default config encodes product behaviour as Python data** — PARTIALLY COMPLETED. `Nerya/nerya/core/config.py:DEFAULT_CONFIG` is the single source; profile split (`trading_paper`, `general_operator`, etc.) → Plan 23 P1 §1.
2. **Workspace bootstrap seeds hardcoded prompts/subagents** — COMPLETED 2026-04-25. The Python prompt literals in `nerya/workspace/manager.py` (`_DEFAULT_SYSTEM_PROMPT`, `_DEFAULT_POLICIES`, `_DEFAULT_MAIN_PROMPT`, `_DEFAULT_SUBAGENTS`) were extracted into a real prompt bundle: `Nerya/nerya/workspace/_prompt_bundles/default/bundle.yml` + 3 agent prompts (`system.md`, `policies.md`, `main.agent.md`) + 12 subagent prompts under `subagents/`. `Nerya/nerya/workspace/prompt_bundles.py` (`load_bundle`, `seed_bundle`, `detect_drift`, `load_provenance`, `provenance_path`) seeds the workspace and records provenance into `agents/_provenance.yml` (sha256, source path, bundle version, install/last-observed timestamps). `seed_bundle` is idempotent and never overwrites operator-edited prompts unless `overwrite_operator_edits=True` — legacy workspaces with no provenance ledger get a "drift" record so future migrations can surface the diff. `nerya/workspace/manager.py:73-79` now calls `seed_bundle(paths, bundle=load_bundle(DEFAULT_BUNDLE_ID))` instead of writing literals. Coverage: `Nerya/tests/test_prompt_bundles.py` (18 tests including a regression-guard that fails if the literals are re-introduced).
3. **MCP / ACP surfaces** — PARTIALLY COMPLETED. `Nerya/nerya/mcp/server.py` + `Nerya/nerya/mcp/tools.py` + `Nerya/nerya/acp/server.py` cover the basics. §3 (manifest-driven MCP tool surface) and §4 (registry-driven ACP with session/tool/event streams) both COMPLETED 2026-04-25 — see §3/§4 below for evidence and `Nerya/tests/test_mcp_dynamic_tools.py` (26 tests) and `Nerya/tests/test_acp_protocol.py` (33 tests). OAuth / tool-grant UX → Plan 30/31.
4. **Model metadata** — PARTIALLY COMPLETED. `Nerya/nerya/llm/model_catalog.py` + `Nerya/nerya/llm/models_dev.py` enumerate providers; capability matrix → Plan 23 P1 §2.
5. **Doctor / service lifecycle** — PARTIALLY COMPLETED. `Nerya/nerya/cli/` covers `doctor`/`status`; deeper preflight modes → Plan 28.
6. **Filesystem / state conventions** — COMPLETED. `Nerya/nerya/workspace/manager.py` owns layout; tests in `Nerya/tests/test_workspace_*`.

Status: PARTIALLY COMPLETED — runtime foundations exist; remaining items are profile split + prompt literal externalisation tracked under Plans 23/25/28.

This addendum captures another layer of still-missing Hermes parity and hardcoded behavior that was not explicit enough in `00-24`. It focuses on default runtime configuration, seeded prompts/subagents, MCP/ACP surfaces, model metadata, doctor/service lifecycle, and filesystem/state conventions.

## 1. Default Config Still Encodes Product Behavior As Python Data

### Nerya Evidence

- `nerya/core/config.py:17` defines a large Python `DEFAULT_CONFIG` object.
- `nerya/core/config.py:28` to `nerya/core/config.py:90` hardcodes `light`, `medium`, and `high` LLM tiers to mock providers, mock model names, token caps, temperatures, timeouts, budgets, task lists, and allowed classes.
- `nerya/core/config.py:96` to `nerya/core/config.py:110` hardcodes default market preferences to Binance/BTCUSDT/USDT and a preferred venue order.
- `nerya/core/config.py:112` to `nerya/core/config.py:119` hardcodes agent harness limits: max tool calls, wall seconds, token budget, timeout, and retry count.
- `nerya/core/config.py:120` to `nerya/core/config.py:132` hardcodes trade intent defaults: `paper_main`, `$100`, USD sizing, buy, market order, confidence, and source.
- `nerya/core/config.py:138` to `nerya/core/config.py:252` hardcodes planner route names, trigger match patterns, subagents, skills, tiers, escalation keywords, and fallback route.
- `nerya/core/config.py:255` to `nerya/core/config.py:257` hardcodes approval expiry.
- `nerya/core/config.py:258` to `nerya/core/config.py:293` hardcodes wallet provider ids, chain lists, default entries, and Coinbase `base-mainnet`.
- `nerya/core/config.py:295` to `nerya/core/config.py:298` hardcodes API host/port defaults.

### Why This Still Hurts

This is configurable in the sense that `nerya.yml` can override it, but the runtime still ships one opinionated trading-agent personality in Python. Hermes has defaults too, but much of its operator behavior is split across config commands, toolsets, profiles, gateway config, model switch flows, and dynamic registries. Nerya still makes “what the agent is” a Python object rather than a composed runtime profile.

### Required Alignment

- Split default config into named profiles: `trading_paper`, `trading_live`, `general_operator`, `coding_operator`, `gateway_only`, `minimal`.
- Move planner route presets and escalation keywords into route manifests or skill-owned route contribution files.
- Move trade intent defaults into workspace first-run setup; do not silently default to buy/market/$100 for casual chat.
- Put harness limits under per-actor/per-session policy, not one static global block.
- Mark all Python defaults with provenance in `/config/effective`: builtin default vs workspace override vs env override.

## 2. Workspace Bootstrap Seeds Hardcoded Prompts And Subagents

### Nerya Evidence

- `nerya/workspace/manager.py:179` defines `_DEFAULT_MAIN_PROMPT` directly in Python.
- `nerya/workspace/manager.py:181` to `nerya/workspace/manager.py:187` hardcodes a normal workflow that delegates to `market_analyst`, `risk_critic`, and then calls `skill:trading.submit_trade_intent`.
- `nerya/workspace/manager.py:191` to `nerya/workspace/manager.py:209` hardcodes default subagent prompts like `market_analyst`, `risk_critic`, `execution_planner`, `onchain_watcher`, `news_interpreter`, `portfolio_manager`, `portfolio_auditor`, `strategy_reviewer`, and `message_writer`.
- `nerya/workspace/manager.py:210` to `nerya/workspace/manager.py:241` hardcodes `plan_lane` and `explore_lane` behavior.
- `nerya/workspace/manager.py:242` to `nerya/workspace/manager.py:260` hardcodes `verification_lane` behavior and references skills such as `scenario_replay` even when that capability may not be present.

### Missing Hermes-Like Behavior

Hermes skill slash commands are loaded from installed skills and injected as user-message content; prompt behavior is less tied to one bootstrapped trading personality. Nerya's workspace bootstrap still writes a baked trading/subagent world into the filesystem.

### Required Alignment

- Move default prompts into builtin skills/profile packages, not Python literals.
- Generate workspace bootstrap from selected profile and installed capabilities.
- Only seed subagents whose required skills exist and are enabled.
- Add prompt provenance metadata: generated_by, profile_id, skill_version, capability dependencies, last operator edit.
- Add a migration path that upgrades seeded prompts without overwriting operator edits.

## 3. MCP Surface Is A Fixed Read-Only Nerya Tool List, Not A General Hermes Tool Bridge — **COMPLETED 2026-04-25**

### Nerya Evidence

- `nerya/mcp/server.py:40` to `nerya/mcp/server.py:47` hardcodes MCP server instructions as a Nerya trading agent and explicitly says mutating trade/approval/vault operations are not exposed.
- `nerya/mcp/tools.py:5` to `nerya/mcp/tools.py:17` documents a mostly read-only MCP surface and excludes proposal apply/rollback, vault mutations, direct trade submission, signer policy edits, and live toggles.
- `nerya/mcp/tools.py:122` to `nerya/mcp/tools.py:145` exposes market ticker/klines through fixed methods.
- `nerya/mcp/tools.py:147` to `nerya/mcp/tools.py:217` exposes portfolio, strategy history/explain, and risk preview through fixed methods.
- `nerya/mcp/tools.py:219` to `nerya/mcp/tools.py:245` exposes trigger emit/dry-run/routes with source forced to `webhook`.
- `nerya/mcp/tools.py:249` to `nerya/mcp/tools.py:270` exposes proposal list/show through fixed methods.
- `nerya/mcp/tools.py:284` to `nerya/mcp/tools.py:312` registers a static list of Nerya tools.

### Hermes Evidence

- `hermes-agent/tools/mcp_tool.py` is a full MCP client/tool bridge rather than only a Nerya server surface.
- `hermes-agent/toolsets.py:537` to `hermes-agent/toolsets.py:584` includes plugin-registered toolsets and aliases in the live toolset list.
- `hermes-agent/tools/registry.py` centralizes tool registration and dispatch.

### Required Alignment

- Add an MCP client side to Nerya so Nerya can consume arbitrary MCP servers, not only expose its own fixed server.
- Make MCP server tool list generated from selected skill manifests and policy, not static methods.
- Support per-tool risk metadata, approval requirements, actor scopes, input/output schemas, and streaming/progress.
- Stop forcing `trigger_emit` source to `webhook`; use actor/source identity from the MCP caller.
- Add toolset composition: builtin Nerya skills, external MCP servers, gateway tools, coding tools, and trading tools should be selectable per profile/session.

### Implementation 2026-04-25

- New module `Nerya/nerya/mcp/dynamic_tools.py` introduces a manifest-driven MCP surface that walks the live `SkillRegistry` and emits one `MCPTool` per surviving action.
  - `mcp_tool_name(skill_id, action_name)` (lines 64-73) yields a stable `nerya_<skill>__<action>` namespace that can never collide with the legacy `nerya_*` flat namespace.
  - `_action_input_schema(spec)` (lines 76-83) reuses `ActionSpec.input_schema` verbatim so manifests are the single source of truth for MCP-side validation.
  - `MCPPolicy` (lines 86-142) drives every filter knob: `preset` (operator preset id), `allow_mutating` (default `False`), `allow_skills` / `deny_skills`, `allow_actions` / `deny_actions`, `include_unimplemented`, `live_trading_enabled`, `allow_no_agent_action`. `with_overrides` lets the runtime layer extend deny lists without rebuilding the dataclass.
  - `policy_from_config(config)` (lines 145-179) reads `mcp.dynamic_tools.{enabled,preset,allow_mutating,…}` and falls back to `agent.operator.preset` when no explicit MCP preset is configured, so the MCP surface inherits the planner's operator preset by default.
  - `MCPTool` (lines 182-220) captures every field an MCP client cares about — name, description, skill/action ids, JSON input schema, permissions, risk_gate, approval_gate, agent_query_only flag, tags, status, and a `decision`/`decision_reason` audit pair.
  - `_DroppedTool` (lines 223-238) records why an action was filtered (`mutating_action_blocked`, `unimplemented`, `skill_deny_listed`, `action_not_allowlisted`, `action_deny_listed`, `missing_agent_action`, `preset:<reason>`).
  - `_build_tool` (lines 241-287) wires each `MCPTool.fn` to `client.skill.call(skill_id, action_name, payload, caller="mcp")` so dispatch goes through the same chokepoint as planner / SDK / API callers (journal, approvals, availability probes, overflow spool all stay on).
  - `_eval_preset` (lines 296-318) projects a manifest entry into the `(alias, skill_id, action_name, agent_query_only, query_only, risk_gate, approval_gate)` row shape `operator_presets.evaluate` expects so the operator-mode preset is the *outer* policy gate (a `read_only` preset hides mutating actions even when `allow_mutating=True`).
  - `DynamicMCPRegistry.build` (lines 361-463) iterates skills/actions in a deterministic order and records both kept tools and dropped diagnostics. Sorted output keeps the surface stable across boots.
- Server wiring `Nerya/nerya/mcp/server.py:42-128`: `create_server(...)` now accepts `dynamic_policy`, `include_legacy`, `include_dynamic`. Reads `mcp.{include_legacy, dynamic_tools.enabled}` from config to flip both layers independently and stashes the resolved registry on the FastMCP instance as `_nerya_dynamic_registry` for tests/dashboard introspection. `build_dynamic_registry()` (lines 112-128) computes the registry without instantiating FastMCP — useful for the capability matrix and headless tests.
- `_register_dynamic_tool(mcp, tool)` (lines 153-181) wraps each `MCPTool` as a FastMCP `@mcp.tool()` callable with a generic `**kwargs` signature; runtime payload validation happens in `SkillRuntime` via `validate_payload(input_schema)`, so we don't need FastMCP to introspect the schema.
- Configuration: `Nerya/nerya/core/config.py:335-368` ships the new `mcp.{include_legacy,dynamic_tools.{enabled,preset,allow_mutating,include_unimplemented,allow_skills,deny_skills,allow_actions,deny_actions,allow_no_agent_action}}` block. Defaults keep behaviour conservative — legacy tools stay on, dynamic tools enabled but read-only, no preset override, and no skill/action allow/deny lists.
- Capability surface: `Nerya/nerya/api/routes_capability.py:222-247` adds `_mcp_dynamic_section(client)` that builds the dynamic registry (with policy from config) and exposes both kept tools (with names/permissions/schemas/decisions) and dropped diagnostics. Wired into `_capability_matrix` (line 269) and behind a dedicated `GET/POST /runtime/mcp_dynamic` endpoint (`_mcp_dynamic_endpoint`, lines 313-318) so dashboards can render the live MCP surface without booting an MCP client.
- Coverage: `Nerya/tests/test_mcp_dynamic_tools.py` ships 26 cases that lock down the policy decisions, naming, schema fidelity, dispatch wiring, capability-matrix integration, and a smoke test against the real built-in skill set. Highlights: stable + safe naming under unicode/special chars, default policy keeps only `agent_query_only` actions, mutating allow-list works, `read_only` preset still hides mutating actions even with `allow_mutating=True`, `live_trading` preset gates trading-side mutations on `runtime.live_trading_enabled`, allow/deny lists for skills *and* actions, dropped-record reasons explain every filter, schema verbatim from the manifest, real workspace boots and emits N read-only tools with unique `nerya_<skill>__<action>` names.
- Compilation/regression: `python -m pytest tests/test_mcp_dynamic_tools.py tests/test_operator_presets.py tests/test_model_registry.py tests/test_parallel_tools.py tests/test_harness.py tests/test_capability_drift.py tests/test_action_availability.py tests/test_operator_skill.py -q` → 144 passed; full sweep `python -m pytest tests/` → 1609 passed, 2 skipped, 1 warning (12m56s).

## 4. ACP Surface Is Narrow And Hand-Written, Not A Full Editor-Agent Protocol — **COMPLETED 2026-04-25**

### Nerya Evidence

- `nerya/acp/server.py:1` describes a stdio JSON-RPC server for approval + turn surface.
- `nerya/acp/server.py:41` maps methods manually in a dict.
- `nerya/acp/server.py:52` to `nerya/acp/server.py:61` exposes a small method set: pending approvals, proposal list, skills, trigger explain, recent turns, submit message.
- `nerya/acp/server.py:85` to `nerya/acp/server.py:90` returns boolean capabilities rather than a rich protocol contract.
- `nerya/acp/server.py:122` to `nerya/acp/server.py:140` approves/rejects by editing pending approval files.
- `nerya/acp/server.py:157` to `nerya/acp/server.py:168` submits message by emitting a trigger, not by running a streaming turn session.
- `nerya/acp/server.py:247` to `nerya/acp/server.py:270` uses line-delimited JSON-RPC without richer framing, streaming events, cancellation, or file-diff/tool UI.

### Missing Hermes-Like Behavior

Nerya ACP is useful, but it is closer to a control shim than a coding/editor agent protocol. It does not expose terminal/file/patch tools, streamed tool progress, model deltas, session branching, or editor-side approval state like Hermes TUI/gateway surfaces.

### Required Alignment

- Define an ACP capability schema with event streams, tool registry, approval prompts, cancellation, file refs, diff refs, and session ids.
- Replace hand-written methods with registry-driven method/action declaration.
- Make approval file moves atomic and actor-scoped.
- Add `session.interrupt`, `session.resume`, `session.branch`, `tool.call`, `tool.approve`, and `event.subscribe` equivalents.
- Add IDE-facing auth and workspace isolation.

### Implementation 2026-04-25

- New module `Nerya/nerya/acp/methods.py` (lines 1-400) introduces the registry-driven layer:
  - `MethodSpec` dataclass (lines 52-80) — typed metadata (name, handler, description, category, params/result schemas, tags, deprecated flag) with `asdict()` for `meta.methods` introspection.
  - `MethodRegistry` (lines 83-157) — thread-safe table replacing `AcpServer._methods()`. Supports `add()`, `register()` with `override=True`, `get()`, `names()`, `specs()`, `categories()`, and `asdict()`.
  - `_Session` + `SessionStore` (lines 165-262) — in-process talk-track envelope with id/title/status/parent_id/actor/tags/metadata and `create()`/`get()`/`require()`/`list()`/`update_status()` semantics.
  - `_Subscriber` + `EventBus` (lines 270-379) — pub-sub bus with glob-style kind matching (`turn.*`, `*`), session filtering, queue overflow handling that emits a synthetic `event.dropped` marker, `subscribe`/`unsubscribe`/`publish`/`drain`/`stats`.
  - `AcpError` (lines 387-399) — JSON-RPC compatible structured error carrying code/message/data.

- `Nerya/nerya/acp/server.py` (lines 1-540) was refactored to be registry-driven:
  - `AcpServer` is now a dataclass holding `methods: MethodRegistry`, `sessions: SessionStore`, `events: EventBus`, populated at construction by `register_default_methods()` (lines 121-300).
  - `dispatch()` (lines 67-72) looks up the spec from the registry instead of a hand-coded dict — adding a method anywhere is a one-liner.
  - `publish_event()` (lines 75-83) timestamps and forwards to the bus so other layers (turn engine, approvals helper) can stream updates over the same JSON-RPC pipe.
  - `_move_approval()` (lines 84-115) preserves the existing approval atomic-move behaviour and now publishes an `approval.<state>` event to subscribers.
  - 25 methods are registered in 6 categories (`meta`, `agent`, `approvals`, `session`, `tool`, `event`):
    - **meta**: `initialize`, `meta.methods`, `shutdown` — `initialize` advertises `sessions`/`tools`/`events`/`method_categories` in addition to the legacy capability flags.
    - **agent**: `agent.info`, `agent.capabilities`, `agent.skills`, `agent.recent_turns`, `agent.submit_message`, `agent.triggers.explain`, `agent.proposals_list`.
    - **approvals**: `agent.pending_approvals`, `agent.approve`, `agent.reject` (preserved verbatim for legacy clients).
    - **session**: `session.create`, `session.list`, `session.get`, `session.interrupt`, `session.resume`, `session.branch` — branching enforces parent existence and emits `session.created`/`session.interrupted`/`session.resumed` events.
    - **tool**: `tool.list` (mirrors `DynamicMCPRegistry.build` policy view), `tool.call` (dispatches through `client.skill.call` with `caller="acp:<actor>"` and emits `tool.start`/`tool.result`/`tool.error` events), `tool.approve` (alias of `agent.approve`).
    - **event**: `event.subscribe` (returns subscription id with kind glob + optional session filter), `event.unsubscribe`, `event.poll` (paginated drain with `max_items`).
  - Wire shape unchanged: line-delimited JSON-RPC 2.0 (`serve_stdio` lines 524-540, `handle_request` lines 506-521), so all 14 existing `tests/test_acp_server.py` cases keep passing.
- Coverage: `Nerya/tests/test_acp_protocol.py` (33 new tests) covers `MethodRegistry` register/get/categories, `SessionStore` create/branch/interrupt/resume/404, `EventBus` pub-sub/glob/session-filter/overflow/unsubscribe, and the JSON-RPC integration of every new method (`meta.methods` filtered by category, `initialize` capability advertisement, `session.create` event emission, `tool.call` dispatch through `skill.call`, `tool.call` failure events, `event.subscribe` pagination, `AcpError` propagation with `data` field).
- Regression: full sweep `tests/` 1647 passed, 2 skipped — refactor preserves backward compatibility (test_acp_server.py: 14/14, test_acp_protocol.py: 33/33).

## 5. LLM Model Metadata Is Still Much Thinner Than Hermes — **COMPLETED 2026-04-25**

### Nerya Evidence

- `nerya/llm/gateway.py:55` to `nerya/llm/gateway.py:60` resolves tiers from configured tier blocks and creates `ModelRouter` from those tiers.
- `nerya/llm/gateway.py:115` to `nerya/llm/gateway.py:124` checks a local capability matrix for schema JSON support.
- `nerya/llm/gateway.py:221` to `nerya/llm/gateway.py:249` exposes configured provider capability gaps, but the model metadata is still tier/provider matrix oriented.
- `nerya/llm/gateway.py:267` to `nerya/llm/gateway.py:271` exposes compression as a prompt to a light model with `max_tokens` text embedded in the prompt.

### Hermes Evidence

- `hermes-agent/agent/models_dev.py:1` to `hermes-agent/agent/models_dev.py:14` uses models.dev as a provider/model database with context window, max output, cost, capabilities, modalities, knowledge cutoff, release date, cache, and refresh behavior.
- `hermes-agent/agent/models_dev.py:48` to `hermes-agent/agent/models_dev.py:83` models per-model tool, attachment, reasoning, structured-output, modality, context, cost, cutoff, and status fields.
- `hermes-agent/agent/models_dev.py:180` to `hermes-agent/agent/models_dev.py:220` caches the models.dev registry in memory and on disk.

### Required Alignment

- Add model-level metadata to Nerya, not only provider/tier capability rows.
- Track context window, max output, modality support, tool-calling mode, structured-output mode, reasoning support, cache pricing, and release/cutoff metadata.
- Use model metadata for context budget, compression thresholds, attachment handling, and routing.
- Add model registry refresh/cache and dashboard/operator visibility.
- Separate “provider supports capability” from “this exact model supports capability”.

### Implementation 2026-04-25

- New module `Nerya/nerya/llm/model_registry.py` defines `ModelMetadata` (lines 65-130) carrying the full Hermes-shaped record: id/provider/family, context_window, max_output_tokens, cost_input/output/cache_read/cache_write per million tokens, tool_calling/tool_choice/structured_output/streaming/reasoning/prompt_cache flags, input/output modalities, knowledge_cutoff, release_date, status, and a `source` audit field.
- `BUILTIN_MODELS` (lines 140-340) ships an offline-first snapshot for OpenAI (gpt-4o, gpt-4o-mini, gpt-4.1, o3, o3-mini), Anthropic (claude-3.5-sonnet/-haiku, claude-3.7-sonnet, claude-3-opus), Google Gemini (2.0-flash, 1.5-pro, 1.5-flash), DeepSeek (chat, reasoner), xAI (grok-2, grok-3), and the deterministic `mock` tier. `BUILTIN_ALIASES` (lines 343-365) handles date-suffixed variants (`gpt-4o-2024-11-20` → `gpt-4o`).
- `ModelRegistry` (lines 372-470) resolves lookups in a deterministic order — exact builtin → alias regex → on-disk cache (`<workspace>/state/llm/model_registry_cache.json`, both `models.dev` and flat shapes) → synthetic `unknown` entry. Never raises on missing data.
- Per-tier enrichment: `LLMGateway.capabilities()` (`Nerya/nerya/llm/gateway.py:200-260`) now stamps each tier with `model_metadata` and emits a `model_registry` summary block; `routes_capability._model_registry_section` + `_model_registry_endpoint` (`Nerya/nerya/api/routes_capability.py:235-260, 282-291`) surface the same payload through `GET/POST /runtime/capability_matrix` and a dedicated `GET/POST /runtime/model_registry` route.
- Compression budget: new `budget_for_model(provider, model_id, …)` helper (`Nerya/nerya/llm/compression.py:208-242`) derives the working budget from `context_window` minus `reserve_output`, with a `headroom_ratio` safety margin and a fallback floor — replaces the legacy hardcoded 8 192-token assumption.
- Coverage: `Nerya/tests/test_model_registry.py` ships 30 cases — builtin completeness/typing, alias resolution, disk-cache shapes, corrupt-cache handling, builtins outranking the cache, providers/list_models filtering, summary/empty/unknown branches, compression budget for huge/known/unknown models, capability matrix endpoint payload + route registration, and end-to-end `LLMGateway.capabilities()` enrichment via a real workspace bootstrap.

## 6. Doctor/Preflight Is Trading-Readiness Oriented, Not Full Operator-Agent Diagnostics

### Nerya Evidence

- `nerya/ops/preflight.py:23` to `nerya/ops/preflight.py:25` defines production profiles around trading rollout: `prod_paper`, `canary_live`, `full_live`.
- `nerya/ops/preflight.py:117` checks LLM keys.
- `nerya/ops/preflight.py:155` checks live/mock conflict.
- `nerya/ops/preflight.py:233` checks capability gaps.
- `nerya/ops/preflight.py:256` checks connector reachability.
- `nerya/ops/preflight.py:364` checks account credentials.
- `nerya/ops/preflight.py:448` optionally smoke-tests LLM providers.
- `nerya/ops/preflight.py:519` checks route truth.

### Hermes Evidence

- `hermes-agent/hermes_cli/doctor.py:164` starts a broad `run_doctor` diagnostic flow.
- `hermes-agent/hermes_cli/doctor.py:189` to `hermes-agent/hermes_cli/doctor.py:204` checks Python and virtualenv.
- `hermes-agent/hermes_cli/doctor.py:247` to `hermes-agent/hermes_cli/doctor.py:276` checks `.env` and API key setup.
- `hermes-agent/hermes_cli/doctor.py:276` to `hermes-agent/hermes_cli/doctor.py:292` checks config file presence and can create it from an example.
- `hermes-agent/hermes_cli/doctor.py:131` to `hermes-agent/hermes_cli/doctor.py:158` checks systemd linger for gateway service persistence.

### Required Alignment

- Add a general `nerya doctor` mode that checks terminal/tool harness, browser/web tools, MCP clients, gateway services, dashboard streaming, auth, sessions, interrupts, files, and provider/model metadata.
- Keep trading preflight separate from operator-agent doctor.
- Add repair suggestions and optional `--fix` for missing local config/scaffold, not only pass/fail reports.
- Add gateway service diagnostics: process lock, systemd/launchd/Windows service status, webhook reachability, Telegram polling conflicts, last delivery failure.

## 7. Service Lifecycle And Background Jobs Are Not Hermes-Level

### Nerya Evidence

- `nerya/api/local_server.py:54` builds a stdlib HTTP server and boots `InternalClient` directly.
- `nerya/api/local_server.py:57` calls `routes_gateway.launch_configured_gateways_on_start(client)` as a direct boot side effect.
- `nerya/cli/commands/runtime.py:171` to `nerya/cli/commands/runtime.py:187` exposes MCP/ACP/Cron commands, but cron run is essentially manual `run-once` from CLI.
- `nerya/triggers/scheduled_session.py:287` to `nerya/triggers/scheduled_session.py:296` directly imports and boots `AgentKernel`/`SkillKernel` for scheduled sessions.

### Hermes Evidence

- Hermes has gateway install/stop/restart/uninstall service flows in `hermes_cli` and gateway service health diagnostics.
- `hermes-agent/cron/jobs.py:117` to `hermes-agent/cron/jobs.py:198` parses interval/cron/timestamp schedule strings.
- `hermes-agent/cron/jobs.py:284` computes next runs and `hermes-agent/cron/jobs.py:370` creates jobs with derived schedule state.
- Hermes uninstall/backup flows know to stop gateway services and profile-scoped services.

### Required Alignment

- Add first-class service lifecycle: install, start, stop, restart, status, logs, uninstall for API server, dashboard, gateway, scheduler, and workers.
- Add process locks and duplicate-instance detection for gateway/API/scheduler.
- Split scheduler daemon from manual `cron run-once` and make missed-run/retry/backoff semantics explicit.
- Persist job execution state, last result, next run, failure count, and owner actor.
- Support profile-scoped services and named workspaces.

## 8. Filesystem Layout Is Static And Workspace-Centric, Not Profile/Plugin/Retention Aware

### Nerya Evidence

- `nerya/core/paths.py:56` to `nerya/core/paths.py:166` defines many fixed workspace directories and filenames as Python properties: memory, agents, subagents, skills, accounts, strategies, triggers, scripts, messages, approvals, vault, evolution, providers, artifacts, dev logs, and `nerya.db`.
- `nerya/core/paths.py:169` to `nerya/core/paths.py:172` defaults workspace root to `NERYA_WORKSPACE` or `~/.nerya`.

### Missing Hermes-Like Behavior

Hermes has home/profile concepts, managed install handling, gateway services, model cache, sessions DB, skills config, and backups/uninstall behavior. Nerya has a clean workspace layout, but less profile/version/retention/service awareness.

### Required Alignment

- Add workspace/profile abstraction: named profile, active workspace, service scope, gateway identity, and config home.
- Add schema versioning and migration for workspace files.
- Add retention policy per data class: sessions, messages, attachments, tool results, approvals, dev logs, LLM prompts, trading journals.
- Add backup/export/import for workspace state.
- Add cleanup/doctor checks for orphaned pending approvals, stale locks, oversized attachments, and broken proposal directories.

## 9. Builtin Capability Claims Need Negative Tests

### Nerya Evidence

- `tests/conftest.py:21` enables `NERYA_ALLOW_MOCK_DATA=1` by default.
- `scripts/run_truth_gate.sh:41` references capability honesty, but the default test environment still starts from mock-tolerant behavior.
- `scripts/test_dashboard_ui.py:164` verifies some mock KPI literals are removed from dashboard, but this is not a general capability-truth test for every feature shown in UI/prompt/API.

### Required Alignment

- Add negative tests proving unsupported capabilities are hidden or rejected.
- For every docs/UI/platform claim, test actual runtime support in strict mode.
- Add test matrix for `mock_allowed` vs `strict_runtime` vs `live_opt_in`.
- Add tests that disabled skills do not appear in prompts, MCP tools, dashboard forms, gateway command help, or route planner selected skills.
- Add tests that declared scaffold platforms do not claim attachments/voice/buttons unless implemented.

## 10. Toolsets And Plugins Are Still Not First-Class In Nerya

### Nerya Evidence

- Nerya has skill manifests and skill runtime, but tool selection for agent prompt/action mapping is still partially in config and code (`agent.planner.routes`, `context_builder`, `ACTION_MAP`, dashboard wrappers, MCP registry).
- MCP tools are a static Nerya-specific list rather than a composable toolset registry.

### Hermes Evidence

- `hermes-agent/toolsets.py:537` to `hermes-agent/toolsets.py:584` includes plugin-registered toolsets in the available toolset list.
- `hermes-agent/toolsets.py:631` to `hermes-agent/toolsets.py:655` can create custom toolsets at runtime.
- `hermes-agent/toolsets.py:657` to `hermes-agent/toolsets.py:678` returns resolved toolset info, including direct tools, included toolsets, resolved tools, count, and composite status.

### Required Alignment

- Add `Toolset` as a first-class concept separate from Skill: a toolset composes skills, MCP tools, gateway tools, file tools, terminal tools, and browser tools.
- Make planner routes select toolsets, not raw skill ids.
- Allow user-defined toolsets with includes/excludes, actor scopes, and risk levels.
- Surface active toolsets in prompt, dashboard, API, gateway help, and trace.

## 11. Prompt/Config Drift Still Has No Versioned Contract

### Nerya Evidence

- Default prompts are seeded in workspace files from Python literals.
- Context builder hardcodes action instructions separately.
- Skill manifests declare some but not all agent actions.
- Dashboard copy and suggestions hardcode additional behavior.

### Required Alignment

- Add `PromptContract` / `CapabilityContract` versioning across context builder, skill manifests, default prompts, dashboard UX, gateway commands, MCP/ACP surfaces, and tests.
- Every runtime capability should have one owner manifest and generated consumers.
- Add drift tests that diff generated action catalog against prompt, dashboard, MCP, ACP, and gateway surfaces.

## Highest-Impact Remaining Hardcoding To Remove Next

1. `DEFAULT_CONFIG.agent.planner.routes` should become profile/manifest route contributions.
2. `workspace.manager` default prompts/subagents should become builtin profile assets with provenance and migrations.
3. MCP static registry should become policy-filtered generated tools from skill/action/toolset registry.
4. ACP hand-written method map should become protocol/capability generated methods with streaming/cancel/session support.
5. LLM tier-only capability matrix should be augmented with model-level registry metadata.
6. Doctor/preflight should split into trading readiness vs general operator-agent diagnostics.
7. Filesystem layout should gain profile/schema/retention/backup semantics.

## Bottom Line

The repeated pattern is still broader than gateway/context only:

```text
Nerya has many good primitives, but too many surfaces still encode product behavior in Python defaults, seeded prompt strings, static lists, and hand-written shims.
Hermes-like behavior requires generated capability surfaces, dynamic registries, profile-aware services, and protocol-level session/event semantics.
```

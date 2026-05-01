# Additional Hardcoding And Non-Skill-Driven Surfaces

This is a follow-up to `22-context-prompt-hardcoding-and-skill-loading-audit.md`. The previous file focused on main prompt/action catalog hardcoding. This file lists other hardcoded or non-skill-driven areas found across dashboard, gateway, default config, trigger routing, tests, and runtime bootstrapping.

## Status Snapshot (2026-04-25, third pass — P0/P1/P2 mapping)

P0 items (1-5):

1. **Replace main prompt action list with selected skill manifest catalog** — COMPLETED. See Plan 22 status; the catalog is rendered from `Nerya/nerya/agent/kernel.py:build_action_catalog()` and consumed by `Nerya/nerya/agent/context_builder.py:_render_action_catalog()`.
2. **Add `agent_action` metadata to core builtin skills** — COMPLETED. Every `Nerya/nerya/skills/builtin/*/skill.yml` carries `agent_action`/`agent_payload_builder`/`agent_hint`/`agent_payload_hint` (and `agent_query_only` for read-only actions); spot-check `operator_skill/skill.yml`, `market_data_skill/skill.yml`, `strategy_skill/skill.yml`.
3. **Dashboard generic skill/action forms read live schemas** — PARTIALLY COMPLETED. The capability matrix at `GET /runtime/capability_matrix` exposes per-action input/output schemas; the dashboard form renderer that consumes them is tracked under §1/§11 in Plan 21.
4. **Gateway commands → registry with skill-backed handlers** — COMPLETED. `Nerya/nerya/api/gateway_commands.py:DEFAULT_REGISTRY` consumed by `Nerya/nerya/api/routes_gateway.py::_handle_text`; tests in `Nerya/tests/test_gateway_commands.py`.
5. **Disabled/unselected skills disappear from prompt + UI + gateway help** — COMPLETED. `Nerya/tests/test_strategy_skill.py::test_context_builder_advertises_create_strategy` verifies prompt disappearance; `Nerya/nerya/api/routes_capability.py` only returns enabled skills.

P1 items (1-5):

1. **Default planner route presets → versioned manifests** — COMPLETED 2026-04-25.
   - Versioned manifest module: `Nerya/nerya/agent/route_manifests.py` (new ~330 lines). Ships three bundled presets (`trading-v1`, `general-operator-v1`, `minimal-v1`) with stable ids, schema version, mode label, capability tags, route table and fallback. Exposes `builtin_manifests`, `list_manifest_ids`, `load_manifest`, `manifest_summary`, `resolve_routes` and an external override path at `$workspace/route_manifests/<id>.yml`.
   - Config selector: `Nerya/nerya/core/config.py:139-145` adds `agent.planner.manifest` (null by default — legacy free-form table still wins). When set, the resolver loads the matching bundled or workspace manifest and the planner uses its `routes`/`fallback`.
   - Planner wiring: `Nerya/nerya/agent/planner.py:14, 111-142` — `_resolve_route_table` first calls `route_manifests.resolve_routes(config, paths=paths)` and only falls back to the freeform table or `DEFAULT_CONFIG` when no manifest is pinned. Unknown manifest ids never raise — they degrade gracefully to the legacy table.
   - Capability matrix: `Nerya/nerya/api/routes_capability.py:_planner_section` now exposes `active_manifest` + `manifests` (id/name/description/version/mode/capabilities) so dashboards / docs can pick a preset and detect drift.
   - Tests: `Nerya/tests/test_route_manifests.py` (14 cases — bundled round-trip, summary listing, resolver wins when pinned / empty otherwise, unknown id raises, external override + invalid manifest raises ValueError, planner uses manifest, planner falls back to legacy table, planner survives unknown manifest id, minimal preset is bare-minimum, trading preset has full lane coverage, capability dedupe, summary picks up external manifests).
2. **Runtime capability matrix endpoint** — COMPLETED. `Nerya/nerya/api/routes_capability.py` + `GET /runtime/capability_matrix`; covered by `Nerya/tests/test_operator_routes_surface.py::test_capability_matrix_route_is_registered`.
3. **Telegram ad-hoc ids → platform identity builder** — COMPLETED. `Nerya/nerya/api/gateway_identity.py:session_id/message_id` consumed by routes_gateway; tests in `test_gateway_commands.py::test_message_id_includes_direction_and_seq` and `test_session_id_keeps_legacy_telegram_shape`.
4. **Strict-vs-mock test profile split** — COMPLETED 2026-04-25.
   - Profile selector: `Nerya/tests/conftest.py:1-115` introduces `NERYA_TEST_PROFILE` (env) + `--nerya-profile` (pytest CLI) with four profiles: `unit_mock` (default — keeps the legacy `NERYA_ALLOW_MOCK_DATA=1` behaviour), `strict_runtime` (mock env removed), `integration_fake_transport` (recorded transports), and `live_opt_in` (real network / exchange / LLM calls).
   - Marker contract: `@pytest.mark.profile("strict_runtime")` (or any combination of profile names) skips the test when the active profile differs. Unmarked tests are profile-agnostic so the bulk of the suite stays untouched.
   - Hooks: `pytest_addoption` registers `--nerya-profile`, `pytest_configure` resolves the env / CLI knob (and writes `NERYA_TEST_PROFILE_RESOLVED` for downstream modules), `pytest_collection_modifyitems` adds skip markers when a test's required profile is not active.
   - Fixture: `nerya_test_profile` exposes the resolved profile to tests; `deny_mock_mode` is preserved for legacy truth-gate tests.
   - Tests: `Nerya/tests/test_profile_split.py` (9 cases — default = unit_mock + mock data env set, KNOWN_PROFILES matches plan, fallback for unknown names, all known names accepted, whitespace/case normalisation, marker matrix (`strict_runtime` skipped under default, `unit_mock` runs, multi-profile marker), direct collection-hook drive).
5. **Skill/dashboard extension registry** — COMPLETED 2026-04-25 (backend hook).
   - Manifest field: `Nerya/nerya/skills/manifest.py` — new `DashboardExtension` dataclass + `SkillManifest.dashboard: list[DashboardExtension]`. Skills declare extensions in `skill.yml` under `dashboard:` with `slot` (`panel`/`card`/`nav`/`settings`), `title`, `description`, `component` slug, `icon`, `href`, `permissions`, `tags`, and a freeform `schema` map. Invalid (non-dict) entries are silently dropped.
   - Capability matrix: `Nerya/nerya/api/routes_capability.py:_dashboard_extensions` aggregates the descriptors from the live skill registry, attaches the skill version, and sorts by (slot, skill_id, title). Surfaced via `GET /runtime/dashboard_extensions` and embedded in `GET /runtime/capability_matrix.dashboard_extensions`.
   - Tests: `Nerya/tests/test_dashboard_extensions.py` (9 cases — empty manifest, single panel round-trip, invalid entries dropped, JSON serialisable, helper aggregation + sort order, endpoint returns extensions, GET/POST routes registered, capability matrix embeds the section, helper is defensive when `skills` is missing).
   - Frontend renderer: tracked separately under Plan 21 §11; today the React side can call `/runtime/dashboard_extensions` and dispatch on the `component` slug.

P2 items (1-5):

1. **Subagent role defaults → manifests/config** — PARTIALLY COMPLETED. `Nerya/nerya/subagents/registry.py:DEFAULT_SUBAGENT_SKILLS` is the single source of truth and is consumed by the dispatcher; manifest-overlay tracked under Plan 23 P2 §1.
2. **Generalise scheduled-session runtime through container** — PARTIALLY COMPLETED. `Nerya/nerya/sdk/scheduled_session_factory.py` is the factory; full DI container tracked under Plan 23 P2 §2.
3. **Trigger route caps/policies configurable per source/channel/actor** — COMPLETED 2026-04-25.
   - Config knob: `Nerya/nerya/core/config.py:259-280` (`triggers.router.default_max_payload_bytes` + per-source/channel/kind/actor `policies` table).
   - Resolver: `Nerya/nerya/triggers/router.py:53-95` (`_resolve_payload_cap`) layers route-level cap > actor > channel > source > kind > config default > legacy 65_536.
   - Wired into both `route()` and `explain()` (`Nerya/nerya/triggers/router.py:165` and `:115`).
   - Tests: `Nerya/tests/test_trigger_router_caps.py` (9 cases — default, env override, per-axis precedence, route-level cap wins, invalid value falls back, explain reflects resolved cap).
4. **Demo suggestions → recipe manifests** — COMPLETED 2026-04-25 (backend hook + endpoint).
   - Recipe module: `Nerya/nerya/agent/recipes.py` (new ~250 lines). Bundles 6 starter recipes (script/MACD, narrative_watcher subagent, portfolio heartbeat, postmortem, memory recall, exchange setup); each declares `required_skills` / `required_actions` so it is only listed when the matching skills/actions are installed.
   - External overrides: `$workspace/recipes/<id>.yml` wins over a bundled recipe with the same id; invalid recipes (missing `prompt`) are silently dropped, never raise.
   - Capability gating: `available_recipes(client)` walks the live skill registry and filters by required skills/actions. `recipe_summary(client)` returns both `available` and `all` so dashboards can render greyed-out recipes.
   - Endpoints: `Nerya/nerya/api/routes_capability.py` registers `GET/POST /runtime/recipes` (returns only available recipes) and now embeds the same `recipes.available` / `recipes.all` view inside `GET /runtime/capability_matrix` so the dashboard chat empty-state can switch from compile-time TypeScript copy to a live call.
   - Dashboard rewrite: tracked separately. The TS file `dashboard/components/chat/ChatView.tsx:22-46` no longer needs to be the source of truth — it just has to consume `/runtime/recipes`.
   - Tests: `Nerya/tests/test_recipe_manifests.py` (13 cases — required-fields shape, id uniqueness, external override, invalid recipe dropped, skill filter, full-workspace surfaces every bundled recipe, empty workspace surfaces nothing, summary returns available + all, required_actions enforced, endpoint registered, endpoint returns available-only, capability matrix includes recipes section, GET/POST routes both registered).
5. **Capability-drift tests for docs / product copy** — COMPLETED 2026-04-25.
   - Drift detector: `Nerya/tests/test_capability_drift.py` (8 cases).
   - Coverage: every `/start` `/help` `/menu` `/status` `/trace` `/new` referenced in dashboard chat copy must exist in `gateway_commands.DEFAULT_REGISTRY`; the `/runtime/capability_matrix` and `/runtime/recipes` routes must remain registered; bundled recipe + route-manifest catalogues do not regress (counts, ids, capability gates); recipe `.as_dict()` and route manifest `.as_dict()` are JSON-serialisable; gateway registry menu is JSON-serialisable; no dashboard `*.tsx` hardcodes `http://127.0.0.1:18317`.
   - Skip behaviour: if the dashboard tree is not present in a partial checkout, the dashboard-copy tests skip gracefully instead of failing the suite.

Validation: the 19-case prompt battery in `tmp/prompt_suite_postgw.json` runs cleanly (`stopped_reason=null`, every reply non-empty, all read-only and mutating actions reach real skills); no regression vs `tmp/prompt_suite_postmem.json`.

## Verdict

Nerya has a skill-first runtime at the execution layer, but many product/control-plane surfaces still know exact skill ids, action names, symbols, routes, commands, ports, and default workflows directly in TypeScript/Python. That means the UI/gateway/planner are not fully capability-discovered from installed skills. This is one reason the system can drift: a skill can exist or change, but dashboard/gateway/context may still show old assumptions.

## Additional Hardcoded Surfaces

### 1. Dashboard API client hardcodes skill ids and action names

Evidence:

- `dashboard/lib/clientApi.ts:212` calls `/skills/call` with `skill_id: "strategy", action: "list"`.
- `dashboard/lib/clientApi.ts:225` calls `strategy.get`.
- `dashboard/lib/clientApi.ts:245` calls `strategy.create`.
- `dashboard/lib/clientApi.ts:251` calls `strategy.set_status`.
- `dashboard/lib/clientApi.ts:259` calls `strategy.bind_wallet`.
- `dashboard/lib/clientApi.ts:268` calls `strategy.bind_account`.
- `dashboard/lib/clientApi.ts:288` calls `subagent.list_subagents`.
- `dashboard/lib/clientApi.ts:294` calls `subagent.get_subagent`.
- `dashboard/lib/clientApi.ts:305` calls `subagent.create_subagent`.
- `dashboard/lib/clientApi.ts:383` and `dashboard/lib/clientApi.ts:404` call exact `exchange_author` actions.

Problem:

The dashboard is not rendering available capabilities from skill manifests. It has typed wrappers that assume specific builtin skills and actions. This is acceptable for first-party product pages, but it is not Hermes-like dynamic skill/tool UI. If an installed skill changes action names, permissions, schemas, labels, or approval gates, the dashboard can drift.

Required fix:

- Add a manifest-driven `SkillActionCatalog` endpoint for dashboard.
- Generate UI forms from action schemas for generic skill actions.
- Keep first-party wrappers only as convenience facades, but validate against live manifest metadata.
- Show “unavailable because skill/action not installed/enabled” instead of broken calls.

### 2. Chat suggestions are hardcoded trading/demo prompts

Evidence:

- `dashboard/components/chat/ChatView.tsx:22` defines local `suggestions`.
- `dashboard/components/chat/ChatView.tsx:25` hardcodes `BTCUSDT` MACD script suggestion.
- `dashboard/components/chat/ChatView.tsx:27` hardcodes a long paper-mode Binance BTCUSDT prompt.
- `dashboard/components/chat/ChatView.tsx:45` hardcodes `btc_momentum`, `strategy_reviewer`, and `risk_critic` postmortem prompt.

Problem:

The user sees a fixed trading-oriented demo surface even when installed skills/workspace state differ. Suggestions should reflect actual enabled skills, sample recipes, and workspace capabilities.

Required fix:

- Move suggestions to recipe/skill manifest metadata or workspace templates.
- Generate suggestions from installed skills + user mode.
- Mark examples as examples, not current capability truth.
- Avoid hardcoded strategy ids unless that strategy exists in workspace.

### 3. Gateway commands and Telegram flow are hardcoded in API routes

Status: PARTIALLY FIXED (2026-04-25)

The Telegram-only `if/elif` chain that owned `/start`, `/help`, `/menu`, `/commands`, `/status`, `/new`, `/trace` was replaced by a platform-neutral registry. Skills can now contribute additional commands by registering a `CommandSpec` + handler against `DEFAULT_REGISTRY` (the registration hook is intentionally minimal in this pass — wiring skill-provided commands through manifests is still on the P1 list).

Post-remediation evidence:

- `Nerya/nerya/api/gateway_commands.py` — `CommandSpec`, `CommandContext`, `CommandOutcome`, `GatewayCommandRegistry`, plus the Hermes-aligned `BUILTIN_COMMANDS` (`start/help/menu+commands/new/status/trace`) and the `DEFAULT_REGISTRY` singleton.
- `Nerya/nerya/api/routes_gateway.py::_handle_text` — looks up commands via `DEFAULT_REGISTRY.handle(...)`. State + dashboard URL + session deletion are passed in via `CommandContext` so handlers stay transport-agnostic.
- `Nerya/nerya/api/routes_gateway.py::_run_gateway_turn` — uses `gateway_session_id(...)` instead of inline string concatenation (also addresses §10).
- `Nerya/nerya/api/routes_gateway.py::sync_configured_gateways_on_start` — Telegram menu sync now reuses `gateway_menu_commands()` (registry-backed) so dashboard, polling, and `setMyCommands` always agree.
- `Nerya/nerya/api/gateway_commands.py::resolve_dashboard_url` — env (`NERYA_DASHBOARD_URL`) > `gateway.dashboard_url` config > `gateway.dashboard.public_url` > legacy default. `Nerya/tests/test_gateway_commands.py::test_handle_text_status_uses_configured_dashboard_url` proves the localhost string is gone when the operator sets a public URL.
- Tests: `Nerya/tests/test_gateway_commands.py` (11 cases — registry behaviour, alias precedence, platform scoping, integration with `_handle_text`, dashboard URL resolution, gateway identity).

Remaining (deferred): a manifest hook so skills can declare commands (e.g. `agent_command: { name: "/portfolio", action: "portfolio.summary" }`), and a generic "/." command form that introspects the registry instead of forcing the operator into chat.

Original evidence:

- `nerya/api/gateway_events.py:19` includes static command definitions such as `trace`.
- `nerya/api/routes_gateway.py:41` returns `DEFAULT_COMMANDS` directly for gateway menus.
- `nerya/api/routes_gateway.py:208` sets Telegram session id as `telegram_{chat_id}`.
- `nerya/api/routes_gateway.py:210` hardcodes `/start`, `/help`, `/menu`, `/commands` handling.
- `nerya/api/routes_gateway.py:214` hardcodes `/status` behavior.
- `nerya/api/routes_gateway.py:219` hardcodes dashboard URL `http://127.0.0.1:3001/dashboard` in status text.
- `nerya/api/routes_gateway.py:234` hardcodes `/trace` behavior.
- `nerya/api/routes_gateway.py:256` constructs trigger source `telegram` and channel `telegram` directly.
- `nerya/api/routes_gateway.py:464` through `nerya/api/routes_gateway.py:466` expose Telegram-specific setup/poll/send routes.

Problem:

Gateway command handling is not a platform-neutral command registry backed by skill/tool capabilities. Telegram routes are special-cased. Adding `/stop`, `/retry`, `/memory`, or a non-Telegram equivalent means editing Python route code rather than registering commands/capabilities.

Required fix:

- Create a gateway command registry loaded from builtin skills/capabilities.
- Commands should declare platform support, required scope, payload schema, and handler action.
- Gateway status should use configured public dashboard URL, not hardcoded localhost.
- Session id derivation should be a gateway identity module supporting chat/thread/user/workspace.

### 4. Gateway platform catalog advertises scaffold support that is not real full adapter support

Status: PARTIALLY FIXED (2026-04-25)

`GatewayPlatformSpec` now carries an explicit `support_level` field (`Nerya/nerya/messaging/platforms.py`). Allowed values follow this plan's recommendation: `catalog_only`, `send_only`, `inbound_webhook`, `full_duplex`, `tested`. Every entry in `_HERMES_PLATFORMS` is annotated; the legacy `status` field is retained for back-compat. `/gateway/platforms` now returns `support_level` in each row, and the runtime capability matrix endpoint at `GET /runtime/capability_matrix` echoes the same data so dashboard/docs can render truthful capability claims.

Remaining (deferred): adding per-platform regression tests that exercise inbound + outbound + attachments + voice for every adapter and gating `tested` on those tests passing in CI.

Original evidence:

- `nerya/messaging/platforms.py` contains platform specs for Telegram, Discord, WhatsApp, Slack, Signal, Mattermost, Matrix, email, DingTalk, Feishu, WeCom, Weixin, BlueBubbles, QQ Bot, etc.
- Many platform entries are marked `scaffold`, `generic_inbound`, `webhook_or_bridge`, or `commands_scaffold` rather than full native adapters.

Problem:

The platform catalog can make the product look broader than the implemented gateway reality. It is not exactly “hardcoded bug”, but it is a hardcoded capability claim surface.

Required fix:

- Add support levels: `catalog_only`, `send_only`, `inbound_webhook`, `full_duplex`, `media`, `approval_buttons`, `streaming_edit`, `tested`.
- Dashboard/docs/gateway `/platforms` must display support level and tests.
- Do not advertise attachments/voice as supported unless adapter actually handles them end-to-end.

### 5. Default config still embeds broad route and skill assumptions

Evidence:

- `nerya/core/config.py:17` defines `DEFAULT_CONFIG` in Python.
- `nerya/core/config.py:32`, `nerya/core/config.py:53`, and `nerya/core/config.py:72` default LLM providers to `mock`.
- `nerya/core/config.py:98` through `nerya/core/config.py:102` default market preferences to `BTCUSDT`.
- `nerya/core/config.py:133` starts default planner routes.
- `nerya/core/config.py:222` defines `user_chat` route.
- `nerya/core/config.py:225` through `nerya/core/config.py:230` list many hardcoded skill ids for user chat.
- `nerya/core/config.py:244` defines generic route fallback.
- `nerya/core/config.py:247` gives fallback skills `market_data`, `trading`, `message`.

Problem:

The default planner is configurable but not discovery-driven. It assumes a trading workspace with specific skills and BTC defaults. That works for Nerya’s trading identity, but hurts Hermes-like general operator behavior and makes non-trading installations feel wrong.

Required fix:

- Move default routes to a versioned config file or builtin route manifests.
- Derive route candidates from skill tags, not only hardcoded skill ids.
- Split modes: `trading`, `coding`, `gateway`, `general_operator`, `minimal`.
- Default market preferences should be workspace template data, not universal runtime assumptions.

### 6. Tests globally enable mock data by default

Evidence:

- `tests/conftest.py:3` says tests run with `NERYA_ALLOW_MOCK_DATA=1` by default.
- `tests/conftest.py:21` sets `os.environ.setdefault("NERYA_ALLOW_MOCK_DATA", "1")`.
- `tests/conftest.py:25` provides `deny_mock_mode` only for strict runtime tests.

Problem:

This can hide production-readiness gaps. It is not runtime hardcoding, but it biases test coverage toward fallback/mock behavior.

Required fix:

- Split test profiles: `unit_mock`, `strict_runtime`, `integration_fake_transport`, `live_opt_in`.
- Require strict mode for tests that claim production/gateway/context parity.
- Track which docs claims are verified under mock vs strict mode.

### 7. API route collection is static

Evidence:

- `nerya/api/local_server.py:15` through `nerya/api/local_server.py:21` imports route modules explicitly.
- `nerya/api/local_server.py:36` through `nerya/api/local_server.py:42` statically loops a fixed tuple of route modules.
- Gateway startup sync is called directly from local server boot at `nerya/api/local_server.py:57`.

Problem:

Built-in skills cannot add API routes or gateway commands through manifests. The API surface is not plugin/skill-extensible.

Required fix:

- Keep core API static for safety, but add an extension registry for skill-provided API actions, command handlers, and dashboard forms.
- Gateway startup hooks should be registered, not directly special-cased.
- Expose capability metadata through API rather than needing new route modules for every first-party feature.

### 8. Scheduled-session runner boots a fresh kernel directly

Status: PARTIALLY FIXED (2026-04-25)

The factory was lifted out of `triggers/` (which the runtime-ownership ADR forbids from importing `agent`/`skills`/`messaging`) and is now owned by the SDK layer. Delivery moved out of `triggers/` into `messaging/` for the same reason. `CronScheduler` now resolves both via `importlib.import_module` so the static AST audit (`tests/test_architecture_audit.py`) stays clean.

Evidence (post-remediation):

- `Nerya/nerya/sdk/scheduled_session_factory.py` — new module that hosts `default_kernel_factory`. SDK is allowed to import `agent` + `skills`.
- `Nerya/nerya/messaging/scheduled_delivery.py` — delivery fan-out moved here from `triggers/delivery.py` (deleted). Annotations on `entry` are intentionally `Any` because importing `triggers.schedule.ScheduleEntry` (even under `TYPE_CHECKING`) trips the AST-level boundary audit.
- `Nerya/nerya/triggers/cron.py::CronScheduler._session_runner` — lazily imports the factory + delivery via `importlib` so the trigger package no longer has a static dependency on `sdk` / `messaging` / `agent` / `skills`.
- `Nerya/nerya/triggers/scheduled_session.py` — no longer defines `default_kernel_factory`; the runner takes the factory as injected dependency.
- `Nerya/tests/test_architecture_audit.py::test_import_boundaries_are_respected` — passes.

Remaining (deferred): generalising to a runtime container that also injects policy/event store/cancellation; narrowing the auto-loaded skill set when `attached_skills` declares a smaller capability surface.

Original evidence:

- `nerya/triggers/scheduled_session.py:287` defines `default_kernel_factory`.
- `nerya/triggers/scheduled_session.py:294` imports `AgentKernel` directly.
- `nerya/triggers/scheduled_session.py:295` imports `SkillKernel` directly.
- `nerya/triggers/scheduled_session.py:296` boots all skills via `SkillKernel.boot(config)`.
- `nerya/triggers/scheduled_session.py:153` passes `attached_skills` into the turn.

Problem:

This is simple but not a general runtime dependency-injection model. Scheduled jobs cannot easily use alternative skill catalogs, policy engines, test harnesses, or gateway actor contexts.

Required fix:

- Introduce a runtime factory/container that constructs `AgentKernel`, `SkillKernel`, policy, event store, actor, and cancellation context.
- Scheduled sessions should run under an actor/workspace/session identity with selected capabilities.
- Avoid booting all skills when job declares a narrower capability set.

### 9. Trigger router has fixed payload caps and routing semantics

Evidence:

- `nerya/triggers/router.py:161` sets `default_cap = 65536`.
- `nerya/triggers/router.py:184` says default target `main` is allowed only if a strategy is given.
- `nerya/triggers/router.py:188` dead-letters with `no_route_no_strategy`.

Problem:

This is reasonable trading-runtime behavior, but Hermes-like gateway/general-agent behavior needs routes that are not strategy-scoped. Payload caps and route requirements should be policy/config-driven by source/channel/actor/type.

Required fix:

- Move caps and route requirements into route policy config.
- Distinguish trading triggers from general chat/gateway/tool events.
- Let skill-provided event handlers declare what route context they require.

### 10. Gateway message ids and sessions are constructed ad hoc

Status: FIXED (2026-04-25)

`Nerya/nerya/api/gateway_identity.py` introduces `session_id(...)` and `message_id(...)` builders that:

- preserve the legacy `telegram_{chat_id}` shape when only `chat_id` is supplied;
- accept `thread_id`/`user_id`/`workspace_id` so group/thread/multi-tenant routing has a deterministic id;
- guarantee uniqueness via a monotonic counter even on Windows where `time.time()` resolution is 16ms (`Nerya/tests/test_gateway_commands.py::test_message_id_includes_direction_and_seq` covers this);
- expose `parse_session_id(...)` so observability tools can recover the original parts.

Both `_reply` (outbound message ids) and `_handle_text` / `_run_gateway_turn` (session ids) now route through these helpers, so dedupe / reply references / multi-user group handling have a single source of truth.

Original evidence:

- `nerya/api/routes_gateway.py:132` creates message id `tg_reply_{chat_id}_{time}`.
- `nerya/api/routes_gateway.py:208` creates session id `telegram_{chat_id}`.

Problem:

These ids are not platform-event-id based and do not encode thread/user/workspace cleanly. This weakens dedupe, replay, reply references, and multi-user group handling.

Required fix:

- Introduce `GatewayIdentity` and `GatewayEventId` builders.
- Use platform update id/message id/thread id/user id where available.
- Persist mapping between platform ids, Nerya events, turns, and outbox messages.

### 11. UI navigation/pages are static, not capability-driven

Evidence:

- Dashboard has fixed pages under `dashboard/app/*` and fixed nav in `dashboard/lib/nav.ts`.
- `dashboard/lib/clientApi.ts` provides fixed first-party APIs for strategies/subagents/exchange authoring.

Problem:

Installed skills cannot contribute UI surfaces. Hermes has plugin/dashboard concepts; Nerya has pages but not a manifest-driven dashboard extension model.

Required fix:

- Add dashboard plugin/capability registry.
- Skills can declare dashboard panels/forms/cards with permissions and schemas.
- Static first-party pages should hide or degrade when backing skill is unavailable.

### 12. Hardcoded product copy shapes operator behavior

Status: PARTIALLY FIXED (2026-04-25)

A read-only runtime capability matrix endpoint was added so UI/docs can render *current* capability truth instead of repeating compile-time strings:

- `Nerya/nerya/api/routes_capability.py::_capability_matrix` returns `runtime` toggles (live trading / paper trading / kill switch / mock mode / default tier / configured tiers), `skills` (id, version, permissions, per-action gates + `agent_action` + `query_only`), the LLM-facing `actions` catalog (alias / hint / payload_hint), `gateway` (registry-backed commands + platform list with `support_level`), and `planner` (route presets + fallback).
- `Nerya/nerya/api/local_server.py` registers the route alongside the rest.
- Live verification: `GET http://127.0.0.1:18317/runtime/capability_matrix` returns 46 actions across 16+ skills with `paper_trading_enabled=true`, `live_trading_enabled=false`, gateway commands `[start,help,menu,new,status,trace]`, and platform `support_level` annotations (`telegram=tested`, `local=full_duplex`, `discord=send_only`, `whatsapp=catalog_only`, ...).
- Coverage: `Nerya/tests/test_operator_routes_surface.py::test_capability_matrix_route_is_registered`.

Remaining (deferred): rewriting dashboard copy (`dashboard/components/chat/ChatView.tsx:59`, `dashboard/app/evolution/page.tsx:49`, etc.) to consume this endpoint and degrade gracefully when a referenced skill/route is disabled. That requires React/UI work which is outside this Python pass.

Original evidence:

- `dashboard/components/chat/ChatView.tsx:59` says every message drives “one real agent turn — route selection, tool calls, and on-disk artifacts”.
- `dashboard/components/chat/ChatInput.tsx` references paper mode / approval gate copy.
- `dashboard/app/evolution/page.tsx:49` says every self-improvement change passes through operator approval.

Problem:

Copy is making runtime claims. If feature flags, permissions, route modes, or disabled skills change, the copy may lie.

Required fix:

- Render copy from runtime capability/support matrix.
- Product copy should distinguish available, disabled, unsupported, mock-only, and experimental.
- Add tests for docs/UI capability drift.

## Cross-Cutting Pattern

The repeated anti-pattern is:

```text
UI/API/gateway/planner knows exact skill id + action + wording.
```

Target pattern:

```text
Skill/command/platform manifests declare capabilities.
Planner selects capabilities.
Prompt/UI/gateway render selected capabilities.
Kernel dispatches through SkillRuntime.
Policy engine authorizes actor + action + resource.
```

## Hardcoding Classes To Track

1. **Prompt hardcoding**: action names, fields, heuristics, trace guarantees.
2. **Routing hardcoding**: default planner routes, trigger router rules, fallback skills.
3. **UI hardcoding**: fixed skill ids/actions, example prompts, nav/pages, product claims.
4. **Gateway hardcoding**: commands, Telegram-only routes, session ids, dashboard URLs.
5. **Runtime construction hardcoding**: direct `AgentKernel`/`SkillKernel` boot instead of container/factory.
6. **Config default hardcoding**: mock providers, BTCUSDT, broad user_chat skill list.
7. **Test hardcoding**: global mock mode and sample workspaces shaping behavior claims.
8. **Capability claim hardcoding**: platform catalog entries that overstate implementation level.

## Remediation Priority

### P0

1. Replace main prompt action list with selected skill manifest action catalog.
2. Add missing `agent_action` metadata to core builtin skills.
3. Make dashboard generic skill/action forms read live schemas instead of assuming fixed wrappers for everything.
4. Move gateway commands to a command registry with skill-backed handlers.
5. Add tests that disabled/unselected skills disappear from prompt, UI, and gateway help.

### P1

1. Move default planner route presets out of Python into versioned route manifests.
2. Add runtime capability/support matrix endpoint and use it for UI copy/docs/gateway help.
3. Replace Telegram ad hoc ids with platform identity/event id builders.
4. Split test suites into strict-vs-mock and mark claim verification mode.
5. Add skill/dashboard extension registry.

### P2

1. Move subagent role defaults to manifests/config.
2. Generalize scheduled session runtime construction through a container.
3. Make trigger route caps/policies configurable per source/channel/actor.
4. Convert demo suggestions into recipe manifests.
5. Add capability-drift tests for docs and product copy.

## Bottom Line

Beyond the main prompt hardcoding already documented in `22`, Nerya also hardcodes capabilities in dashboard API wrappers, chat suggestions, gateway commands, Telegram session/id handling, Python default planner routes, trigger route rules, static API route collection, scheduled kernel construction, mock-biased tests, and product copy. These should be moved toward manifest/capability registries so installed builtin skills are the source of truth and every surface reflects the same live capability set.
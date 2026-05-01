# 27 — Eighth-Pass Install, DB, Frontend, Lifecycle, and Residual Hardcoding Gaps

## Status (2026-04-25)

Section status:

1. **DB schema versioning** — COMPLETED. `Nerya/nerya/db/migrations.py` now ships a versioned `Migration` registry with a `schema_version(version, name, applied_at)` ledger (`apply_migrations` returns the list of newly applied versions). The registry validator rejects duplicate or non-contiguous versions at startup so renumbering accidents are caught immediately. `current_version()` returns the highest applied version. Covered by `Nerya/tests/test_db_migrations.py` (7 tests).
2. **SQLite contention / WAL** — PARTIALLY COMPLETED. `Nerya/nerya/db/sqlite.py` covers timeout + autocommit; explicit `BEGIN IMMEDIATE` + retry jitter → Plan 27 P1.
3. **Workspace state store** — COMPLETED. `Nerya/nerya/workspace/state_store.py` covers atomic JSON + RLock; corruption recovery already returns `{}` rather than crashing.
4. **Workspace layout** — COMPLETED. `Nerya/nerya/workspace/layout.py` is the single source for directory + journal names.
5. **Artifact store** — PARTIALLY COMPLETED. `Nerya/nerya/workspace/artifact_store.py` covers blob persistence; manifest + retention → Plan 27 P1.
6. **Install / service lifecycle** — PARTIALLY COMPLETED. `Nerya/nerya/install/service.py` covers Windows NSSM + Linux systemd; cross-platform polish → Plan 27 P1.
7. **Skill installer / marketplace** — PARTIALLY COMPLETED. `Nerya/nerya/skills/installer.py` covers pending/promote; rollback / enable-per-surface → Plan 30.
8. **Script sandbox** — PARTIALLY COMPLETED. `Nerya/nerya/scripts/sandbox.py` enforces denylist + workspace chroot; subprocess/seccomp/AppContainer → Plan 21 P2.
9. **Dashboard layout / theme / plugins** — PENDING. Tracked under Plans 21/30.

Status: PARTIALLY COMPLETED — runtime foundations exist; remaining items are migration-path hardening + plugin shell tracked under Plans 21/27/30.

This is another code-backed pass after `26-seventh-pass-security-approval-data-connector-gaps.md`. It focuses on areas that were still under-specified: install/update lifecycle, database/schema evolution, process/background execution, frontend state/UX hardcoding, retention/backup/export/import, and runtime sandboxing. This pass is still not a claim of exhaustiveness; it is a newly verified delta list.

## Evidence Read

### Nerya

- `nerya/db/migrations.py:1` describes migrations as a single idempotent file; `nerya/db/migrations.py:7` stores all DDL in one `MIGRATIONS` list; `nerya/db/migrations.py:10`, `18`, `26`, `35`, `45` create only `dedupe`, `cooldown`, `proposals`, `approvals`, and `llm_usage`.
- `nerya/db/sqlite.py:11` opens SQLite with `timeout=5.0` and autocommit; `nerya/db/sqlite.py:16` applies idempotent DDL, but there is no schema version table or ordered migration history.
- `nerya/workspace/state_store.py:1` is atomic JSON for runtime flags; `nerya/workspace/state_store.py:19` uses an in-process `RLock`; `nerya/workspace/state_store.py:25-26` silently returns `{}` on JSON corruption.
- `nerya/workspace/layout.py:15` hardcodes required workspace directories; `nerya/workspace/layout.py:42` hardcodes required journal names.
- `nerya/workspace/artifact_store.py:1` persists blobs, but `nerya/workspace/artifact_store.py:16-29` only provides simple write/hash helpers, with no manifest, retention, provenance, or cleanup policy.
- `nerya/install/service.py:6-8` hardcodes three service install paths; `nerya/install/service.py:28` hardcodes `nerya-agent`; `nerya/install/service.py:261` defaults install port to `18317`; `nerya/install/service.py:216`, `225-230` encode Windows NSSM-specific assumptions.
- `nerya/skills/installer.py:72-151` stages and promotes a skill, but the lifecycle is pending/promote/list rather than a full marketplace/update/rollback/enable-per-surface system.
- `nerya/scripts/sandbox.py:1-7` explicitly says the real sandbox would use subprocess/seccomp/AppContainer and current logic is best-effort; `nerya/scripts/sandbox.py:17-33` is static denylist data embedded in code.
- `dashboard/app/layout.tsx:18-26` hardcodes `lang="en"`, fixed shell layout, static max width, and no theme/provider/auth/error boundary at the root.
- `dashboard/components/Sidebar.tsx:15`, `24`, `35` stores a single sidebar collapsed flag in `localStorage`; navigation itself is imported from static `lib/nav`.

### Hermes

- `hermes_state.py:34-110` has a versioned session/message schema, `schema_version`, FTS5, and FTS triggers.
- `hermes_state.py:123-214` documents and implements multi-process SQLite contention handling using short timeout, explicit `BEGIN IMMEDIATE`, retry jitter, and WAL checkpointing.
- `hermes_state.py:252-349` runs ordered migrations through schema versions 2-6 and ensures indexes/FTS after migrations.
- `hermes_state.py:1016-1116` provides full-text session search with snippets, filters, pagination, and CJK fallback.
- `hermes_state.py:1198-1208` exposes session/export APIs for backup/import workflows.
- `tools/process_registry.py:2-29` defines background process tracking, poll/wait/kill, and interrupt support; `tools/process_registry.py:54-59` persists a process checkpoint and enforces TTL/max-process policy; `tools/process_registry.py:421-494` can spawn via non-local environment backends.
- `hermes_cli/backup.py:2-7` documents backup and import of the whole Hermes home directory.
- `hermes_cli/web_server.py:2088-2119` exposes dashboard theme discovery and persistence; `hermes_cli/web_server.py:2127-2165` discovers dashboard plugin manifests.

## Newly Found Gaps and Hardcoded Surfaces

### 1. Database Schema Is Idempotent DDL, Not a Versioned Product Contract

Nerya currently creates a few operational tables with `CREATE TABLE IF NOT EXISTS`, but it does not persist schema version, migration version, feature version, or compatibility version.

Missing vs Hermes:

- `schema_version` table and monotonic migration path.
- ordered migrations with rollback/repair instructions.
- migration tests that load an old DB and verify current runtime can read it.
- database compatibility report in `doctor`/preflight.
- automatic detection of partially-applied migrations.
- schema drift alert when code and DB are out of sync.
- upgrade safety path for operators who already have a long-running workspace.

Hardcoding to remove:

- The `MIGRATIONS` list is a hidden product contract in Python source rather than a versioned migration layer.
- Table choices encode only current trading/proposal needs, not general agent sessions, gateway messages, tool events, or memory/search indexes.

What to align:

- Add `schema_version` and ordered migration registry.
- Split operational DB domains: `sessions`, `messages`, `tool_events`, `gateway_events`, `approvals`, `artifacts`, `skills`, `memories`, `processes`.
- Add versioned acceptance tests for old DB fixtures.

### 2. Session and Message Store Still Does Not Match Hermes-Grade Search/Replay

Nerya has journals and state JSON, but the DB layer does not store normalized session messages with message IDs, roles, timestamps, tool calls, reasoning details, cost metadata, gateway source, and FTS index.

Missing vs Hermes:

- searchable message table with FTS.
- snippet search with role/source filters.
- CJK search fallback.
- message replay around a matching result.
- session metadata such as model, pricing, token counts, parent session, source, title.
- durable storage of reasoning details and tool call payloads.
- export of one session or all sessions for backup/analysis.

Hardcoding to remove:

- Journals are file-name/category driven; the agent cannot query its own historic context as a first-class capability.
- “context recall” remains dependent on handcrafted context construction and workspace files, not a schema-backed memory/search tool.

What to align:

- Implement a `SessionStore` equivalent with FTS and structured messages.
- Route gateway/chat/CLI/agent turns through this store.
- Make context compression and memory retrieval query the store instead of only reading current session payloads.

### 3. SQLite Concurrency Is Too Naive for Multi-Gateway/Multi-Agent Use

Nerya enables WAL and an in-process lock in JSON store, but it lacks Hermes-style application-level write contention handling for multi-process writers.

Missing vs Hermes:

- explicit `BEGIN IMMEDIATE` write transactions.
- jittered retry on `database is locked`/`busy`.
- WAL checkpoint policy.
- multi-process lock semantics for JSON state files.
- recovery from corrupted JSON state.
- writer metrics: retries, lock waits, failed writes.

Hardcoding to remove:

- `timeout=5.0` in `sqlite.py` hides lock contention rather than making it observable and recoverable.
- `StateStore` silently swallowing JSON corruption can erase operator state semantics.

What to align:

- Add a shared DB write helper with jitter/retry/checkpoint.
- Add file-locking for JSON state or migrate runtime state to DB.
- Treat corrupted state as an explicit diagnostic event, not `{}`.

### 4. Process and Background Job Management Is Not Hermes-Level

Nerya has service install scripts and agent loops, but it does not expose a general process registry for background commands/tools/subagents.

Missing vs Hermes:

- spawn/poll/wait/kill process API.
- background output buffering and tailing.
- interrupt-aware waits.
- host PID and sandbox PID tracking.
- crash recovery from persisted process checkpoint.
- TTL/LRU cleanup for finished jobs.
- watch patterns and notifications for background job milestones.
- environment-backed process execution for remote/sandbox environments.

Hardcoding to remove:

- Background lifecycle is split across ad hoc service install, scheduler, harness, and subagent paths rather than one managed process substrate.
- There is no unified “operator can see/stop/resume this job” abstraction.

What to align:

- Add `ProcessRegistry` as a core tool/runtime service.
- Surface process status in dashboard, gateway, and API.
- Bind process actions to the tool permission model.

### 5. Install/Update/Uninstall Lifecycle Is Too Narrow and Hardcoded

Nerya has `systemd --user`, `launchd`, and NSSM install/uninstall/status helpers, but not a full product lifecycle.

Missing vs Hermes:

- backup-before-update.
- import/restore path.
- config/schema migration during update.
- self-update/version check/rollback.
- profile-aware install paths.
- service health probe after install.
- clear repair command for broken service state.
- uninstall that can optionally preserve/delete data.
- generated support bundle with logs/config/state summary.

Hardcoding to remove:

- service name, default port, log paths, and platform behavior are embedded in code.
- Windows assumes NSSM and suggests package managers inline.
- service install is not connected to capability/profile config.

What to align:

- Add `nerya backup`, `nerya import`, `nerya update`, `nerya repair`, `nerya support-bundle`.
- Make service profiles configurable and versioned.
- Add install/update lifecycle tests on generated unit/plist/service content.

### 6. Workspace Layout and Journals Are Static, Not Profile/Capability Driven

`layout.py` defines fixed directories and journal names. That is better than random files, but still not Hermes-like capability discovery.

Missing vs Hermes:

- profile-aware home/workspace layout.
- capability manifest declaring required dirs, journals, DB tables, retention, and permissions.
- plugin/skill-specific storage namespaces.
- retention and cleanup policy per artifact/journal class.
- portability check when moving a workspace between machines.
- data-classification tags: secret, private, generated, cache, audit.

Hardcoding to remove:

- Required journals and directories should not be a global list owned by core runtime only.
- Skill/plugin storage requirements should be declared by manifests and validated by preflight.

What to align:

- Introduce a workspace schema manifest.
- Let built-in skills/gateways/register storage needs.
- Add `doctor` checks for missing/unknown/legacy paths.

### 7. Backup, Export, Import, and Retention Are Still Incomplete

Nerya has artifact persistence and journals, but no full backup/import lifecycle comparable to Hermes.

Missing vs Hermes:

- whole-home backup archive.
- restore/import with validation.
- session export and all-session export.
- redacted export for support/debugging.
- retention policies for logs, artifacts, messages, approvals, process records.
- restore conflict handling.
- backup metadata: app version, schema version, profile, OS, created_at.

Hardcoding to remove:

- Artifact writes do not carry schema/provenance/retention metadata.
- There is no central data inventory, so backup/export must guess what matters.

What to align:

- Add `BackupManifest` and export/import commands.
- Add artifact metadata sidecars or DB records.
- Add redaction-aware support bundle generation.

### 8. Frontend Shell Is Static and Not Runtime-Capability Driven

The dashboard root hardcodes English, layout width, shell, sidebar/header composition, and static navigation. This creates a “demo dashboard” feel instead of a Hermes-grade operator UI.

Missing vs Hermes:

- runtime theme discovery and persistence.
- user-installed dashboard plugins/extensions.
- capability-driven nav items.
- auth-aware root layout.
- global error boundary.
- loading/offline/connection state boundary.
- locale/timezone settings.
- mobile/responsive operator layout.
- keyboard accessibility and command palette.
- toast/notification preference center.
- stream-aware rendering of turns/tool output/gateway events.

Hardcoding to remove:

- `<html lang="en">` should come from locale config or browser/user profile.
- fixed shell dimensions and static nav should be generated from backend capabilities and permissions.
- local sidebar collapsed state is UI-only; it is not profile-scoped, synchronized, or portable.

What to align:

- Add `/api/dashboard/capabilities`, `/api/dashboard/theme`, `/api/dashboard/plugins`, `/api/profile/ui`.
- Render nav and panels from capability manifests.
- Add frontend streaming primitives for partial assistant text, tool status, approval waits, and gateway events.

### 9. Skill Installer Is Staging-Oriented, Not a Full Skill Hub

Nerya can fetch/stage/promote/list installed skills, which is a good base. But the operator experience still lacks Hermes-style marketplace/search/update/surface binding.

Missing:

- discover/search skills from registries.
- update installed skill with diff and rollback.
- enable/disable skill per profile, agent, gateway, or trust level.
- dependency resolution and version constraints.
- signed provenance or allowlisted sources.
- skill compatibility check against Nerya core version.
- UI to inspect permissions before install.
- test sandbox for newly-installed skills before promotion.

Hardcoding to remove:

- Fetch kinds and static analysis policy live in installer code.
- Promotion only moves directories; it does not update a capability registry consumed by context builder/tool permission/frontend nav.

What to align:

- Create skill registry DB tables and capability index.
- Make skill install/update emit capability deltas.
- Wire skill enablement into context construction and tool schemas.

### 10. Runtime Sandbox Is Explicitly Best-Effort and Denylist-Based

The current script sandbox self-documents that it is not a real isolation boundary.

Missing vs a Hermes-grade operator agent:

- subprocess isolation by default.
- OS-specific sandbox profile: AppContainer/seccomp/pledge/container.
- filesystem allowlist instead of denylist.
- network allow/deny policy per skill/tool.
- CPU/memory/wall-time limits.
- stdout/stderr capture with size limits.
- audit log of blocked operations.
- permission prompt when a script requests new capability.
- separate trust tiers for built-in, installed, generated, and user-provided scripts.

Hardcoding to remove:

- `_BLOCKED_PATH_NEEDLES` and `_BLOCKED_ENV_KEYWORDS` are static prompt-era security assumptions encoded in code.
- Scripts cannot declare why they need a path/env/network capability and route through approval.

What to align:

- Replace denylist sandbox with capability-scoped runner.
- Bind script execution to skill manifest permissions.
- Persist sandbox events and expose them in dashboard/API.

### 11. Frontend/API Auth and Operator State Need Product-Grade Boundaries

Earlier docs covered coarse API auth, but this pass adds UI-state and lifecycle auth boundaries.

Missing:

- per-route frontend auth boundary.
- persisted session refresh and logout handling.
- CSRF strategy for dashboard mutations.
- user/profile scoped local UI state.
- access-controlled dashboard plugins.
- permission-aware hiding/disabling of tool/action controls.
- audit trail for operator UI actions.

Hardcoding to remove:

- Dashboard assumes one trusted operator shell.
- Static nav means unauthenticated or unauthorized capabilities can be discoverable even if API rejects them later.

What to align:

- Add UI auth provider and route guards.
- Add capability/permission claims to dashboard bootstrap payload.
- Make every operator action carry actor/session/source metadata.

### 12. Observability Still Lacks Queryable Lifecycle Telemetry

Nerya journals are useful, but lifecycle signals remain scattered and file-based.

Missing:

- queryable event store for install/update/service/process/db/frontend/gateway events.
- standard event schema with actor, source, correlation_id, session_id, workspace_id.
- dashboard timeline that merges agent turns, tools, gateway messages, approvals, processes, and service health.
- metrics for DB lock retries, gateway delivery failures, process exits, frontend disconnects, backup results.
- support bundle and operator diagnostics built from the same event schema.

Hardcoding to remove:

- Journal categories in `layout.py` are fixed strings, not event schema types.
- Logs are organized for developers, not for an operator asking “what just happened and what is stuck?”

What to align:

- Introduce an `events` table or append-only event store.
- Emit lifecycle telemetry from DB, process registry, gateway, frontend SSE/WebSocket, approval, and skill installer.

### 13. i18n, Timezone, and Locale Are Not Just Cosmetic

Because Nerya spans gateway messages, trading timestamps, approvals, and dashboard state, locale/timezone hardcoding creates real operator mistakes.

Missing:

- profile timezone.
- timestamp display policy: absolute, relative, exchange-local, gateway-local.
- locale-aware number/price formatting.
- translation strategy for dashboard and gateway operator messages.
- CJK search/retrieval path for memory/session history.
- consistent date parsing in API payloads and human gateway commands.

Hardcoding to remove:

- `lang="en"` in the dashboard root.
- mixed timestamp formatting across journals, gateway replies, and UI.

What to align:

- Add profile locale/timezone settings.
- Normalize all persisted timestamps to UTC plus source timezone metadata.
- Add CJK-capable search in session/message store.

### 14. Error Recovery and “Stuck State” Handling Is Still Underdesigned

The user experience complaints likely come not only from missing features, but from poor recovery when a turn/tool/gateway/process gets stuck.

Missing:

- stuck turn detector.
- stuck approval detector.
- stuck gateway delivery retry/dead-letter.
- stuck background process cleanup.
- interrupted message replacement semantics.
- frontend reconnect and replay from last event ID.
- idempotent resume after server restart.
- operator-visible “why blocked” reason.

Hardcoding to remove:

- Busy/interrupt semantics should not be one-off agent-loop branches; they need a durable state machine.
- Gateway and dashboard should both see the same state machine, not infer from partial JSON blobs.

What to align:

- Define turn/session/process/gateway delivery state machines.
- Persist state transitions.
- Add replayable event stream and dead-letter queues.

### 15. Capability Truth Is Still Split Across Code, Docs, Frontend, and Runtime

The core pattern across this pass: Nerya still has product truth hardcoded in multiple places.

Examples:

- DB schema truth: `migrations.py`.
- workspace truth: `layout.py`.
- service truth: `install/service.py`.
- skill lifecycle truth: `skills/installer.py`.
- frontend nav/shell truth: dashboard static files.
- sandbox security truth: `scripts/sandbox.py` denylist.
- session/context truth: context builder and journals instead of session DB.

Required alignment principle:

- Capability truth should be declared in manifests/profiles/policies and compiled into context/tool schemas/frontend nav/API permissions, not manually duplicated.
- Built-in skills should provide tool behavior and operator instructions; the core prompt/context should not embed product actions directly.
- Every declared capability needs implementation evidence, tests, UI/API exposure, and permission policy.

## Add to Roadmap

### P0 — Stop Lying About Runtime Capability

- Add a capability registry that says `declared`, `implemented`, `enabled`, `permitted`, `tested`.
- Make dashboard/API/gateway/context builder read from this registry.
- Mark current unsupported Hermes-like features as missing rather than pretending parity.

### P0 — Build Versioned Session/Message/Event Storage

- Add schema-versioned DB migrations.
- Add sessions/messages/tool_events/gateway_events/events/processes/artifacts tables.
- Add FTS/CJK fallback and export/import.

### P0 — Add Durable Interrupt/Replay/Stuck-State Semantics

- Create state machines for turns, gateway deliveries, approvals, and processes.
- Add replayable stream with resume cursor.
- Add dead-letter and retry controls.

### P1 — Add Process Registry and Backup/Restore

- Implement background process registry with poll/wait/kill/recover.
- Add backup/import/support-bundle commands.
- Add service repair/update lifecycle.

### P1 — Make Frontend Runtime-Driven

- Add auth-aware layout, capability-driven nav, theme/plugin endpoints, global error/offline boundaries, and stream-aware turn rendering.
- Add profile-scoped UI state for locale/timezone/theme/sidebar.

### P1 — Replace Denylist Sandbox With Capability Runner

- Move script execution to subprocess/container boundary.
- Drive allowed filesystem/network/env access from skill/tool permission policy.
- Emit sandbox audit events.

## Acceptance Tests to Add

- Load a v1 DB fixture, migrate to current, and verify schema version and data preservation.
- Simulate two writers contending on SQLite and assert jitter retry succeeds without long UI freeze.
- Create a session with CJK messages and verify search returns snippets.
- Start a background process, poll output, wait, kill, restart runtime, and recover checkpoint.
- Backup a workspace, import into a fresh workspace, and verify sessions/skills/config/artifacts are present.
- Install a skill, inspect permissions, promote it, disable it, update it, rollback it.
- Dashboard boot with no auth, expired auth, and restricted role; verify nav/actions match permissions.
- Frontend reconnect after stream interruption and replay from last event ID.
- Sandbox script attempts env/path/network access and produces audited deny/approval events.
- Service install generated unit/plist/NSSM config includes configured profile, port, workspace, logs, and health check.

## Do Not Claim Yet

Do not claim Nerya has Hermes-level parity in:

- session history/search/replay,
- DB migration safety,
- process/background execution,
- backup/restore/update lifecycle,
- frontend runtime plugins/themes/auth/offline boundaries,
- skill marketplace/update/rollback,
- capability-driven dashboard/API/context truth,
- real sandbox isolation,
- multi-process lock handling,
- stuck-state recovery.
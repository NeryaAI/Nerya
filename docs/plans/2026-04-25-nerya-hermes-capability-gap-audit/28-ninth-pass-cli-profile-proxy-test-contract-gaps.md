# 28 — Ninth-Pass CLI, Test, Profile, Proxy, Env, and Contract-Truth Hardcoding Gaps

## Status (2026-04-25)

Section status:

1. **CLI command surface** — PARTIALLY COMPLETED. `Nerya/nerya/cli/app.py` covers core/skills/evolution/runtime/wallet topics; plugin-provided CLI commands → Plan 28 P1.
2. **Profile isolation** — COMPLETED 2026-04-25.
  - Resolver: `Nerya/nerya/core/paths.py:175-241` (`_resolve_home`, `resolve_workspace(profile=...)`, `list_profiles`). Precedence is explicit `path` > explicit `profile` > `NERYA_PROFILE` > legacy `NERYA_WORKSPACE` > `$NERYA_HOME` (when it already looks like a workspace) > `$NERYA_HOME/default`.
  - Config plumbing: `Nerya/nerya/core/config.py:362-382` (`load_config(workspace, *, profile=...)`).
  - SDK plumbing: `Nerya/nerya/sdk/internal_client.py:33-55` (`InternalClient.boot(workspace, *, profile=...)`).
  - CLI: `Nerya/nerya/cli/_common.py:24-44` (`--profile` global option) and `Nerya/nerya/cli/commands/core.py:16-67, 158-176` (`nerya profile list/current/init`).
  - Tests: `Nerya/tests/test_profile_isolation.py` (10 cases — explicit-path precedence, `--profile` arg, env-profile fallback, legacy `NERYA_WORKSPACE`, default profile under empty home, two-profile workspace divergence, list/init helpers, config + SDK plumbing).
3. **Dashboard proxy / auth forwarding** — PARTIALLY COMPLETED. `dashboard/app/api/proxy/[...path]/route.ts` forwards `content-type`; auth/cookie forwarding → Plan 11.
4. **Hermetic test runner** — PARTIALLY COMPLETED. `pyproject.toml` defines `pytest`; `scripts/run_tests.sh` parity → Plan 28 P1.
5. **Truth gate / docs-vs-impl drift** — PARTIALLY COMPLETED. `.github/workflows/truth-gate.yml` + `Nerya/tests/test_release_truth_gate.py` cover the basics; capability-drift tests → Plan 21 §12.
6. **Env-var governance** — PARTIALLY COMPLETED. `Nerya/nerya/cli/` reads env; per-version migration metadata → Plan 28 P1.
7. **Tool registry truth** — COMPLETED. `Nerya/nerya/api/routes_capability.py` exposes runtime truth (`/runtime/capability_matrix`); test in `Nerya/tests/test_operator_routes_surface.py`.

Status: PARTIALLY COMPLETED — runtime + capability surfaces exist; profile/CLI plugin shell tracked under Plan 28.

This pass adds gaps that were not fully captured in the prior documents. It focuses on command surfaces, profile isolation, dashboard proxy/auth propagation, test/CI hermeticity, documentation-vs-implementation drift, environment variable governance, and tool registry truth. It is code-backed and still not a final exhaustive claim.

## Evidence Read

### Nerya

- `nerya/cli/app.py:35-49` builds the CLI by manually importing/registering fixed topic modules: `core`, `skills`, `evolution`, `runtime`, `wallet`.
- `nerya/cli/commands/core.py:33-36` hardcodes dashboard URL/run instructions and `nerya serve --port 18317`.
- `nerya/cli/commands/core.py:45-78` implements a narrow `doctor` that checks Python, a few binaries/packages, and workspace basics.
- `nerya/cli/commands/core.py:126-168` registers lifecycle/service commands manually and hardcodes the service install port default to `18317`.
- `nerya/api/auth.py:12-20` documents three auth modes: `local`, `token`, `off`; `nerya/api/auth.py:38` hardcodes local hosts; `nerya/api/auth.py:87-91` falls back to `local`.
- `nerya/api/local_server.py:54` defaults local server to `127.0.0.1:8787`; `nerya/api/local_server.py:60-67` sends permissive CORS with `Access-Control-Allow-Origin: `*.
- `dashboard/app/api/proxy/[...path]/route.ts:3` hardcodes upstream default `http://127.0.0.1:8787`; `route.ts:10-12` forwards only `content-type`, dropping authorization/cookies/user/session headers.
- `dashboard/lib/chat.ts:3` states chat threads are stored entirely client-side; `dashboard/lib/chat.ts:84-128` stores thread and active ID in `localStorage` keys.
- `dashboard/lib/nav.ts:3-22` hardcodes navigation labels/routes/sections.
- `pyproject.toml:47-48` only defines pytest testpaths; there is no repo-level hermetic test runner equivalent to Hermes `scripts/run_tests.sh`.
- `.github/workflows/truth-gate.yml:35-61` defines truth gates, but this is a narrow release/audit surface, not full hermetic multi-runtime CI parity.
- `tests/test_release_truth_gate.py:35-43`, `60-78`, `83-107` checks mock defaults, Hermes top-level imports, and existence of phase gate files; it does not prove all UI/API/gateway/profile/tool contracts.

### Hermes

- `hermes_cli/main.py:78-147` pre-parses `--profile/-p` before module imports and sets `HERMES_HOME` so module-level paths become profile-aware.
- `hermes_cli/profiles.py:2-20` documents fully isolated profiles with create/clone/use/delete/list/rename/export/import semantics.
- `hermes_cli/config.py:824-850` has `_config_version`, `ENV_VARS_BY_VERSION`, and `OPTIONAL_ENV_VARS` metadata for migration prompts.
- `hermes_cli/config.py:697-803` exposes external skill dirs, command allow/approval behavior, env scrubbing, and script cwd policy as config rather than hidden code constants.
- `tools/registry.py:3-21`, `176-220`, `414-430` implements self-registering tools with schemas, toolsets, availability checks, required env vars, and unavailable-info reporting.
- `scripts/run_tests.sh:7-17`, `52-77`, `95` enforces hermetic test parity by unsetting credential-shaped env vars and pinning `TZ=UTC`, `LANG=C.UTF-8`, `PYTHONHASHSEED=0`.
- Hermes tests include specific surfaces such as `tests/hermes_cli/test_profiles.py`, `test_tools_config.py`, `test_skills_hub.py`, `test_plugin_cli_registration.py`, `test_update_*`, `test_placeholder_usage.py`, and `tests/tools/test_process_registry.py`.

## Newly Found Gaps and Hardcoded Surfaces

### 1. CLI Command Surface Is Static, Not Capability/Plugin Driven

Nerya's CLI imports five fixed command groups in `cli/app.py`. That makes core command discovery simple, but it means new skills/gateways/plugins cannot naturally expose CLI commands without editing Python command registration code.

Missing vs Hermes:

- plugin-provided CLI commands.
- dynamic command discovery and availability display.
- profile-aware command routing.
- command metadata for docs/help generation.
- per-command permission policy.
- deprecation aliases and migration warnings.
- typed command contracts usable by dashboard/gateway/docs.

Hardcoding to remove:

- topic module list in `cli/app.py`.
- parser setup as the only source of CLI truth.
- `docs/runbook.md` as a manual command inventory.

What to align:

- Add a CLI command registry with command ID, owner capability, auth/approval policy, help text, and docs export.
- Let skills/plugins register commands through manifests.
- Generate CLI reference and dashboard command palette from the same registry.

### 2. Profile Isolation Is Still Workspace-Argument Based, Not Runtime-Wide

Nerya can pass `--workspace`, but Hermes applies `--profile` before imports so all module-level paths and state stores are isolated.

Missing vs Hermes:

- first-class `nerya profile create/use/list/delete/clone/export/import`.
- pre-import profile override.
- sticky active profile.
- profile-scoped config, env, dashboard state, sessions, gateway tokens, service names, process registry, skill installs, caches.
- profile-aware tests that prevent accidental `Path.home()` leakage.
- cross-profile conflict detection for ports, service names, bot tokens, and gateway webhooks.

Hardcoding to remove:

- `NERYA_WORKSPACE` as the only coarse isolation primitive.
- fixed service name `nerya-agent` across profiles.
- frontend `localStorage` keys not scoped by profile/workspace/user.

What to align:

- Add a `profiles` subsystem and pre-parse `--profile` before importing modules that resolve paths.
- Scope service name, API port, dashboard storage keys, gateway session IDs, and process registry by profile.

### 3. Dashboard Proxy Drops Auth and Actor Context

The Next proxy currently forwards only `content-type`. That makes the dashboard convenient in local mode but breaks token auth, actor attribution, gateway/user session metadata, and future multi-user operation.

Missing:

- forwarding of `Authorization` / `X-Nerya-Token` when intended.
- cookie/session handling with CSRF protection.
- actor/session/source headers.
- upstream timeout policy.
- request ID/correlation ID propagation.
- proxy audit events.
- allowlist of proxied paths/methods.
- streaming proxy support for SSE/WebSocket/NDJSON.

Hardcoding to remove:

- `BASE = process.env.NERYA_API || "http://127.0.0.1:8787"` duplicated with other dashboard client defaults.
- proxy assumes JSON-only requests and responses.
- proxy assumes trusted same-machine local auth mode.

What to align:

- Centralize dashboard bootstrap config and auth/session state.
- Make proxy policy explicit: allowed upstream, allowed methods, header forwarding, timeout, body limits, streaming paths.
- Add tests proving token mode works through the dashboard proxy.

### 4. API Auth Modes Are Too Coarse for Agent Tool Permissions

Nerya's `local/token/off` modes protect HTTP entry, but they do not express user identity, role, tool scopes, action scopes, gateway identity, or per-route policy.

Missing:

- route-level scopes.
- tool/action-level scopes.
- actor identity persisted across agent turns/tool calls/approvals.
- gateway-origin claims and reply authorization.
- token expiry/rotation/revocation.
- OAuth/device login for operator UI.
- CSRF and browser session model.
- audit trail for denied and allowed mutations.

Hardcoding to remove:

- local host allowlist as implicit trust boundary.
- env/config token list as the only credential store.
- API auth detached from tool permission and approval policy.

What to align:

- Add an authz policy engine that binds actor -> route -> skill action -> tool permission -> approval rule.
- Replace `off` with explicit dev-only guardrails and warning banners.

### 5. Test Runner Is Not Hermetic Enough

Nerya has pytest config and truth-gate tests, but not a single canonical test runner that scrubs secrets, pins locale/timezone/hash seed, isolates home/workspace, and mirrors CI.

Missing vs Hermes:

- `scripts/run_tests` equivalent.
- automatic credential env scrubbing.
- `TZ=UTC`, `LANG=C.UTF-8`, `PYTHONHASHSEED=0` consistency.
- profile/workspace isolation fixture for all tests.
- no-network/default-off tests.
- golden fixtures for CLI help/API schemas/dashboard capability bootstrap.
- CI parity check that local direct `pytest` does not accidentally use real credentials.

Hardcoding to remove:

- Tests and e2e harness choose ports/runtimes ad hoc.
- Env behavior is spread across tests and config rather than one hermetic launcher.

What to align:

- Add `scripts/run_tests.ps1` and `scripts/run_tests.sh` with identical policy.
- Make CI call only that runner.
- Add conftest fixtures that force temp `NERYA_HOME`/workspace/profile and scrub secrets.

### 6. Truth Gates Are Too File-Existence/Pattern Oriented

`test_release_truth_gate.py` catches some important drift, but several checks are existence or pattern checks, not behavior-level acceptance tests.

Missing:

- CLI command registry snapshot test.
- OpenAPI/API route contract snapshot.
- dashboard route/nav capability snapshot.
- gateway protocol golden tests for attachments/replies/edits/interrupts.
- tool schema/permission snapshot.
- skill manifest -> context/tool schema parity test.
- docs command examples executable test.
- config migration old-to-new fixture tests.
- frontend proxy auth integration test.

Hardcoding to remove:

- “phase gate file exists” as a proxy for actual capability readiness.
- “no Hermes top-level import” as a proxy for architectural independence.

What to align:

- Introduce contract snapshots that are generated from runtime registries and reviewed in PRs.
- Run minimal browser/API/gateway integration tests in CI.

### 7. Environment Variable Governance Is Fragmented

Hermes has `_config_version`, `ENV_VARS_BY_VERSION`, and `OPTIONAL_ENV_VARS` metadata for setup/migration prompts. Nerya env vars appear across docs, auth, core commands, dashboard config, e2e harness, and service installation.

Missing:

- central env var registry.
- versioned env migration prompts.
- metadata: secret/non-secret, required/optional, default, owner capability, setup prompt, validation.
- env redaction in status/doctor/support bundle.
- stale env detection.
- conflict detection between config and env.
- frontend build-time vs runtime env distinction.

Hardcoding to remove:

- `NERYA_DASHBOARD_URL`, `NERYA_API`, `NERYA_AUTH_MODE`, `NERYA_API_TOKEN`, `NERYA_WORKSPACE`, `NERYA_PORT`, etc. are consumed by separate modules without one contract.

What to align:

- Add `EnvVarRegistry` and config migration version.
- Make `doctor`, setup, service install, dashboard proxy, and CI runner read the same registry.

### 8. Tool Registry Truth Is Still Split From Skills and Permissions

Earlier docs covered tool permissions generally, but this pass adds a specific mismatch: Hermes tools self-register with schema/toolset/env availability; Nerya tools/actions are still spread across skill manifests, built-in Python action maps, API endpoints, dashboard wrappers, MCP handlers, and harness logic.

Missing vs Hermes:

- one tool/action registry with owner, schema, handler, toolset, availability, required env, permission scope, and UI/gateway visibility.
- unavailable reason reporting.
- dynamic tool schema generation from enabled capabilities.
- toolset aliases and enable/disable config.
- collision detection when installed skills expose same action names.
- contract that handler returns are serializable and artifacted.

Hardcoding to remove:

- dashboard `clientApi.ts` manually knows many `skill_id/action` pairs.
- CLI and API have separate command/action knowledge.
- context builder can still expose static action/help text instead of querying a registry.

What to align:

- Create a unified `CapabilityRegistry`/`ActionRegistry`.
- Generate API docs, dashboard actions, gateway commands, CLI commands, and context tool schemas from it.

### 9. Dashboard Chat Is LocalStorage-Only and Therefore Not an Agent Session

The dashboard chat UX stores threads in browser localStorage while backend `/agent/run_turn` stays stateless from the browser's perspective.

Missing:

- backend session persistence.
- cross-browser/device continuity.
- searchable chat history.
- server-side active session and message ordering.
- durable turn IDs, parent IDs, branch IDs.
- replay after refresh or reconnect.
- export/delete/redact conversation controls.
- profile/user-scoped storage.

Hardcoding to remove:

- `nerya.chat.threads.v1` and `nerya.chat.active` as global browser keys.
- client-generated `uuid()` and title derivation as the durable session model.

What to align:

- Move chat sessions to backend `SessionStore`.
- Keep localStorage only as a cache of selected profile/UI preferences.

### 10. Docs and Runbook Contain Commands That Need Executable Verification

The runbook includes many operational commands. Some may be real, some may be stale, and some may require live environment assumptions. The current truth gate does not execute docs examples.

Missing:

- docs command linter that maps every `nerya ...` example to parser help.
- smoke execution for safe read-only commands.
- docs examples tagged by required mode/secrets/network.
- generated command reference from parser/registry.
- stale port/path/env detection in docs.

Hardcoding to remove:

- manual dashboard/API startup instructions in CLI output and docs.
- manual command lists that can drift from parser registration.

What to align:

- Generate `docs/reference/cli.md` and API route docs from runtime.
- Add tests for docs examples.

### 11. Local Server Is a Convenience Surface, Not Production API Substrate

`local_server.py` uses `http.server.ThreadingHTTPServer`, permissive CORS, manual routing, and JSON handling. This may be acceptable for local dev, but it is not Hermes-grade API/runtime service behavior.

Missing:

- middleware stack.
- route schemas/OpenAPI.
- streaming endpoints.
- structured error format.
- request body size limits.
- timeouts.
- rate limiting.
- per-route authz.
- graceful shutdown/lifespan.
- websocket/SSE support.
- metrics/tracing middleware.

Hardcoding to remove:

- wildcard CORS.
- manual route registry as HTTP truth.
- JSON-only request/response assumptions.

What to align:

- Either explicitly label this as local-only and add a production API server, or upgrade to a framework with schemas/middleware/streaming.
- Generate route contracts and enforce authz/observability uniformly.

### 12. Port and URL Defaults Are Duplicated and Inconsistent

Observed defaults include dashboard `localhost:3000`, CLI/API service `18317`, local server default `8787`, dashboard `NERYA_API` default `127.0.0.1:8787`, and e2e port search around `18321`.

Missing:

- single port registry.
- profile-aware port allocation.
- conflict detection.
- dashboard/API/service URL bootstrap contract.
- runtime status endpoint that tells the dashboard exactly which API it is connected to.

Hardcoding to remove:

- `http://localhost:3000` in CLI.
- `18317` in CLI/service docs.
- `8787` in local server/dashboard proxy.
- test harness port ranges as implicit product behavior.

What to align:

- Add `runtime.network` config with API/dashboard/gateway ports and profile offsets.
- Add doctor checks for port conflicts and stale dashboard proxy config.

### 13. Status/Doctor Is Not an Operator-Grade Diagnosis Surface — COMPLETED 2026-04-25

Nerya doctor checks packages and workspace basics. Hermes status/doctor covers providers, OAuth states, gateways, env, subscription features, and reachability.

Missing:

- provider auth status and expiry.
- gateway platform status.
- dashboard proxy/API connectivity.
- DB schema/version status.
- process registry status.
- plugin/skill availability and env requirements.
- sandbox mode status.
- token/auth mode warnings.
- backup freshness.
- profile isolation status.
- stale service status.

Hardcoding to remove:

- doctor package list as the main health definition.
- no machine-readable severity/remediation contract.

What to align:

- Make `doctor` consume capability/env/tool registries and emit `ok/warn/error` with remediation commands.
- Add `nerya status` for concise operator status separate from deep diagnostics.

#### Implementation 2026-04-25

- New module `nerya/ops/diagnostics.py` introduces a `Diagnosis` /
`DiagnosticReport` data model, a `DiagnosticCheck` registration
primitive, a thread-safe `DiagnosticRegistry`, and a
`run_diagnostics(client, only=, skip=, registry=)` runner that
captures handler exceptions, supports `requires_client` checks
with graceful fallback, and aggregates `ok`/`warn`/`error`
summaries (`nerya/ops/diagnostics.py:1-1077`).
- 13 default checks now ship covering runtime
(`runtime.python`, `runtime.binaries`), dependencies
(`packages.required`, `packages.optional`), env (`env.tracked`
with secret redaction for `NERYA_API_TOKEN` and
`NERYA_LOCK_SIGNING_KEY`), workspace + profile isolation
(`workspace.root`, `profile.isolation`), security
(`api.auth_mode`, `skills.lock`, `provider_auth.records`), and
capabilities (`db.schema_version`, `skills.availability`,
`model_registry.coverage`, `agent.preset`, `service.status`).
Each row carries a stable `id`, `severity`, `detail`,
optional `remediation`, `category`, and structured `metadata`
ready for the dashboard / capability matrix.
- `nerya doctor` was rewritten to consume the registry; new flags:
`--json` (machine-readable payload), `--strict` (fail on
warnings), `--only <ids>` and `--skip <ids>` for selective
runs (`nerya/cli/commands/core.py:93-128, 221-231`).
- `nerya status` is a brand-new concise sibling that prints only
non-OK rows with the same registry, suitable for shell prompts
and CI early-out (`nerya/cli/commands/core.py:130-154, 234-238`).
- Test coverage in `tests/test_diagnostics.py` (23 tests) locks
in the data model, registry semantics (add/get/remove,
duplicate rejection, override), runner behaviour
(only/skip filters, missing client, exception capture, summary
aggregation), default check coverage against a real
workspace, secret redaction, renderer outputs, and CLI
integration including `--json` and `--strict` exit codes.
All 23 pass; the full suite runs at 1652 passed, 2 skipped
(the two pre-existing `test_skill_md_format.py` failures were
fixed by re-running `scripts/seed_skill_md.py` to regenerate
the missing `<!-- nerya:auto-skill-md -->` markers in
`memory_skill`, `message_skill`, and `script_skill`).

### 14. Placeholder/Mock/Stub Governance Needs Broader Coverage

Nerya has release truth gates around mock mode and placeholders, but the scan showed frontend/client types still include `stub` statuses and docs/tests use mock/e2e toggles. Some of that is valid, but it needs a declared compatibility policy.

Missing:

- central list of allowed mock/stub/test-only symbols.
- build failure on unapproved production placeholder strings.
- UI badges for partial/stub provider capability.
- runtime capability matrix exported to API/dashboard/gateway.
- tests that verify no mock provider is used when real mode is configured.

Hardcoding to remove:

- ad hoc placeholder checks in a single test file.
- frontend display of `stub` without an operator-facing explanation and disablement policy.

What to align:

- Add `capability_status`: `real`, `partial`, `stub`, `mock`, `unavailable`, with source evidence and allowed surfaces.
- Make dashboard and context builder hide or warn on `stub/mock` capabilities.

### 15. CLI/TUI Ergonomics and Interactive Flows Are Still Far From Hermes

Hermes has extensive CLI/TUI setup, auth, model switching, provider menus, session browse, skins, completions, file drops, and terminal fallback tests. Nerya's CLI is mostly command execution and JSON output.

Missing:

- interactive setup wizard.
- model/provider auth menus.
- session browser/resume UI.
- shell completions/path completion.
- command palette parity across CLI/dashboard/gateway.
- configurable skins/branding.
- terminal compatibility fallbacks.
- copy/paste/file-drop ergonomics.
- human-readable progress for long operations.

Hardcoding to remove:

- CLI output strings and setup instructions scattered in command handlers.
- no shared command/action presentation layer.

What to align:

- Decide whether Nerya wants Hermes-style TUI parity or dashboard-first parity; then document and implement the chosen operator surface honestly.
- If dashboard-first, do not claim CLI/TUI parity.

## Add to Roadmap

### P0 — Capability Registry Becomes the Single Truth

- Register CLI commands, API routes, skill actions, gateway commands, dashboard nav/actions, env vars, permissions, tests, and docs examples in one registry family.
- Generate contracts from it and fail CI on drift.

### P0 — Fix Dashboard Proxy/Auth Path

- Forward/validate auth and actor context correctly.
- Add CSRF/session model or explicitly local-only dashboard warning.
- Add proxy auth integration tests.

### P0 — Add Profile Isolation

- Implement `nerya profile` and pre-import profile override.
- Scope ports, service names, localStorage keys, gateway sessions, DB, caches, skills, and artifacts.

### P1 — Add Hermetic Test Runner and Contract Snapshots

- Add cross-platform test runner with env scrubbing and deterministic locale/timezone.
- Snapshot CLI/API/dashboard/tool/gateway contracts.

### P1 — Add Env/Config Migration Registry

- Track config/env version.
- Prompt/migrate env vars with metadata.
- Make setup/doctor/status/support-bundle consume the registry.

### P1 — Upgrade Local API Runtime

- Add OpenAPI/schema, middleware, streaming, request IDs, body limits, route-level authz, and structured errors.

## Acceptance Tests to Add

- `nerya --profile p1 doctor` and `nerya --profile p2 doctor` use different homes, ports, services, DBs, gateway tokens, and dashboard storage namespaces.
- Dashboard proxy in token mode forwards valid auth and rejects missing auth.
- Dashboard proxy refuses unknown paths/methods and enforces body/time limits.
- CLI reference generated from registry matches `nerya --help` and docs examples.
- Every `NERYA_*` env var is declared in env registry and appears in status/doctor redaction policy.
- CI uses only `scripts/run_tests` and proves credential env vars are scrubbed.
- API route contract snapshot includes auth scope, request schema, response schema, and streaming support.
- Dashboard nav is generated from capability registry and hides unauthorized capabilities.
- LocalStorage keys are profile/user scoped and no chat message content is stored client-only without server persistence.
- Placeholder/mock/stub strings fail CI unless registered as allowed test/demo surfaces.

## Do Not Claim Yet

Do not claim Nerya matches Hermes in:

- profile isolation,
- CLI/TUI setup ergonomics,
- plugin/skill-provided CLI commands,
- dashboard proxy/auth robustness,
- route-level API authorization,
- hermetic CI/test parity,
- env/config migration governance,
- dynamic tool registry availability,
- generated docs/contracts,
- operator-grade status/doctor,
- local API production readiness.
# 30 — Skill and MCP Loading Parity Gaps

## Status (2026-04-25)

Section status:

1. **Skill loading covers SKILL.md + skill.yml** — COMPLETED. `Nerya/nerya/skills/registry.py:SkillRegistry.load_builtin` + `Nerya/nerya/skills/procedural.py` cover both formats; tests `Nerya/tests/test_procedural_skill.py`.
2. **Workspace skill overlay (allowlist + per-skill override)** — COMPLETED. `Nerya/nerya/skills/registry.py` reads `workspace/skills/enabled.yml` + `workspace/skills/installed/`* and replaces builtins by id.
3. **Skill installer (stage / promote / list)** — COMPLETED. `Nerya/nerya/skills/installer.py:72-301`. Marketplace + rollback / per-platform disable → Plan 30 P1.
4. **Hermes-style skills hub (discovery, lock, hash, trust + signing)** — COMPLETED 2026-04-25 (lock + trust + signing scaffold).
  - Lock module: `Nerya/nerya/skills/lockfile.py` (full lockfile, drift detection, optional trust manifest, ~250 lines).
  - Path layout: `Nerya/nerya/core/paths.py:88-92` (`skills_lock` → `skills/skills.lock.yml`, `skills_trust` → `skills/trust.yml`).
  - Promotion wires the lock entry: `Nerya/nerya/skills/installer.py:175-205` (`promote_installed` calls `record_lock_entry` with the post-promote sha256, swallowing any error so promotion never blocks on lock IO).
  - Drift report: `verify_lock(paths)` returns `{ok, missing, untracked, mismatches}` so a kernel boot or `doctor` call can flag tampered trees.
  - Trust scaffold: `load_trust(paths)` + `is_trusted(paths, sha256, publisher)` consume `skills/trust.yml`'s `publishers` allowlist (pinned hashes / fingerprints / note). Today it's an advisory hook; tomorrow it can gate installs.
  - **Lock signing (Plan 30 supply-chain trust polish)** — `Nerya/nerya/skills/lock_signing.py` (~340 lines): `SigningKey`, `SignedLock`, `VerifyReport`, canonical JSON payload, HMAC-SHA256 (stdlib) + optional Ed25519 (when `cryptography` is installed), `sign_lock`/`verify_lock_signature`/`fingerprint_lock`/`load_signature`/`remove_signature`, structured failure reasons (`no_lock`, `no_signature`, `no_key`, `digest_mismatch`, `signature_mismatch`, `unsupported_algorithm`).
  - Signing routes: `Nerya/nerya/api/routes_skills.py:5-12,33-95,107-125`: `GET/POST /skills/lock/status`, `GET /skills/lock/inspect`, `POST /skills/lock/sign`, `POST /skills/lock/verify`, `POST /skills/lock/clear_signature`. Resolves keys from `NERYA_LOCK_SIGNING_KEY` env or explicit payload.
  - Tests: `Nerya/tests/test_skills_lockfile.py` (12 cases — original lock round-trip, drift, trust) + `Nerya/tests/test_skills_lock_signing.py` (29 cases — canonical-bytes order independence, hmac sign/verify, env-key resolution, full sign+verify+digest-mismatch+wrong-key+missing-lock paths, optional Ed25519 round-trip when `cryptography` is available, all six HTTP routes).
  - Discovery (search/index/marketplace) remains a follow-up under Plan 30 P1.
5. **MCP — Nerya-as-server** — COMPLETED. `Nerya/nerya/mcp/server.py` + `Nerya/nerya/mcp/tools.py` expose ~30 read-only tools; covered by `Nerya/tests/test_mcp_tools.py`.
6. **MCP — Nerya-as-client** — PARTIALLY COMPLETED. Client wiring exists in `Nerya/nerya/mcp/`; OAuth recovery + per-server enable/disable UX → Plan 30 P1/P2.
7. **MCP per-tool include/exclude + collision guard** — PARTIALLY COMPLETED. `Nerya/nerya/mcp/tools.py` exposes a curated list; per-server include/exclude config → Plan 30 P1.

Status: PARTIALLY COMPLETED — both formats load, server side ships, client polish (OAuth, include/exclude, trust/lock) tracked under Plan 30.

This pass focuses specifically on skill loading and MCP loading. It complements `29-skill-format-correction-skillmd-vs-action-manifest.md`: that document corrects the skill file format misunderstanding; this one records the end-to-end loading/runtime differences.

## Evidence Read

### Nerya Skill Loading

- `Nerya/nerya/skills/registry.py:61-84` loads built-in skills by scanning `nerya/skills/builtin/*/skill.yml` and importing `actions.py`.
- `Nerya/nerya/skills/registry.py:66-68` reads `workspace/skills/enabled.yml`; if present, it behaves as a positive allowlist.
- `Nerya/nerya/skills/registry.py:86-112` layers `workspace/skills/installed/*/skill.yml` over built-ins; a user skill with the same ID replaces the built-in entry.
- `Nerya/nerya/skills/registry.py:116-133` imports `actions.py`, first via built-in package path, then via file path fallback.
- `Nerya/nerya/skills/installer.py:8-9` says external skills are local directories with `skill.yml` and `actions.py`.
- `Nerya/nerya/skills/installer.py:221-223`, `251-257`, and `282` reject or search for `skill.yml`, not `SKILL.md`.
- `Nerya/nerya/skills/installer.py:169-175` promotes a skill by appending its ID to `skills/enabled.yml`.
- `Nerya/nerya/skills/installer.py:290-301` only static-analyzes `actions.py`; instruction Markdown is not a first-class scanned artifact.

### Nerya MCP Loading

- `Nerya/nerya/mcp/server.py:1-4` implements a FastMCP server wrapper; Nerya exposes itself to MCP clients.
- `Nerya/nerya/mcp/server.py:30-49` creates a single `FastMCP("nerya")` server and registers every item from `NeryaTools.registry()`.
- `Nerya/nerya/mcp/server.py:39-46` hardcodes server instructions and explicitly says mutating trade/approval/vault operations are not exposed.
- `Nerya/nerya/mcp/tools.py:1-21` describes a curated mostly read-only MCP surface.
- `Nerya/nerya/mcp/tools.py:284-313` builds a static list of tool pairs such as `nerya_info`, `nerya_skills_list`, `nerya_market_ticker`, etc.
- `Nerya/nerya/mcp/tools.py:316-321` serializes the static registry for docs/client discovery.

### Hermes Skill Loading

- `hermes-agent/tools/skills_hub.py:2260-2304` scans optional skills by recursively finding `SKILL.md` files and reading frontmatter.
- `hermes-agent/tools/skills_hub.py:2307-2319` parses YAML frontmatter from `SKILL.md`.
- `hermes-agent/tools/skills_hub.py:2550-2601` installs a scanned skill from quarantine into the profile skills directory and records source, identifier, trust level, scan verdict, hash, files, and metadata in a lock file.
- `hermes-agent/hermes_cli/skills_config.py:27-47` stores disabled skills globally or per platform.
- `hermes-agent/hermes_cli/skills_config.py:53-66` discovers all installed skills ignoring disabled state.
- `hermes-agent/hermes_cli/skills_config.py:129-177` provides an interactive enable/disable flow by platform/category.

### Hermes MCP Loading

- `hermes-agent/hermes_cli/mcp_config.py:1-9` implements `hermes mcp add/remove/list/test/configure` and stores config under `mcp_servers`.
- `hermes-agent/hermes_cli/mcp_config.py:70-84` reads/writes `mcp_servers` in config.
- `hermes-agent/hermes_cli/mcp_config.py:100-121` parses explicit per-server env assignments.
- `hermes-agent/hermes_cli/mcp_config.py:158+` performs temporary server discovery/probing before saving/using a server.
- `hermes-agent/hermes_cli/mcp_config.py:349-390` lets the user enable all or selectively choose MCP tools from a server.
- `hermes-agent/hermes_cli/mcp_config.py:444-506` lists configured MCP servers with transport, tool include/exclude status, and enabled/disabled state.
- `hermes-agent/tools/mcp_tool.py:1554-1578` loads `mcp_servers` from config and interpolates env vars.
- `hermes-agent/tools/mcp_tool.py:1989-1996` converts MCP tool schemas into prefixed Hermes tool schemas.
- `hermes-agent/tools/mcp_tool.py:2148-2253` registers discovered MCP tools into the central tool registry with include/exclude filtering, prompt-injection description scanning, collision guards, utility tools for resources/prompts, and toolset aliases.
- `hermes-agent/tools/mcp_tool.py:2256-2278` connects to one server and registers tools.
- `hermes-agent/tools/mcp_tool.py:2285-2359` connects enabled servers, in parallel, and returns currently registered MCP tools.
- `hermes-agent/tools/mcp_tool.py:2362-2390` exposes `discover_mcp_tools()` as model-tool discovery entrypoint.
- `hermes-agent/tools/mcp_tool.py:1307-1423` detects MCP OAuth/auth failures, attempts recovery/reconnect, retries once, and returns structured `needs_reauth` errors.
- `hermes-agent/model_tools.py:132-146` discovers built-in tools, then MCP tools, then plugin tools.
- `hermes-agent/model_tools.py:196-264` filters final model tool definitions by enabled/disabled toolsets and registry availability.

## Key Difference Summary

Nerya currently has:

- local action-manifest skill loading,
- built-in/user-installed `skill.yml` overlays,
- `actions.py` import-based execution,
- simple `enabled.yml` allowlist,
- a Nerya-as-MCP-server export surface.

Hermes has:

- `SKILL.md` instruction skill discovery and hub installation,
- trust/scan/hash/lockfile metadata for installed skills,
- per-platform/global skill disablement,
- central tool registry for built-in/plugin/MCP tools,
- Hermes-as-MCP-client dynamic server loading,
- per-server tool include/exclude,
- MCP resources/prompts utility tools,
- MCP OAuth/reconnect/retry/error semantics,
- toolset availability filtering before model context exposure.

## Detailed Gaps

### 1. Nerya Does Not Load Standard `SKILL.md` Skills

Nerya's skill registry and installer require `skill.yml`. It does not treat `SKILL.md` as the entrypoint for instruction loading.

Missing:

- `SKILL.md` recursive discovery.
- YAML frontmatter parsing.
- Markdown body preservation and summarization.
- `scripts/`, `references/`, `templates/` progressive disclosure.
- instruction-only skills with no executable actions.
- skill context-size warnings for large `SKILL.md`.

Required alignment:

- Add a `SkillDocLoader` for `SKILL.md`.
- Keep `actions.yml`/legacy `skill.yml` for tool execution only.
- Install standard skills as instruction-only unless they opt into executable Nerya actions.

### 2. `skills/enabled.yml` Is a Coarse Allowlist, Not Policy

Nerya only has a global positive allowlist. Hermes can disable skills globally or per platform while still discovering installed skills.

Missing:

- per-platform enablement: CLI, dashboard, Telegram, Discord, API, subagent.
- per-profile enablement.
- per-skill trust tier.
- disable reason and audit trail.
- category-level toggles.
- UI/CLI workflow for choosing enabled skill subsets.

Required alignment:

- Replace `enabled.yml` with a policy config like:

```yaml
skills:
  disabled: []
  platform_disabled:
    telegram: []
    dashboard: []
  per_profile:
    default:
      enabled: []
      disabled: []
```

- Keep a compatibility loader for existing `enabled.yml`.

### 3. User Skills Can Shadow Built-ins Too Easily

Nerya registers installed user skills after built-ins and explicitly allows replacing built-ins with the same ID. This is powerful but risky.

Missing:

- signed/trusted override policy.
- explicit operator confirmation for built-in shadowing.
- collision report in `doctor`.
- namespace separation: `builtin.strategy` vs `user.strategy`.
- rollback to previous built-in/user skill version.
- provenance in runtime tool/context output.

Required alignment:

- Block shadowing by default.
- Require `allow_shadow_builtin: true` plus approval and audit.
- Show active skill source/version/hash wherever tools/actions are listed.

### 4. Nerya Imports `actions.py` at Registry Load Time

The registry imports `actions.py` when loading skills. Hermes tool registration also imports Python modules, but Hermes separates instruction skills from tool registry and has stronger tool availability filtering.

Missing:

- lazy action loading.
- dependency availability checks before import.
- safe import sandbox for untrusted installed skills.
- import side-effect detection.
- action-level unavailable reasons.
- typed availability checks like Hermes registry `check_fn` / `requires_env`.

Required alignment:

- Store action metadata without importing code first.
- Import handlers lazily on first execution or after trust checks.
- Add `availability` fields to action manifests.

### 5. Skill Installer Scans Code, Not Skill Instructions

Nerya static-analyzes `actions.py`; it does not scan `SKILL.md` instructions because `SKILL.md` is not first-class.

Missing:

- Markdown prompt-injection scan.
- external URL/reference scan.
- hidden Unicode/invisible text scan for instruction files.
- context bloat warning.
- dependency/license/provenance summary.
- separate scan verdicts for instructions vs executable code.

Required alignment:

- Scan `SKILL.md`, references, scripts, templates, and action manifests separately.
- Record scan verdicts in install lockfile.

### 6. Nerya Has No Skill Hub Equivalent

Nerya can install from local/archive/git-style sources, but it does not provide a hub/search/inspect workflow comparable to Hermes.

Missing:

- source router for official/trusted/community registries.
- metadata search by name/description/tags.
- inspect before install.
- quarantine before promote.
- install lockfile with source/trust/hash/files/metadata.
- update/reinstall/rollback.
- analytics like install count/trust/security audits.

Required alignment:

- Build or adapt a `SkillHub` around `SKILL.md` metadata.
- Keep Nerya action manifests as an extension, not the required base skill format.

### 7. Nerya MCP Is Only Server-Side Exposure, Not MCP Client Loading

Nerya exposes its tools through a FastMCP server. It does not load external MCP servers as tools for the agent.

Missing vs Hermes:

- `mcp_servers` config.
- stdio/SSE/StreamableHTTP client transports.
- external MCP server discovery.
- per-server connect timeouts.
- background MCP event loop.
- parallel server startup.
- automatic tool registration into model tool/action registry.
- MCP resources/prompts utility tools.
- dynamic `tools/list_changed` refresh.
- server reconnect and shutdown lifecycle.

Required alignment:

- Add an MCP client subsystem separate from `nerya.mcp.server`.
- External MCP tools should become first-class tools/actions subject to Nerya permissions and context policy.

### 8. Nerya MCP Tool List Is Static

`NeryaTools.registry()` returns a hand-written list of methods. Hermes MCP tools are discovered dynamically from configured servers and registered into a central tool registry.

Missing:

- live discovery of tools.
- dynamic schemas from MCP `inputSchema`.
- include/exclude filters.
- collision guards.
- toolset aliases.
- availability checks.
- server status and unavailable reasons.

Required alignment:

- Replace static Nerya MCP export registry with generated capability/action registry output.
- Add a separate external MCP import registry for tools loaded into the agent.

### 9. Nerya MCP Does Not Have OAuth/Auth Recovery

Hermes has MCP OAuth handling, token recovery, reconnect, retry, and structured `needs_reauth` errors. Nerya MCP server has no external auth concern because it is not an MCP client.

Missing for parity:

- OAuth/provider config for MCP clients.
- token storage/refresh.
- non-interactive auth failure handling.
- structured “requires re-auth” errors.
- reconnect after auth recovery.
- model-facing instruction not to retry endlessly.

Required alignment:

- Add MCP OAuth manager if Nerya loads external HTTP MCP servers.
- Bind MCP auth credentials to vault/profile/actor policy.

### 10. Nerya MCP Env Handling and Subprocess Safety Are Missing

Hermes filters stdio subprocess env, interpolates configured env vars, resolves commands against filtered PATH, and avoids leaking host credentials by default.

Missing:

- stdio MCP subprocess env filtering.
- explicit env pass-through config.
- command resolution diagnostics.
- path allowlist.
- subprocess lifecycle tracking.
- per-server resource limits.
- profile-scoped MCP env/credentials.

Required alignment:

- External MCP stdio servers must run under ProcessRegistry/sandbox policy.
- Default env should be scrubbed, with explicit opt-in env keys.

### 11. MCP Tool Descriptions Are Not Scanned or Context-Safe

Hermes scans MCP tool descriptions for prompt-injection-like content before registering them. Nerya has no external MCP loading, and its own MCP export descriptions are hand-written.

Missing once external MCP is added:

- prompt-injection scan for external tool descriptions.
- description sanitization or warning metadata.
- stripping cross-tool references to unavailable tools.
- context budget controls for huge schemas/descriptions.

Required alignment:

- Run external MCP tool descriptions through the same prompt/security scanner used for skill docs.

### 12. Tool Registry and Toolset Filtering Are Not Unified

Hermes has a central registry where built-in tools, MCP tools, and plugins all register. `model_tools.get_tool_definitions()` then filters by enabled/disabled toolsets and availability before exposing schemas to the model.

Nerya has multiple partial registries:

- `SkillRegistry` for `skill.yml` actions,
- agent kernel action maps,
- `NeryaTools.registry()` for MCP export,
- API routes,
- dashboard wrappers,
- harness tool runner,
- config defaults.

Missing:

- single registry for model-visible tools/actions.
- toolset names and aliases.
- tool availability checks.
- required env/secrets.
- unavailable reason reporting.
- collision handling across built-ins/user skills/MCP/plugin tools.
- dynamic rebuild after skill/MCP changes.

Required alignment:

- Build a `CapabilityRegistry` / `ToolRegistry` that all surfaces consume.
- MCP import, skill actions, CLI commands, dashboard actions, and gateway commands should register into it.

### 13. Nerya MCP Export Is Not Permission/Actor Aware Enough

The Nerya MCP server deliberately hides high-risk operations, which is safer than exposing everything. But it does not expose a general actor-aware permission model.

Missing:

- MCP client identity.
- per-tool scopes.
- per-client allowlists.
- audit trail with actor/client/session.
- approval escalation for mutating tools.
- read-only mode vs operator mode.
- transport-level auth for the Nerya MCP server.

Required alignment:

- Treat Nerya's MCP server as an API surface with authn/authz, not just a local read-only convenience.
- Use the same permission engine as dashboard/API/gateway/tool runner.

### 14. MCP Resources and Prompts Are Missing

Hermes registers utility tools for `list_resources`, `read_resource`, `list_prompts`, and `get_prompt` when external MCP servers support them. Nerya does not support MCP resources/prompts as imported capabilities.

Missing:

- resource listing/reading.
- prompt listing/getting.
- resource URI safety policy.
- prompt trust/context injection policy.
- caching and provenance for MCP-provided resources/prompts.

Required alignment:

- Add MCP resources/prompts support with explicit context admission control.

### 15. Dynamic Refresh and Hot Reload Are Incomplete

Nerya has `AgentKernel.refresh_action_map()` for skill manifest changes, but the broader system does not have Hermes-style MCP dynamic refresh or unified registry rebuild.

Missing:

- MCP `tools/list_changed` refresh.
- skill install/update reload without restart across agent, dashboard, API, gateway, MCP export.
- registry version counter.
- clients subscribed to capability changes.
- context manifest recording registry version.

Required alignment:

- Add `capability_registry_version` and event stream.
- Skill/MCP updates should emit capability-change events consumed by dashboard/gateway/agent.

## Required Architecture Split

Nerya should split loading into four loaders:

1. **SkillDocLoader** — loads `SKILL.md` instructions for model context.
2. **ActionManifestLoader** — loads `actions.yml`/legacy `skill.yml` for executable actions.
3. **MCPClientLoader** — loads external MCP servers into the tool registry.
4. **MCPServerExporter** — exports selected Nerya capabilities to external MCP clients.

These must feed a single `CapabilityRegistry` that records:

- id,
- kind: `skill_doc`, `action`, `tool`, `mcp_tool`, `mcp_resource`, `mcp_prompt`, `cli_command`, `gateway_command`,
- owner/source/version/hash,
- trust level,
- enabled platforms,
- permissions/scopes,
- availability/check_fn,
- schema/context summary,
- test status,
- registry version.

## Roadmap Additions

### P0 — Add Standard Skill Loader

- Parse `SKILL.md` frontmatter/body.
- Install `SKILL.md`-only skills.
- Convert built-ins to `SKILL.md + actions.yml`.

### P0 — Build Unified Capability Registry

- Merge skill docs, action manifests, MCP import, MCP export, CLI/API/dashboard/gateway surfaces.
- Add collision detection and source provenance.

### P0 — Add External MCP Client Loading

- `nerya mcp add/remove/list/test/configure`.
- `mcp_servers` config with stdio/http transports.
- include/exclude per server.
- env filtering and vault-backed credentials.

### P1 — MCP OAuth and Dynamic Refresh

- OAuth manager for HTTP MCP.
- reconnect/retry/needs_reauth semantics.
- tools/list_changed refresh.
- resources/prompts utility tools.

### P1 — Skill Hub and Per-Platform Skill Policy

- Search/inspect/install/update/rollback standard skills.
- Per-platform skill enable/disable.
- lockfile with trust/hash/source/scan metadata.

## Acceptance Tests

- A `SKILL.md`-only standard skill installs, appears in skill search, and can be selected into context without executable actions.
- A legacy `skill.yml` skill still loads but is marked as an action manifest compatibility path.
- A built-in skill directory with `SKILL.md + actions.yml + actions.py` exposes instructions and actions separately.
- A user skill cannot shadow a built-in unless explicit shadow approval is present.
- `nerya mcp add` probes a stdio test server, lets the user include/exclude tools, and saves `mcp_servers` config.
- External MCP tools appear in the unified tool registry with prefixed names and toolset aliases.
- Disabled MCP servers are skipped without deleting config.
- MCP tool description prompt-injection warnings are recorded.
- MCP OAuth 401 returns structured `needs_reauth` and does not loop retries.
- MCP resources/prompts are listed/read only if enabled by policy.
- Dashboard, gateway, API, and context builder see the same registry version after skill/MCP changes.

## Do Not Claim Yet

Do not claim Nerya has Hermes-level skill/MCP loading until:

- `SKILL.md` is the skill instruction standard,
- action manifests are separated from skills,
- external MCP servers can be loaded as tools,
- MCP tools flow through the same registry/permission/context policy as built-in actions,
- MCP OAuth/reconnect/include/exclude/resources/prompts are implemented,
- per-platform skill policy and skill hub semantics exist,
- all capability surfaces are generated from one registry rather than duplicated lists.


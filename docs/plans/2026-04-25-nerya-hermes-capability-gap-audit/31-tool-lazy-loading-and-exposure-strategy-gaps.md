# 31 — Hermes Tool Lazy Loading, Availability Filtering, and Dynamic Schema Strategy

## Status (2026-04-25)

Section status:

1. **Central tool registry with collision guard** — COMPLETED. `Nerya/nerya/skills/registry.py:SkillRegistry` is the single source of truth; user skills overlay builtins by id with explicit replacement.
2. **Tool/action availability filtering** — COMPLETED. `Nerya/nerya/skills/manifest.py:ActionSpec` now carries `requires_env`, `requires_secret`, and `check_fn` (lines 70-90). The probe runner lives in `Nerya/nerya/skills/availability.py` (`probe_action`, `build_availability_table`, `AvailabilityVerdict`). `Nerya/nerya/agent/kernel.py:build_action_catalog(skills, config=..., include_unavailable=False)` filters unavailable actions out of the planner-visible catalog. The capability matrix exposes the verdict (`Nerya/nerya/api/routes_capability.py:_skill_summaries` / `_action_catalog`). Covered by `Nerya/tests/test_action_availability.py` (8 tests).
3. **Per-actor toolset selection** — PARTIALLY COMPLETED. Subagent dispatcher (`Nerya/nerya/subagents/dispatcher.py`) carries per-role allowlist + denylist; per-session toolset config → Plan 31 P1.
4. **Dynamic schema rewriting** — COMPLETED. `Nerya/nerya/agent/kernel.py:build_action_catalog()` filters out actions whose required permissions, env vars, secrets, or `check_fn` predicates are unmet (`include_unavailable=True` keeps the older shape for the dashboard). Capability matrix shows the verdict so dashboards can render greyed-out rows with the missing-env reason. Covered by `Nerya/tests/test_action_availability.py::test_build_action_catalog_filters_unavailable`.
5. **Lazy load actions only on dispatch** — PARTIALLY COMPLETED. `Nerya/nerya/skills/registry.py:_import_actions` imports `actions.py` once at boot; per-action lazy-import → Plan 31 P2.
6. **Capability matrix endpoint** — COMPLETED. `Nerya/nerya/api/routes_capability.py` + `GET /runtime/capability_matrix` (Plan 23 P1 §2).
7. **MCP-as-client dynamic registration** — PARTIALLY COMPLETED. `Nerya/nerya/mcp/` covers basics; live re-discovery + per-server enable/disable → Plan 30/31.

Status: PARTIALLY COMPLETED — registry + permission filter + capability matrix all ship; remaining items are dynamic availability probes + per-session toolset selection tracked under Plan 31.

This pass focuses on Hermes' tool-loading strategy and what Nerya should learn from it. The important correction is: Hermes does not simply dump every possible tool into every model call. It uses a staged strategy:

1. discover/register tools into a central registry,
2. group tools by toolset,
3. select enabled/disabled toolsets per run/session,
4. run availability checks before exposing schemas,
5. dynamically rewrite schemas/descriptions based on actually available tools,
6. dispatch handlers only when a selected model call invokes a tool.

Nerya has some good runtime chokepoints (`ToolRunner`, budget, timeout, retry), but it still lacks Hermes' registry/toolset/availability/schema-pruning strategy.

## Evidence Read

### Hermes

- `hermes-agent/tools/registry.py:56-73` discovers built-in tools by importing only modules that contain top-level `registry.register(...)` calls.
- `hermes-agent/tools/registry.py:76-97` stores tool metadata in `ToolEntry`: name, toolset, schema, handler, check function, required env, async flag, description, emoji, and max result size.
- `hermes-agent/tools/registry.py:100-110` uses a central `ToolRegistry` with an `RLock`; comments call out MCP dynamic refresh mutating the registry while other threads read it.
- `hermes-agent/tools/registry.py:176-227` registers tools with collision protection; non-MCP tools cannot be shadowed by other toolsets.
- `hermes-agent/tools/registry.py:258-286` returns model tool definitions only when the tool's `check_fn` passes.
- `hermes-agent/tools/registry.py:292-309` dispatches tool handlers by name only when invoked.
- `hermes-agent/tools/registry.py:329-348` exposes schema and tool-to-toolset mapping helpers.
- `hermes-agent/tools/registry.py:393-433` reports toolset requirements and unavailable toolsets based on required env/check functions.
- `hermes-agent/model_tools.py:132-146` performs staged discovery: built-in tools, then MCP tools, then plugin tools.
- `hermes-agent/model_tools.py:196-264` resolves `enabled_toolsets`/`disabled_toolsets`, then asks the registry for schemas only for selected tool names.
- `hermes-agent/model_tools.py:266-270` computes `available_tool_names` from schemas that survived availability checks.
- `hermes-agent/model_tools.py:272-283` dynamically rebuilds `execute_code` schema so it mentions only sandbox tools that are actually available.
- `hermes-agent/model_tools.py:285-308` dynamically rebuilds or removes the Discord schema based on bot intent/action availability.
- `hermes-agent/model_tools.py:310-328` strips references to unavailable web tools from `browser_navigate` descriptions to avoid model hallucinating missing tools.
- `hermes-agent/toolsets.py:537-585` includes plugin/MCP-registered toolsets when listing all toolsets.
- `hermes-agent/toolsets.py:611-628` validates static toolsets, plugin toolsets, and registry aliases.

### Nerya

- `Nerya/nerya/skills/registry.py:61-84` loads all selected built-in `skill.yml` manifests and imports `actions.py` immediately.
- `Nerya/nerya/skills/registry.py:86-112` loads installed skills after built-ins and lets user skills replace built-ins with the same ID.
- `Nerya/nerya/skills/manifest.py:13-56` has action specs with schema, permissions, risk/approval gates, and agent hints, but no `check_fn`, `requires_env`, or availability object.
- `Nerya/nerya/skills/runtime.py:43+` executes skill calls and validates input schema, but does not expose a central model-tool registry with availability filtering.
- `Nerya/nerya/harness/tool_runner.py:94-176` provides budget, timeout, retry, and trace for skill calls, but it operates after an action was already selected.
- `Nerya/nerya/agent/kernel.py:314-435` still maintains an action map/fallback plus manifest-derived action aliases.
- `Nerya/nerya/agent/context_builder.py:247-271` renders an action catalog into the model context; it is not equivalent to Hermes' toolset-filtered model schema provider.
- `Nerya/nerya/mcp/tools.py:284-313` exposes a static MCP tool registry rather than the live capability/tool registry.

## Key Difference

Hermes' loading strategy is closer to:

```text
Tool module/import discovery -> central registry -> toolset resolution -> availability filtering -> dynamic schema pruning -> model call -> dispatch on demand
```

Nerya is closer to:

```text
skill.yml discovery -> actions.py import -> action map/catalog -> model JSON action -> ToolRunner/runtime call
```

Nerya has execution controls, but not enough exposure controls.

## Detailed Gaps

### 1. Nerya Imports Action Code Too Early

Nerya imports `actions.py` during registry load. Hermes imports tool modules to register metadata too, but Hermes then gates exposure through toolsets and `check_fn` before schemas reach the model. Nerya does not have the equivalent availability gate.

Missing:

- lazy handler import or lazy dependency import.
- import-time side-effect detection.
- per-action `check_fn`.
- per-action `requires_env` / `requires_secret`.
- unavailable reason reporting.
- load metadata without loading executable code.

Required alignment:

- Split action metadata loading from handler loading.
- Make `actions.yml` load without importing `actions.py`.
- Import handlers only on first dispatch or after an availability/trust check.

### 2. Nerya Lacks Toolset-Level Selection

Hermes exposes tools through named toolsets, supports enabled/disabled toolsets, and includes plugin/MCP toolsets dynamically. Nerya has skill IDs and actions, but no equivalent toolset abstraction for model exposure.

Missing:

- toolsets for groups like `trading-read`, `trading-write`, `wallet`, `messaging`, `gateway`, `memory`, `browser`, `mcp:<server>`, `devtools`.
- composite toolsets.
- toolset aliases.
- per-session enabled/disabled toolsets.
- per-gateway/platform toolsets.
- dashboard/CLI selection of toolsets.

Required alignment:

- Add `toolset` metadata to actions and MCP-imported tools.
- Make every model call choose a toolset profile rather than loading all action aliases.

### 3. Availability Is Not Checked Before Model Exposure

Hermes only returns a tool schema if `check_fn` succeeds. Nerya can validate payload at runtime and fail inside action execution, but the model may still see actions that are not actually available.

Missing:

- provider/secret/env availability checks before rendering action catalog.
- dependency checks: binary, package, node script, wallet provider, RPC URL, API key.
- market/account/strategy availability predicates.
- live vs paper availability predicates.
- unavailable reasons surfaced to UI/gateway/doctor.

Required alignment:

- Add `availability` to action manifests:

```yaml
actions:
  - name: swap
    toolset: wallet-write
    availability:
      env: [OKX_API_KEY]
      secrets: [okx.api_secret]
      python_packages: [httpx]
      config_paths: [wallet.okx_os.api_key_ref]
      account_modes: [paper, canary]
```

- Evaluate it before exposing to model and dashboard.

### 4. Nerya Does Not Dynamically Prune Schema Descriptions

Hermes rewrites `execute_code`, Discord, and browser schemas based on available tools/intents. It explicitly strips references to unavailable tools so the model does not hallucinate calls.

Nerya still has action hints, payload hints, and context text that can mention capabilities without verifying they are enabled.

Missing:

- schema/hint post-processing based on actual available actions.
- removal of cross-action references to unavailable actions.
- dynamic narrowing of enums based on configured accounts/markets/providers.
- gateway-specific schema pruning.
- disabled capability warnings.

Required alignment:

- Build an `ActionSchemaBuilder` that receives `available_action_names` and rewrites hints/descriptions before context/model exposure.
- Add tests that disabled tools are not mentioned in any visible schema/hint/context block.

### 5. No Central Tool Registry for Built-ins, Skills, MCP, Plugins

Hermes has one registry. Nerya has multiple partial registries and static lists.

Missing:

- one registry for all model-callable things.
- common `ToolEntry` shape: name, owner, toolset, schema, handler, check_fn, required_env, permissions, max_result_size, description, source hash.
- registry snapshot/version.
- collision policy.
- deregistration for dynamic refresh.
- thread-safe mutation/read snapshots.

Required alignment:

- Add `nerya.tools.registry.ToolRegistry`.
- Feed it from built-in actions, installed actions, external MCP tools, CLI/gateway commands, and plugins.
- Make context builder, dashboard, API, MCP export, and gateway command menu consume registry snapshots.

### 6. Result Size and Context Budget Are Not Tool-Metadata Driven

Hermes tool entries can carry `max_result_size_chars`, and model tools can use budget config to limit context bloat. Nerya has turn budget and safe JSON truncation in context builder, but action result limits are not consistently declared per tool/action.

Missing:

- per-action max result size.
- result artifact policy.
- model-visible summary vs full artifact reference.
- tool-output compression policy.
- UI/gateway rendering limits.

Required alignment:

- Add `max_result_size_chars` / `artifact_policy` / `summary_policy` to action/MCP tool entries.
- Store oversized results as artifacts and pass references to context.

### 7. Collision Handling Is Too Weak

Hermes rejects non-MCP shadowing and guards MCP collisions. Nerya intentionally lets installed skills override built-ins by ID.

Missing:

- tool/action name collision guard.
- skill ID namespace policy.
- explicit shadow approval.
- collision report in doctor/status.
- deterministic conflict resolution for built-in/user/plugin/MCP actions.

Required alignment:

- Default: reject shadowing.
- Allow explicit override only with provenance, approval, and rollback metadata.

### 8. Plugins and MCP Should Respect the Same Enable/Disable Semantics

Hermes plugin/MCP toolsets flow through the same `enabled_toolsets`/`disabled_toolsets` path. Nerya's future MCP/plugins must not create separate bypass paths.

Missing:

- plugin toolset registration.
- external MCP toolsets as `mcp-<server>`.
- aliases for friendly names.
- enable/disable by profile/gateway/session.
- registry-driven dashboard/doctor/tool availability.

Required alignment:

- Do not bolt MCP tools directly onto the prompt or agent loop.
- Register them in the same registry as skills and built-ins.

### 9. Nerya's ToolRunner Is Execution-Stage, Not Exposure-Stage

Nerya's `ToolRunner` is useful: it enforces budget, timeout, retries, and traces execution. But it runs after the model already selected an action. Hermes prevents many bad selections by filtering schemas before the model sees them.

Missing:

- pre-exposure filtering.
- per-call toolset profile.
- action visibility by context/lane.
- availability snapshot included in context manifest.

Required alignment:

- Keep `ToolRunner`, but add a preceding `ToolExposurePlanner`.
- The sequence should be:

```text
trigger/session -> choose toolset profile -> evaluate availability -> build schemas/context -> model call -> ToolRunner dispatch
```

### 10. Need a Lazy Loading Policy, Not Just Lazy Imports

The real lesson from Hermes is not only “import later”. It is “only expose what is relevant, available, permitted, and context-safe”.

Nerya's target policy should include:

- **Discover lazily**: scan metadata without importing handlers where possible.
- **Load lazily**: import/connect heavy tools only when selected or when availability check needs it.
- **Expose lazily**: include schemas only for selected toolsets and passing availability.
- **Describe lazily**: remove unavailable cross-references and shrink descriptions per context.
- **Execute lazily**: dispatch handlers only on tool call.
- **Persist lazily**: store large results as artifacts, not context.
- **Refresh lazily**: update registry version on skill/MCP/plugin changes and notify subscribers.

## Proposed Nerya Design

### ToolEntry

```python
@dataclass
class ToolEntry:
    name: str
    owner: str
    source: Literal["builtin", "installed", "mcp", "plugin"]
    toolset: str
    schema: dict
    handler_ref: str
    check_fn_ref: str | None
    required_env: list[str]
    required_secrets: list[str]
    permissions: list[str]
    risk_gate: str
    approval_gate: str
    max_result_size_chars: int | None
    description: str
    trust_level: str
    source_hash: str
```

### Tool Exposure Flow

```text
1. registry.snapshot()
2. resolve requested toolsets
3. apply actor/profile/platform permission policy
4. run availability checks with memoization
5. build available_tool_names
6. rewrite schemas/hints/descriptions to remove unavailable references
7. record registry/context manifest
8. send tool schemas / action catalog to model
```

### Compatibility Mapping

- legacy `skill.yml.actions[*]` -> `ToolEntry` metadata.
- `actions.py` functions -> handler refs, not immediate imports.
- external MCP tools -> `ToolEntry(source="mcp")`.
- dashboard/API/gateway actions -> views over registry, not separate static lists.

## Roadmap Additions

### P0 — Registry and Exposure Split

- Implement central `ToolRegistry` and `ToolExposurePlanner`.
- Move action catalog generation onto registry snapshots.
- Add availability checks and unavailable reasons.

### P0 — Lazy Handler Loading

- Load action/MCP/plugin metadata without importing heavy handlers.
- Resolve handler on dispatch.
- Add import side-effect tests.

### P1 — Dynamic Schema Pruning

- Remove unavailable cross-tool references.
- Narrow enums/options by runtime config.
- Add tests ensuring hidden tools are not mentioned in schemas/context.

### P1 — Toolset Profiles

- Add toolsets and composite toolsets.
- Define default profiles for chat, gateway, dashboard, subagent, coding, trading-read, trading-write.

### P1 — Result Budget Metadata

- Add per-tool result size limits and artifact policies.
- Store large outputs externally and pass references.

## Acceptance Tests

- A missing env var causes a tool to be absent from exposed schemas but present in registry with unavailable reason.
- A disabled toolset is absent from model schemas and not mentioned by any remaining tool description.
- A tool with unavailable dependency is visible in doctor/dashboard as unavailable, but not exposed to the model.
- A user-installed action cannot shadow a built-in action without explicit override metadata.
- External MCP tools register as `mcp-<server>` toolsets and respect enabled/disabled toolsets.
- Dynamic schema builder strips references to disabled tools.
- Handler import does not happen during metadata scan.
- Handler import happens on dispatch and is audited.
- Oversized tool output is artifacted and summarized.
- Context manifest records registry version, selected toolsets, exposed tools, hidden tools, and availability reasons.

## Do Not Claim Yet

Do not claim Nerya has Hermes-style lazy tool loading until:

- tools/actions are registered centrally,
- heavy handlers are not imported just to expose metadata,
- toolsets determine exposure,
- availability checks filter schemas before model calls,
- schema descriptions are pruned based on actual available tools,
- MCP/plugin tools share the same registry path,
- context manifests record exposure decisions,
- disabled/unavailable tools are not mentioned to the model.


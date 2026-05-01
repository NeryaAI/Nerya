# Nerya Skill Loading and Script Execution Refactor Plan

Date: 2026-04-28
Status: proposed refactor plan
Scope: skill loading, skill selection, script execution, tool catalog, MCP exposure, prompt/context assembly

## Executive Summary

Your concern is correct: current Nerya has drifted back toward an action-manifest system where `SKILL.md` carries `actions`, `input_schema`, `agent_action`, `agent_payload_hint`, and those entries are rendered into the model's action catalog. That makes the system look like it is "loading skills", but in practice it is using `SKILL.md` as a disguised tool-schema registry.

The target should be closer to Hermes and Claude Code:

- `SKILL.md` is a markdown playbook and selection surface, not the schema source for every callable action.
- The model sees a compact skill index first, then selectively loads the full skill body.
- Supporting files under `references/`, `templates/`, `scripts/`, and `assets/` are loaded on demand.
- Scripts are executed through a small number of generic, audited primitives such as file read, shell/script run, approval, and live-control tools.
- Deterministic tools remain separate from skills. Tool schemas belong in a real tool registry, not inside every `SKILL.md`.
- MCP exports tools from the tool registry, not from `SKILL.md` action blocks.

The refactor should remove the need to stuff script input/output schemas into `SKILL.md` just to make tests pass. Tests should validate progressive skill loading and script execution behavior, not force every script into a model-visible action catalog.

## Current Evidence

### Repo Rules Already Say This

- `Nerya/AGENTS.md:25` says a skill is the model/operator-facing playbook declared by `SKILL.md`, and executable logic belongs under `scripts/`.
- `Nerya/AGENTS.md:100` says not to encode skill instructions, action catalogs, or large schemas in YAML/manifests.
- `Nerya/AGENTS.md:104` says scripts are executable helpers, not the skill definition itself.
- `Nerya/AGENTS.md:106` says mutating scripts must be deterministic, accept structured input, return JSON-serializable output, and route side effects through safe runtime paths.

### Current Runtime Drift

- `Nerya/nerya/skills/manifest.py:14` defines `ActionSpec`, making action schemas a core part of skill manifests.
- `Nerya/nerya/skills/manifest.py:258` parses `actions_raw` from the skill document.
- `Nerya/nerya/skills/manifest.py:260` turns each action entry into an `ActionSpec`.
- `Nerya/nerya/skills/registry.py:82` parses `SKILL.md` as a typed manifest.
- `Nerya/nerya/skills/registry.py:96` imports skill action handlers after parsing manifest actions.
- `Nerya/nerya/skills/registry.py:249` documents `scripts/handlers.py` as a handler surface.
- `Nerya/nerya/skills/registry.py:251` documents per-action `scripts/<action>.py` as another handler surface.
- `Nerya/nerya/agent/kernel.py:510` builds the action map by merging `agent_action` declarations from skill manifests.
- `Nerya/nerya/agent/kernel.py:547` builds the LLM-facing action catalog from skill manifests.
- `Nerya/nerya/agent/kernel.py:603` reads `agent_payload_hint` from action specs.
- `Nerya/nerya/agent/kernel.py:626` carries full `SKILL.md` instructions into each action-catalog row so the renderer can sample excerpts.
- `Nerya/nerya/agent/context_builder.py:100` renders the action catalog into the planner prompt.
- `Nerya/nerya/agent/context_builder.py:119` labels the rendered block as skills available this turn.
- `Nerya/nerya/agent/context_builder.py:123` explicitly prints action inventory with payload and tags.
- `Nerya/nerya/agent/context_builder.py:124` forces a strict JSON action reply shape.
- `Nerya/nerya/mcp/dynamic_tools.py:22` says dynamic MCP tools use `ActionSpec.input_schema` verbatim as the JSON schema.

This chain is the core problem: `SKILL.md -> ActionSpec -> action catalog -> tool call context -> MCP schema`. It turns skills into a schema transport layer.

### Existing Procedural Loader Is Not Enough

- `Nerya/nerya/skills/procedural.py:67` loads procedural `SKILL.md`.
- `Nerya/nerya/skills/procedural.py:98` still creates a synthetic `run` action.
- `Nerya/nerya/skills/procedural.py:143` creates a handler that returns the markdown body.

This is better than many per-script actions, but still treats "load skill body" as an action in the same action catalog. The target should be a dedicated skill-loading primitive, not another business action.

## Reference Behavior to Learn From

### Hermes Pattern

Hermes separates skills from tools more cleanly:

- `hermes-agent/tools/skills_tool.py:9` describes progressive disclosure.
- `hermes-agent/tools/skills_tool.py:10` says only metadata is shown in `skills_list`.
- `hermes-agent/tools/skills_tool.py:11` says full instructions load via `skill_view` when needed.
- `hermes-agent/tools/skills_tool.py:12` says linked files are loaded on demand.
- `hermes-agent/tools/skills_tool.py:53` defines `skills_list` as progressive disclosure tier 1.
- `hermes-agent/tools/skills_tool.py:54` defines `skill_view` as progressive disclosure tier 2/3.
- `hermes-agent/tools/skills_tool.py:647` implements `skills_list`.
- `hermes-agent/tools/skills_tool.py:804` implements `skill_view`.
- `hermes-agent/tools/skills_tool.py:1043` collects linked `references`, `templates`, assets, and scripts.
- `hermes-agent/tools/skills_tool.py:1154` discovers `scripts/` as linked files.
- `hermes-agent/tools/skills_tool.py:1268` tells the model to call `skill_view(name, file_path)` to load linked files.
- `hermes-agent/agent/skill_commands.py:209` scans skill commands by finding `SKILL.md` files.
- `hermes-agent/agent/skill_commands.py:235` parses frontmatter and body for skill selection.
- `hermes-agent/tools/registry.py:258` exposes tool definitions only after availability checks.
- `hermes-agent/tools/registry.py:261` states only tools whose `check_fn()` passes are exposed.

Hermes does not need to put every script's call schema into `SKILL.md`. It exposes skill markdown and supporting files progressively; tools remain tools.

### Claude Code Pattern

Claude Code also separates the skill body from deterministic tool schemas:

- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\skills\loadSkillsDir.ts:405` documents `skill-name/SKILL.md` as the supported directory format.
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\skills\loadSkillsDir.ts:431` locates each `SKILL.md` entrypoint.
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\skills\loadSkillsDir.ts:447` parses frontmatter and markdown content.
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\skills\loadSkillsDir.ts:458` extracts path-based conditional activation metadata.
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\skills\loadSkillsDir.ts:771` separates conditional skills from unconditional skills.
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\skills\loadSkillsDir.ts:986` activates conditional skills whose path patterns match touched files.
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\SkillTool\SkillTool.ts:331` defines a skill execution/loading tool.
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\SkillTool\SkillTool.ts:1065` strips frontmatter before injecting skill body.
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\SkillTool\SkillTool.ts:1095` injects `SKILL.md` content as a meta user message.

Claude Code's file, shell, edit, MCP, and plan tools are real tools with real schemas. Skills guide the model on when/how to use those tools; they are not a replacement for the tool registry.

## Root Cause

Nerya currently conflates four concepts:

1. **Skill**: markdown instructions for a capability or workflow.
2. **Script**: executable helper files referenced by the skill.
3. **Tool**: deterministic primitive exposed to the model/provider or MCP.
4. **Domain action**: privileged business side effect such as trading, wallet, deployment, or runtime promotion.

Because these are conflated, tests and integrations pressure developers to add more `actions` metadata to `SKILL.md`. That temporarily makes the agent call something, but it creates long-term problems:

- Prompt context grows with action inventory instead of useful reasoning state.
- The model learns to select action names rather than read the skill and operate the workspace.
- Scripts become hidden RPC endpoints instead of documented helper files.
- The action catalog becomes another schema language competing with MCP/tool schemas.
- Skill authoring becomes too hard: authors must know Nerya-specific `agent_action`, `agent_payload_builder`, and `input_schema` fields.
- General coding ability gets weaker because the agent is boxed into business actions instead of using file/shell primitives.

## Target Architecture

### Principle 1 — Skills Are Markdown Playbooks

`SKILL.md` should contain:

- `name`
- `description`
- optional `version`
- optional `tags`
- optional `allowed_tools`
- optional `paths` or `activation` metadata
- optional `requires` / `setup` metadata
- human-readable `When to use`
- human-readable workflow steps
- references to supporting files under `references/`, `templates/`, `scripts/`, and `assets/`

`SKILL.md` should not contain:

- per-action `input_schema`
- per-action `output_schema`
- `agent_action`
- `agent_payload_builder`
- `agent_payload_hint`
- full business RPC catalogs
- copied script CLI help
- large JSON schemas

### Principle 2 — Tool Schemas Live in Tool Registry

Tool schemas should live in one deterministic tool layer:

- `workspace.read`
- `workspace.list`
- `workspace.search`
- `workspace.edit`
- `workspace.write`
- `shell.run`
- `process.start`
- `process.output`
- `skill.list`
- `skill.view`
- `skill.view_file`
- `script.run`
- `approval.request`
- `live_control.*` for privileged runtime operations

These tools are the only provider/MCP schemas shown to the model. They can be audited, permissioned, tested, and versioned independently from skill markdown.

### Principle 3 — Scripts Are Supporting Files, Not Auto-Tools

Scripts under `scripts/` should be executed in one of two ways:

- The skill instructs the model to inspect a script or run it with `shell.run` / `script.run`.
- `script.run` receives a bounded generic payload: skill id, script path or script id, arguments, optional stdin JSON, timeout, and approval mode.

The script itself should own its command contract:

- `--help` for human/model-readable usage.
- optional `--schema` or `schema.json` for machine-readable arguments.
- JSON stdout for structured results.
- non-zero exit and JSON/text stderr for failures.

The schema is discovered on demand when the script is relevant; it is not stuffed into global prompt context.

### Principle 4 — Domain Actions Become Internal APIs, Not Skill Markdown

Privileged runtime operations can still exist, but they should be explicit internal tools or service APIs, not `SKILL.md` action blocks.

Examples:

- `trading.submit_intent`
- `wallet.sign_request`
- `strategy.promote`
- `runtime.deploy`
- `secret.resolve`

These should have first-class permission, risk, approval, and audit gates. A skill can describe when to use them, but should not define them.

### Principle 5 — Selective Loading, Not Context Stuffing

Default context should include only:

- a compact skill index: id, title, description, tags, activation hints, source path, digest;
- a compact tool index: tool name, purpose, risk class;
- project rules and active workspace state;
- current transcript.

Full skill bodies load only through `skill.view`. Supporting files load only through `skill.view_file` or workspace read tools. Script help/schema loads only after the skill has been selected.

## Proposed Runtime Model

### SkillIndex

`SkillIndex` should be a data structure, not a business skill. It should be built from every `SKILL.md` and contain:

- `skill_id`
- `name`
- `description`
- `tags`
- `source`
- `skill_dir`
- `skill_md_path`
- `digest`
- `activation.paths`
- `activation.toolsets`
- `requires.env`
- `requires.commands`
- `linked_files` summary

The model-facing `skill.list` tool returns rows from this index.

### SkillView

`skill.view(skill_id)` returns:

- stripped markdown body;
- frontmatter metadata;
- linked files summary;
- warnings for missing setup/env;
- digest and source path.

`skill.view_file(skill_id, path)` returns one supporting file. This should support `references/`, `templates/`, `scripts/`, and `assets/`, with path traversal protection.

### ScriptRunner

`script.run` should be a generic deterministic tool:

- input: `skill_id`, `script_path`, `args`, `stdin_json`, `timeout_ms`, `cwd_policy`, `risk_mode`;
- output: `exit_code`, `stdout`, `stderr`, `json`, `truncated`, `artifact_refs`, `duration_ms`;
- approval: required for mutation, network, live trading, secrets, external processes, or destructive commands;
- evidence: every run writes command, cwd, env redaction, stdout/stderr refs, and digest into EvidenceBundle.

Important: `script.run` does not need to know every script's custom schema up front. If the model needs the script contract, it reads `scripts/README.md`, runs `--help`, or asks `script.inspect` on demand.

### ToolCatalog

`ToolCatalog` should be generated from a real tool registry. It should not be generated from `SkillManifest.actions`.

Each tool entry should include:

- name;
- description;
- JSON schema;
- risk class;
- permission class;
- read/write/live classification;
- availability verdict;
- MCP export policy;
- examples only where they are stable and concise.

### MCP Exposure

MCP should expose deterministic tools, not skill action blocks.

Recommended exported MCP tools:

- `nerya_skill_list`
- `nerya_skill_view`
- `nerya_skill_view_file`
- `nerya_workspace_read`
- `nerya_workspace_search`
- `nerya_script_run`
- selected privileged tools only under explicit config allowlist

MCP should not generate one MCP tool per `SKILL.md` action. That is exactly the schema explosion to remove.

## Migration Plan

### Phase 0 — Freeze the Drift

- [ ] Stop adding new `actions`, `input_schema`, `agent_action`, and `agent_payload_hint` blocks to `SKILL.md`.
- [ ] Add a lint rule that fails new skill docs containing these fields outside a temporary legacy allowlist.
- [ ] Mark existing action-bearing `SKILL.md` files as legacy hybrid skills.
- [ ] Update contributor docs to say skill docs are markdown playbooks, not tool schemas.

Acceptance criteria:

- New skills can be added with markdown + scripts only.
- Tests no longer require adding fake action schemas just to make a script callable.

### Phase 1 — Introduce the New Skill Loader

- [ ] Add `nerya/skills/doc_loader.py` for pure `SKILL.md` parsing.
- [ ] Parse only safe frontmatter metadata: name, description, tags, allowed_tools, activation, requires, setup.
- [ ] Strip frontmatter before returning the skill body.
- [ ] Build linked-file summaries for references/templates/scripts/assets.
- [ ] Add digest and source path to every loaded skill document.
- [ ] Keep legacy `SkillManifest.from_skill_md` behind a compatibility flag only.

Acceptance criteria:

- `SKILL.md` without actions loads cleanly.
- `SKILL.md` with actions loads as legacy hybrid and produces warnings.
- Full skill body is not included in every prompt by default.

### Phase 2 — Replace Action Catalog With SkillIndex + ToolCatalog

- [ ] Replace `_render_action_catalog` with two smaller sections: `SkillIndexContext` and `ToolCatalogContext`.
- [ ] `SkillIndexContext` renders only skill metadata rows.
- [ ] `ToolCatalogContext` renders only deterministic primitives from the tool registry.
- [ ] Remove `skill_instructions` from action-catalog rows.
- [ ] Remove payload hints from skill docs and move primitive tool schemas into the tool registry.
- [ ] Add prompt rule: load a skill with `skill.view` before following its workflow.

Acceptance criteria:

- Main prompt no longer contains per-skill action inventories.
- The model can answer "what skills are installed" from `skill.list`.
- The model can load one selected skill body on demand.

### Phase 3 — Add `skill.view_file` and `script.inspect`

- [ ] Add `skill.view_file(skill_id, file_path)` with path traversal protection.
- [ ] Add `script.inspect(skill_id, script_path)` to return `--help`, optional `--schema`, and file digest.
- [ ] Keep script inspection read-only and query-safe.
- [ ] Add linked file summaries to skill view results.

Acceptance criteria:

- A skill can mention `scripts/foo.py` without embedding its entire interface.
- The model can inspect script usage only when it has chosen that skill.

### Phase 4 — Add Generic `script.run`

- [ ] Implement `script.run` as a tool-registry primitive.
- [ ] Enforce skill-relative script paths and workspace chrooting.
- [ ] Support args, stdin JSON, timeout, output truncation, and result refs.
- [ ] Classify scripts as read-only, mutating, networked, or live-control via policy, not via SKILL.md action schemas.
- [ ] Route mutating script runs through approval and EvidenceBundle.

Acceptance criteria:

- Scripts execute without becoming individual LLM tools.
- Script output is structured enough for the model to continue.
- Dangerous scripts require approval before execution.

### Phase 5 — Migrate Built-in Skills

Migration order:

- [ ] `skill_index_skill`: replace with native `skill.list/view/view_file` tools.
- [ ] `operator_skill`: split into native workspace/shell/process tools; keep SKILL.md as usage guide only.
- [ ] `strategy_skill`: convert creation/update workflows to markdown workflow + file templates + validation scripts.
- [ ] `strategy_validation_skill`: keep validation scripts; invoke through `script.run` or dedicated validator tools.
- [ ] `exchange_skill`, `wallet_skill`, `trading_skill`: move privileged operations to explicit internal tools with risk gates.
- [ ] `team_skill`, `subagent_skill`: decide whether they are workflow skills or native orchestration tools; do not leave them half action/half skill.

Acceptance criteria:

- Built-in `SKILL.md` files read like human playbooks.
- Tool schemas live outside skill docs.
- Built-ins still pass existing behavior tests through compatibility shims.

### Phase 6 — Rework MCP

- [ ] Stop generating MCP tools from `ActionSpec.input_schema`.
- [ ] Export deterministic tool-registry primitives only.
- [ ] Add explicit config allowlists for privileged MCP tools.
- [ ] Keep skill discovery as `skill.list/view/view_file` MCP tools.
- [ ] Add MCP diagnostics showing why a tool is hidden.

Acceptance criteria:

- MCP surface is small and stable.
- Installing a new markdown skill does not explode MCP tool count.
- Privileged operations are visible only when explicitly configured.

### Phase 7 — Remove Legacy Action Manifest Dependency

- [ ] Add deprecation warnings when loading `actions` from `SKILL.md`.
- [ ] Add migration script to move action metadata out of `SKILL.md` into tool registry definitions or script docs.
- [ ] Delete legacy `skill.yml` fallback for built-ins after all built-ins migrate.
- [ ] Keep out-of-tree legacy compatibility for one release cycle behind a config flag.
- [ ] Remove `agent_action` and `agent_payload_hint` from normal skill authoring docs.

Acceptance criteria:

- Nerya can run without parsing any `actions` block from built-in skills.
- All provider/MCP schemas come from tool registry definitions.

### Phase 8 — Regression and Eval Suite

Add golden scenarios:

- [ ] List skills returns metadata only.
- [ ] Load selected skill returns markdown body and linked files.
- [ ] Load supporting reference file on demand.
- [ ] Inspect script usage on demand.
- [ ] Run read-only script without creating a dedicated action.
- [ ] Run mutating script and require approval.
- [ ] Strategy creation uses files/templates/scripts rather than `strategy.create` action schema.
- [ ] MCP list remains stable after adding a new markdown skill.
- [ ] Context budget stays bounded with 30+ skills installed.
- [ ] Legacy hybrid skill emits warning but still works during migration.

## Prompt Policy After Refactor

Default prompt should say:

- You have a compact skill index and a compact tool catalog.
- If a user asks for a workflow capability, search/list skills first.
- Load the selected skill body only when needed.
- Load supporting files only when the skill points to them.
- Use workspace/file/shell/script tools to perform the work.
- Do not invent action names from skill docs.
- Do not expect every script to be a tool.
- For live trading, wallet signing, deployment, secrets, and irreversible side effects, use privileged tools and approval gates.

It should not say:

- Reply with a strict JSON object selecting an action from every skill's action catalog.
- Only call actions in the catalog above.
- Missing capability means propose a new action.
- Every skill action has payload hints in prompt.

Those rules preserve the old rigid planner and should be removed from workspace-native coding mode.

## Recommended File-Level Changes

### Replace or Narrow These Components

- `Nerya/nerya/skills/manifest.py`: split `SkillDoc` from legacy `SkillManifest`. `ActionSpec` becomes legacy/internal only.
- `Nerya/nerya/skills/registry.py`: load `SKILL.md` as skill docs first; load legacy action manifests only through compatibility mode.
- `Nerya/nerya/skills/procedural.py`: replace synthetic `run` action with native `skill.view` behavior.
- `Nerya/nerya/agent/kernel.py`: stop building the default model action map from `agent_action` declarations.
- `Nerya/nerya/agent/context_builder.py`: stop rendering action inventory as "skills available this turn".
- `Nerya/nerya/mcp/dynamic_tools.py`: stop exporting one tool per skill action.

### Add These Components

- `Nerya/nerya/skills/doc_loader.py`
- `Nerya/nerya/skills/index.py`
- `Nerya/nerya/tools/registry.py`
- `Nerya/nerya/tools/skill_tools.py`
- `Nerya/nerya/tools/script_tools.py`
- `Nerya/nerya/agent/skill_context.py`
- `Nerya/nerya/agent/tool_context.py`
- `Nerya/tests/test_skill_progressive_loading.py`
- `Nerya/tests/test_script_run_tool.py`
- `Nerya/tests/test_mcp_tool_surface_stability.py`

## Design Rules for Future Skills

A good Nerya skill should look like a reusable operator playbook:

- It explains when to use the skill.
- It describes workflow steps in markdown.
- It links to references/templates/scripts by relative path.
- It tells the agent which generic tools it may need.
- It gives examples at the workflow level.
- It avoids Nerya-specific action schema fields.

A good skill script should look like a CLI helper:

- It has a clear filename and purpose.
- It supports `--help`.
- It accepts flags or stdin JSON.
- It returns JSON when structured output matters.
- It exits non-zero on failure.
- It never requires the model to know hidden Python function names.

A good native tool should look like a deterministic API:

- It has one schema source in the tool registry.
- It has risk and permission metadata.
- It has bounded output and result refs.
- It participates in EvidenceBundle.
- It can be exported to MCP if policy allows.

## Non-Goals

- Do not remove risk gates or approval gates.
- Do not expose live trading through generic script execution without policy.
- Do not make every skill a provider tool.
- Do not require all scripts to have formal JSON Schema on day one.
- Do not break existing legacy skills immediately; migrate behind compatibility flags.

## Final Target

After this refactor, a typical task should flow like this:

1. User asks for a capability.
2. Agent sees compact skill index and tool catalog.
3. Agent calls `skill.list` or `skill.view` only for relevant skills.
4. Agent reads any referenced `references/`, `templates/`, or `scripts/` file on demand.
5. Agent uses workspace tools to edit files or `script.run` to execute helpers.
6. Privileged side effects go through explicit approval-gated native tools.
7. The final report cites files changed, scripts run, validations performed, and remaining risk.

That is the Hermes/ClaudeCode-like model Nerya should converge on: skills guide behavior; tools execute capabilities; scripts are supporting artifacts; context is loaded selectively.

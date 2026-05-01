# SKILL.md + Scripts Migration Plan

Date: 2026-04-25
Status: planning artifact for the next implementation pass
Scope: `Nerya/` skill loading, built-in skill format, docs, tests, and operator-facing guidance

## Decision

Nerya skills must follow the standard skill shape: `SKILL.md` plus optional `scripts/`, `references/`, and `templates/`. Do not add new `skill.yml`, `skill.yaml`, `manifest.yml`, `manifest.yaml`, or `actions.py` files for skill definitions.

The target split is:

- `SKILL.md`: the only model/operator-facing skill definition.
- `scripts/`: optional executable helpers and adapters invoked by the agent/runtime.
- `references/`, `templates/`: optional supporting assets loaded progressively.
- Legacy YAML/action files: migration-only compatibility input, not a format to extend.

## Principles

1. Skill instructions are Markdown, not YAML.
2. Executable behavior belongs in `scripts/`, not `actions.py`.
3. Scripts are helpers selected by a skill workflow; they are not the skill definition.
4. Runtime permissions, gates, and availability should be enforced by the generic script bridge and Nerya safety layers, not hand-authored skill YAML.
5. Do not convert YAML into a giant tool-call catalog in prompt context.
6. Prefer progressive disclosure: load `SKILL.md` first, then references/scripts/templates only when needed.
7. Any touched capability must move away from legacy YAML/action files rather than adding fields to them.

## Target Directory Shape

```text
nerya/skills/builtin/<name>_skill/
├── SKILL.md
├── scripts/
│   ├── <helper>.py
│   └── <helper>.schema.json      # optional if the script cannot self-describe
├── references/                   # optional
└── templates/                    # optional
```

Forbidden for new skill work:

```text
skill.yml
skill.yaml
manifest.yml
manifest.yaml
actions.py
```

## Script Contract

A Nerya skill script should be runnable and inspectable without a per-skill YAML manifest.

Required conventions:

- Accept structured input by one of:
  - `--input-json <json>`
  - `--input-file <path>`
  - stdin JSON
  - explicit CLI flags for simple scripts
- Return JSON to stdout for machine-consumed results.
- Return non-zero exit code on business or validation failure.
- Never print plaintext secrets.
- Mutating scripts must route state changes through existing safe Nerya SDK/proposal/risk-gate paths.
- Document usage in `SKILL.md`.

Recommended conventions:

- Support `--schema` to print a JSON schema for script input/output.
- Support `--dry-run` for mutating scripts.
- Keep heavyweight guidance in `references/`; keep `SKILL.md` compact.
- Store generated artifacts under approved workspace paths only.

## Phase 0 — Freeze the Rule

### Files to update

- `AGENTS.md`
  - Done in this planning pass: declares `SKILL.md` as the required skill entry point.
  - Done in this planning pass: forbids new skill YAML and `actions.py` files.
  - Done in this planning pass: makes `scripts/` the executable surface.

- `docs/skill-first-trading.md`
  - Replace old `skill.yml + actions.py` examples with `SKILL.md + scripts/`.
  - Explain that executable helpers are scripts selected by skill workflow, not the skill itself.

- `docs/reference-capability-map.md`
  - Remove claims that Nerya's `skill.yml` mirrors plugin/tool capability declarations.
  - Reword as: `SKILL.md` mirrors agent skill instructions; scripts provide optional executable helpers.

- `docs/plans/2026-04-25-nerya-hermes-capability-gap-audit/29-skill-format-correction-skillmd-vs-action-manifest.md`
  - Update target design from `SKILL.md + actions.yml/actions.py` to `SKILL.md + scripts/`.
  - Mark `actions.yml/tool_manifest.yml/actions.py` as rejected as a canonical skill format.

- `docs/plans/2026-04-25-nerya-hermes-capability-gap-audit/30-skill-and-mcp-loading-parity-gaps.md`
  - Update the migration text so `SKILL.md` is not just compatible fallback; it is the canonical skill format.

- `docs/plans/2026-04-25-nerya-hermes-capability-gap-audit/31-tool-lazy-loading-and-exposure-strategy-gaps.md`
  - Clarify that lazy exposure applies to executable scripts/tools, not skill instruction files.

### Acceptance checks

```bash
rg -n "create .*skill\.ya?ml|manifest\.ya?ml \+ actions\.py|skill\.yml \+ actions\.py|new .*actions\.py" AGENTS.md docs
```

Expected: no current guidance asks developers to create skill YAML or new `actions.py` files.

## Phase 1 — Add a Generic Script Bridge

### Files to change

- `nerya/skills/script_bridge.py` (new)
  - Resolve scripts under an approved skill directory.
  - Validate the path remains inside the skill's `scripts/` directory.
  - Run scripts with structured input.
  - Capture stdout/stderr/exit code.
  - Parse JSON stdout for machine results.
  - Enforce timeout, max output size, and redaction.

- `nerya/skills/script_schema.py` (new)
  - Load script schemas from either:
    - `scripts/<name>.schema.json`
    - script `--schema` output
  - Do not use YAML.

- `nerya/skills/registry.py`
  - Load `SKILL.md` as the canonical skill doc.
  - Index scripts as supporting executable assets, not as automatic model tools.
  - Treat legacy `skill.yml` and `actions.py` as compatibility inputs with warning/deprecation metadata only.

- `nerya/skills/runtime.py`
  - Add a generic path for invoking approved scripts through `script_bridge`.
  - Preserve safety gates for mutating script categories.
  - Fall back to legacy action dispatch only during migration.

### Tests to add/update

- `tests/test_skill_script_bridge.py`
  - Runs a fixture script with stdin JSON.
  - Rejects path traversal outside `scripts/`.
  - Enforces timeout and output truncation.
  - Parses JSON stdout and captures structured errors.

- `tests/test_skill_script_schema.py`
  - Loads `.schema.json`.
  - Loads schema from `--schema`.
  - Rejects invalid schema output.

- `tests/test_procedural_skill.py`
  - Ensure `SKILL.md`-only skills load as instruction skills without generating fake tool actions by default.

## Phase 2 — Stop Procedural Skills Becoming Fake Tools

### Files to change

- `nerya/skills/procedural.py`
  - Remove the default synthetic `run` action behavior for normal `SKILL.md` skills.
  - Replace it with `SkillDoc` or `SkillInstructions` output that can be selected by the context builder.
  - Keep an explicit compatibility path only if an existing caller invokes legacy `skill_<id>`.

- `nerya/agent/context_builder.py`
  - Add selected skill instruction blocks to context.
  - Record which `SKILL.md` files were included.
  - Do not expose `skill_<id>` action aliases just because a `SKILL.md` exists.

- `nerya/agent/kernel.py`
  - Split skill instruction selection from executable script/tool selection.
  - Keep any executable surface limited to approved scripts and built-in core runtime tools.

- `nerya/api/gateway_commands.py`
  - Route `/<skill>` to skill instruction loading, not action execution.
  - Return an operator-visible explanation when a skill is instruction-only.

### Tests to add/update

- `tests/test_gateway_commands.py`
  - `/<skill>` loads skill instructions and continues the user task.
  - `/<skill>` does not produce a fake tool call.

- `tests/test_agent_loop.py`
  - A chat turn with a matched skill includes selected `SKILL.md` guidance and still ends with normal user-visible output.

- `tests/test_skill_truth_envelopes.py`
  - Skill selection evidence records the `SKILL.md` path and reason.

## Phase 3 — Convert Built-In Skill Directories

### File group: built-in skill docs

For every directory under `nerya/skills/builtin/*_skill/`:

- Keep or rewrite `SKILL.md` as the canonical instruction file.
- Move long operational content to `references/`.
- Move executable helpers from `actions.py` into `scripts/`.
- Delete or stop reading `skill.yml` after behavior is represented by `SKILL.md` + scripts.
- Do not create `actions.yml` or any replacement YAML manifest.

### File group: existing action modules to migrate into scripts

Convert each existing `actions.py` into one or more focused scripts under the same skill directory:

- `nerya/skills/builtin/message_skill/actions.py` -> `message_skill/scripts/`
- `nerya/skills/builtin/trading_skill/actions.py` -> `trading_skill/scripts/`
- `nerya/skills/builtin/strategy_skill/actions.py` -> `strategy_skill/scripts/`
- `nerya/skills/builtin/trigger_skill/actions.py` -> `trigger_skill/scripts/`
- `nerya/skills/builtin/memory_skill/actions.py` -> `memory_skill/scripts/`
- `nerya/skills/builtin/evolution_skill/actions.py` -> `evolution_skill/scripts/`
- `nerya/skills/builtin/operator_skill/actions.py` -> `operator_skill/scripts/`
- `nerya/skills/builtin/subagent_skill/actions.py` -> `subagent_skill/scripts/`
- `nerya/skills/builtin/team_skill/actions.py` -> `team_skill/scripts/`
- `nerya/skills/builtin/trace_skill/actions.py` -> `trace_skill/scripts/`
- every other `nerya/skills/builtin/*_skill/actions.py` -> matching `scripts/`

### Conversion order

1. Start with read-only or low-risk skills:
   - `trace_skill`
   - `memory_skill` read actions
   - `operator_skill` read/list/search helpers
2. Then operator UX helpers:
   - `message_skill`
   - `subagent_skill`
   - `team_skill`
3. Then state-writing workspace helpers:
   - `strategy_skill`
   - `trigger_skill`
   - `evolution_skill`
4. Last, trading/wallet/exchange helpers:
   - `trading_skill`
   - wallet/account/onchain/exchange-related skills

### Tests

- Run focused tests after each group instead of waiting for the whole migration.
- Expected first pass:
  - `python -m pytest tests/test_procedural_skill.py tests/test_skill_script_bridge.py tests/test_skill_script_schema.py -q`
- Expected runtime pass:
  - `python -m pytest tests/test_agent_actions.py tests/test_action_registry.py tests/test_action_availability.py -q`

## Phase 4 — Installer, Doctor, and CI Enforcement

### Files to change

- `nerya/skills/installer.py`
  - Install `SKILL.md`-only skills as valid first-class skills.
  - Install skills with `scripts/` as valid script-backed skills.
  - Reject new skill packages whose only entry point is `skill.yml`, `manifest.yml`, or `actions.py`.
  - Scan `SKILL.md`, `references/`, `scripts/`, and `templates/` separately.

- `nerya/skills/kernel.py`
  - `doctor()` reports legacy YAML/action-file usage by skill id and path.
  - `doctor()` fails or warns depending on migration phase.

- `nerya/cli/commands/skills.py`
  - `nerya skill view <id>` displays `SKILL.md` metadata and body summary first.
  - Show scripts as a secondary section.

- `nerya/api/routes_skills.py`
  - Return skill docs separately from scripts.

- `nerya/api/routes_capability.py`
  - Capability matrix separates `skills` and `scripts/tools` instead of treating action schemas as the skill.

- `tests/test_skill_installer.py`
  - `SKILL.md`-only package installs.
  - `SKILL.md + scripts/` package installs.
  - YAML-only or `actions.py`-only package is rejected for new installs.
  - Legacy installed YAML/action package is flagged as migration-only.

- `tests/test_architecture_audit.py`
  - Add a guard that no documentation asks developers to create new skill YAML or `actions.py`.
  - Add a guard that every built-in skill has `SKILL.md`.

## Phase 5 — Remove Legacy YAML and Action Files

### Files to delete after conversion

- `nerya/skills/builtin/*_skill/skill.yml`
- `nerya/skills/builtin/*_skill/skill.yaml`
- `nerya/skills/builtin/*_skill/manifest.yml`
- `nerya/skills/builtin/*_skill/manifest.yaml`
- `nerya/skills/builtin/*_skill/actions.py`

### Code to remove after deletion

- YAML-first loading branches in `nerya/skills/registry.py`.
- `SkillManifest.from_yaml()` as a canonical path in `nerya/skills/manifest.py`.
- Default synthetic `skill_<id>` tool generation for `SKILL.md` files.
- Tests that assert YAML-only or `actions.py`-only skill loading is normal behavior.
- Docs that describe YAML action manifests or `actions.py` as the target architecture.

### Final acceptance checks

```bash
cd Nerya
rg -n "skill\.ya?ml|manifest\.ya?ml|actions\.py" nerya/skills/builtin docs AGENTS.md
python -m pytest tests/test_procedural_skill.py tests/test_skill_script_bridge.py tests/test_skill_script_schema.py tests/test_skill_installer.py tests/test_architecture_audit.py -q
```

Expected final state:

- No built-in skill depends on skill YAML or `actions.py`.
- `SKILL.md` is required for every built-in skill.
- Executable helpers live under `scripts/`.
- `/<skill>` loads procedural instructions, not fake action aliases.
- Mutating scripts still pass through Nerya safety layers, risk gates, proposals, journaling, redaction, and tests.
- Dashboard/API/CLI display skill instructions separately from executable scripts.

## Rejected Path

Do not replace `skill.yml` with `actions.yml`, `tool_manifest.yml`, or `actions.py` as the canonical next abstraction. That keeps the core problem: developers still define skill behavior through Nerya-specific runtime metadata instead of standard skill structure. If a future runtime requires a serialized tool registry, it should be generated from script discovery and explicit script contracts, not hand-authored as a skill file.

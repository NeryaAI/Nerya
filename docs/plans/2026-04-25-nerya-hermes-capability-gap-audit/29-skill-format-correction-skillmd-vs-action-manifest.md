# 29 — Skill Format Correction: Hermes Uses `SKILL.md`, Nerya Uses YAML Action Manifests

## Status (2026-04-25)

Section status:

1. **Recognise SKILL.md as a first-class skill** — COMPLETED. `Nerya/nerya/skills/procedural.py` parses YAML frontmatter + Markdown body and exposes a synthetic `run` action so any `SKILL.md` drops into the runtime (Plan 02 P0 §4).
2. **Co-exist YAML action manifest with SKILL.md** — COMPLETED. The runtime keeps both: structured action manifests (`Nerya/nerya/skills/manifest.py:SkillManifest.from_yaml`) for tool registration, and procedural skills (`Nerya/nerya/skills/procedural.py:ProceduralSkill`) for model-facing playbooks.
3. **Loader for both formats** — COMPLETED. `Nerya/nerya/skills/registry.py:SkillRegistry.load_builtin` walks the builtin tree and feeds either format into the unified registry.
4. **Tests** — COMPLETED. `Nerya/tests/test_procedural_skill.py` covers SKILL.md ingest + run-handler output.
5. **Migrate Nerya prompts/instructions out of `agent_hint` / context_builder into SKILL.md** — PENDING. Tracked under Plan 25 P1 (workspace prompt literals).

Status: COMPLETED for the loader/runtime; remaining migration of prompt content from Python literals into SKILL.md → Plan 25.

This document records a key correction discovered after re-checking Hermes and Nerya code: Nerya's current `skill.yml` format is not the same thing as Hermes Agent's skill format. Nerya is mixing two concepts that should be separate:

1. **Agent skill** — human/model-readable procedural instructions, usually `SKILL.md` with YAML frontmatter and Markdown body.
2. **Tool/action manifest** — machine-readable action schema, permissions, handlers, approval policy, and runtime dispatch metadata.

Nerya currently calls the second one a skill. That is a major design mismatch and explains why the current skill system feels unlike a standard skill system.

## Code Evidence

### Hermes Agent

- `hermes-agent/skills/software-development/test-driven-development/SKILL.md:1` is a Markdown skill file with YAML frontmatter.
- `hermes-agent/skills/software-development/test-driven-development/SKILL.md:2-8` declares `name`, `description`, `version`, `author`, `license`, and `metadata`.
- `hermes-agent/skills/software-development/test-driven-development/SKILL.md:10+` contains Markdown procedural instructions for the model/operator.
- `hermes-agent/tools/skills_hub.py:688-700` parses YAML frontmatter from `SKILL.md` content.
- `hermes-agent/tools/skills_hub.py:2271-2284` enumerates optional skills by recursively finding `SKILL.md` and reading frontmatter metadata.
- `hermes-agent/tools/skills_hub.py:2307-2319` again confirms the skill parser expects Markdown frontmatter, not a separate YAML-only action file.
- `hermes-agent/tools/skills_hub.py:2570-2582` treats `SKILL.md` size/context cost as the skill loading concern.

### Nerya

- `Nerya/nerya/skills/builtin/strategy_skill/skill.yml:1` starts with `id`, `version`, `title`, `description`, `source`, `permissions`.
- `Nerya/nerya/skills/builtin/strategy_skill/skill.yml:13+` declares `actions` with `permissions`, `agent_action`, `agent_payload_builder`, `approval_gate`, and `input_schema`.
- `Nerya/nerya/skills/manifest.py:13-56` defines `ActionSpec`, which is an action/tool dispatch spec, not a model-readable skill instruction object.
- `Nerya/nerya/skills/manifest.py:79-116` loads `skill.yml` via `SkillManifest.from_yaml()`.

## Correction

Hermes skills are **not** Nerya-style YAML action manifests.

Hermes skill shape:

```text
<skill>/SKILL.md
<skill>/scripts/...
<skill>/references/...
<skill>/templates/...
```

`SKILL.md` shape:

```markdown
---
name: test-driven-development
description: Use when implementing any feature or bugfix...
version: 1.1.0
metadata:
  hermes:
    tags: [testing, development]
---

# Test-Driven Development

Procedural instructions...
```

Nerya current shape:

```text
<skill>/skill.yml
<skill>/actions.py
```

`skill.yml` shape:

```yaml
id: strategy
permissions:
  - strategy.read
actions:
  - name: list
    permissions: [strategy.read]
    agent_action: list_strategies
    input_schema: {...}
```

That is much closer to a **tool/action registry manifest** than a standard agent skill.

## Why This Matters

### 1. The LLM Should Load Skill Instructions, Not Action Schemas

A standard skill tells the model:

- when to use the skill,
- workflow steps,
- gotchas,
- examples,
- failure/recovery patterns,
- which scripts/references/templates to open only when needed.

Nerya's `skill.yml` mostly tells the runtime:

- action names,
- JSON schemas,
- permission strings,
- payload builders,
- handler mapping.

This makes the model see something like API metadata instead of operational know-how.

### 2. Nerya Is Conflating Skill Loading With Tool Registration

Current Nerya design mixes:

- model-facing instructions,
- action schemas,
- Python handler routing,
- permissions,
- approval gates,
- context visibility,
- natural-language hints.

These should be related, but not the same file or same abstraction.

### 3. This Encourages Prompt/Context Hardcoding

Because `skill.yml` lacks rich procedural Markdown, Nerya compensates by putting model instructions into:

- `context_builder.py`,
- `agent_hint`,
- static action catalogs,
- planner defaults,
- dashboard wrappers,
- built-in prompts.

That is exactly the hardcoding pattern the audit is trying to remove.

### 4. Nerya Skills Are Hard to Share With Standard Skill Ecosystems

Hermes and broader agent skill ecosystems expect `SKILL.md`. Nerya's `skill.yml` is custom and not directly compatible with:

- Hermes skills hub,
- Claude/Codex-style skill directories,
- `SKILL.md` frontmatter discovery,
- skill marketplace search/install,
- progressive disclosure of references/scripts/templates.

## Required Target Design

Nerya should split the current `skill.yml` concept into two layers.

### Layer A — Standard Agent Skill

Use `SKILL.md` as the model/operator-facing entry point:

```text
nerya/skills/builtin/strategy_skill/SKILL.md
```

Responsibilities:

- name/description/version metadata via YAML frontmatter,
- when-to-use instructions,
- workflows,
- examples,
- warnings,
- references to scripts/templates/references,
- context-loading policy written for the model,
- no giant embedded action schema dump.

### Layer B — Action Manifest

Keep machine-readable dispatch metadata, but rename it so it is not confused with a standard skill:

```text
nerya/skills/builtin/strategy_skill/actions.yml
```

or:

```text
nerya/skills/builtin/strategy_skill/tool_manifest.yml
```

Responsibilities:

- action names,
- JSON input/output schemas,
- handler mapping,
- permissions,
- risk/approval gates,
- availability checks,
- required env/secrets,
- UI/gateway/CLI visibility,
- tests/contracts.

## Migration Plan

### P0 — Stop Calling YAML Action Manifests “Skills”

- Keep backwards compatibility for `skill.yml`, but mark it as legacy action manifest.
- Introduce `SKILL.md` as required for every built-in and installed skill.
- Rename internal concepts:
  - `SkillManifest` -> `ActionManifest` or `ToolManifest` where it describes actions.
  - `ActionSpec` stays as action/tool schema.
  - Add a separate `SkillDoc` / `SkillInstructions` model for `SKILL.md`.

### P0 — Add `SKILL.md` Parser

- Parse YAML frontmatter from `SKILL.md`, compatible with Hermes-style frontmatter.
- Required fields: `name`, `description`.
- Recommended fields: `version`, `author`, `license`, `metadata.nerya.tags`, `metadata.nerya.permissions`, `metadata.nerya.related_skills`.
- Preserve Markdown body as the instruction content.

### P0 — Update Context Loading

- Context builder should select relevant `SKILL.md` files or summaries by trigger/capability.
- It should not dump all action schemas into every turn.
- It should load action/tool schemas separately only for enabled/permitted actions.
- It should record which `SKILL.md` sections were included in the context manifest.

### P1 — Convert Built-In Nerya Skills

For each built-in skill directory:

```text
strategy_skill/
  SKILL.md
  actions.yml       # migrated from current skill.yml actions block
  actions.py
  references/
  templates/
```

Convert at least:

- `strategy_skill`,
- `exchange_skill`,
- `strategy_review_skill`,
- wallet/account skills,
- messaging/gateway skills,
- memory/reflection/evolution skills.

### P1 — Compatibility With Hermes/Standard Skills

- Allow installing a standard `SKILL.md`-only skill as instruction-only.
- Allow optional `actions.yml` if the skill provides executable Nerya actions.
- Do not require every skill to have `actions.py`.
- For external Hermes-style skills, preserve directory layout and load `scripts/`, `references/`, `templates/` progressively.

### P1 — Update Skill Hub/Installer

- Search and inspect `SKILL.md` metadata, not only `skill.yml`.
- Show trust/source/hash/scan verdict for `SKILL.md` and action manifest separately.
- Warn if `SKILL.md` is huge and would consume too much context.
- Scan Markdown for prompt-injection/security risks separately from action code.

## Acceptance Tests

- A Hermes-style `SKILL.md` with frontmatter installs successfully as instruction-only.
- A Nerya skill with `SKILL.md + actions.yml + actions.py` exposes both instructions and executable actions.
- Legacy `skill.yml` still loads but emits a deprecation warning and is treated as action manifest, not full skill.
- Context builder includes selected `SKILL.md` summaries and records them in context manifest.
- Tool schema generation reads `actions.yml`, not `SKILL.md` body.
- Dashboard skill page shows two tabs: `Instructions` and `Actions`.
- Skill hub search uses `SKILL.md` frontmatter `name/description/tags`.
- External standard skill directories with `scripts/`, `references/`, and `templates/` are preserved.
- CI fails if a built-in skill lacks `SKILL.md` after the migration deadline.

## Documentation Updates Needed

- `02-skill-loading-and-execution.md` should be reworded: Nerya currently has action manifests, not standard skills.
- `22-context-prompt-hardcoding-and-skill-loading-audit.md` should state that action hints are not a substitute for `SKILL.md` procedural instructions.
- `28-ninth-pass-cli-profile-proxy-test-contract-gaps.md` should treat CLI/API/dashboard/action registry generation as separate from skill instruction loading.

## Do Not Claim Yet

Do not claim Nerya has Hermes-compatible or standard skill support until:

- it can install and load `SKILL.md` frontmatter/body,
- it separates skill instructions from action schemas,
- built-in skills have `SKILL.md`,
- context loading is skill-document driven,
- action schemas are generated from a separate manifest/registry,
- standard `SKILL.md`-only skills can exist without executable actions.


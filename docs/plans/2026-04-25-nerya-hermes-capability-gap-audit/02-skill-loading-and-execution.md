# 02 - Skill Loading and Execution Gap

## Current Nerya Capability

Nerya has a strong built-in skill model for trading-native actions.

Evidence:

- Built-in skills live under `nerya/skills/builtin/<name>_skill/` with manifests and action modules.
- The repo currently has 24 built-in skill directories, including trading, trigger, message, market data, strategy, evolution, subagent, trace, wallet, exchange, and capability developer skills.
- `nerya/skills/runtime.py` validates action input against manifests, journals start/done/error events, and enforces caller policy such as `subagent_only`.
- `nerya/skills/kernel.py` composes registry/runtime/tool runner wiring.
- Tests include `tests/test_skill_manifest.py`, `tests/test_skill_runtime_contracts.py`, `tests/test_skill_schema_phase6.py`, `tests/test_skill_installer.py`, `tests/test_skill_scaffold.py`, and `tests/test_generalist_skills.py`.

## Hermes Capability

Hermes treats skills as a user-facing procedural-memory ecosystem.

Evidence:

- `agent/skill_commands.py` scans user skills and injects skill content into the conversation flow.
- `tools/skills_tool.py`, `tools/skill_manager_tool.py`, `tools/skills_hub.py`, `tools/skills_sync.py`, and `tools/skills_guard.py` expose skill list/view/install/manage/sync behavior.
- `hermes_cli/skills_config.py` enables/disables skills per platform.
- `hermes_cli/skills_hub.py` supports browsing/installing from a hub.

## Gap

Nerya has a better **typed action-skill runtime** but a weaker **operator skill ecosystem**.

Missing or weak areas:

- user-installed skills are not as central as built-ins,
- no Hermes-equivalent skill hub/browse/install/sync UX,
- no slash-command-like `/<skill>` user invocation model,
- no per-platform enable/disable and visible skill help parity,
- no mature skill self-improvement loop based on observed failures,
- no clear distinction between executable action skills, procedural prompt skills, operator workflow skills, and MCP/tool backed skills.

## P0 Alignment Items

1. Add a user skill root, e.g. `workspace/skills/` plus `~/.nerya/skills/`, and make discovery visible in CLI/API/dashboard. **Status: COMPLETED 2026-04-25.** `Nerya/nerya/skills/registry.py:60-180` now scans `workspace/skills/installed/` (legacy), `workspace/skills/<id>/` (top-level), and `~/.nerya/skills/` (or `$NERYA_USER_SKILLS_ROOT`). `Nerya/nerya/skills/registry.py:158-185` adds `_user_skill_roots()` and `_register_procedural()` helpers. CLI surface: `Nerya/nerya/cli/commands/skills.py:90-175`. Dashboard surface routes through the existing `/skills` endpoints (no UI work needed because they read `skills.list()` which now reflects the new roots). Coverage: `Nerya/tests/test_procedural_skill.py` (4 cases — installed/<id>/SKILL.md, run-action body, top-level workspace/<id>, journal `loaded_via`).
2. Add skill commands: `nerya skill list`, `view`, `install`, `enable`, `disable`, `sync`, `doctor`. **Status: COMPLETED 2026-04-25.** Existing: `list`, `enable`, `install`, `promote`, `installed`. Added: `disable` (`Nerya/nerya/cli/commands/skills.py:90-105`), `view` (`Nerya/nerya/cli/commands/skills.py:107-122`), `doctor` (`Nerya/nerya/cli/commands/skills.py:124-132`), `sync` (`Nerya/nerya/cli/commands/skills.py:134-142`). Each command surfaces through `SkillKernel.view/doctor/reload` (`Nerya/nerya/skills/kernel.py:50-141`) and the SDK shim `Nerya/nerya/sdk/skill_api.py:30-43`. Subparser registration: `Nerya/nerya/cli/commands/skills.py` (`register()` block updated to add `disable`, `view`, `doctor`, `sync`).
3. Add chat invocation: `/skills`, `/<skill-name>`, `/skill view <name>`. **Status: PARTIALLY COMPLETED 2026-04-25.** `/skills` and `/skill view <id>`/`/skill doctor` are wired through the gateway command registry (`Nerya/nerya/api/gateway_commands.py:142-220, 273-295`); `BUILTIN_COMMANDS` exposes them on every gateway. `/<skill-name>` direct invocation is still a follow-up — the registry needs a fallback handler that resolves unknown `/foo` to a procedural-skill `run` call. Coverage: `Nerya/tests/test_gateway_commands.py:53-100` (3 new cases — registry baseline includes `/skills` + `/skill`, `/skills` lists `operator`, `/skill view operator` renders manifest, `/skill view <missing>` falls back gracefully).
4. Support procedural `SKILL.md` skills separately from action manifests. **Status: COMPLETED 2026-04-25.** New module `Nerya/nerya/skills/procedural.py` (1-130) parses YAML frontmatter, builds a synthetic `SkillManifest` with a single `run` action, and returns the markdown body for the agent to splice into context. Registry wires both `installed/<id>/SKILL.md` (fallback when no `skill.yml`) and top-level `workspace/skills/<id>/SKILL.md` (`Nerya/nerya/skills/registry.py:86-145`). Procedural skills are flagged `source="procedural"` and tagged `procedural` so dashboards can distinguish them. Coverage: `Nerya/tests/test_procedural_skill.py` (4 cases).
5. Add skill execution transcript entries that explain exactly what skill was loaded and why. **Status: COMPLETED 2026-04-25.** Runtime journal start record now carries `loaded_via` (`builtin`/`user_installed`/`procedural`/`user_root`), `manifest_path`, and the resolved `permissions` (`Nerya/nerya/skills/runtime.py:111-138`). The `kind: skill.call.start` event therefore answers "which skill loaded, from which manifest path, with which permissions". Coverage: `Nerya/tests/test_procedural_skill.py::test_procedural_skill_journals_loaded_via`.

## P1 Alignment Items

1. Add skill marketplace/hub compatibility or import path for agentskills.io/OpenClaw/Hermes skills.
2. Add skill policy gates: allowed tools, paths, network domains, trading/write actions.
3. Add skill self-improvement proposals from failures and repeated user corrections.
4. Add dashboard skill manager.

## Acceptance Gate

A P0-ready skill system should allow: install a user skill, invoke it from chat by name, have Nerya load its instructions, run allowed tools, produce output, and show the operator which skill and permissions were used.
# Third-Pass Edge And Ecosystem Parity Gaps

## Status (2026-04-25)

This document enumerates ecosystem-level parity items that are largely **product/UI surface** rather than runtime gaps. The core runtime + skills + gateways already shipped (see Plans 01-08). Section status:

1. **TUI quality and interactive controls** — PENDING. Backend hooks exist (`Nerya/nerya/agent/streaming.py`, `POST /agent/interrupt`); Hermes-style TUI client → Plan 21 P2.
2. **CLI command surface** — PARTIALLY COMPLETED. `Nerya/nerya/cli/` covers `init`, `run`, `gateway`, `cron`, `mcp`, `acp`, `tools`, `skills`, `doctor`. Remaining `auth`, `profiles`, `plugins` commands → Plan 28.
3. **Sessions and profiles** — PARTIALLY COMPLETED. Session store at `Nerya/nerya/agent/session.py`; profile selector → Plan 28.
4. **Packaging / install / update** — PARTIALLY COMPLETED. `Nerya/nerya/install/` covers basic bootstrap; Docker / Homebrew / Nix → Plan 27.
5. **Python library usage / batch runner** — PENDING (Plan 21 P2). Nerya's `nerya.sdk` covers programmatic use; batch runner not yet packaged.
6. **GitHub ecosystem skills** — PARTIALLY COMPLETED. `Nerya/nerya/skills/builtin/operator_skill` provides git access; dedicated GitHub PR review skill → Plan 30.
7. **Docs / website / reference** — PENDING (out of scope for capability sprint).
8. **Optional skill ecosystem** — PARTIALLY COMPLETED. `Nerya/nerya/skills/builtin/capability_developer_skill` allows authoring; remote skill hub → Plan 30.
9. **Benchmark / eval environments** — PENDING. RL/eval loops out of scope for current sprint.
10. **Migration tooling** — PENDING. Tracked under Plan 30 follow-ups.

Status: PARTIALLY COMPLETED — runtime foundations exist; the remainder is a product-shell sprint tracked under Plans 21/27/28/30.

This file is a third pass over Hermes capabilities that are easy to miss because they are not part of the core agent loop, Telegram gateway, or context manager. They still matter because they make Hermes usable as a daily operator/coding agent rather than only a runtime library.

## Evidence Used

Hermes evidence came from directory/test/doc discovery, especially:

- TUI and CLI UX: `hermes-agent/ui-tui/src/*`, `hermes-agent/tui_gateway/*`, `hermes-agent/tests/cli/test_cli_*.py`, `hermes-agent/tests/tui_gateway/*`, `website/docs/user-guide/tui.md`.
- Session/product docs: `website/docs/user-guide/sessions.md`, `website/docs/user-guide/profiles.md`, `website/docs/user-guide/git-worktrees.md`, `website/docs/developer-guide/session-storage.md`.
- Installation/packaging/update: `Dockerfile`, `docker/`, `nix/`, `flake.nix`, `packaging/homebrew/hermes-agent.rb`, `setup-hermes.sh`, `website/docs/getting-started/*`, release notes.
- Python/library usage: `website/docs/guides/python-library.md`, `mcp_serve.py`, `mini_swe_runner.py`, `batch_runner.py`.
- GitHub/software-dev ecosystem: `skills/github/*`, `website/docs/guides/github-pr-review-agent.md`, `website/docs/guides/webhook-github-pr-review.md`, `skills/software-development/*`.
- Docs/website/reference ecosystem: `website/docs/reference/*`, `website/docs/developer-guide/*`, `website/scripts/extract-skills.py`.
- Optional skill ecosystem: `optional-skills/*`, `skills/*/DESCRIPTION.md`, `skills/*/*/SKILL.md`.
- Benchmark/eval environments: `environments/benchmarks/*`, `tinker-atropos`, `trajectory_compressor.py`, `datagen-config-examples/*`.
- Migration/integration docs: `website/docs/guides/migrate-from-openclaw.md`, `optional-skills/migration/openclaw-migration/SKILL.md`, autonomous-agent optional skills.

## More Missing Surfaces

### 1. TUI Quality And Interactive Controls

Nerya dashboard chat is not equivalent to Hermes TUI. Missing details include:

- Full keyboard-first flow: history navigation, multiline compose, paste, copy, clear, reset, new session.
- Interrupt from keyboard that maps to backend cancellation, not only local request abort.
- Approval UI in terminal, including choices, denial reasons, sudo/secret prompts.
- Streaming renderer that distinguishes assistant text, tool trail, diffs, progress, and system messages.
- Busy-session switching guard, queued input drain, retry/undo/compress commands.
- File-drop/image command/browser-connect command flows.
- Context warning display when prompt approaches budget.
- Background TUI refresh when gateway/session state changes.
- Terminal width-aware rendering, code block/diff wrapping, ANSI handling.
- Slash command registry and help that stays in sync with backend capability.

### 2. CLI Command Surface

Hermes has many daily commands that are not just developer niceties:

- `init`, `new`, `resume`, `status`, `logs`, `doctor`, `auth`, `profiles`, `plugins`, `mcp`, `cron`, `gateway`, `tools`, `skills`.
- Branch/worktree commands and project navigation helpers.
- Plan command and structured plan editing.
- Copy command and clipboard integration.
- Image/file/drop commands for multimodal prompts.
- Browser connect/disconnect commands.
- Model switch/profile switch commands with session persistence.
- Non-ASCII credential handling and env sanitization.
- Loading indicators and concise failure formatting.
- CLI config watch and hot reload for MCP/tools.

Nerya should decide whether it wants an equivalent first-class CLI or explicitly stay dashboard/API-first. If parity is the goal, CLI cannot remain a thin service wrapper.

### 3. Packaging, Installation, Updating, Portability

Hermes ships across environments. Nerya still needs a parity checklist for:

- Homebrew or equivalent package manifest.
- Nix flake and reproducible dev shell.
- Docker image and runtime compose docs.
- Termux/mobile constraints.
- Windows/Powershell setup and service install.
- Update command and migration notes.
- Release notes tied to capability changes.
- Version compatibility for CLI, web dashboard, gateway, skills, MCP.
- Startup bootstrap checks: Python/node versions, optional deps, browser deps, Docker, git.
- Self-contained uninstall/cleanup instructions.

### 4. Config Profiles And Workspace Switching

Additional missing config ergonomics:

- Named profiles for model/provider/gateway/tool configuration.
- Workspace/project switching with isolated sessions, memories, skills, env vars, logs.
- Overlay config precedence: global -> profile -> workspace -> session -> gateway/channel.
- Safe config edit commands and schema validation.
- Config diff and rollback.
- Redacted config export for support/debugging.
- Stale config cleanup, especially base URLs and provider env refs.
- Per-channel gateway display/config overrides.

### 5. Git, PR, Code Review, Worktree Workflow

Hermes has GitHub/software-development skills that imply missing Nerya coding-agent workflow:

- GitHub auth skill and credential handling.
- Issue creation/commenting and PR review workflows.
- PR body templates and conventional-commit references.
- Code review output templates and review comment mapping.
- Webhook GitHub PR review bot flow.
- Worktree creation/switching, branch display, dirty-tree guard.
- CI troubleshooting reference flow.
- Patch series tracking and rollback after failed test/build.
- Remote repo provider abstraction beyond local file edits.

### 6. Documentation And Reference Generation

Hermes has a user/developer docs site and generated references. Nerya needs:

- User guide split by operator tasks, not only architecture notes.
- Developer guide for adding tools, providers, platforms, skills, context engines.
- Generated skill catalog and optional-skill catalog.
- Generated tools/toolsets reference.
- Gateway platform setup docs per platform with troubleshooting.
- Security model doc that matches actual auth/permission code.
- API reference/versioning/changelog.
- Migration guide from Hermes/OpenClaw-like systems if Nerya is positioned as replacement.
- Docs tests or extraction scripts to catch stale examples.

### 7. Python Library / Embedding / Batch Runner

Hermes can be consumed beyond one interactive app. Missing Nerya parity surfaces:

- Stable Python library API for embedding Nerya agent loop.
- Batch runner for many prompts/tasks with output artifacts.
- Mini SWE runner or equivalent coding benchmark runner.
- MCP serve mode as standalone deployment surface.
- Headless gateway/service mode with health checks.
- Structured trajectory export for offline analysis.
- Scriptable SDK examples that are maintained against live API contracts.

### 8. Benchmarks And SWE/Terminal Evaluation

Hermes has benchmark/environment folders that imply an evaluation culture:

- TerminalBench-like task environment.
- SWE-style environment with patches and grading.
- Browser task datagen examples.
- Trajectory compression configs.
- Eval runner that can compare agent variants.
- Regression suite for “agent feels worse” cases, not just unit tests.
- Scorecards: task success, interruption recovery, latency, cost, duplicate messages, tool failure recovery.

### 9. Optional Skill Breadth And Marketplace Categories

Earlier docs covered “skill hub”, but not breadth. Hermes has categories Nerya may need or intentionally reject:

- GitHub, software-development, devops, feeds, research, productivity, note-taking, maps, Google Workspace, Notion, Linear.
- Apple/iMessage/FindMy/Reminders/Notes integrations.
- Blockchain optional skills beyond Nerya trading connectors.
- Security/1Password/OSS forensics/Sherlock-style skills.
- MLOps/inference/training/chroma/faiss/vllm/llama.cpp/outlines skills.
- Creative/artifact/document/PPT/PDF/OCR skills.
- Smart-home and automation skills.
- Migration skills from OpenClaw and other agent systems.

Nerya does not need all of these for trading, but if the benchmark is Hermes, the ecosystem breadth gap is large.

### 10. Localization, Themes, Website, Branding

These are not core intelligence but affect product maturity:

- Web dashboard i18n/localization infrastructure.
- Theme system and UI density controls across web/TUI/gateway.
- Website docs, screenshots, banner/assets, public landing page.
- Consistent naming, release notes, upgrade docs.
- Accessible UI components and keyboard navigation.
- Error copy that gives operator next actions.

### 11. Provider-Specific Edge Cases

Beyond generic provider routing:

- Anthropic OAuth flow and prompt caching specifics.
- Bedrock auth modes and model/tool compatibility denylist.
- Gemini native/cloud-code adapters.
- Copilot/Google Code Assist provider modes.
- OpenRouter/NVIDIA/Minimax/custom auxiliary model URL quirks.
- Local model context discovery.
- Cross-loop client cache and async resource cleanup.
- Provider-specific rate guard and error classifier.
- Cloudflare/header/proxy edge handling.

### 12. Prompt Assembly And Display Polish

More subtle Hermes features to align:

- Prompt builder with compact sections and provider-specific formatting.
- Subdirectory hints injected based on cwd/project tree.
- Title generator for sessions.
- Display emoji/compact output tests.
- Redaction pipeline before display and logs.
- Context references resolver for files/URLs/sessions/artifacts.
- Compression focus topic and manual compression feedback.
- Human-readable insight summaries from session history.

### 13. Webhook And External Event Ingestion

Nerya has trading triggers, but Hermes-like general inbound event surface also includes:

- Dynamic webhook routes with signatures and rate limits.
- GitHub PR webhook review flow.
- Deliver-only webhook mode.
- Generic webhook subscriptions skill.
- Webhook replay/dedup/debug logs.
- Mapping inbound webhook events to sessions, channels, skills, or cron jobs.

### 14. Data Retention, Export, Import, Backup

Previously mentioned only lightly. Need explicit product behavior:

- Export sessions, memories, artifacts, logs, config, skills.
- Import/migrate from old workspace or Hermes/OpenClaw workspace.
- Retention policy per media/artifact/log/session type.
- Backup/restore command and dashboard control.
- Redacted support bundle generator.
- Delete-me/privacy controls for user/chat IDs and attachments.

### 15. Operational Reliability And Observability

More production details:

- Health endpoints for service, gateway, cron, model providers, MCP servers.
- Metrics for turns, tools, cost, latency, failure classes, queue depth.
- Structured logs with correlation IDs across gateway -> agent -> tool -> provider.
- Error classifier with user-facing remediation.
- Crash recovery for open turns and background processes.
- Watchdog for gateway adapters and reconnect loops.
- Alerting to home channel when critical services fail.

### 16. Agent Personality / UX Policy Layer

Hermes has a mature “operator assistant” feel. Nerya needs explicit UX policies:

- When to ask vs act autonomously.
- How to report partial failure and next action.
- How verbose to be per channel/platform.
- How to summarize tool-heavy work without hiding evidence.
- How to handle user frustration, correction, and repeated interruption.
- How to preserve latest user instruction over older memory/context.
- Per-channel personas and response formatting.

### 17. Safety Review And Red-Team Harness

Nerya has trading risk gates, but Hermes-like general agent safety needs:

- Red-team optional skills and jailbreak/refusal detection references.
- Tool-level URL/file/path/command/security review tests.
- OSS forensics and dependency risk skills.
- Prompt-injection test corpus across gateway/web/files/docs.
- Approval bypass regression tests.
- Secret leakage regression tests across model, logs, gateway, artifacts.

## Third-Pass Priority Additions

If the first two backlogs are already accepted, add these to the roadmap:

1. TUI/CLI parity plan: slash commands, keyboard flows, approval UI, file/image/browser commands.
2. Packaging/update plan: Docker, Nix, Windows service, Homebrew-like installer, release/migration docs.
3. GitHub/code-review/worktree flow: PR review bot, worktree safety, CI troubleshooting, branch/dirty guards.
4. Docs/reference generation: skill/tool/platform/API catalogs that cannot drift silently.
5. Embedding/eval harness: Python library API, batch runner, SWE/terminal/browser benchmarks, trajectory export.
6. Config/profile/workspace switching: overlays, validation, redacted export, rollback.
7. Webhook/external events: signatures, subscriptions, replay, generic event routing.
8. Backup/retention/support bundle: export/import/delete policies and UI.
9. Provider edge-case hardening: OAuth, prompt caching, cloud/local provider quirks, rate/error classifiers.
10. Product UX policy: ask/act rules, channel verbosity, latest-correction handling, evidence summaries.

## Correction To Previous Addenda

`17` and `18` cover the big runtime/gateway/platform layers, but still did not fully capture TUI depth, CLI command ergonomics, installation/update portability, GitHub/PR/worktree workflows, generated docs/reference infrastructure, embedding/batch/eval runners, optional skill ecosystem breadth, localization/theme maturity, provider-specific edge cases, webhook subscriptions, backup/export/retention, and product-level UX policy.
# Coding Agent and File Tool Improvement Plan

Date: 2026-04-27
Status: proposed implementation plan
Scope: Nerya runtime, operator skill, agent loop, subagents, dashboard evidence surfaces

How to use this document:

- Treat P0 as the implementation backlog for making Nerya reliable on normal
repository coding tasks.
- Treat P1/P2 as capability expansion after stale-edit prevention and evidence
replay are working.
- Keep each milestone independently shippable; do not wait for browser tools,
LSP, or worktree isolation before improving file reads and exact edits.
- When implementation begins, update this file with status notes rather than
replacing the plan with a new audit.

## Executive Summary

Nerya already has a useful operator tool surface: it can read files, list
folders, search text, patch/write files, run terminal commands, and manage
background processes through the `operator` skill. It also has a real
`AgentKernel` turn loop, a `ToolRunner` chokepoint, query-only parallel calls,
streaming events, cancellation, session search, and coding-oriented subagent
roles.

The main gap is not that Nerya has no coding tools. The gap is that those tools
are still exposed as trading-runtime skills rather than as a first-class coding
agent substrate. Claude Code's stronger design is the combination of:

1. a strict tool protocol,
2. a file-read state cache,
3. stale-edit prevention,
4. structured tool-result pairing,
5. read-only concurrency with serialized writes,
6. context-budget-aware file reading,
7. coding-specific loop behavior,
8. verification evidence that can be replayed.

This document proposes a concrete Nerya-native plan to close that gap while
preserving Nerya's core rules: skill-first operation, workspace chrooting,
proposal/approval gates for mutating changes, and trading safety.

## Canonical Implementation TODO Checklist

This checklist is the canonical execution order. Later sections keep the
background analysis and detailed design notes, but implementation should follow
this ordered list rather than the older scattered P0/P1/P2 numbering.

### Phase 0 — Product Boundary and Modes

Goal: decide when Nerya behaves like a coding agent, when it behaves like a
trading control plane, and where approvals are mandatory.

- Define runtime modes: `read_only`, `coding`, `validated`, `operator`,
`live_control`.
- Add a task router that maps user requests to modes:
`workspace_native`, `trading_control`, `read_only_answer`,
`live_side_effect`, `scheduled_event`.
- Make `workspace_native` the default for coding, strategy authoring,
debugging, docs, SDK, skill, MCP, and provider implementation tasks.
- Keep `live_control` for live trading, wallet signing, account actions,
runtime promotion, external deployment, and irreversible side effects.
- Update prompts so coding tasks start with workspace file inspection, not
domain action selection.
- Add mode labels to chat/dashboard events so users know whether the agent
is editing code, validating code, or operating live runtime state.

Exit criteria:

- "Write/create/modify/debug a strategy" routes to coding mode.
- "Enable/promote/execute live" routes to live-control mode.
- The UI clearly shows the current mode and approval boundary.

### Phase 1 — Provider-Shaped Agent Loop and Transcript

Goal: replace rigid action-first planning with Claude-Code-like model/tool
recursion for workspace tasks.

- Add `nerya/agent/workspace_native.py`.
- Add transcript block types: `assistant_message`, `tool_use`,
`tool_result`, `attachment`, `compact_boundary`, `tombstone`, `interrupt`.
- Implement recursive loop:
`build context -> call model -> collect tool_use -> run tools -> append tool_result -> repeat`.
- Keep tool-use ids and tool-result ids paired exactly.
- Add malformed transcript validation before every model call.
- Add safe repair for orphaned tool results and fallback streaming failures.
- Preserve current `AgentKernel` for scheduled/event/trading automation
until the new loop is proven.
- Add replay/debug endpoint for one workspace-native turn.

Exit criteria:

- A coding task can do multiple read/search/edit/shell iterations without
a bespoke JSON action schema.
- Replay shows every tool call and result in order.
- The loop stops with a clear `stop_reason`.

### Phase 2 — Layered Prompt, Project Rules, and Context Builder

Goal: assemble prompt/messages/tools like Claude Code: static rules, dynamic
state, project rules, compact skill/tool indexes, then transcript.

- Add `nerya/agent/prompt_sections.py`.
- Add `nerya/agent/project_rules.py`.
- Load scoped rules from `AGENTS.md`, `NERYA.md`, and `.nerya/rules/*.md`.
- Walk from touched file directories up to workspace root and merge deeper
rules with higher priority.
- Add git status, current branch, recent commits, and dirty-file summary as
a bounded system-context section.
- Split prompt into cacheable static sections and dynamic sections.
- Render only compact `SkillIndex` rows and relevant `ToolCatalog` rows in
coding mode.
- Record loaded rule files and prompt section digests in EvidenceBundle.

Exit criteria:

- Before editing, the agent sees relevant local rules and coding style.
- Prompt context can be explained by section and source file.
- Adding a new rule file changes routing/style without code changes.

### Phase 2.5 — Plan Mode and Todo Progress Surface

Goal: add Claude-Code-like planning without making every task slower or less
autonomous.

- Add `PlanMode` as a temporary permission/context mode, not a separate agent.
- Add a persistent plan file path per session under `.nerya/plans/` or
`state/plans/`.
- Add `plan.enter` for ambiguous/high-impact implementation work.
- Add `plan.exit` to present the plan file for approval and switch back to
coding mode after approval.
- Add `todo.write` / `todo.update` as a lightweight progress tracker with
`pending`, `in_progress`, and `completed` statuses.
- Require at most one `in_progress` todo in normal solo coding mode.
- Restore todos from transcript/session state after resume or compaction.
- Show plan/todo state in dashboard chat events and final reports.
- Keep simple fixes out of plan mode; use todos only when the task has multiple
meaningful steps.

Exit criteria:

- Ambiguous feature work can pause in plan mode, inspect files, write a plan,
request approval, then resume coding with the same session state.
- Long coding tasks show live progress without forcing a rigid domain planner.
- Todo state survives interruption, compaction, and session restore.

### Phase 3 — Claude-Like Workspace File Primitives

Goal: make files the primary API for coding and strategy authoring.

- Add always-on `workspace.glob` for fast file discovery.
- Add always-on `workspace.search` for ripgrep-style search with file/line
refs.
- Upgrade `workspace.read` / `operator.read_file` with `offset`, `limit`,
`max_tokens`, file hash, read range, and repeated-read dedup.
- Add `FileStateCache` keyed by path, range, hash, mtime, and normalized
content.
- Add `workspace.edit` with `{path, old_string, new_string, replace_all?, expected_hash?}`.
- Require prior read or expected hash before editing existing files.
- Fail closed on stale file, missing old string, non-unique old string, and
oversized/binary files.
- Preserve line endings, encoding, quote style, and indentation where
possible.
- Keep `workspace.write` for new files and explicit full rewrites, but
prefer exact-string edit for existing files.
- Return structured patch, changed line count, before/after hash, and
clickable file refs for every write/edit.

Exit criteria:

- Existing files cannot be edited without read-state or hash protection.
- Stale edits tell the agent exactly to re-read and retry.
- Strategy/source files can be created and modified without domain creation
tools.

### Phase 4 — Shell Execution, Risk Policy, and Permissions

Goal: let the agent run useful local commands while keeping destructive/live
operations gated.

- Add shell command categories: `read_only`, `local_validation`,
`workspace_write`, `network`, `destructive`, `live_side_effect`.
- Add command parser/semantic classifier for shell operators, redirects,
`cd`, command substitution, `eval`, env hijacking, and parser differentials.
- Add timeout, cwd, background mode, output cap, and persisted output refs.
- Add sandbox-aware auto-allow for safe local commands.
- Add explicit approval for destructive commands, network writes, live
trading, wallet/account actions, deploys, and sandbox overrides.
- Replace simple `approval_gate` strings with `PermissionDecision` records:
`behavior`, `reason_type`, `reason`, `suggestions`, `scope`, `can_persist`.
- Support per-tool, per-command-prefix, per-path, per-skill, per-MCP-server,
and per-mode permission rules.
- Show approval prompts with exact command/diff/side-effect scope.

Exit criteria:

- Local tests/backtests run with low friction.
- Live/destructive commands require scoped approval with a clear reason.
- Large shell outputs are stored by ref and summarized in context.

### Phase 5 — Context Budget, Compaction, Interrupts, and Recovery

Goal: make long-running coding sessions robust.

- Add `nerya/agent/context_budget.py`.
- Add `nerya/agent/coding_compact.py`.
- Apply tool-result budget before model calls.
- Microcompact old tool results before transcript compaction.
- Preserve exact recent tail messages.
- Preserve tool-use/tool-result pairing through compaction.
- Preserve read-file hashes, changed-file index, invoked skills, pending
approvals, validation state, and next steps outside lossy summaries.
- Add compaction failure circuit breaker.
- Add `interrupts.py` with persistent interrupt events and replacement
prompt linkage.
- Add structured error taxonomy and recovery suggestions for stale files,
unknown tools, prompt-too-long, output-cutoff, provider errors, and permission
denial.

Exit criteria:

- A long coding session can compact and continue without losing changed
files or next steps.
- Interrupting a long command records what completed and what did not.
- Provider/context errors trigger recovery before generic failure.

### Phase 6 — EvidenceBundle and Operator-Facing Timeline

Goal: make progress, diffs, errors, validations, and final reports transparent.

- Add/upgrade `EvidenceBundle` schema for read files, edits, shell commands,
approvals, validations, compactions, interrupts, and final risk summary.
- Emit timeline events: `think`, `tool_use`, `tool_progress`,
`tool_result`, `diff`, `validation`, `approval`, `compact`, `interrupt`,
`final`.
- Add diff cards for `workspace.edit` and `workspace.write`.
- Add shell result cards with command, cwd, timeout, risk class, exit status,
output preview, and output ref.
- Add validation cards with command, status, duration, and failure summary.
- Generate final report from EvidenceBundle, not only from model memory.
- Add dashboard/API endpoints for evidence lookup by session/turn/artifact.

Exit criteria:

- User can see exactly what changed, what ran, what failed, and what remains
risky.
- Final answers include verified/not-verified facts grounded in evidence.

### Phase 7 — Strategy-as-Code Package Flow

Goal: strategy creation becomes code generation plus validation plus optional
registration.

- Add `docs/conventions/strategy-package.md`.
- Add `templates/strategy/python-script/` with `strategy.yml`, `script.py`,
tests, README, and validation report template.
- Define standard strategy package layout and script entrypoint contract.
- Define validation commands for schema, unit tests, backtest/simulation,
risk gate, and registration dry-run.
- Let the agent create/edit strategy package files directly in coding mode.
- Add small runtime actions only for `strategy.reload`, `strategy.validate`,
`strategy.register`, and `strategy.promote`.
- Keep `strategy.create` as a dashboard/API convenience wrapper, not the main
agent path.
- Write validation reports into the strategy package and link them in UI.

Exit criteria:

- "Create an ETH mean-reversion strategy and backtest it" produces real
files, tests/backtest evidence, and a validation report without first calling
a bespoke strategy-generation tool.

### Phase 8 — Artifact Index and Runtime Drift Detection

Goal: connect file-first artifacts with Nerya runtime state.

- Add `nerya/agent/artifact_index.py`.
- Index strategies, skills, scripts, triggers, accounts, docs, templates,
SDK examples, and MCP configs from files.
- Infer artifact type/id/path/entrypoint/manifest/digest.
- Compare file index with runtime registry and report drift.
- Add `nerya artifacts list` CLI/API.
- Add dashboard artifact inventory with registered/valid/stale/missing
states.
- Use artifact index in prompt context instead of stuffing full workspace
state.

Exit criteria:

- After the agent writes or edits an artifact, Nerya can report whether it
is registered, valid, stale, or needs reload.

### Phase 9 — SkillIndex, ToolCatalog, and MCP Capability Registry

Goal: make skills/MCP discoverable without bloating context or confusing them
with workspace file tools.

- Add `nerya/agent/skill_index.py` with frontmatter-only rows and on-demand
`load_skill_body`.
- Track invoked skill ids and digests for compaction preservation.
- Add `nerya/agent/tool_catalog.py` for deterministic action/tool rows,
availability, schemas, gates, and trust levels.
- Add path-scoped skill activation via `paths` metadata.
- Add `context: fork` support for long prompt skills.
- Add `nerya/mcp/capability_registry.py`.
- Classify MCP tools, prompts, resources, and `skill://` resources.
- Use fully qualified MCP tool names and remote-skill trust policies.
- Add MCP reload/cache invalidation and dashboard connection states.

Exit criteria:

- "What skills are available?" uses `SkillIndex`.
- Coding/file questions use workspace primitives.
- MCP failure or auth needs do not break the agent loop.

### Phase 10 — Domain Action Diet and Compatibility Layer

Goal: reduce narrow domain tools that block general reasoning while preserving
product APIs and safety.

- Inventory every skill action and classify it as `primitive`, `validator`,
`privileged`, `convenience`, or `deprecated_for_agent`.
- Hide `deprecated_for_agent` actions from workspace-native coding prompts.
- Keep privileged actions for live trading, wallet signing, account actions,
secret access, runtime promotion, and irreversible side effects.
- Keep validator actions for schema, risk, backtest, route/schedule,
exchange connectivity, and account sanity checks.
- Convert creation actions to templates/docs/file-first workflows where
possible.
- Keep convenience wrappers for dashboard/API users.
- Add compatibility notes so old APIs remain usable during migration.

Exit criteria:

- Missing domain creation tools no longer block ordinary code generation.
- The main coding prompt is smaller and less domain-action constrained.

### Phase 11 — Subagents, Checkpoints, Worktrees, and UI Debugging

Goal: add Claude-Code-like advanced workflow support after the core loop is
stable.

- Add subagent write scopes and read-only explorer roles for coding mode.
- Add checkpoint/resume support with changed-file and validation state.
- Add optional worktree/snapshot isolation for risky refactors.
- Add browser-assisted frontend debugging for dashboard work.
- Add background task output files and progress checks for long-running
jobs.
- Add parent-child evidence linkage for delegated work.

Exit criteria:

- Parallel workers can investigate independently without overlapping writes.
- Long coding tasks can resume after interruption or compaction.

### Phase 12 — Evaluation and Regression Suite

Goal: make the more flexible agent safe to evolve.

- Add `coding_agent_eval_runner`.
- Add golden transcript scenarios: read-only question, create strategy,
modify strategy, stale edit, prompt too long, interrupt, shell approval, MCP
failure, large output, validation repair, and live-control refusal/approval.
- Assert structural transcript events, not only final text.
- Add prompt-size and compact-quality probes.
- Add shell-policy shadow-mode comparisons before enforcing new rules.
- Add kill switches for workspace-native routing, read dedup, compaction,
shell classifier, and MCP skill ingestion.
- Run focused evals before each major rollout and record results in docs.

Exit criteria:

- Nerya can increase agent flexibility without regressing file safety,
permission safety, compaction continuity, or strategy validation behavior.

### Recommended First PR Cut

If implementing incrementally, the first PR should be intentionally small:

- Add mode enum and task router skeleton.
- Add provider-shaped transcript block dataclasses.
- Add `workspace.glob/search/read/edit` wrappers backed by existing
`operator_skill` handlers.
- Add stale-read/hash metadata to reads and exact-string edit preconditions.
- Add a minimal EvidenceBundle timeline for read/edit/shell.
- Add one golden eval: create a simple strategy package from files and run a
dry validation command.

Do not start by rewriting trading/domain skills. First make the general
workspace-native agent loop real, then gradually move domain creation workflows
onto files/templates.

## Current Repo Evidence

### Existing Nerya capabilities

- `nerya/skills/builtin/operator_skill/SKILL.md` declares `read_file`,
`list_dir`, and `search_files` as `agent_query_only: true`.
- `nerya/skills/builtin/operator_skill/SKILL.md` declares mutating actions such
as `patch_file`, `write_file`, `terminal`, and `process_start` behind risk and
approval gates.
- `nerya/skills/builtin/operator_skill/scripts/handlers.py` implements path
safety through `_safe_path`, resolving every path under the active workspace
root.
- `nerya/skills/builtin/operator_skill/scripts/handlers.py` implements
destructive-command heuristics for terminal/process commands.
- `nerya/harness/tool_runner.py` is the central execution chokepoint with
budget, timeout, retry, query-only detection, and parallel query-only batches.
- `nerya/harness/result_store.py` persists oversized tool outputs under
`state/tool_results/` and returns references.
- `nerya/agent/kernel.py` records turn steps and supports plan -> subagent ->
context -> think -> act -> observe -> replan -> close.
- `nerya/agent/kernel.py` has a safety net that forces another iteration after
read-only query actions when no operator-facing reply has been produced.
- `nerya/subagents/registry.py` already defines `coding_agent` and `code_critic`
lanes.
- `nerya/agent/streaming.py`, `nerya/agent/session.py`, and
`nerya/agent/session_search.py` provide the basis for replay and operator UI.

### Reference design lessons from Claude Code

The Claude Code expanded source shows several patterns worth adapting, not
copying verbatim:

- A tool is not just a callable. It carries schema, validation, permission,
execution, result serialization, display metadata, and concurrency semantics.
- File reading is a tracked state mutation. Read results populate a
`readFileState` cache that later edit tools use to detect stale writes.
- Editing is intentionally narrower than arbitrary file writing. Exact
`old_string -> new_string` replacement is preferred because it is auditable and
easy to reject when ambiguous.
- Read-only tools may run concurrently; write tools run serially.
- Large tool results are budgeted and replaced with references rather than
pushed fully into the model context.
- The loop continues after tool results until the model either answers, reaches a
limit, or hits an explicit stop condition.
- Tool calls, tool results, permissions, errors, interruptions, and summaries are
all first-class transcript events.

## Design Goals

1. Make Nerya a reliable coding agent for real repositories, not only a trading
  agent that can occasionally inspect files.
2. Preserve Nerya's safety model: workspace chroot, approval gates, proposal
  lifecycle, no secret leakage, no trading bypass.
3. Improve file reading so the model can inspect large repos without exhausting
  context.
4. Prevent stale edits and accidental overwrites.
5. Make coding work replayable: every file read, edit, command, test, error, and
  unverified gap should be visible to the operator.
6. Keep the implementation incremental. The first milestone should improve the
  existing `operator` skill rather than replacing the skill runtime.

## Non-Goals

- Do not embed Claude Code or Hermes as a runtime dependency.
- Do not bypass the skill-first execution model.
- Do not make mutating file operations silently apply without the existing risk
and approval policy.
- Do not add broad new dependencies until the lightweight implementation proves
insufficient.
- Do not make trading subagents inherit unrestricted coding/write permissions.

## Target Architecture

```text
User / trigger / dashboard
        |
        v
AgentKernel coding mode
        |
        +-- Context file loader
        +-- Coding loop policy
        +-- Evidence bundle builder
        |
        v
ToolRunner
        |
        +-- Coding tool metadata
        +-- Query-only parallel scheduler
        +-- Serialized mutating operations
        +-- Budget / timeout / retry / cancellation
        |
        v
operator skill scripts
        |
        +-- read_file / list_dir / search_files
        +-- edit_file / multi_edit / write_file / patch_file
        +-- terminal / process registry
        |
        v
workspace state
        |
        +-- file_state cache
        +-- tool_results refs
        +-- transcripts / sessions / turn_steps
        +-- evidence bundles
```

The key architectural change is to add a coding-specific state layer between
`AgentKernel` and the operator skill. The operator skill can remain the concrete
execution surface, but the kernel and harness must know enough about file state,
path scopes, and verification evidence to guide a real coding loop.

## P0 Implementation Plan

### P0.1 Add `FileStateCache`

Create `nerya/agent/file_state.py`.

Responsibilities:

- Track every successful file read by `session_id`, `turn_id`, and normalized
workspace-relative path.
- Store `mtime_ns`, `size`, `sha256`, `read_mode`, `start_line`, `end_line`,
`content_hash`, and whether the read was complete or partial.
- Expose `record_read(path, content, metadata)`.
- Expose `assert_fresh_for_edit(path, expected_complete=True)`.
- Expose `record_write(path, new_content, metadata)`.
- Support an in-memory fast path and JSON persistence under
`state/file_state/<session_id>.json`.

Why this matters:

- Without read state, Nerya can write based on stale context.
- Exact edits become safer because the edit tool can prove the agent has seen the
current file contents.
- Session replay can explain why an edit was accepted or rejected.

Suggested metadata schema:

```json
{
  "path": "nerya/agent/kernel.py",
  "abs_path_hash": "sha256-of-absolute-path",
  "mtime_ns": 1777220000000000000,
  "size": 12345,
  "sha256": "content-hash-if-small-or-full-read",
  "read_mode": "full|range|head|binary_summary",
  "start_line": 1,
  "end_line": 200,
  "total_lines": 900,
  "complete": false,
  "session_id": "sess_x",
  "turn_id": "turn_x",
  "recorded_at": "2026-04-27T00:00:00Z"
}
```

Acceptance tests:

- reading a file records a state entry,
- editing after a complete read succeeds,
- editing after external modification fails,
- editing after only a partial read requires either a complete read or an exact
hash-confirming mode,
- Windows path normalization treats `/` and `\\` consistently.

### P0.2 Upgrade `read_file`

Extend `nerya/skills/builtin/operator_skill/scripts/handlers.py::read_file`.

Required improvements:

- Add line-numbered output option: `number_lines: true`.
- Add Claude-compatible aliases: `offset` and `limit`, mapped to `start` and
`end`.
- Detect binary files before decoding large chunks.
- Refuse risky device-like paths even if a symlink points inside the workspace.
- Add `encoding` and `line_endings` to output.
- Add `content_sha256` for complete reads or small range reads.
- Persist large reads through `ResultStore` and return `ref_id` plus summary.
- Return `total_lines`, `start_line`, `end_line`, and `complete` consistently.
- Record successful reads in `FileStateCache`.

Proposed output shape:

```json
{
  "path": "nerya/agent/kernel.py",
  "content": "1| ...\n2| ...",
  "truncated": false,
  "bytes": 12000,
  "lines": 200,
  "total_lines": 900,
  "start_line": 1,
  "end_line": 200,
  "complete": false,
  "encoding": "utf-8",
  "line_endings": "LF",
  "content_sha256": "...",
  "ref_id": null
}
```

Implementation notes:

- Keep the current `max_bytes` behavior for backward compatibility.
- Prefer adding fields over changing existing field names.
- If `number_lines` is true, line numbers should be visible to the model but not
included in `content_sha256`.
- The default max should stay conservative; the agent should use ranges and
search rather than reading huge files.

Acceptance tests:

- `offset/limit` and `start/end` produce the same slice,
- binary file returns a safe summary or structured refusal,
- oversized file creates a `ResultStore` ref,
- successful read updates `FileStateCache`,
- line numbers do not corrupt write freshness checks.

### P0.3 Split file mutation tools

Current `patch_file` does too much: exact replacement and unified-diff patching.
Split the model-facing actions while preserving backward compatibility.

Add actions:

1. `edit_file`
  - exact `old_string` / `new_string`, default `replace_all=false`, requires a
   unique match unless `replace_all=true`.
  - requires fresh file state.
  - returns structured patch summary.
2. `multi_edit`
  - array of exact edits applied serially.
  - validates all edits before writing.
  - refuses overlapping or order-dependent ambiguous edits.
3. `write_file`
  - keep current behavior, but require explicit `mode` and include previous
   file metadata in the result.
4. `patch_file`
  - keep unified diff and legacy exact replacement, but update the prompt to
   prefer `edit_file` for normal source edits.

Required stale-write behavior:

- If target exists and no complete/fresh read exists, return
`error_kind="stale_or_unread"`.
- If target changed after read, return `error_kind="file_changed"` with current
metadata and ask the model/operator to re-read.
- If `old_string` is not found, return candidate context when safe.
- If `old_string` matches multiple locations and `replace_all=false`, refuse and
ask for more context.

Acceptance tests:

- exact edit succeeds after read,
- edit without read fails,
- edit after external modification fails,
- duplicate old string fails unless `replace_all=true`,
- `multi_edit` is atomic: no write occurs if any edit is invalid,
- generated patch is visible in the result and journal.

### P0.4 Add coding tool metadata to `ToolRunner`

Extend action specs or runtime wrappers with fields the harness can understand:

```yaml
agent_query_only: true
agent_concurrency_safe: true
agent_mutates_paths: false
agent_requires_fresh_read: false
agent_result_kind: file_read|file_write|terminal|search|process
agent_verification_command: false
```

For mutating actions:

```yaml
agent_query_only: false
agent_concurrency_safe: false
agent_mutates_paths: true
agent_requires_fresh_read: true
```

Harness behavior:

- Continue using `agent_query_only` for parallel dispatch.
- Refuse parallel execution of anything with `agent_mutates_paths=true`.
- Attach path scope and result kind to `ToolCallRecord`.
- Emit structured streaming events for `tool.start`, `tool.progress`,
`tool.complete` with result kind and path scope.

Acceptance tests:

- read-only search/list/read can run in `call_parallel`,
- edit/write/terminal cannot join query-only parallel batches,
- `ToolCallRecord` includes path scope and result kind,
- event stream exposes enough metadata for dashboard display.

### P0.5 Improve terminal as a coding verification tool

Current terminal support is useful but should become more coding-aware.

Add fields:

- `purpose`: `inspect|test|build|format|run|install|other`,
- `expected_duration_s`,
- `verification`: boolean,
- `safe_read_only`: boolean inferred from command parser,
- `touched_paths`: best-effort list for commands like `pytest tests/x.py`,
`npx tsc --noEmit`, `ruff check path`.

Behavior:

- Read-only commands can be marked collapsible and concurrency-safe only when
parser confidence is high.
- Build/test/format commands should be captured in the EvidenceBundle.
- Destructive command refusal should include exact matched pattern and safe
alternative guidance.
- Long commands should recommend `process_start` when timeout is likely.

Acceptance tests:

- `python -m pytest tests/test_x.py -q` is classified as verification,
- `npx tsc --noEmit` is classified as verification/read-only,
- `rm -rf` is refused with destructive pattern,
- terminal output truncation produces a `ResultStore` ref.

### P0.6 Add `CodingMode` to `AgentKernel`

Add a coding/operator preset selected by route, prompt classifier, or explicit
operator mode.

Mode fields:

```yaml
agent:
  operator:
    mode: read_only|coding|review|deploy
    max_iterations: 8
    require_summary_after_query: true
    require_verification_after_write: true
    allowed_actions:
      - operator.read_file
      - operator.list_dir
      - operator.search_files
      - operator.edit_file
      - operator.multi_edit
      - operator.patch_file
      - operator.terminal
```

Loop policy:

1. Discover: list/search before reading.
2. Read: read exact files/ranges.
3. Edit: prefer exact `edit_file` or `multi_edit`.
4. Verify: run the narrowest test/build/typecheck relevant to changed files.
5. Summarize: produce changed files, checks, and remaining risk.

The existing safety net that forces a reply after read-only tools should remain,
but coding mode should also force one more iteration after writes when no
verification has been run.

Acceptance tests:

- a mock coding turn that only searches forces a final natural-language answer,
- a mock coding turn that edits but does not verify gets a replan prompt,
- max iteration exhaustion returns a useful fallback with evidence and gaps.

### P0.7 Build `EvidenceBundle`

Create `nerya/agent/evidence.py`.

EvidenceBundle fields:

```json
{
  "turn_id": "turn_x",
  "session_id": "sess_x",
  "task_type": "coding",
  "files_read": [
    {"path": "...", "complete": true, "start_line": 1, "end_line": 200}
  ],
  "files_changed": [
    {"path": "...", "operation": "edit_file", "patch_ref": "tr_..."}
  ],
  "commands_run": [
    {"cmd": "python -m pytest tests/test_x.py -q", "exit_code": 0, "verification": true}
  ],
  "tool_errors": [
    {"tool": "operator.edit_file", "error_kind": "file_changed"}
  ],
  "approvals": [],
  "result_refs": [],
  "unverified_gaps": []
}
```

Storage:

- Write JSON under `state/evidence/<turn_id>.json`.
- Append a compact row to `journals/evidence.jsonl`.
- Attach evidence summary to the final `AgentTurnResult`.
- Expose via API for dashboard and session replay.

Acceptance tests:

- evidence includes every file read and changed in a coding turn,
- evidence includes terminal commands and exit codes,
- evidence records unverified gaps when tests are skipped or fail,
- session search can find evidence entries by path or command.

### P0.8 Dashboard/API evidence surface

Add API endpoints:

- `GET /agent/turns/{turn_id}/evidence`,
- `GET /agent/sessions/{session_id}/evidence`,
- `GET /workspace/tool-results/{ref_id}`.

Dashboard additions:

- Coding turn timeline: read/search/edit/test/summary.
- File diff viewer for `edit_file`, `multi_edit`, and `patch_file`.
- Command output viewer with truncation refs.
- Warnings for stale edit refusal, destructive command refusal, and unverified
gaps.

Acceptance tests:

- API returns evidence for a completed coding turn,
- dashboard renders changed files and commands from mocked evidence,
- result ref endpoint never exposes files outside `state/tool_results`.

## P1 Implementation Plan

### P1.1 Repo index and symbol search

Add `nerya/workspace/indexer.py`:

- cache file tree by mtime,
- ignore common large folders: `.git`, `node_modules`, `.venv`, `.next`,
`dist`, `build`, caches,
- support `glob`, `literal search`, and optional symbol extraction,
- use `rg` when available, fallback to Python search.

New actions:

- `operator.find_files`,
- `operator.grep`,
- `operator.symbols`,
- `operator.repo_overview`.

Goal:

- Reduce blind file reads.
- Make the agent start coding tasks with a cheap map of relevant files.

### P1.2 Context file loader

Add `nerya/agent/context_files.py`.

Load, in order:

1. workspace root `AGENTS.md`, `README.md`, `CONTRIBUTING.md`,
2. nearest nested `AGENTS.md` for every file being edited,
3. package/tool config summaries: `pyproject.toml`, `package.json`,
  `tsconfig.json`, `pytest.ini`, etc.,
4. Nerya project memory / workspace notes when enabled.

Rules:

- Never inject huge files fully.
- Treat docs as instructions only within their path scope.
- Record context files in EvidenceBundle.

### P1.3 Diagnostics and LSP-like checks

Start lightweight before adding full LSP:

- Python: `python -m pytest <narrow>` and optionally `python -m py_compile`.
- TypeScript: `npx tsc --noEmit`.
- Frontend: package-specific `npm run build` or `npm run lint` only when
configured.
- Markdown/docs: no formatter unless repo already defines one.

Later add:

- symbol outline,
- go-to definition,
- references,
- diagnostics cache.

### P1.4 Subagent write scopes

Extend subagent specs with:

```json
{
  "name": "coding_agent",
  "task": "fix parser",
  "read_scope": ["nerya/agent/**", "tests/test_parser.py"],
  "write_scope": ["nerya/agent/parser.py", "tests/test_parser.py"],
  "verification": ["python -m pytest tests/test_parser.py -q"]
}
```

Dispatcher rules:

- Parallel subagents may share read scopes.
- Parallel subagents must not overlap write scopes.
- Parent agent owns final integration.
- `code_critic` should default to no write scope.

### P1.5 Checkpoints and resume

Add `nerya/agent/checkpoints.py`.

Checkpoint after:

- plan created,
- discovery complete,
- edits proposed,
- edits applied,
- verification complete,
- interruption/cancellation.

Checkpoint data:

- task summary,
- current plan,
- files read with hashes,
- files changed,
- pending verification,
- last safe resume point.

## P2 Implementation Plan

### P2.1 Worktree or snapshot isolation

For larger coding tasks, support optional isolated workspaces:

- Git worktree when repo is Git-backed.
- Copy-on-write snapshot for non-Git workspaces.
- Apply changes back through a reviewable patch proposal.

This should remain optional because Nerya's normal workspace/proposal model is
safer and simpler for small edits.

### P2.2 Browser-assisted frontend debugging

Add browser tools only after the file/terminal loop is stable:

- `browser_snapshot`,
- `browser_screenshot`,
- `browser_click`,
- `browser_type`,
- `browser_console_logs`.

Use them only for frontend/dashboard tasks. They should write screenshots and
logs to `state/artifacts/` and link them in EvidenceBundle.

### P2.3 Coding eval runner

Create deterministic scenarios:

- read-only repo Q&A,
- exact one-file edit,
- stale edit refusal,
- test failure diagnosis,
- frontend typecheck fix,
- multi-subagent inspect/review workflow.

This becomes a release gate for coding-agent quality.

## Safety Model

### Path safety

All filesystem tools must continue resolving through the active workspace root.
Absolute paths are allowed only when they resolve inside the root. Symlinks must
be resolved before safety checks.

### Secret safety

- Never include raw secrets in `EvidenceBundle`.
- Redact environment variables in terminal output.
- Refuse reads from vault paths unless the calling skill is explicitly
credential-aware and returns redacted summaries.

### Mutating operations

Mutating file operations should remain behind the existing Nerya approval model
unless an operator explicitly enables a coding mode that allows local workspace
patches. Even in that mode:

- stale edit checks are mandatory,
- write scopes must be logged,
- destructive terminal commands are refused or require explicit approval,
- all changes are evidence-backed.

### Trading isolation

Coding lanes must not inherit live trading privileges. The current subagent
skill denylist should remain, and coding actions should not be able to invoke
wallet/trading surfaces directly.

## Suggested File-Level Changes

### Runtime modules

- Add `nerya/agent/file_state.py`.
- Add `nerya/agent/evidence.py`.
- Add `nerya/agent/context_files.py`.
- Add `nerya/agent/checkpoints.py` in P1.
- Extend `nerya/agent/kernel.py` for coding mode loop policy and evidence finalization.
- Extend `nerya/harness/tool_runner.py` to carry result kind, path scope, and
mutation/query metadata.
- Extend `nerya/harness/result_store.py` with typed refs for file read, patch,
terminal output, screenshot, and diagnostics.

### Operator skill

- Update `nerya/skills/builtin/operator_skill/SKILL.md` with new actions and
narrower guidance.
- Update `nerya/skills/builtin/operator_skill/scripts/handlers.py`:
  - improved `read_file`,
  - new `edit_file`,
  - new `multi_edit`,
  - stricter `patch_file`,
  - terminal classification,
  - file state integration.

### API and dashboard

- Extend `nerya/api/routes_agent.py` for evidence endpoints.
- Add tool-result readback route with ref safety checks.
- Add dashboard panels for evidence timeline, diffs, command outputs, and stale
edit warnings.

### Tests

Add or extend:

- `tests/test_operator_skill.py`,
- `tests/test_coding_file_state.py`,
- `tests/test_operator_edit_file.py`,
- `tests/test_coding_evidence.py`,
- `tests/test_agent_coding_loop.py`,
- `tests/test_subagent_write_scopes.py`,
- dashboard tests for evidence rendering.

## Milestone Plan

### Milestone 1 — Safe file read/write loop

Deliverables:

- `FileStateCache`,
- upgraded `read_file`,
- `edit_file`,
- stale edit refusal,
- tests for read/edit freshness.

Exit criteria:

- Nerya can read a file, edit it safely, and reject stale writes.

### Milestone 2 — Coding verification loop

Deliverables:

- terminal purpose classification,
- EvidenceBundle,
- kernel coding mode replan after writes,
- narrow verification captured in evidence.

Exit criteria:

- A coding turn can edit a file, run a narrow test, and summarize changed files
and verification output.

### Milestone 3 — Repo discovery and context files

Deliverables:

- repo index/search improvements,
- context file loader,
- AGENTS/README/config summaries,
- evidence records context files used.

Exit criteria:

- Nerya can answer repo-structure questions and make edits while respecting
applicable local instructions.

### Milestone 4 — Subagent coding workflow

Deliverables:

- read/write scopes,
- non-overlap enforcement,
- parent integration protocol,
- code critic read-only mode.

Exit criteria:

- Parent can assign one subagent to inspect tests and another to inspect
implementation without overlapping writes, then integrate safely.

### Milestone 5 — Operator UI and evals

Deliverables:

- evidence dashboard,
- diff/output viewer,
- coding eval scenarios,
- release checklist updates.

Exit criteria:

- Operators can review what Nerya did without reading raw JSONL logs.

## Concrete Acceptance Scenarios

### Scenario A: Read-only code question

Prompt: "Where is session search implemented?"

Expected:

- uses `search_files` and `read_file`,
- no mutating actions,
- final answer cites file paths and evidence shows files read,
- no unverified gaps except no runtime test needed.

### Scenario B: Safe one-file edit

Prompt: "Fix typo in docs/runbook.md."

Expected:

- reads target file,
- calls `edit_file` with exact old/new string,
- verifies no stale write,
- records changed file and patch,
- summarizes change.

### Scenario C: Stale edit refusal

Prompt: model reads file, external process modifies file, model attempts edit.

Expected:

- edit fails with `file_changed`,
- no partial write occurs,
- final answer asks to re-read or retries by reading current file first,
- evidence records refusal.

### Scenario D: Code change with test

Prompt: "Fix failing parser test."

Expected:

- searches relevant test and parser,
- reads both,
- edits exact source/test files,
- runs narrow `pytest`,
- final answer includes test result and changed files,
- evidence includes command output and exit code.

### Scenario E: Parallel read-only subagents

Prompt: "Have one agent inspect tests and another inspect implementation."

Expected:

- two read-only scopes can run in parallel,
- no overlapping writes,
- parent integrates findings,
- code critic cannot mutate files.

## Open Decisions

1. Should local coding mode allow immediate patch application by default, or
  should all file writes remain proposal-only unless a dev-mode flag is set?
2. Should `FileStateCache` persist full small-file content or only hashes and
  metadata?
3. Should dashboard display full file diffs from `ResultStore`, or should diffs
  be separate artifact records under `state/artifacts/patches/`?
4. Should repo indexing be always-on, or lazily triggered by coding tasks?
5. Should terminal verification commands be inferred, operator-selected, or both?

## Recommended Defaults

- Immediate local edits are acceptable in dev/coding mode, but every write must
be evidence-backed and stale-checked.
- Production/trading workspaces should keep proposal-first mutation unless the
operator explicitly enables dev mode.
- Persist only hashes and metadata in `FileStateCache`; store full oversized
contents through `ResultStore`.
- Keep repo indexing lazy until performance data says otherwise.
- Treat verification inference as best-effort; the model/operator should still
see and approve the chosen command when risk is non-trivial.

## Final Target State

Nerya should be able to operate like this:

1. understand repo instructions,
2. discover relevant files cheaply,
3. read exact ranges without context blowup,
4. edit only after proving freshness,
5. serialize writes and parallelize safe reads,
6. run narrow verification,
7. preserve a replayable transcript and evidence bundle,
8. show the operator a clean diff/test summary,
9. keep trading and wallet privileges isolated from coding lanes.

That target keeps Nerya's native skill-first identity while giving it the
coding-agent reliability expected from mature operator tools.

## Additional Claude Code Runtime Lessons: Compaction, Interrupts, and Error Recovery

The first half of this plan focuses on coding tools and file reads. Nerya should
also learn from Claude Code's runtime reliability mechanisms. Mature coding
agents fail less because they never fail, but because they treat compression,
interrupts, tool mismatch, provider failures, and partial outputs as normal
states in the loop.

### Context compression design

Claude Code uses several layers of context control rather than one global
"summarize everything" step:

1. **Token warning and blocking thresholds**
  - It computes an effective context window for the active model, reserves
   output tokens, and derives warning, error, auto-compact, and blocking
   thresholds.
  - This lets the loop warn, compact, or block before the provider rejects the
  request.
2. **Auto-compact before API call**
  - The query loop checks token usage before each model call.
  - If the context is above the auto-compact threshold, it summarizes old
  conversation state and replaces the live prompt with compacted messages.
  - It tracks consecutive compact failures and has a circuit breaker so a bad
  compaction state does not burn unlimited requests.
3. **Microcompact for tool results**
  - Claude Code separately compacts old tool results from high-volume tools
   such as file read, grep, glob, shell, web search, file edit, and file write.
  - This matters because coding loops often generate huge tool outputs while
  the actual useful content is only the latest result or a summary.
4. **Prompt-too-long reactive recovery**
  - Provider-side prompt-too-long errors are withheld temporarily.
  - The loop first tries recovery paths such as collapse drain, reactive
  compaction, or lossy old-context truncation.
  - Only if recovery fails does it surface the API error to the user.
5. **Compaction-aware attachments**
  - Images/documents are stripped or replaced with markers before compaction
   when the summarizer does not need raw media.
  - Skill listings and other reinjectable attachments are excluded from the
  summary so stale tool descriptions do not pollute future context.
  - After compaction, caches and state that are invalidated by the compacted
  transcript are reset, but skill content that should survive is preserved.
6. **Summary must preserve work continuity**
  - Compaction prompts explicitly ask the model to preserve file reads, code
   changes, test output, errors, and current next steps.
  - Autonomous mode summaries instruct the agent to continue work, not greet
  the user or restart planning from scratch.

#### Nerya recommendation: layered compaction

Nerya already has transcript compaction tests and a `ResultStore`, but coding
mode needs a more explicit compression policy.

Add `nerya/agent/context_budget.py`:

- `estimate_messages_tokens(messages, model)`
- `get_context_thresholds(model)`
- `should_warn_context(...)`
- `should_microcompact_tool_results(...)`
- `should_autocompact_transcript(...)`
- `is_blocking_limit(...)`

Add `nerya/agent/coding_compact.py`:

- preserve recent tail exactly,
- preserve all unresolved tool_use/tool_result pairs,
- preserve approvals, errors, cancellations, and current plan,
- replace old large tool results with `ResultStore` refs,
- summarize old coding work with changed files, tests, commands, failures, and
next steps,
- keep context-file instructions as scoped references rather than flattening
all docs into the summary.

Suggested compact summary schema:

```json
{
  "summary_type": "coding_compact",
  "source_turn_ids": ["turn_a", "turn_b"],
  "task_goal": "...",
  "current_plan": ["..."],
  "files_read": [{"path": "...", "ref_id": "tr_..."}],
  "files_changed": [{"path": "...", "patch_ref": "tr_..."}],
  "commands_run": [{"cmd": "...", "exit_code": 0}],
  "errors": [{"kind": "tool_error", "message": "..."}],
  "open_questions": ["..."],
  "next_actions": ["..."]
}
```

Acceptance tests:

- compaction never leaves orphaned `tool_result` without a matching `tool_use`,
- compaction preserves the last N messages exactly,
- compaction preserves approval and interruption events,
- old large file reads are replaced by refs,
- compacted coding turn can continue editing/testing without re-discovering
everything from scratch,
- repeated compaction failures stop after a small configured limit and return a
clear blocked reason.

### Interrupt and cancellation design

Claude Code treats interruption as a first-class transcript event, not merely as
an exception:

1. **AbortController per turn**
  - Each query has an abort controller.
  - Child agents/tools receive child abort controllers that abort when the
  parent aborts but do not cancel the parent if they fail locally.
  - This prevents leaked listeners and ensures nested work stops when the
  top-level turn is interrupted.
2. **Synthetic interruption messages**
  - When a request or tool use is interrupted, Claude Code injects a synthetic
   user message such as `[Request interrupted by user]` or `[Request  interrupted by user for tool use]`.
  - This makes the interruption visible to the next model call and to session
  replay.
3. **Submit-interrupt distinction**
  - If the user interrupts because they submitted a replacement prompt, the
   queued new user message is enough context, so the loop can skip an extra
   interruption marker.
  - This avoids confusing the model with duplicate "interrupted" context.
4. **Tool cleanup on abort**
  - The loop checks for abort after tool batches.
  - It performs best-effort cleanup for resources that may be left locked or
  hidden by a tool.
  - It returns a terminal reason such as `aborted_tools` rather than pretending
  the turn completed normally.
5. **Max-turns and cancellation interaction**
  - Even on abort, the loop checks whether max turns were reached and emits a
   structured attachment.
  - That prevents a cancellation path from bypassing normal turn-limit
  accounting.

#### Nerya recommendation: interrupt protocol for coding mode

Nerya already has `CancelToken`, but coding mode should persist richer
interruption events.

Add `nerya/agent/interrupts.py`:

- `InterruptReason`: `user_cancelled`, `user_replaced_prompt`, `timeout`,
`approval_denied`, `shutdown`, `parent_cancelled`, `tool_abort`.
- `InterruptEvent`: session id, turn id, tool call id, reason, message,
created_at, resumable boolean.
- `record_interrupt(...)` writes to `journals/interrupts.jsonl` and the session
transcript.

Extend `AgentKernel`:

- when a `CancelToken` is tripped during a tool, append a synthetic transcript
event,
- if a replacement prompt exists, link the cancelled turn to the replacement
message instead of adding duplicate interruption text,
- return `stopped_reason="cancelled:<reason>"`,
- finalize EvidenceBundle with partial files read, commands run, and open work,
- mark pending background processes as `needs_cleanup` unless explicitly kept.

Extend long-running tools:

- terminal/process tools should check cancellation before start and after
completion,
- background processes should be listed in evidence on cancellation,
- file writes should be atomic and cancellation should not leave partial writes.

Acceptance tests:

- cancelling during a read-only tool records an interruption event,
- cancelling during terminal execution records command output available so far,
- replacement prompt links old turn -> new turn,
- subagent cancellation propagates from parent to child but child failure does
not cancel the parent,
- evidence for an interrupted turn says exactly what completed and what did not.

### Error handling and recovery design

Claude Code has several important error-handling patterns:

1. **Tool errors become tool_result blocks**
  - Unknown tool, validation failure, permission denial, and runtime exceptions
   are converted into structured `tool_result` messages with `is_error=true`.
  - The model can then recover by choosing another tool or fixing its input.
2. **Unknown/deferred tools get actionable guidance**
  - If a tool is unavailable because it was not loaded, the model is told to
   load/select the tool first and retry.
  - This is better than a generic "tool not found" failure.
3. **Pre-tool hooks can modify, deny, or stop**
  - Hooks may update inputs, add context, produce progress, deny permission, or
   prevent continuation.
  - Slow hook phases are measured and logged.
4. **Provider errors are classified**
  - Prompt-too-long, request-too-large, image too large, invalid PDF, rate
   limit, invalid model, duplicate tool ids, and unexpected tool_result errors
   all get specific user-facing messages.
  - Some messages include recovery actions such as compact, rewind, model
  switch, or start a new session.
5. **Recoverable streaming errors are withheld**
  - Max-output-token errors and prompt-too-long errors are not immediately
   streamed to the user.
  - The loop attempts escalation or continuation first.
6. **Max output token recovery**
  - If output hits a token cap, the loop can retry with a higher output cap or
   inject a meta message asking the model to resume directly and break work
   into smaller pieces.
7. **Tool-use pairing repair**
  - Before sending messages back to the API, Claude Code defensively repairs or
   strips orphaned tool_use/tool_result pairs and duplicate tool_use IDs.
  - In strict modes it can throw instead of repairing, which is useful for evals
  or training data.
8. **Stop hooks avoid API-error death spirals**
  - If the last message is an API error, Claude Code skips normal stop hooks.
  - This prevents loops like error -> hook blocking -> retry -> same error.

#### Nerya recommendation: structured error taxonomy

Create `nerya/agent/error_recovery.py` with a stable taxonomy:

```text
validation_error        model produced invalid tool input
permission_denied       policy or operator denied the action
unknown_tool            tool/action not registered or unavailable
stale_file              file changed since read
tool_runtime_error      tool raised unexpectedly
retryable_tool_error    network/rate/transient tool failure
provider_rate_limit     LLM provider rate limit or capacity
provider_prompt_too_long context too large
provider_output_cutoff  max output tokens reached
provider_media_error    image/PDF/document too large or invalid
transcript_corrupt      tool_use/tool_result mismatch
cancelled               user/tool/parent cancellation
budget_exceeded         turn or daily budget exceeded
approval_required       action stopped waiting for approval
```

Every `ToolCallRecord` and turn step should include:

```json
{
  "ok": false,
  "error_kind": "stale_file",
  "recoverable": true,
  "retry_after_s": null,
  "suggested_next_action": "re_read_file",
  "user_visible_message": "File changed after it was read; read it again before editing."
}
```

Recommended recovery policies:

- `validation_error`: ask model to repair input in next loop.
- `unknown_tool`: refresh action catalog or expose tool-loading guidance.
- `stale_file`: force re-read before another edit.
- `provider_prompt_too_long`: microcompact tool results, then transcript compact,
then lossy old-context truncation only as last resort.
- `provider_output_cutoff`: ask model to continue directly; preserve partial
assistant output as context.
- `transcript_corrupt`: repair when safe; otherwise stop with `needs_rewind`.
- `permission_denied`: continue with read-only alternatives if possible.
- `budget_exceeded`: stop and summarize partial evidence.

Acceptance tests:

- invalid tool input becomes a recoverable observation, not a kernel crash,
- provider prompt-too-long triggers compaction before surfacing failure,
- max-output cutoff triggers one continuation attempt,
- orphaned tool results are repaired or rejected before model call,
- API/provider errors skip normal stop hooks to avoid retry loops,
- every failed turn has a clear `stopped_reason` and EvidenceBundle gap.

## Additional P0/P1 Backlog Items

The following items should be added to the earlier roadmap.

### P0.9 Context budget and compression

Deliverables:

- `context_budget.py`,
- coding microcompact for old tool results,
- compact summary schema for coding turns,
- circuit breaker for repeated compaction failures,
- compaction evidence entries.

Exit criteria:

- A large coding session with repeated file reads and terminal output can
continue after compaction without orphaned tool messages or lost next steps.

### P0.10 Interrupt event persistence

Deliverables:

- `interrupts.py`,
- interruption journal,
- replacement-prompt linkage,
- partial EvidenceBundle finalization,
- background process cleanup markers.

Exit criteria:

- User can interrupt a long coding turn, send a new prompt, and session replay
clearly shows what was interrupted and what replaced it.

### P0.11 Error recovery taxonomy

Deliverables:

- `error_recovery.py`,
- stable error kinds on `ToolCallRecord`, `TurnStep`, and EvidenceBundle,
- recovery suggestions returned to the model,
- prompt-too-long and output-cutoff recovery policy.

Exit criteria:

- Common tool/provider/transcript errors produce actionable recovery behavior
rather than generic failure strings.

### P1.6 Transcript pairing repair

Deliverables:

- pre-model-call validator for assistant tool calls and user tool results,
- safe repair mode for production,
- strict mode for tests/evals,
- journal entries whenever repair activates.

Exit criteria:

- Nerya cannot send malformed tool_use/tool_result history to an LLM provider
without first repairing or stopping with a clear recovery instruction.

## Additional Claude Code Lessons: Skill, MCP, and Context Loading

This section addresses one subtle but important point: Claude Code does not
"know" skills because every skill body is stuffed into the main prompt. It
knows a bounded **skill index** first, then loads the full skill only when the
model deliberately invokes it.

### Claude Code skill loading model

Observed source files:

- `src/skills/loadSkillsDir.ts`
- `src/skills/bundledSkills.ts`
- `src/skills/mcpSkillBuilders.ts`
- `src/commands.ts`
- `src/tools/SkillTool/SkillTool.ts`
- `src/services/mcp/client.ts`

Claude Code has multiple skill sources:

1. **User/project/policy skill directories**
  - `getSkillsPath(...)` maps user, project, policy-managed, and plugin
   sources to skill roots.
  - The canonical `/skills/` format is `skill-name/SKILL.md`.
  - A loose single markdown file inside `/skills/` is ignored; this keeps the
  namespace predictable.
2. **Legacy command directories**
  - Legacy `/commands/` can still load markdown commands.
  - If a directory contains `SKILL.md`, Claude Code treats that file as the
  skill and names it from the parent directory.
3. **Bundled skills**
  - Built-in skills are registered programmatically with name, description,
   `whenToUse`, allowed tools, model/effort overrides, optional fork context,
   and optional reference files.
  - Reference files are extracted lazily on first invocation, then the skill
  prompt is prefixed with a base directory so the model can read extra files
  on demand.
4. **Plugin skills**
  - Enabled plugins contribute skills into the same command/skill model.
5. **MCP skills**
  - If MCP skill support is enabled and a server supports resources, Claude
   Code discovers skills from MCP-provided skill resources and turns them into
   prompt commands with `loadedFrom === "mcp"`.

Key design point: skill frontmatter is the **index**, not the execution body.
Claude Code estimates index cost from `name`, `description`, and `when_to_use`,
while the full markdown body is loaded by `SkillTool` only when invoked.

Important frontmatter fields Claude Code understands include:

- `description` — one-line routing hint,
- `when_to_use` — richer model-side routing hint,
- `allowed-tools` — tool allowlist for shell/tool use during the skill,
- `paths` — path-scoped activation hints,
- `user-invocable` — whether it appears as a slash/user command,
- `model` / `effort` — optional execution lane override,
- `context: fork` and `agent` — run the skill in a forked agent context,
- `hooks` and `shell` — optional controlled execution hooks.

### Why Claude Code knows which skills exist

Claude Code builds a command list at startup and refresh points:

1. `getSkills(cwd)` loads skill directory commands, plugin skills, bundled
  skills, and built-in plugin skills.
2. `loadAllCommands(cwd)` merges bundled skills, plugin skills, disk skills,
  workflow commands, plugin commands, and built-in commands.
3. `getSkillToolCommands(cwd)` filters the merged command list to prompt-based,
  model-invocable skills.
4. `SkillTool` receives that skill list plus MCP skills from `AppState.mcp` and
  exposes a compact skill index to the model.
5. When the model invokes a skill, `getPromptForCommand(...)` returns the full
  `SKILL.md` body, with a base-directory prefix when local reference files are
   available.

So the context flow is two-stage:

```text
startup/refresh -> compact skill index in model context
model chooses SkillTool(skill_name, args)
SkillTool loads full SKILL.md body
optional forked agent executes skill playbook
result is summarized back to parent turn
```

This is the main reason Claude Code can support many skills without turning the
main prompt into a giant concatenated manual.

### Claude Code MCP loading model

Claude Code treats MCP as a dynamic capability plane, not just a static config:

1. MCP server configs are loaded by scope and disabled servers are skipped.
2. Local transports (`stdio`/SDK) and remote transports (`http`/`sse`) have
  separate concurrency handling.
3. For each connected server, Claude Code fetches:
  - `tools/list` for tool schemas,
  - prompt commands,
  - resources,
  - skill resources when MCP skill support is enabled.
4. MCP tools are converted to normal tool definitions and normally receive a
  fully qualified `mcp__server__tool` name to avoid collisions.
5. If a server supports resources, Claude Code adds list/read MCP resource tools
  once so the model can inspect MCP resources on demand.
6. MCP fetch failures degrade gracefully: the server becomes `failed` or
  `needs-auth`, the tool surface is emptied, and the session continues.
7. MCP skills are explicitly marked `loadedFrom === "mcp"`; `SkillTool` filters
  for that marker and prevents remote MCP skill bodies from executing inline
   shell snippets.

Nerya should copy the separation, not the exact implementation: MCP tools,
resources, prompts, and skills should be different capability categories with
separate trust rules, cache invalidation, and UI visibility.

### Current Nerya comparison

Nerya already has several good pieces:

- `nerya/skills/manifest.py` makes `SKILL.md` frontmatter the canonical typed
manifest path.
- `nerya/agent/context_builder.py` renders skill descriptions and a short
`## When to use` excerpt instead of dumping whole skill bodies.
- `nerya/agent/kernel.py::build_action_catalog` groups actions by skill and
carries each skill's description/instructions for prompt rendering.
- `nerya/skills/procedural.py` can load procedural `SKILL.md` playbooks.
- `nerya/mcp/dynamic_tools.py` exposes manifest-driven MCP tool surfaces.
- `nerya/agent/transcript_compact.py` already validates pairing and keeps recent
tail messages during transcript compaction.

But Nerya currently blurs three separate surfaces:

1. **Typed skill action manifest** — deterministic actions with schemas,
  permissions, gates, journals, and handlers.
2. **Prompt skill playbook** — instructions the model should read and follow
  only when the skill is selected.
3. **Capability/tool transport** — MCP, ACP, dashboard, CLI, or direct internal
  calls that expose actions to external clients.

Claude Code keeps those surfaces more separate. That separation is why it can
show the model "available skills" without loading every skill body, and why MCP
skills do not accidentally become local shell-capable skills.

### Corrected interpretation for `workspace_skill`

The previously added `workspace_skill` should not be treated as a Claude
Code-style global skill loader or generic coding/file tool. That design would be
wrong for three reasons:

1. It overloads one skill with too many routing meanings.
2. It makes workspace state lookup look like skill discovery.
3. It risks hiding the difference between trading-domain state and source-code
  file operations.

Correct role:

- `workspace_skill` is only a **read-only trading workspace state reader**.
- It may answer questions about strategies, scripts, accounts, triggers,
schedules, portfolio snapshots, and trade-intent defaults.
- It must not be used to read arbitrary source files; coding/file reads belong
to the `operator` file tools.
- It must not be used to discover installed skills or MCP servers; that should
be a dedicated `SkillIndex` / MCP registry surface.
- It must not become a mutation router; domain skills still own mutations.

The skill file should say this explicitly so the model cannot infer that
`workspace` is Nerya's equivalent of Claude Code's skill loader.

### Nerya target design: separate SkillIndex, ToolCatalog, and ContextBudget

Add three explicit runtime services.

#### `SkillIndex`

Purpose: answer "what skills exist and when should I use them?" without dumping
full `SKILL.md` bodies.

Suggested file:

- `nerya/agent/skill_index.py`

Responsibilities:

- scan built-in skills, installed workspace skills, procedural skills, plugin
skills, and future MCP skills,
- parse `id`, `title`, `description`, `when_to_use`, `tags`, `paths`,
`user_invocable`, `context`, `model`, `effort`, `source`, and `trust_level`,
- compute a stable `skill_digest` from the `SKILL.md` body,
- expose `list_skill_index()` with frontmatter-only rows,
- expose `load_skill_body(skill_id)` for deliberate on-demand loading,
- track `invoked_skills` per turn/session for compaction preservation,
- invalidate caches when skill files or MCP resource versions change.

Recommended index row:

```json
{
  "id": "strategy_validation",
  "title": "Strategy Validation",
  "description": "Backtest and validate a strategy before live use.",
  "when_to_use": "Use before deploying or changing strategy logic.",
  "source": "builtin",
  "trust_level": "local_builtin",
  "paths": ["strategies/**"],
  "actions_count": 4,
  "body_tokens_estimate": 1800,
  "digest": "sha256:..."
}
```

#### `ToolCatalog`

Purpose: answer "what deterministic actions/tools can be called right now?"
without mixing in long skill playbooks.

Suggested file:

- `nerya/agent/tool_catalog.py`

Responsibilities:

- build current typed action catalog from `SkillRegistry`,
- merge dynamic MCP tools with fully qualified names,
- expose availability verdicts (`available`, `disabled`, `needs_auth`,
`missing_env`, `blocked_by_preset`),
- preserve action gates, permission gates, mutability, and payload schemas,
- keep tool descriptions short and machine-checkable,
- return error-kind metadata for unknown/unavailable tools.

#### `ContextBudget`

Purpose: decide what gets included in each model call.

Suggested files:

- `nerya/agent/context_budget.py`
- `nerya/agent/coding_compact.py`

Responsibilities:

- include only the skill index rows relevant to the current turn,
- include action schemas only for selected/routable skills,
- keep full `SKILL.md` bodies out of the main context until invocation,
- preserve invoked skill names/digests across compaction,
- microcompact old tool results before transcript compaction,
- emit a context-budget report into EvidenceBundle.

### MCP parity plan for Nerya

Add `McpCapabilityRegistry` instead of treating MCP as only an external tool
adapter.

Suggested files:

- `nerya/mcp/capability_registry.py`
- `nerya/mcp/skill_resources.py`
- `nerya/mcp/resource_tools.py`

Design:

1. Load scoped MCP server configs from workspace/user config.
2. Connect with concurrency separated by transport type.
3. Fetch and classify:
  - `tools/list` -> deterministic tools,
  - prompts -> prompt commands,
  - resources -> inspectable data,
  - `skill://...` resources -> remote skill playbooks.
4. Give tools fully qualified names such as `mcp__server__tool`.
5. Mark remote skill bodies as `trust_level: remote_mcp`.
6. For remote MCP skills:
  - never execute inline shell blocks,
  - do not grant local file tools by default,
  - require explicit allowlist before mutating local workspace,
  - record server name and resource URI in EvidenceBundle.
7. Add `mcp.reload` and cache invalidation when servers send list/resource
  change notifications.
8. Show connection states in dashboard: `connected`, `disabled`, `failed`,
  `needs_auth`, `stale_cache`.

### P0/P1 additions for skill and MCP support

#### P0.12 Build `SkillIndex`

Deliverables:

- `skill_index.py`,
- frontmatter-only skill index,
- on-demand `load_skill_body`,
- invoked-skill tracking,
- cache invalidation tests.

Exit criteria:

- The main prompt can list 50+ skills using only compact descriptions and
`when_to_use`, while full `SKILL.md` bodies are loaded only after explicit
skill selection.

#### P0.13 Split `workspace_skill` from skill discovery

Deliverables:

- update `workspace_skill/SKILL.md` wording to state it is only a read-only
trading workspace state reader,
- add a separate `skill_index` read-only action or service for installed skill
discovery,
- ensure coding/file questions route to `operator.read_file` and not
`workspace.read_script` unless the object is a Nerya workspace script.

Exit criteria:

- Asking "what skills are available?" uses `SkillIndex`, while asking "what
strategies/accounts/routes exist?" uses `workspace_skill`.

#### P0.14 Add MCP capability ingestion

Deliverables:

- MCP tool/resource/skill classification,
- fully qualified MCP tool names,
- remote skill trust policy,
- resource list/read tools,
- dashboard MCP state surface.

Exit criteria:

- Connecting an MCP server updates Nerya's runtime tool catalog without restart,
and failed/unauthenticated MCP servers degrade without breaking the agent loop.

#### P1.7 Path-scoped skill activation

Deliverables:

- support `paths` metadata in Nerya `SKILL.md`,
- bias routing when user mentions matching files/directories,
- keep unmatched path-scoped skills out of the default prompt unless requested.

Exit criteria:

- A repository with many domain skills only surfaces the few skills relevant to
the touched files and user request.

#### P1.8 Forked skill execution

Deliverables:

- `context: fork` support for prompt skills,
- child-agent result summarization,
- parent-child evidence linkage,
- compaction preservation of parent/child skill decisions.

Exit criteria:

- A long skill playbook can run in a child context and return a compact result
without polluting the parent agent's main context.

### Acceptance tests for skill/MCP/context loading

Add tests that cover:

- `SkillIndex` lists skill frontmatter without full bodies,
- `load_skill_body` returns the exact `SKILL.md` body and base directory,
- a `paths`-scoped skill appears only when relevant,
- full skill bodies are preserved by digest across compaction,
- MCP tool names are fully qualified and collision-safe,
- MCP resources add list/read tools once,
- MCP skill bodies cannot execute local shell snippets by default,
- failed MCP connection returns `needs_auth`/`failed` without breaking turn
planning,
- `workspace_skill` is not selected for source-code file reads,
- coding questions choose `operator.read_file` / search tools, then load only
the relevant skill body if a skill is actually needed.

## Product Pivot: From Skill-Centric Trading Assistant to Workspace-Native Coding Agent

The most important product-level conclusion is that Nerya should not copy only
Claude Code's individual tools. It should copy Claude Code's **capability
posture**: a strong general agent can inspect the workspace, edit real files,
run checks, learn conventions, and create durable artifacts without a bespoke
business tool for every noun.

The current Nerya design is safe and auditable, but it is too rigid for a
powerful coding product:

- creating a strategy is routed through `strategy.create` / script proposal
workflows instead of letting the agent author code and config directly,
- reading workspace state requires domain-specific read actions, even when the
same information exists as files the model could inspect,
- many capabilities are represented as one skill per domain noun, which makes
the model ask "which product tool should I use?" instead of "what files and
commands solve the task?",
- the agent loop is action-catalog driven; if an action is missing, the model's
intelligence cannot compensate,
- domain skills encode product workflows, but they also become bottlenecks for
exploratory coding, refactoring, debugging, and strategy iteration.

The target should be a two-layer product:

```text
Layer 1: Workspace-native agent substrate
  - read/list/search/edit files
  - run terminal/checks/tests/backtests
  - inspect git diff and project docs
  - maintain plans/evidence/context
  - load skills as optional playbooks

Layer 2: Nerya domain control plane
  - trading/risk/order execution APIs
  - strategy registry and lifecycle views
  - schedule/trigger management
  - portfolio/account state
  - governance, approvals, live-trading safety
```

Layer 1 must be strong enough that Nerya can build and modify Layer 2 artifacts
without needing a special tool for each operation. Layer 2 remains valuable for
runtime safety and operator UX, but it should not be the only way the agent can
create or modify strategy logic.

### Core principle: files are the primary product API

Claude Code feels flexible because repository files are the source of truth.
The model can discover conventions, create files, edit code, run tests, observe
errors, and iterate. Nerya should adopt the same rule:

> If a capability can be represented safely as workspace files plus commands,
> prefer file-first agent work over a bespoke domain action.

Domain actions should exist only when they add something files cannot provide:

- irreversible external side effects,
- live trading/order placement,
- secret access,
- account custody,
- structured dashboard actions that must be atomic,
- privileged approval flows,
- compatibility APIs for UI/SDK clients.

Everything else should be editable as ordinary workspace artifacts.

### What should change conceptually

#### 1. `operator` becomes the always-on substrate, not just another skill

Today `operator` is exposed through the same skill/action catalog as trading
skills. That makes it feel optional and competes with domain tools.

Target:

- `read_file`, `list_dir`, `search_files`, `patch_file`, `write_file`, and
`terminal` are always available in coding/product-building modes.
- They are presented as primitive workspace tools, not as a domain skill the
planner may or may not select.
- Domain skill selection becomes secondary; the agent first explores files and
docs, then decides whether a domain API is needed.

Implementation options:

- Keep existing `operator_skill` handlers internally to preserve audit, gates,
budgets, and path safety.
- Add a `WorkspaceToolAdapter` that exposes those actions directly to the agent
loop under primitive names.
- Do not require route manifests or `select_skills()` to include `operator` for
normal coding turns.

#### 2. Strategy creation becomes code generation plus registration, not a one-shot tool

The current strategy path encourages a narrow generated artifact. A stronger
agent should be able to create the full strategy package:

```text
strategies/my_strategy/
  strategy.yml
  script.py
  README.md
  tests/test_my_strategy.py
  backtests/latest.json
  validation_report.md
```

Target flow:

1. Agent reads existing strategies and templates via file tools.
2. Agent creates or edits strategy files directly.
3. Agent runs deterministic validation commands.
4. Agent writes a validation report and evidence bundle.
5. Agent optionally calls a small `strategy.register` or `strategy.reload`
  domain action so the dashboard/runtime indexes the new files.

This is closer to Claude Code: code first, runtime registration second.

Keep domain actions, but narrow their role:

- `strategy.create` becomes a convenience wrapper for simple UI/API callers,
not the main agent path.
- `strategy.validate` / `strategy.reload` / `strategy.promote` remain useful
because they perform deterministic runtime checks and lifecycle transitions.
- `trading.submit_trade_intent` remains gated because it can affect money.

#### 3. Workspace state should be discoverable both as files and as APIs

`workspace_skill` should not be the only path to know what exists. It is a
read-only convenience API for runtime objects. For a coding agent, the stronger
path is:

```text
list_dir("strategies")
read_file("strategies/foo/strategy.yml")
search_files("allowed_skills")
terminal("python -m pytest ...")
```

Target:

- `workspace_skill` answers high-level operator state questions quickly.
- File tools remain the canonical path for code/config understanding.
- The agent prompt should say: "When modifying or debugging, inspect files; when
answering dashboard/runtime inventory questions, use workspace state APIs."

#### 4. Replace many narrow creation tools with scaffolds and conventions

Nerya does not need a tool for every artifact type. It needs documented
conventions and reusable templates the model can copy or adapt.

Use files like:

- `templates/strategy/python-script/`
- `templates/strategy/event-driven/`
- `templates/skill/typed-skill/`
- `templates/mcp/server/`
- `docs/conventions/strategy-package.md`
- `docs/conventions/testing.md`

Then let the agent use file tools to instantiate them.

Domain actions should be reduced to:

- `register_*`: load/index an artifact after files exist,
- `validate_*`: run deterministic checks and return structured failures,
- `promote_*`: gated lifecycle transitions,
- `execute_*`: external side effects.

This is the right boundary between agent flexibility and product safety.

### Proposed architecture: `WorkspaceNativeAgent`

Add a coding-first runtime lane separate from the current trading-agent lane.

Suggested files:

- `nerya/agent/workspace_native.py`
- `nerya/agent/workspace_tools.py`
- `nerya/agent/artifact_index.py`
- `nerya/agent/project_conventions.py`
- `nerya/agent/coding_modes.py`

#### `WorkspaceNativeAgent`

Responsibilities:

- receive freeform operator tasks,
- always expose primitive workspace tools,
- build a compact project context from docs, AGENTS-style rules, file tree, git
status, and skill index,
- run an iterative coding loop: discover -> edit -> verify -> summarize,
- call Nerya domain actions only when deterministic runtime state or external
side effects are needed,
- produce evidence bundles for every meaningful edit/check.

Loop shape:

```text
1. classify task: question | edit | create | debug | run | trading-side-effect
2. load project rules and compact file tree
3. use primitive workspace tools for discovery
4. edit files directly with stale-read protection
5. run narrow checks/backtests
6. optionally call domain register/reload/validate/promote actions
7. summarize diff, evidence, risks, next steps
```

#### `WorkspaceToolAdapter`

Expose existing operator handlers as first-class tools:

```text
workspace.list
workspace.read
workspace.search
workspace.edit
workspace.write
workspace.shell
workspace.git_status
workspace.git_diff
```

These tools are still backed by `SkillRuntime` / `ToolRunner`, but the model
sees them as primitives. That preserves security while improving routing.

Required behavior:

- `workspace.read` supports exact line ranges and returns file hash.
- `workspace.edit` requires a matching last-read hash or exact old text.
- `workspace.shell` supports timeout, cwd, env allowlist, and output storage.
- Large outputs go to `ResultStore`; the model receives summaries plus refs.
- All writes are journaled with before/after hashes.

#### `ArtifactIndex`

A file-first product needs an index of what files mean.

Responsibilities:

- scan `strategies/`, `skills/`, `scripts/`, `triggers/`, `accounts/`, `docs/`,
and templates,
- infer artifact type from path and manifest content,
- expose compact rows to the agent and dashboard,
- detect drift between files and runtime registry,
- support `register/reload` commands based on changed files.

Example row:

```json
{
  "artifact_type": "strategy",
  "id": "mean_reversion_eth",
  "path": "strategies/mean_reversion_eth/strategy.yml",
  "entrypoint": "strategies/mean_reversion_eth/script.py",
  "registered": true,
  "last_validated_at": "2026-04-27T...",
  "drift": false
}
```

#### `ProjectConventions`

Claude Code relies heavily on repository conventions. Nerya should formalize
that through lightweight docs loaded on demand:

- where strategies live,
- expected `strategy.yml` fields,
- script entrypoint contract,
- how to run tests/backtests,
- how to register/reload artifacts,
- what requires approval,
- what must never be edited directly.

The agent should read these convention docs before generating strategy code.
This is stronger and more maintainable than embedding strategy creation logic
inside a tool prompt.

### Skill model after the pivot

Skills should become **playbooks**, not the main execution substrate.

Use skills for:

- specialized procedures,
- domain checklists,
- external provider workflows,
- optional expert modes,
- reusable guidance that the model loads when relevant.

Do not use skills for:

- basic file reads,
- basic file writes,
- repository search,
- ordinary code generation,
- strategy package creation when files/templates are enough.

This maps Nerya closer to Claude Code:

```text
Skill index: compact routing hints
Skill body: optional playbook loaded on demand
Workspace tools: always-on primitives
Domain skills: privileged runtime APIs and safety gates
```

### Domain tool reduction plan

Classify every current action into one of four groups.

#### Keep as privileged domain actions

These should remain tools because they perform external or gated side effects:

- live order placement,
- order cancellation,
- wallet signing,
- secret access,
- exchange account operations,
- promotion to live/canary,
- trigger enable/disable if it affects live automation.

#### Keep as deterministic validators

These are useful because they return machine-checkable facts:

- strategy schema validation,
- backtest execution,
- risk gate checks,
- route/schedule validation,
- exchange connectivity checks,
- portfolio/account sanity checks.

#### Convert to file-first workflows

These should become templates/docs plus direct file edits:

- strategy creation,
- script creation,
- skill scaffold creation,
- route manifest drafting,
- dashboard copy/config updates,
- SDK example creation.

#### Keep as convenience wrappers only

These can remain for dashboard/API users but should not be the primary agent
path:

- one-shot strategy create,
- one-shot script propose,
- one-shot skill scaffold proposal,
- simple schedule creation from text.

### Migration roadmap

#### P0.15 Add workspace-native mode

Deliverables:

- `WorkspaceNativeAgent` lane,
- always-on primitive workspace tools,
- prompt rules that prefer file inspection/editing for coding tasks,
- existing `AgentKernel` remains available for trading/event-driven turns.

Exit criteria:

- A user can say "create a BTC breakout strategy" and the agent creates a real
strategy directory and files without calling `strategy.create` as the first
step.

#### P0.16 Strategy-as-code package convention

Deliverables:

- `docs/conventions/strategy-package.md`,
- `templates/strategy/python-script/`,
- standard package layout,
- validation command contract,
- reload/register command contract.

Exit criteria:

- Strategy generation is reproducible from files and templates, not hidden in a
domain tool implementation.

#### P0.17 Artifact index and drift detection

Deliverables:

- `artifact_index.py`,
- `nerya artifacts list` CLI/API,
- dashboard artifact inventory,
- file-vs-runtime drift report.

Exit criteria:

- If the agent writes `strategies/foo/strategy.yml`, Nerya can report whether
`foo` is registered, valid, stale, or missing runtime reload.

#### P0.18 File-first validation loop

Deliverables:

- standard validation commands for strategy packages,
- `workspace.shell` check runner integration,
- EvidenceBundle entries for command, stdout/stderr refs, changed files,
validation status,
- repair loop for failed validation.

Exit criteria:

- The agent can iterate on generated strategy code until schema/tests/backtest
checks pass, then summarize the remaining risks.

#### P1.9 Domain action diet

Deliverables:

- inventory all skill actions,
- mark each action as `primitive`, `validator`, `privileged`, `convenience`, or
`deprecated_for_agent`,
- hide `deprecated_for_agent` actions from the main coding prompt,
- keep them available to dashboard/API clients where useful.

Exit criteria:

- The agent prompt becomes smaller and more flexible; missing business actions
no longer block ordinary code generation.

#### P1.10 Unified task router

Deliverables:

- router that chooses between `workspace_native`, `trading_control`,
`read_only_answer`, `live_side_effect`, and `scheduled_event` lanes,
- side-effect lane always requires domain tools and approvals,
- coding lane always starts with workspace tools.

Exit criteria:

- "Write a new strategy" routes to workspace-native coding.
- "Enable this strategy live" routes to privileged trading control.
- "What strategies exist?" can use workspace state API or file index.

#### P1.11 Product UX changes

Deliverables:

- dashboard shows generated files, diffs, validation evidence, and reload state,
- operator can approve file patches and runtime promotion separately,
- chat UI labels whether the agent is in coding mode or live-control mode,
- strategy pages link to source files and validation reports.

Exit criteria:

- Users understand whether the agent is editing code, validating code, or
performing live/runtime side effects.

### Prompt and policy changes

The main agent prompt should change from action-first to workspace-first.

Current mental model:

```text
Pick a skill/action from the catalog, call it, observe result.
```

Target mental model:

```text
For coding/product tasks, inspect files first. Reuse conventions. Edit real
workspace artifacts. Run checks. Use domain APIs only for runtime state,
validation, registration, or external side effects.
```

Suggested prompt rules:

- Do not invent a domain action when a file edit solves the task.
- Before creating a strategy, inspect at least one existing strategy or template.
- Before modifying a file, read it and record its hash.
- After writing code, run the narrowest relevant validation command.
- Use domain tools for money, accounts, registration, validation, and lifecycle
transitions — not for ordinary code authoring.
- If a requested artifact does not have a tool, create the files directly using
project conventions.

### Safety model after increasing flexibility

More flexibility does not mean less safety. It means safety moves to the right
layer.

Keep strict gates for:

- shell commands that are destructive or network/live-trading related,
- writes outside workspace root,
- secret reads,
- live trading toggles,
- wallet signing,
- external deployment,
- runtime promotion.

Relax friction for:

- creating files in a strategy worktree,
- editing non-live configs,
- writing tests/docs/reports,
- running read-only grep/list/read commands,
- running local validation/backtest commands with bounded timeouts.

Recommended modes:

```text
read_only       read/search/list only
coding          read/search/edit/test inside workspace, no live side effects
validated       coding + deterministic validators/backtests
operator        validated + runtime reload/register with approval
live_control    privileged trading/account actions with explicit approval
```

### Product success criteria

Nerya should be considered Claude-Code-like enough when these scenarios work:

1. **Create strategy from prompt**
  - User says: "write an ETH mean-reversion strategy and backtest it".
  - Agent inspects templates/examples, creates files, runs validation, fixes
  errors, and reports diff/evidence.
  - No bespoke `create_strategy_from_text` action is required.
2. **Modify existing strategy**
  - User says: "tighten the stop-loss logic in this strategy".
  - Agent searches files, edits code, runs tests/backtest, and summarizes
  behavioral impact.
3. **Debug runtime issue**
  - User says: "why is this strategy not firing?".
  - Agent reads strategy files, route manifests, schedules, logs, and runtime
  state, then proposes or applies file/runtime fixes.
4. **Add a new provider or integration**
  - Agent creates code/tests/docs using conventions, not a special provider
   creation tool.
  - Domain registration/credential handling remains gated.
5. **Operate safely**
  - User says: "turn this live".
  - Agent switches from coding mode to live-control mode, runs validators,
  shows risk evidence, and requires explicit approval.

### Revised final target state

The stronger Nerya product should feel like this:

- The agent can understand and modify its own workspace like a coding assistant.
- Strategies, skills, routes, and SDK examples are normal files first.
- Domain APIs remain for validation, indexing, governance, and side effects.
- Skills are optional expert playbooks, not mandatory wrappers around every
operation.
- MCP expands the available tool/resource/skill surface, but remote skills are
trust-scoped and do not automatically gain local mutation power.
- Context stays small through skill indexes, file indexes, result storage,
microcompaction, and on-demand file reads.
- The dashboard becomes an operator review surface for files, diffs, evidence,
validations, and promotions — not the only place where artifacts can be made.

This pivot keeps Nerya's trading safety while removing the bottleneck that made
it weaker than Claude Code: too many product-specific tools and not enough
confidence in a general workspace-native agent loop.

## Claude Code Implementation Study: 12 Critical Agent Mechanics

This section maps the 12 concrete mechanics Nerya should learn from Claude
Code. The goal is not to clone every TypeScript file, but to copy the runtime
shape: general workspace tools, precise transcript handling, safe permissions,
compact context, and visible evidence.

Reference source areas inspected:

path: C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src

- Agent loop and tool recursion: `src/query.ts`
- System prompt assembly: `src/constants/prompts.ts`
- Project/user context and `CLAUDE.md` discovery: `src/context.ts`,
`src/utils/claudemd.ts`
- Skill loading: `src/skills/loadSkillsDir.ts`, `src/tools/SkillTool/SkillTool.ts`
- MCP tool/resource/skill loading: `src/services/mcp/client.ts`
- File reads: `src/tools/FileReadTool/FileReadTool.ts`
- Precise edits/writes: `src/tools/FileEditTool/FileEditTool.ts`,
`src/tools/FileWriteTool/FileWriteTool.ts`
- Shell execution and risk controls: `src/tools/BashTool/BashTool.tsx`,
`src/tools/BashTool/bashPermissions.ts`, `src/tools/BashTool/bashSecurity.ts`,
`src/tools/BashTool/pathValidation.ts`, `src/tools/BashTool/readOnlyValidation.ts`
- Permissions: `src/utils/permissions/permissions.ts`,
`src/hooks/useCanUseTool.ts`
- Progress/diff/error UI: per-tool `UI.tsx`, `src/commands/diff`,
`src/cli/print.ts`, `src/bridge/sessionRunner.ts`
- Verification/evals: bundled `verify` / `batch` skills, verifier agents,
VCR/regression comments and prompt-sensitivity extraction paths.

### Claude Code Source Reference Index

Use these file/line references when implementing the TODO phases. Paths are
relative to `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src`.

Reference policy: this section intentionally records only source locations and
implementation lessons. Do **not** copy Claude Code source code into Nerya; use
these references to inspect behavior and design Nerya-native equivalents.

#### 1. Agent loop implementation

- `query.ts:365` — builds `messagesForQuery` from messages after compact boundary.
- `query.ts:379` — applies aggregate tool-result budget before the model call.
- `query.ts:659` — calls the model with messages, system prompt, tools, MCP state, and options.
- `query.ts:833` — collects streamed assistant `tool_use` blocks.
- `query.ts:1382` — chooses streaming executor or `runTools` for collected tool calls.
- `query.ts:1395` — yields tool result messages and normalizes them for the API transcript.
- `query.ts:1647` — records tool-result counts before recursing.
- `query.ts:1716` — constructs the next message array with assistant messages and tool results.

#### 2. Prompt, messages, and tools assembly

- `constants/prompts.ts:444` — main `getSystemPrompt(...)` entrypoint.
- `constants/prompts.ts:457` — loads skill commands, output style, and environment info in parallel.
- `constants/prompts.ts:464` — derives enabled tool names from the active tool list.
- `constants/prompts.ts:493` — injects session-specific guidance based on enabled tools and skill commands.
- `constants/prompts.ts:561` — separates static cacheable prompt content from dynamic sections.
- `context.ts:116` — system context loader with git status snapshot.
- `context.ts:155` — user context loader with project rules and date.
- `commands.ts:360` — loads skill directory commands and plugin skills.
- `commands.ts:451` — merges skill, plugin, bundled, and workflow command sources.
- `commands.ts:476` — returns currently available commands/tools.

#### 3. Tool result回填模型

- `query.ts:552` — initializes `toolResults` for the current loop.
- `query.ts:713` — tombstones orphaned messages during fallback to avoid invalid transcripts.
- `query.ts:854` — pushes normalized tool-result messages from streaming tool execution.
- `query.ts:1395` — pushes normalized tool-result messages from `runTools` execution.
- `query.ts:1437` — maps tool result content back to original `tool_use_id` for summaries.
- `query.ts:1585` — appends assistant messages and tool results for sleep/continuation logic.
- `query.ts:1716` — constructs the next provider input with the paired assistant/tool-result blocks.

#### 4. Context超长与compact

- `query.ts:379` — applies aggregate tool-result budget.
- `query.ts:403` — runs snip compaction before microcompact/autocompact.
- `query.ts:414` — runs microcompact before full autocompact.
- `query.ts:454` — invokes autocompact.
- `query.ts:528` — builds post-compact messages.
- `query.ts:537` — tracks autocompact failure counts.
- `query.ts:1120` — invokes reactive compact after prompt/media errors.
- `services/compact/compact.ts` — post-compact message construction.
- `constants/toolLimits.ts:56` — tool summary size limit used by compact views.

#### 5. 项目规则和代码风格发现

- `context.ts:36` — git status snapshot for project context.
- `context.ts:155` — user context assembly.
- `context.ts:10` — imports `getClaudeMds` for `CLAUDE.md` discovery.
- `context.ts:11` — imports `getMemoryFiles` for memory/rule files.
- `utils/claudemd.ts:238` — records partial rule-file reads in file-state cache.
- `utils/claudemd.ts:1090` — notes cache reset behavior after compaction.
- `utils/claudemd.ts:1139` — tracks what memory/rule files are actually injected into the prompt.
- `utils/claudemd.ts:1472` — iterates read-file cache for rule/memory behavior.

#### 6. 文件选择与读取

- `tools/GlobTool/GlobTool.ts:57` — file pattern discovery tool definition.
- `tools/GlobTool/GlobTool.ts:154` — glob execution with bounded result limits.
- `tools/GlobTool/GlobTool.ts:165` — relativizes paths under cwd to save tokens.
- `tools/GrepTool/GrepTool.ts:56` — search output modes for content, files, and counts.
- `tools/GrepTool/GrepTool.ts:80` — bounded `head_limit` search output.
- `tools/GrepTool/GrepTool.ts:83` — `offset` pagination for search results.
- `tools/FileReadTool/FileReadTool.ts:337` — FileReadTool definition.
- `tools/FileReadTool/FileReadTool.ts:497` — read call accepts `offset` and `limit`.
- `tools/FileReadTool/FileReadTool.ts:542` — checks read dedup state for same unchanged range.
- `tools/FileReadTool/FileReadTool.ts:575` — discovers skills from read file paths in the background.
- `tools/FileReadTool/FileReadTool.ts:589` — activates conditional skills whose path patterns match the read file.
- `tools/FileReadTool/FileReadTool.ts:842` — stores structured file-read state for non-text/media reads.
- `tools/FileReadTool/FileReadTool.ts:1032` — stores structured file-read state for text reads.

#### 7. 精准编辑

- `tools/FileEditTool/prompt.ts:5` — tells model it must read before editing.
- `tools/FileEditTool/prompt.ts:18` — explains exact `old_string` matching.
- `tools/FileEditTool/prompt.ts:26` — explains `replace_all` behavior.
- `tools/FileEditTool/FileEditTool.ts:281` — rejects edit if file was not read.
- `tools/FileEditTool/FileEditTool.ts:306` — rejects stale edits after file modification.
- `tools/FileEditTool/FileEditTool.ts:336` — rejects non-unique `old_string` unless `replace_all` is true.
- `tools/FileEditTool/FileEditTool.ts:520` — updates read-file state after successful edit.
- `tools/FileWriteTool/FileWriteTool.ts:203` — rejects write if existing file was not read.
- `tools/FileWriteTool/FileWriteTool.ts:332` — updates read-file state after write.

#### 8. Shell命令执行与风险限制

- `tools/BashTool/BashTool.tsx:55` — progress threshold for long-running shell commands.
- `tools/BashTool/BashTool.tsx:227` — Bash input schema: command, timeout, description, background, sandbox override.
- `tools/BashTool/BashTool.tsx:292` — persisted output path field for large outputs.
- `tools/BashTool/bashPermissions.ts:16` — imports AST security parser.
- `tools/BashTool/bashPermissions.ts:84` — Bash classifier feature integration.
- `tools/BashTool/bashPermissions.ts:991` — exact command permission matching.
- `tools/BashTool/bashPermissions.ts:1680` — shadow-mode AST parsing setup area.
- `tools/BashTool/bashPermissions.ts:1832` — sandbox auto-allow check.
- `tools/BashTool/bashSecurity.ts:783` — shell metacharacter validation.
- `tools/BashTool/bashSecurity.ts:946` — parser differential risk handling.
- `tools/BashTool/bashSecurity.ts:1764` — documented parser-differential exploit class.

#### 9. 权限审批

- `utils/permissions/permissions.ts:122` — allow-rule collection.
- `utils/permissions/permissions.ts:137` — permission request message construction.
- `utils/permissions/permissions.ts:213` — deny-rule collection.
- `utils/permissions/permissions.ts:275` — always-allowed tool rule matching.
- `utils/permissions/permissions.ts:473` — main permission decision path.
- `hooks/useCanUseTool.ts` — UI/runtime bridge for tool-use approval decisions.

#### 10. 代码修改验证

- `tools/AgentTool/built-in/verificationAgent.ts:35` — verification-agent guidance for bug fixes and regression checks.
- `skills/bundled/verify.ts` — bundled verification skill.
- `skills/bundled/verifyContent.ts` — verification skill content.
- `skills/bundled/batch.ts:14` — batch skill guidance to run project tests.
- `commands/init-verifiers.ts:43` — verifier setup detects common test frameworks.

#### 11. 进度、diff、错误、最终报告展示

- `tools/FileReadTool/UI.tsx` — read result UI renderer.
- `tools/FileEditTool/UI.tsx` — edit/diff UI renderer.
- `tools/BashTool/UI.tsx` — shell queued/progress/result/error renderer.
- `tools/FileEditTool/FileEditTool.ts:551` — computes single-file git diff after edit in remote diff path.
- `tools/FileWriteTool/FileWriteTool.ts:350` — computes single-file git diff after write in remote diff path.
- `commands/diff/index.ts` — user-facing diff command.
- `bridge/sessionRunner.ts:138` — bridge activity logging for tool use.

#### 12. 评测和回归

- `entrypoints/cli.tsx:51` — prompt extraction path for prompt-sensitivity evals.
- `query.ts:768` — transcript/VCR stability note for streamed tool input backfill.
- `query.ts:1260` — hook death-spiral prevention note for synthetic errors.
- `tools/FileReadTool/FileReadTool.ts:531` — read-dedup regression/soak comment.
- `utils/file.ts:275` — edit regression validation note.

#### 13. Skill 与 MCP 加载发现机制

- `skills/loadSkillsDir.ts:405` — documents the supported `skill-name/SKILL.md` directory format.
- `skills/loadSkillsDir.ts:431` — locates each skill's `SKILL.md` entrypoint.
- `skills/loadSkillsDir.ts:447` — parses SKILL.md frontmatter and markdown body.
- `skills/loadSkillsDir.ts:458` — extracts path-based conditional skill activation metadata.
- `skills/loadSkillsDir.ts:771` — separates conditional skills from unconditional skills.
- `skills/loadSkillsDir.ts:997` — activates conditional skills when touched file paths match.
- `tools/SkillTool/SkillTool.ts:332` — defines the model-invocable skill execution tool.
- `tools/SkillTool/SkillTool.ts:875` — allowlists safe skill metadata exposed back to the model.
- `tools/SkillTool/SkillTool.ts:1065` — strips frontmatter before injecting skill content.
- `tools/SkillTool/SkillTool.ts:1095` — injects SKILL.md content as a meta user message.
- `services/mcp/client.ts:2172` — fetches tools for connected MCP clients.
- `services/mcp/client.ts:2345` — refreshes MCP tools, commands, and resources.
- `services/mcp/client.ts:2441` — measures loaded MCP command metadata size.
- `services/mcp/client.ts:2720` — normalizes MCP tool results.
- `services/mcp/client.ts:2802` — handles MCP tool calls that require URL elicitation.
- `services/mcp/client.ts:3206` — reports MCP re-authorization needs when tokens expire.
- `services/mcp/client.ts:3226` — clears MCP connection cache after session expiration.

#### 14. 中断、报错、恢复与 transcript 修复

- `query.ts:713` — tombstones orphaned messages when retry/fallback changes transcript shape.
- `query.ts:905` — clears tool results and tool-use blocks before retry to avoid orphaned ids.
- `query.ts:986` — preserves real errors instead of reporting misleading interruption text.
- `query.ts:1030` — cleans up hidden/locked MCP state on interrupt.
- `query.ts:1046` — skips interruption message for submit-interrupt control flow.
- `query.ts:1208` — sends fallback/repair model calls after selected failures.
- `query.ts:1260` — prevents hook-generated synthetic-error death spirals.
- `query.ts:1499` — skips duplicate interruption messages for submit-interrupt paths.
- `query.ts:1507` — enforces max-turn behavior after abort.


#### 15. Plan Mode 与 Todo 进度面

- `commands.ts:632` — registers `/plan` as the plan-mode toggle command.
- `commands/plan/index.ts:6` — describes `/plan` as enabling plan mode or showing the current plan.
- `commands/plan/plan.tsx:72` — enables plan mode when not already active.
- `commands/plan/plan.tsx:94` — shows the current plan when already in plan mode.
- `tools/EnterPlanModeTool/prompt.ts:6` — describes the plan-mode workflow: explore, understand patterns, design approach, present plan, clarify, then exit.
- `tools/EnterPlanModeTool/prompt.ts:23` — external prompt recommends proactive plan mode for non-trivial implementation.
- `tools/EnterPlanModeTool/prompt.ts:108` — internal prompt narrows plan mode to genuinely ambiguous/high-rework tasks.
- `tools/EnterPlanModeTool/EnterPlanModeTool.ts:36` — defines the model-invocable enter-plan-mode tool.
- `tools/ExitPlanModeTool/prompt.ts:6` — requires the model to write the plan to the plan file before requesting approval.
- `tools/ExitPlanModeTool/prompt.ts:15` — limits exit-plan-mode usage to implementation planning, not pure research.
- `tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts:149` — defines the plan-approval tool surface.
- `tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts:236` — asks the user to confirm exiting plan mode.
- `tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts:268` — rejects exit when no plan file exists.
- `tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts:318` — changes permission mode when exiting plan mode.
- `tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts:361` — restores the pre-plan permission mode after approval.
- `tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts:465` — tells the agent it may proceed after approval.
- `utils/plans.ts:79` — resolves the persistent plans directory.
- `utils/plans.ts:156` — recovers missing plan files from snapshots when possible.
- `utils/plans.ts:234` — copies plan files for forked sessions without clobbering the original.
- `utils/plans.ts:270` — documents the plan-content propagation chain from file to tool input/transcript.
- `bootstrap/state.ts:156` — tracks whether the session has exited plan mode.
- `bootstrap/state.ts:1349` — handles plan-mode transition side effects.
- `utils/todo/types.ts:4` — defines todo statuses: `pending`, `in_progress`, `completed`.
- `utils/todo/types.ts:8` — defines todo item fields including content, status, and active form.
- `tools/TodoWriteTool/prompt.ts:3` — positions TodoWrite as structured task-list management for coding sessions.
- `tools/TodoWriteTool/prompt.ts:153` — requires an `activeForm` for live progress display.
- `tools/TodoWriteTool/prompt.ts:184` — instructs the model to keep at least one task `in_progress` while working.
- `tools/TodoWriteTool/TodoWriteTool.ts:31` — defines the model-invocable TodoWrite tool.
- `tools/TodoWriteTool/TodoWriteTool.ts:65` — updates app-state todos for the current session.
- `tools/TodoWriteTool/TodoWriteTool.ts:69` — clears the visible todo list when all todos are completed.
- `tools/TodoWriteTool/TodoWriteTool.ts:82` — warns when a multi-step todo list lacks verification.
- `utils/sessionRestore.ts:73` — scans transcript for the last TodoWrite block.
- `utils/sessionRestore.ts:138` — restores TodoWrite state from transcript during session restore.

### 0. How Claude Code handles Plan and Todo

Claude Code has two different planning surfaces, and Nerya should learn both
but keep them separate:

1. **Plan Mode** is a temporary permission/context state for ambiguous or
high-impact implementation tasks. It lets the agent inspect the codebase, write
a concrete plan to a persistent plan file, and request user approval before
coding.
2. **TodoWrite** is a lightweight in-session progress surface. It is not an
approval gate. It keeps the user and agent aligned during multi-step work and is
restored from the transcript when sessions resume.

Claude Code's important Plan Mode behaviors:

- It is entered explicitly through `/plan` or by the model-invocable
`EnterPlanMode` tool.
- It is meant for implementation work where approach ambiguity or rework risk is
high, not for pure research or trivial fixes.
- The plan content lives in a real plan file, not only in chat text.
- `ExitPlanMode` does not accept arbitrary plan text from the model; it expects
the plan file to exist and uses that file as the approval artifact.
- Exiting plan mode asks for approval, then restores the previous permission
mode so the agent can continue coding under the right safety policy.
- Plan files are copied/recovered across resume/fork flows so approved plans do
not silently disappear.

Claude Code's important Todo behaviors:

- Todo items use a tiny schema: content, status, and `activeForm` for progress
UI.
- Statuses are constrained to `pending`, `in_progress`, and `completed`.
- The model is instructed to use todos proactively for non-trivial coding work,
mark tasks complete immediately, and keep progress visible.
- When all todos complete, the visible todo list can be cleared.
- Session restore scans transcript history for the latest TodoWrite call and
rebuilds todo state.

Should Nerya learn this?

Yes, but not by copying Claude Code's exact UX. Nerya should adopt the
underlying separation of concerns:

- **Plan Mode for approval**: use it only when the implementation direction,
risk, or product decision needs operator alignment before edits.
- **Todo Surface for execution**: use it for live progress and resumability on
multi-step coding tasks.
- **No rigid global planner**: do not force every coding task through a domain
planner before the model can inspect/edit files. The workspace-native loop should
still choose read/search/edit/shell tools directly.

Nerya implementation recommendation:

- Add `PlanArtifact` with fields: `session_id`, `plan_id`, `path`, `status`,
`created_at`, `approved_at`, `source_refs`, `risk_notes`, and `next_steps`.
- Store plan markdown under `.nerya/plans/<session-or-slug>.md` or
`state/plans/<session-or-slug>.md` and snapshot it into EvidenceBundle.
- Add `plan.enter` as a mode transition from `coding` to `planning` with
read/search/safe-shell tools enabled and write/live-control tools disabled by
default.
- Add `plan.exit` as an approval request that renders the plan file and returns
either `approved`, `rejected`, or `needs_revision`.
- Add `todo.update` as a provider-shaped tool result block, not a domain skill.
- Persist latest todos in session state and also reconstruct them from transcript
for recovery.
- Display current plan/todo state in dashboard chat, progress timeline, and final
summary.
- Teach prompts that plan mode is for ambiguous/high-risk implementation, while
simple obvious tasks should execute directly.

Recommended Nerya TODO order:

- [ ] Add `planning` as a temporary workspace-native mode.
- [ ] Add persistent `PlanArtifact` storage and session linkage.
- [ ] Add `plan.enter` / `plan.exit` transcript block types and UI events.
- [ ] Disable write/live-control tools while in unapproved plan mode.
- [ ] Render plan approval as a dashboard/chat approval request.
- [ ] Add `TodoItem` schema with `pending` / `in_progress` / `completed`.
- [ ] Add `todo.update` as a first-class progress tool.
- [ ] Restore todos and active plan from transcript/session state.
- [ ] Include plan/todo state in compaction summaries and EvidenceBundle.
- [ ] Add evals for trivial task no-plan, ambiguous task plan-required, rejected
plan revision, approved plan execution, interruption/resume with todos, and
compaction preserving plan state.

### 1. How one agent loop works

Claude Code's core loop in `query.ts` is recursive and transcript-driven.
At a high level, it rebuilds the post-compact message window, applies result
budgets and compaction, calls the model with the current tool set, streams
assistant output, collects `tool_use` blocks, executes tools, normalizes
`tool_result` messages, appends the paired assistant/tool-result blocks, and
recurses until a stop condition is reached.

Important details:

- The loop does not rely on a fixed domain planner deciding all actions up
front. The model can choose tools after seeing each observation.
- Tool calls are collected from streamed assistant content blocks, not from a
separate Nerya-style action JSON contract.
- Tool execution can be streaming: completed tool results may be yielded while
the model stream is still being processed.
- Stop reasons include no tool use, abort, max turns, hook stop, prompt too
long, and fallback/recovery paths.
- Tools can update `ToolUseContext`; the updated context is passed into the next
recursive call.

Nerya recommendation:

- Add a `WorkspaceNativeAgent` loop that is model-tool-call native and does not
force all actions through a preplanned `plan.skills` list.
- Keep the current `AgentKernel` for scheduled trading/event automation, but use
the new loop for coding, strategy authoring, debugging, and workspace tasks.
- Store every loop iteration as `assistant_message`, `tool_use`, `tool_result`,
`context_update`, and `stop_reason` records so replay is possible.

### 2. How prompt, messages, and tools are assembled

Claude Code builds context in layers:

1. Static system sections: identity, task behavior, tool usage rules, tone,
  output efficiency.
2. Dynamic system sections: session guidance, memory, environment info,
  language/output style, MCP instructions, scratchpad, tool-result summary
   rules, token budget.
3. User context: `CLAUDE.md`/memory files/current date.
4. System context: git status snapshot and optional debug injection.
5. Messages after the latest compact boundary.
6. Tool definitions from built-ins, skills, MCP, plugins, workflow tools, and
  refreshed MCP state.

`getSystemPrompt(...)` receives the active tool list so the prompt can include
only guidance relevant to currently enabled tools. `getUserContext(...)`
discovers `CLAUDE.md` unless disabled or in bare mode. `getSystemContext(...)`
adds git status once as a snapshot.

Nerya recommendation:

- Split context assembly into `StaticSystemPrompt`, `DynamicRuntimeContext`,
`ProjectRulesContext`, `ArtifactIndexContext`, `SkillIndexContext`, and
`ToolCatalogContext`.
- Do not render the full action catalog for every domain skill in coding mode.
Render primitive workspace tools first, then only relevant skill/tool index
rows.
- Add a stable prompt cache boundary: static instructions before the boundary;
repo state, MCP state, and active files after it.

### 3. How tool results are fed back to the model

Claude Code converts each tool execution into a user-side `tool_result` message
that references the original `tool_use_id`. In `query.ts`:

- assistant stream emits `tool_use` blocks,
- `runTools(...)` or `StreamingToolExecutor` executes them,
- each update may yield a UI message and a normalized API message,
- normalized messages are appended to `toolResults`,
- the next recursive call sends `[...messagesForQuery, ...assistantMessages, ...toolResults]` back to the provider.

Important details:

- Tool results are paired to the exact tool use id.
- Orphaned streaming messages are tombstoned when fallback occurs.
- Attachments such as file-change notices, skill discovery, queued commands, or
memory prefetch can also be added before the next model call.
- Large tool output is not blindly kept inline; result storage and compact
summaries prevent context explosion.

Nerya recommendation:

- Stop treating observations as ad hoc strings in a planner prompt.
- Introduce provider-shaped transcript blocks: `assistant.tool_use`,
`user.tool_result`, `attachment`, `tombstone`, and `compact_boundary`.
- Every Nerya tool result should carry `tool_use_id`, `tool_name`, `ok`,
`error_kind`, `summary`, `inline_content`, and optional `result_ref`.

### 4. How context is compacted when too long

Claude Code uses multiple compaction layers before giving up:

1. `applyToolResultBudget(...)` limits aggregate old tool-result size.
2. History snip can remove low-value history.
3. Microcompact runs before autocompact.
4. Context collapse can project a compacted view over older messages.
5. Autocompact summarizes the transcript and builds post-compact messages.
6. Reactive compact/truncation handles provider prompt-too-long or media errors.
7. Consecutive compaction failures are tracked to avoid infinite retries.

Important details:

- Compaction happens before the API call when thresholds are exceeded.
- It preserves recent tail messages exactly.
- It tracks compaction usage/cost and emits telemetry.
- It resets/marks post-compaction state so cache misses are explainable.
- It preserves invoked skills and avoids malformed tool-use/tool-result pairs.

Nerya recommendation:

- Upgrade `transcript_compact.py` into a layered system:
`tool_result_budget -> microcompact -> transcript_compact -> reactive_repair`.
- Preserve exact tail messages and all open tool-use/tool-result pairs.
- Preserve changed-file index, read-file hashes, invoked skills, pending approvals,
and validation status as non-lossy metadata outside the freeform summary.

### 5. How project rules and code style are discovered

Claude Code discovers project rules mainly through `CLAUDE.md` and memory files:

- `getUserContext(...)` calls `getMemoryFiles()` and `getClaudeMds(...)`.
- Discovery can be disabled by env vars or bare mode.
- Additional directories from `--add-dir` are included.
- `getSystemContext(...)` adds git status, current branch, main branch, recent
commits, and git user.
- File reads can trigger dynamic skill discovery and conditional skill
activation based on file paths.

Nerya recommendation:

- Support a Nerya equivalent of `CLAUDE.md`: `NERYA.md`, `AGENTS.md`, and
`.nerya/rules/*.md` loaded by directory hierarchy.
- Add `ProjectRulesLoader` that walks from cwd to workspace root, merges rules
by scope, and records which rules were loaded in EvidenceBundle.
- Add path-scoped rules and skills so reading/editing `strategies/foo/**`
activates strategy conventions, while dashboard files activate frontend
conventions.

### 6. How it chooses which files to read

Claude Code gives the model general discovery tools rather than preselecting all
files:

- `GlobTool` finds filenames by pattern.
- `GrepTool` searches text/symbols.
- `FileReadTool` reads exact files with `offset` and `limit`.
- `BashTool` can run read-only commands such as `git status`, `find`, `rg`,
`cat`, `head`, and `tail` when appropriate.
- `FileReadTool` deduplicates repeated reads of the same unchanged range.
- Reading files can trigger dynamic skill discovery for matching paths.

Nerya recommendation:

- Prefer file discovery primitives over domain inventory actions in coding mode:
`workspace.list`, `workspace.search`, `workspace.read`.
- Add file-read dedup, line-range reading, file-size/token caps, and similar-file
suggestions.
- Let the model decide what to read from search results, but record a
`read_set` with paths, ranges, hashes, and reasons.

### 7. How precise editing works

Claude Code's `FileEditTool` is exact-string based:

- The model must read the file before editing.
- `old_string` must match the file exactly after removing line-number prefixes.
- If `old_string` appears multiple times and `replace_all` is false, the edit
fails and asks for a more unique string.
- It refuses stale edits when the file changed since the last read.
- It handles quote-style preservation, CRLF/encoding preservation, notebooks via
a separate notebook tool, and file-size limits.
- It updates `readFileState` after writing so subsequent edits use fresh state.
- It computes structured patches/diffs and notifies IDE/LSP integrations.

Nerya recommendation:

- Replace broad `patch_file` semantics with Claude-like `workspace.edit`:
`{path, old_string, new_string, replace_all?, expected_hash?}`.
- Require prior read or explicit expected hash.
- Return structured patch, changed line count, before/after hash, and stale edit
errors.
- Keep `write_file` for new files, but prefer edit for existing files.

### 8. How shell commands are executed and risk-limited

Claude Code's Bash tool is powerful but heavily guarded:

- Input includes command, timeout, description, background mode, sandbox override.
- It classifies read/search/list commands for collapsed UI and read-only policy.
- It parses commands with tree-sitter or legacy shell parsing.
- It checks semantic risks such as `eval`, command substitution, parser
differentials, redirects, directory changes, path writes, sandbox bypass, and
environment hijacking.
- Sandbox auto-allow can allow safe sandboxed commands while preserving explicit
deny/ask rules.
- Long-running commands can be backgrounded and later read from output files.
- Large outputs are persisted to tool-results storage with previews.

Nerya recommendation:

- Keep `workspace.shell`, but split command policy into:
`read_only`, `local_validation`, `workspace_write`, `network`,
`destructive`, `live_side_effect`.
- Add command AST/semantic checks, output redirection path checks, cwd-change
write checks, timeout/background handling, persisted output paths, and
sandbox-aware auto-allow.
- Never let shell be the only path for file edits; prefer `workspace.edit` so
diffs and stale-read checks are structured.

### 9. How permission approval works

Claude Code has a layered permission model:

- Always allow/deny/ask rules from settings, CLI args, commands, session state.
- Tool-level, MCP server-level, MCP tool-level, and agent-type rules.
- Permission modes such as plan/ask/auto/bypass change default behavior.
- Hooks can require approval or deny.
- Classifiers can require approval for risky Bash commands.
- Permission request messages explain the exact reason: mode, rule, hook,
classifier, sandbox override, working directory, or subcommand results.
- User decisions can update session or persistent permission rules.

Nerya recommendation:

- Replace simple `approval_gate` labels with `PermissionDecision` records:
`{behavior, reason_type, reason, suggestions, can_persist, scope}`.
- Support per-tool, per-command-prefix, per-path, per-MCP-server, per-skill, and
per-mode rules.
- Add explicit modes: `read_only`, `coding`, `validated`, `operator`,
`live_control`.
- Show approval prompts with exact diff/command/side-effect scope, not just
"risk_gate required".

### 10. How code modifications are verified

Claude Code does not hardcode one verifier. It nudges the model through general
workspace behavior:

- Prompt guidance tells the agent to run relevant tests/checks after changes.
- Bash runs project-specific commands discovered from files (`package.json`,
Makefile, pytest, go test, cargo, etc.).
- Verification can be delegated to built-in verifier agents/skills.
- LSP notifications and diagnostics can update after file edits.
- Tool summaries and final responses report what was run and what remains.

Nerya recommendation:

- Add `VerificationPlanner` that discovers project check commands from files:
`package.json`, `pyproject.toml`, `pytest.ini`, `Makefile`, strategy package
metadata, and Nerya validation docs.
- Strategy creation should run file-first checks: schema validate, unit tests,
backtest/simulation, risk gate, and registration dry-run.
- EvidenceBundle must distinguish `tested`, `not_tested`, `failed`, and
`not_applicable`.

### 11. How progress, diff, errors, and final report are shown

Claude Code treats UI as a first-class transcript projection:

- Tool-specific `UI.tsx` renders concise progress and result chrome.
- Read/search results are collapsible; UI does not dump huge file content.
- Edit/write tools compute structured patches and diffs for review.
- Bash has queued/progress/result/error renderers and background hints.
- Bridge/structured IO surfaces tool activity for external clients.
- Final output is expected to summarize changed files, checks, risks, and next
steps.

Nerya recommendation:

- Dashboard/chat should show a timeline of `thinking -> tool_use -> result -> diff -> validation -> final`.
- File edits should render unified/structured diffs and allow approval before
applying when mode requires it.
- Shell commands should show command, cwd, timeout, risk class, progress,
persisted output ref, and interpreted exit status.
- Final report should be generated from EvidenceBundle, not from model memory
alone.

### 12. How it evaluates and prevents regressions

Claude Code's expanded source contains multiple regression/eval surfaces rather
than one monolithic eval runner:

- Prompt extraction paths support prompt-sensitivity evaluation.
- VCR fixture comments show transcript serialization stability matters.
- Tool implementations contain telemetry and kill switches for behavior changes
such as read dedup, tree-sitter Bash parsing, cached microcompact, and output
budgeting.
- Built-in verifier/verification agents encode expected validation behavior.
- Security-sensitive Bash parsing has shadow-mode comparison before becoming
authoritative.
- Regression comments explicitly document prior failures: orphaned tool results,
malformed streaming fallback, prompt leaks, parser differentials, read dedup
confusion, and hook death spirals.

Nerya recommendation:

- Build a `coding_agent_eval_runner` with golden scenarios:
create strategy, edit strategy, stale edit, prompt too long, interrupt,
shell approval, MCP failure, large output, and validation repair.
- Record full transcripts with stable redaction and compare structural events,
not only final text.
- Add shadow-mode policy checks for shell risk classification before enforcing.
- Add kill switches for new compaction, read dedup, and workspace-native routing
behavior.

### Crosswalk: Claude Code mechanism to Nerya implementation


| Claude Code mechanism                       | Nerya should add/change                             |
| ------------------------------------------- | --------------------------------------------------- |
| Recursive `query.ts` loop                   | `WorkspaceNativeAgent` provider-shaped loop         |
| `tool_use` / `tool_result` pairing          | Structured transcript blocks with ids               |
| `getSystemPrompt` layered sections          | Static/dynamic prompt section builder               |
| `CLAUDE.md` discovery                       | `NERYA.md` / `AGENTS.md` scoped rule loader         |
| Glob/Grep/Read/Edit/Bash primitives         | Always-on `workspace.*` primitives                  |
| `readFileState` stale-edit guard            | File hash/range read state and edit preconditions   |
| Exact-string edit tool                      | `workspace.edit(old_string,new_string)`             |
| Bash AST/security/permission layers         | Shell policy classifier and sandbox-aware approvals |
| Permission modes/rules/hooks/classifier     | Rich `PermissionDecision` model                     |
| Tool result budget/microcompact/autocompact | Layered context budget and compact pipeline         |
| Tool-specific UI renderers                  | Evidence timeline + diff/command/result cards       |
| Prompt/tool regression telemetry            | Golden transcript eval runner + kill switches       |


### Concrete Nerya backlog from the 12-point study

#### P0.19 Provider-shaped transcript loop

Deliverables:

- `nerya/agent/workspace_native.py`,
- transcript block types for `tool_use`, `tool_result`, `attachment`,
`compact_boundary`, `tombstone`,
- recursive model/tool loop independent of domain `plan.skills`,
- structural replay tests.

Exit criteria:

- A coding task can loop through arbitrary read/search/edit/shell operations
until completion without requiring bespoke JSON actions for every domain step.

#### P0.20 Layered prompt and project-rules loader

Deliverables:

- `prompt_sections.py`,
- `project_rules.py`,
- `NERYA.md` / `AGENTS.md` / `.nerya/rules/*.md` scoped loading,
- git status and artifact index context sections.

Exit criteria:

- The model sees relevant project rules and code style before editing, and the
evidence record lists which rule files affected the turn.

#### P0.21 Claude-like file primitives

Deliverables:

- `workspace.read` with offset/limit/dedup/hash,
- `workspace.search` and `workspace.glob`,
- `workspace.edit` exact-string replacement,
- stale-read and multiple-match errors,
- structured diff output.

Exit criteria:

- Existing source files can be safely edited without special domain tools, and
stale edits fail closed with actionable re-read instructions.

#### P0.22 Shell policy and approval overhaul

Deliverables:

- command semantic classifier,
- read-only/local-validation/destructive/live-side-effect categories,
- per-command/path/tool permission rules,
- sandbox-aware auto-allow,
- background command output refs.

Exit criteria:

- Local test/backtest commands run with low friction, while destructive/live
commands require explicit scoped approval with clear reasons.

#### P0.23 Evidence-first UI/reporting

Deliverables:

- timeline event schema,
- diff cards,
- shell result cards,
- validation cards,
- final report generated from EvidenceBundle.

Exit criteria:

- Users can see exactly which files were read/changed, which commands ran, what
failed, and what remains risky without reading raw logs.

#### P1.12 Coding agent regression suite

Deliverables:

- golden transcript scenarios,
- structural replay assertions,
- prompt-too-long/compact tests,
- stale edit and shell approval tests,
- strategy-as-code create/edit/validate scenarios.

Exit criteria:

- Nerya can safely evolve the flexible workspace-native agent without regressing
file safety, permission safety, or strategy validation behavior.


# 04 - Coding Ability and Subagent Gap

## Current Nerya Capability

Nerya has early generalist and proposal-oriented coding surfaces, but its strongest abilities are trading/runtime automation.

Evidence:

- `nerya/skills/builtin/evolution_skill/` can propose scripts, prompt patches, learning updates, config patches, and skill scaffolds.
- `nerya/evolution/patch_proposal.py` keeps agent-authored changes as proposals.
- `nerya/skills/builtin/script_skill/` and script sandbox tests restrict unapproved scripts.
- `nerya/subagents/dispatcher.py`, `nerya/subagents/runtime.py`, and `nerya/skills/builtin/subagent_skill/` support subagent creation/dispatch within Nerya's strategy boundaries.
- Tests include `tests/test_evolution_scaffold_phase3.py`, `tests/test_script_sandbox.py`, `tests/test_subagent_runtime_phase3.py`, and `tests/test_specialized_lanes.py`.

## Hermes Capability

Hermes is a mature programming/operator agent.

Evidence:

- `tools/file_tools.py`, `tools/file_operations.py`, `tools/patch_parser.py`, and `tools/path_security.py` implement file and patch operations.
- `tools/terminal_tool.py`, `tools/process_registry.py`, and `tools/code_execution_tool.py` enable command execution, background processes, and sandbox/code execution.
- `tools/browser_tool.py` and web tools support browser/web investigation.
- `tools/delegate_tool.py` and `tools/mixture_of_agents_tool.py` provide general delegation/subagent-like work.
- `mini_swe_runner.py` and `batch_runner.py` support coding benchmarks/batch execution patterns.

## Gap

Nerya's coding ability is mostly **proposal generation and sandboxed script support**, not a real repo-operating coding agent.

Missing or weak areas:

- no first-class repo file editing loop,
- no patch application tool comparable to Hermes,
- no terminal/test/build execution path inside agent turns,
- no browser-assisted frontend debugging path,
- no generic delegation for coding/research/execution roles,
- no subagent worktree/process isolation,
- no merge/integration protocol for subagent changes,
- no coding-specific verification contract,
- no SWE-style benchmark runner or batch worker mode.

## P0 Alignment Items

1. Add a coding toolset: file read/search/write/patch, terminal/test command, process/background registry, browser screenshot/inspect for frontend work. **Status: PARTIALLY COMPLETED 2026-04-25.** Operator skill (`Nerya/nerya/skills/builtin/operator_skill/skill.yml:1-340`, `Nerya/nerya/skills/builtin/operator_skill/actions.py:1-650`) ships `read_file`, `list_dir`, `search_files`, `write_file`, `patch_file` (string find/replace + minimal unified-diff applier), `terminal` with destructive-command guard, and a full process registry: `process_start`, `process_list`, `process_status`, `process_output`, `process_stop` (`Nerya/nerya/skills/builtin/operator_skill/actions.py:511-650`, `Nerya/nerya/skills/builtin/operator_skill/skill.yml:218-340`). `script_skill` provides `execute_code` for sandboxed code execution. Coverage: `Nerya/tests/test_operator_skill.py` (18 cases — read/list/search/write/patch×3/terminal/process×2). Remaining: `browser_*` (snapshot/inspect/click) and a lightweight `web_extract` action — tracked together as a follow-up coding-tools sprint.
2. Add coding-specific action policy: read-only mode, patch mode, test mode, destructive command approval. **Status: PARTIALLY COMPLETED 2026-04-25.** Per-action gating already exists: read-only actions declare `agent_query_only: true` (`Nerya/nerya/skills/builtin/operator_skill/skill.yml`), mutating actions declare `risk_gate: required` + `approval_gate: always`. The destructive-command refusal lives in `Nerya/nerya/skills/builtin/operator_skill/actions.py:50-80`. Remaining: a workspace-level "operator mode" preset (`read_only`, `dev`, `deploy`, `live_trading`) so the operator can globally pin the active surface — tracked with Plan 23 §5.
3. Add a `coding_agent` or `operator_agent` lane separate from trading strategy lanes. **Status: COMPLETED 2026-04-25.** Default `coding_agent` and `code_critic` lanes ship in the subagent registry (`Nerya/nerya/subagents/registry.py:53-64,76-79`). `coding_agent` carries `[operator, script, trace, llm]` so it can read/edit/patch the workspace, run sandboxed code, and self-report. `code_critic` carries `[operator, trace, strategy_review, llm]` — read-only review. The dispatcher denylist (`Nerya/nerya/subagents/dispatcher.py:33-38`) keeps trading-only skills out, and the operator-skill chroot (`Nerya/nerya/skills/builtin/operator_skill/actions.py:62-88`) makes the lanes safe by construction.
4. Add subagent execution primitives: spawn role with bounded task, assigned write scope, no-overlap edit policy, collect final summary and changed files. **Status: COMPLETED.** `Nerya/nerya/subagents/dispatcher.py:1-176` already runs subagents under a shared budget ceiling, denies trading skills, and produces a uniform `SubAgentResult` envelope. `dispatch_many` parallelises bounded-spec subagents. The "no-overlap edit policy" is enforced indirectly through the path-safety guard in the operator skill: each subagent inherits the same `Paths.root` chroot. Coverage: `Nerya/tests/test_subagent_runtime_phase3.py`, `Nerya/tests/test_specialized_lanes.py` (pre-existing).
5. Add coding verification evidence bundle: commands run, outputs read, files changed, known unverified gaps. **Status: PARTIALLY COMPLETED 2026-04-25.** Every skill call already journals `kind: skill.call.start/done/error` with payload keys, manifest path, permissions, and `loaded_via` (`Nerya/nerya/skills/runtime.py:111-168`). `Nerya/nerya/agent/session_search.py` lets the verifier replay everything for a session. Remaining: a typed `EvidenceBundle` struct produced at turn close that aggregates `(commands, files_read, files_written, gaps)` for direct LLM consumption — tracked under Plan 16.

## P1 Alignment Items

1. Add worktree or workspace snapshot support for subagents. **Status: NOT STARTED — tracked.** Path-safety chroot (`Nerya/nerya/skills/builtin/operator_skill/actions.py:62-88`) keeps subagents inside `Paths.root`; lightweight snapshot/worktree spawning is still future work.
2. Add review/critic/verifier subagent roles. **Status: COMPLETED 2026-04-25.** `code_critic` (read-only review lane: `[operator, trace, strategy_review, llm]`) and the existing `verification_lane` / `strategy_reviewer` cover coding + trading review (`Nerya/nerya/subagents/registry.py:38-79`).
3. Add batch runner for repeated tasks and evals. **Status: PARTIALLY COVERED.** `Nerya/nerya/subagents/dispatcher.py:dispatch_many` already runs bounded subagent specs in parallel. A SWE-style `batch_runner.py` driver is still future work.
4. Add patch proposal promotion path from draft to applied change with human approval. **Status: COMPLETED.** `Nerya/nerya/evolution/patch_proposal.py` already carries the draft→approved→applied lifecycle for agent-authored changes. `operator.patch_file` runs through `risk_gate: required` + `approval_gate: always` (`Nerya/nerya/skills/builtin/operator_skill/skill.yml:123-154`) so operator-driven patches share the same approval surface.

## Acceptance Gate

A P0-ready coding/subagent layer should pass: Nerya can assign one subagent to inspect tests, another to inspect implementation, apply a small patch in non-overlapping files, run the narrow test, and produce a verifier-readable evidence bundle.
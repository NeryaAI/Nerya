# Nerya Agent Team Implementation Design

Date: 2026-04-25
Status: phases 1-4 implemented; phase 5 (replay/snapshot/learning) deferred
Reference project: `../ClawTeam`
Target project: `Nerya`

---

## Implementation Status (2026-04-26)

Phases 1–4 from §11 are landed and verified end-to-end against the user's
configured LLM (`C:\Users\Ricky\.nerya\nerya.yml`). The live demo
`scripts/run_nl_team_demo.py --user-config` starts a real `nerya serve`
process and drives a single multi-turn session through HTTP, including a
`strategy.design` turn that fires `strategy_design_team` end-to-end with
shared blackboard, durable artifacts, and a synthesised final report.
Latest verified run: `Nerya/.nl_e2e_runs/20260426_051544/`.

| Phase / Surface | Module(s) | Status |
| --- | --- | --- |
| 1. Durable team core (models, store, mailbox, blackboard) | `nerya/teams/{models,store,mailbox,blackboard}.py` + `tests/test_teams_core.py` | done |
| 2. Templates + orchestrator + gates + aggregator | `nerya/teams/{templates,orchestrator,gates,aggregator}.py` + `tests/test_teams_orchestrator.py` | done — built-in templates: `market_analysis_team`, `strategy_design_team`, `trade_decision_committee` |
| 3. Planner / kernel integration | `nerya/agent/planner.py` (`TurnPlan.team_template`), `nerya/agent/kernel.py` team branch, `nerya/agent/context_builder.py` `team_context` rendering | done (covered by `tests/test_teams_orchestrator.py` + `tests/test_team_e2e_natural_language.py`) |
| 4. Skill / API operator surface | `nerya/skills/builtin/team_skill/` (SKILL.md + scripts/ + actions.py shim), `nerya/api/routes_teams.py` (`/teams/templates`, `/teams/runs`, `/teams/run`, `/teams/get`) + `tests/test_teams_skill_and_routes.py`, `tests/test_team_live_session.py`, `tests/test_team_e2e_natural_language.py` | done — skill + HTTP routes shipped; dashboard view deferred |
| 5. Snapshots / replay / strategy learning reflection | `nerya/teams/snapshot.py` (planned), team-close memory write-back | deferred |

Verification artefacts (run id `team-strategy_design_team-f157072c` in
`Nerya/.nl_e2e_runs/20260426_051544/teams/`):

- `run.json`, `template.json`, `members.json` — durable team state.
- `tasks/task-*.json` — durable task board with dependency / lock metadata.
- `events.jsonl`, `blackboard.jsonl` — shared event + evidence log.
- `synthesis/final_context.json`, `synthesis/final_report.md` — aggregated
  per-role evidence, conflict matrix, consensus signal/distribution and
  task summaries.
- `inboxes/<member>/msg-*.json` — durable inter-agent messaging.

Verified safety boundary: in the live demo, the `execution-planner` task in
`strategy_design_team` was correctly blocked from calling the denylisted
`trading` skill ("subagent 'execution_planner' is not allowed to use
denylisted skills: ['trading']"). The team still completed required
analyst tasks, and the parent kernel produced a paper-only strategy spec
through normal `strategy_skill` actions — proving the design rule from §8:
team children remain advisory and cannot bypass risk/approval gates.

E2E test mirroring this: `tests/test_team_live_session.py` (HTTP-driven,
matches the demo turn shape).

**Open items:**

- Phase 5: snapshot/replay support and team-close reflection writing one
  summarised learning through existing memory paths.
- Dashboard `Teams` page (Phase 4 was scoped to skill + API surface).
- Optional: per-role finer-grained quorum templates beyond the three
  built-ins.

## 1. Goal

Bring the ClawTeam-style Agent Team pattern into Nerya so that market analysis and strategy design are not handled by one monolithic agent turn, but by a coordinated team of specialized agents that share evidence, intermediate conclusions, task state, and final decision context.

The target outcome is:

- A strategy or market-analysis request can start a `TeamRun`.
- The team has explicit roles such as strategy lead, market analyst, on-chain analyst, news analyst, technical analyst, risk critic, execution planner, and portfolio manager.
- Each agent receives a bounded task and skill allowlist.
- Team members communicate through durable inbox/broadcast channels.
- Team members write into a shared blackboard/evidence store instead of only returning isolated outputs.
- The lead agent waits for required members, resolves conflicts, and produces a final decision-ready memo or strategy proposal.
- Only the parent Nerya runtime can submit trading actions; team agents can recommend but cannot bypass risk/approval gates.

## 2. Existing facts from the two repos

### 2.1 ClawTeam pattern to reuse

ClawTeam is built around a durable team coordination layer rather than a simple parallel-map call:

- `clawteam/team/models.py` defines `TeamConfig`, `TeamMember`, `TeamMessage`, and `TaskItem` with member identity, message types, task status, owner, locks, priority, and dependency fields.
- `clawteam/team/manager.py` stores team config under a data directory and manages lifecycle: create team, add/remove members, resolve inboxes, list members, and cleanup.
- `clawteam/team/mailbox.py` provides `send`, `broadcast`, `receive`, `peek`, and event-log behavior on top of a transport.
- `clawteam/transport/base.py` + `clawteam/transport/file.py` separate message semantics from delivery. The file transport uses per-agent inbox files, atomic writes, claim/ack, and dead-letter quarantine.
- `clawteam/team/snapshot.py` captures team config, tasks, events, sessions, costs, and pending inbox messages as restorable snapshots.
- `clawteam/templates/hedge-fund.toml` and `clawteam/templates/strategy-room.toml` show the key product shape: a leader decomposes work, specialized analysts produce signals, risk consolidates, and the leader waits for all required reports before making a decision.
- `clawteam/harness/orchestrator.py` wraps this with phases and gates such as plan artifacts, all-tasks-complete, verification, and optional human approval.

The important design is not the tmux/process spawning itself. For Nerya, the reusable part is the coordination contract: team identity, task board, mailbox, shared artifacts, snapshots, gates, and leader synthesis.

### 2.2 Nerya surfaces to build on

Nerya already has several pieces that should be reused instead of replaced:

- `nerya/agent/kernel.py` already runs `trigger -> plan -> subagents -> context -> main LLM -> skill actions -> journal -> reflection` and records turn steps.
- `nerya/agent/planner.py` already maps trigger kinds to skills, tier, and subagents.
- `nerya/subagents/registry.py` defines role prompts and skill allowlists such as `market_analyst`, `risk_critic`, `execution_planner`, `onchain_watcher`, `news_interpreter`, `portfolio_manager`, `strategy_reviewer`, `plan_lane`, and `verification_lane`.
- `nerya/subagents/runtime.py` already gives each subagent a bounded child runtime with iterative observe/think/act, max iterations, skill allowlists, budget controls, and a denylist that prevents direct `trading`, `wallet`, and `script_runtime` access.
- `nerya/subagents/dispatcher.py` and `dispatch_many` already provide parallel execution with max-parallel and budget limits.
- `nerya/subagents/result_aggregator.py` currently collapses isolated subagent outputs into a simple dict; this is the main gap for shared team intelligence.
- `nerya/agent/memory.py`, `memory_recall.py`, and `working_memory.py` already separate global memory, strategy learnings, recall previews, and per-turn scratchpads.
- Strategy/trading history already records triggers, subagents, risk, orders, fills, reviews, decisions, and artifacts through `strategy_history` and skill actions.
- The repo rule is skill-first: external calls go through skills, and agent-authored changes should go through proposal flow rather than direct workspace mutation.

Therefore, the implementation should extend Nerya's existing subagent runtime into a durable TeamRuntime. It should not import ClawTeam as a runtime dependency, and it should not spawn external Codex/Claude/tmux workers for normal strategy decisions.

## 3. Gap analysis

Current Nerya subagents are useful but not yet a ClawTeam-style team:

| Capability | Current Nerya | Needed Agent Team behavior |
| --- | --- | --- |
| Role registry | Exists via `SubAgentSpec` | Add team templates and role quorum rules |
| Parallel execution | Exists via `dispatch_many` | Add multi-round work with dependencies and waiting |
| Shared context | Parent builds context once | Add append-only team blackboard and evidence ledger |
| Agent messaging | Not first-class between subagents | Add inbox, broadcast, and leader-directed messages |
| Task board | Planner chooses subagent list | Add durable task objects with owner, status, locks, blockers |
| Conflict handling | Aggregator averages/confidence only | Add structured disagreements and adjudication |
| Snapshot/replay | Turn steps and journals exist | Add team-run snapshot/replay linked to strategy/session |
| Product surface | Subagent prompts editable | Add TeamRun creation, status, transcript, artifacts, and final memo |
| Safety boundary | Child denylist exists | Keep children recommendation-only; parent owns trade actions |

## 4. Proposed architecture

### 4.1 Package layout

Add a new runtime package:

```text
nerya/teams/
├── __init__.py
├── models.py              # TeamTemplate, TeamRun, TeamMember, TeamTask, TeamMessage, TeamArtifact
├── store.py               # durable JSON/JSONL store under workspace/teams/
├── mailbox.py             # send/broadcast/receive/peek over store transport
├── blackboard.py          # shared evidence, claims, questions, conflicts, final context
├── templates.py           # built-in market/strategy templates
├── orchestrator.py        # team lifecycle and multi-round execution
├── gates.py               # quorum, all-required-complete, evidence-minimum, risk-review gates
├── aggregator.py          # synthesis input builder and conflict matrix
└── skill.py               # team skill actions exposed to AgentKernel / dashboard
```

Do not replace `nerya/subagents/*`. The team layer should call existing `SubAgentRuntime._run_one` or `dispatch_many` internally, while giving each subagent a richer team context.

### 4.2 Persistent workspace layout

Use the existing workspace pattern and keep all team state inside the configured Nerya workspace:

```text
workspace/
└── teams/
    └── <team_run_id>/
        ├── run.json
        ├── template.json
        ├── members.json
        ├── tasks/
        │   └── task-<id>.json
        ├── inboxes/
        │   └── <agent_name>/msg-<ts>-<id>.json
        ├── events.jsonl
        ├── blackboard.jsonl
        ├── evidence/
        │   └── <artifact_id>.json
        ├── synthesis/
        │   ├── conflict_matrix.json
        │   ├── final_context.json
        │   └── final_report.md
        └── snapshots/
            └── <ts>-<tag>.json
```

This mirrors ClawTeam's file-first durability but stays inside Nerya's workspace rather than `~/.clawteam`.

### 4.3 Core data model

Recommended model fields:

```python
TeamTemplate:
  id: str
  description: str
  lead: str
  members: list[TeamMemberSpec]
  tasks: list[TeamTaskSpec]
  gates: list[TeamGateSpec]
  max_rounds: int
  max_parallel: int
  usd_budget: float | None

TeamRun:
  id: str
  template_id: str
  goal: str
  strategy_id: str | None
  session_id: str | None
  trigger_event_id: str | None
  status: pending|running|synthesizing|completed|failed|cancelled
  phase: plan|research|risk_review|synthesis|verification|close
  created_at: str
  updated_at: str
  created_by: str
  budget: dict
  metrics: dict

TeamMember:
  name: str
  role: str
  subagent_name: str
  required: bool
  allowed_skills: list[str]
  tier: str
  status: idle|running|blocked|completed|failed

TeamTask:
  id: str
  subject: str
  description: str
  owner: str
  status: pending|in_progress|completed|blocked|failed|skipped
  priority: low|medium|high|urgent
  depends_on: list[str]
  required_artifacts: list[str]
  lock_owner: str | None
  result_ref: str | None

TeamMessage:
  id: str
  type: message|broadcast|task_assigned|task_completed|question|answer|conflict|gate_request|gate_result
  from_agent: str
  to: str | None
  content: str
  artifact_refs: list[str]
  created_at: str
  consumed_at: str | None

BlackboardEntry:
  id: str
  kind: evidence|claim|signal|risk|question|assumption|conflict|decision_input
  author: str
  task_id: str | None
  summary: str
  payload: dict
  confidence: float | None
  source_refs: list[str]
  created_at: str
```

All models should be JSON-serializable and redaction-safe. Store writes should be atomic. Message reads should claim/ack or mark consumed to avoid duplicate processing.

## 5. Built-in team templates for Nerya

### 5.1 `market_analysis_team`

Use when the operator asks to analyze a market, asset, regime, or token before strategy design.

Members:

- `market-lead` -> `portfolio_manager` or new `market_synthesis_lead`
- `technical-analyst` -> `market_analyst`
- `onchain-analyst` -> `onchain_watcher`
- `news-sentiment-analyst` -> `news_interpreter`
- `risk-critic` -> `risk_critic`
- `execution-planner` -> `execution_planner`, optional

Required gates:

- Technical, news/sentiment, and risk tasks must complete.
- At least one evidence artifact per required analyst.
- Final report must include signal, confidence, invalidation level, key risks, data freshness, and missing data.

### 5.2 `strategy_design_team`

Use when the operator asks Nerya to create or improve a strategy.

Members:

- `strategy-lead` -> `plan_lane` or new `strategy_lead`
- `market-analyst` -> `market_analyst`
- `feature-researcher` -> new `feature_researcher` with market/news/onchain read skills
- `risk-critic` -> `risk_critic`
- `execution-planner` -> `execution_planner`
- `strategy-reviewer` -> `strategy_reviewer`
- `verification-lane` -> `verification_lane`

Required gates:

- Strategy spec artifact exists.
- Risk critic has reviewed sizing, stop conditions, max loss, stale data behavior, and exchange assumptions.
- Verification lane has reviewed whether the proposal is testable and replayable.
- If a code/config change is proposed, it goes through `PatchProposal`, not direct mutation.

### 5.3 `trade_decision_committee`

Use for high-risk live or near-live decisions.

Members:

- `portfolio-manager` -> final recommendation only
- `market-analyst`
- `risk-critic` required
- `execution-planner` required
- `verification-lane` optional but recommended for high notional

Hard rule:

- Child agents can only produce `recommendation` or `decision_input` entries.
- The parent `AgentKernel` is the only component allowed to dispatch `trading.submit_intent`, so existing risk gate and approval gate remain mandatory.

## 6. Execution flow

### 6.1 Trigger to team plan

1. `AgentKernel.run_turn` receives trigger.
2. `plan_turn` returns either existing `subagents` or a new optional `team_template` field.
3. If `team_template` is present, `AgentKernel` calls `TeamOrchestrator.run(...)` before the main LLM call.
4. Team output is injected into the main context as untrusted but structured `team_context`.
5. Main LLM decides whether to message, propose strategy changes, request more research, or submit a trading intent.

Recommended planner extension:

```python
TurnPlan:
  kind: str
  subagents: list[str]
  team_template: str | None
  skills: list[str]
  tier: str
```

Routes can then say:

```yaml
agent:
  planner:
    routes:
      strategy_design:
        match: [strategy.design, strategy.improve]
        team_template: strategy_design_team
        skills: [strategy, strategy_review, market_data, risk, trace, llm]
        tier: high
      market_analysis:
        match: [market.analysis, token.research, regime.analysis]
        team_template: market_analysis_team
        skills: [market_data, news_social, onchain, portfolio, trace, llm]
        tier: medium
```

### 6.2 Team orchestration loop

Pseudo-flow:

```text
create TeamRun
load template
materialize members and tasks
seed blackboard with goal, trigger, strategy memory, market defaults
broadcast kickoff
for round in 1..max_rounds:
  select runnable tasks whose dependencies are complete
  run assigned subagents with team context, bounded parallelism, and shared budget
  persist each result as artifact + blackboard entries
  route messages/questions/conflicts
  evaluate gates
  if all required gates pass: break
synthesize conflict matrix and final context
ask lead/synthesis agent for final report if needed
persist final_report.md and final_context.json
return TeamRunResult to AgentKernel
```

The key difference from current `dispatch_many` is that each worker sees the shared blackboard and prior messages, not just the original payload.

### 6.3 Team context passed into each subagent

Each child prompt should receive a compact context package:

```json
{
  "team_run": {"id": "...", "goal": "...", "phase": "research"},
  "member": {"name": "technical-analyst", "role": "market_analyst"},
  "task": {"id": "...", "subject": "...", "required_artifacts": ["signal"]},
  "shared_blackboard_preview": [
    {"kind": "claim", "author": "news-sentiment-analyst", "summary": "..."}
  ],
  "open_questions": [],
  "strategy_memory_preview": "...",
  "allowed_output_schema": {
    "signal": "bullish|bearish|neutral|none",
    "confidence": "0..1",
    "evidence": [],
    "risks": [],
    "questions": [],
    "recommended_next_tasks": []
  }
}
```

Blackboard previews must be compacted and source-attributed. Raw third-party data should be wrapped as untrusted input, following current subagent runtime behavior.

## 7. Shared information design

The team should share information through explicit stores, not hidden prompt concatenation.

### 7.1 Blackboard

Use `blackboard.jsonl` for append-only shared knowledge. Every entry must have author, task id, kind, confidence, and source refs.

Recommended entry kinds:

- `evidence`: factual data from a skill call, with source and freshness.
- `claim`: analyst interpretation.
- `signal`: normalized bullish/bearish/neutral signal.
- `risk`: risk condition, exposure limit, invalidation, or safety concern.
- `question`: something another role or lead must answer.
- `conflict`: contradiction between members.
- `decision_input`: synthesized item used by the lead.

### 7.2 Artifacts

Use `evidence/<artifact_id>.json` for larger structured outputs:

- market snapshots
- indicator summaries
- news clusters
- on-chain wallet flow summaries
- risk matrices
- strategy specs
- backtest/replay references

Blackboard entries should reference artifacts by id to avoid bloating prompts.

### 7.3 Memory integration

Keep three memory tiers distinct:

- Global Nerya memory: read-only preview for team context.
- Strategy memory: read-only at kickoff; append only at team close if reflection determines a durable learning.
- Team blackboard: live shared memory for the current team run.

Do not let individual workers write global/strategy memory directly. The team close step should write one summarized learning through existing memory/reflection paths.

## 8. Safety model

The TeamRuntime must preserve Nerya's trading-native safety rules:

- Child agents remain unable to call `trading`, `wallet`, or unsafe script runtime directly.
- Team tasks can call only skills allowed by their member spec and the runtime denylist.
- Team output is advisory until the parent kernel parses a final decision.
- Live trading still requires `runtime.live_trading_enabled: true` and existing approval/risk gates.
- Strategy/config/code changes from the team are proposals and must flow through `PatchProposal`.
- Secrets must never be written into team state, blackboard, inbox, artifacts, or final reports.
- Every team artifact should be redacted before persistence.

## 9. API and skill surface

Expose the team layer as a builtin skill plus API routes.

### 9.1 Skill actions

Add `nerya/skills/builtin/team_skill/`:

```yaml
id: team
summary: Multi-agent team coordination for analysis and strategy design.
actions:
  create_run:
    when: Start a team run from a goal/template.
  get_run:
    when: Inspect team run status, members, tasks, gates, and final output.
  list_runs:
    when: Browse recent team runs.
  append_message:
    when: Send a message or operator note into a team run.
  cancel_run:
    when: Stop a running team run safely.
  snapshot_run:
    when: Capture restorable team state for audit/replay.
```

`create_run` should return a `team_run_id`, current status, and a final artifact path if synchronous. Later, long-running mode can run in a background supervisor.

### 9.2 HTTP routes

Add local API routes:

```text
GET  /teams/templates
POST /teams/runs
GET  /teams/runs
GET  /teams/runs/{run_id}
GET  /teams/runs/{run_id}/events
GET  /teams/runs/{run_id}/blackboard
GET  /teams/runs/{run_id}/artifacts/{artifact_id}
POST /teams/runs/{run_id}/messages
POST /teams/runs/{run_id}/cancel
POST /teams/runs/{run_id}/snapshot
```

Dashboard can then show team status as an operator surface instead of a hidden kernel detail.

## 10. Dashboard design

Add a `Teams` page or extend the existing subagent page:

- Template picker: `market_analysis_team`, `strategy_design_team`, `trade_decision_committee`.
- Goal input and optional `strategy_id` binding.
- Live run timeline: kickoff, task assignment, subagent steps, gate checks, synthesis.
- Member cards: role, status, current task, skill allowlist, tokens/cost.
- Task board: pending/running/completed/blocked.
- Blackboard: evidence, claims, conflicts, risks, open questions.
- Final report: markdown memo, confidence, actionability, unresolved gaps.
- Operator actions: cancel, snapshot, inject note, rerun from snapshot, export report.

## 11. Implementation phases

### Phase 1 — Durable team core, no new LLM behavior

Deliverables:

- `nerya/teams/models.py`
- `nerya/teams/store.py`
- `nerya/teams/mailbox.py`
- `nerya/teams/blackboard.py`
- Unit tests for atomic writes, task state, message send/receive, blackboard append/read.

Acceptance:

- Can create a team run and persist members/tasks.
- Can send/broadcast/receive messages.
- Can append/read blackboard entries.
- No secrets appear in persisted test fixtures.

### Phase 2 — Team templates and orchestrator

Deliverables:

- `templates.py` with `market_analysis_team`, `strategy_design_team`, `trade_decision_committee`.
- `orchestrator.py` that runs existing subagents with assigned tasks.
- `gates.py` with required-member and required-artifact gates.
- `aggregator.py` that builds conflict matrix and final context.

Acceptance:

- A mocked team run executes multiple assigned subagents.
- Required task failure keeps run in failed/blocked state with reason.
- Final context includes per-role evidence and disagreements.

### Phase 3 — AgentKernel planner integration

Deliverables:

- Extend `TurnPlan` with `team_template`.
- Extend route manifests/config to allow `team_template`.
- In `AgentKernel`, run TeamOrchestrator before the main LLM when selected.
- Inject `team_context` into context builder as untrusted structured input.

Acceptance:

- Existing non-team routes behave unchanged.
- Strategy-design triggers run the team path.
- Team output appears in turn steps and strategy history.

### Phase 4 — Skill/API/dashboard operator surface

Deliverables:

- `team_skill` builtin skill.
- API endpoints for templates, runs, events, blackboard, artifacts, messages, cancel, snapshot.
- Dashboard page for creating and inspecting team runs.

Acceptance:

- Operator can start `strategy_design_team` from dashboard.
- Operator can inspect role outputs and final report.
- Operator can send a note into a running team.

### Phase 5 — Replay, snapshots, and strategy learning

Deliverables:

- Snapshot and replay support for team runs.
- Team close reflection that writes one summarized learning through existing memory paths.
- Optional scenario replay/backtest references in strategy design output.

Acceptance:

- A completed team run can be exported and replayed deterministically with mocked LLM/tool outputs.
- Final strategy learning is appended once with source run id.

## 12. Test plan

Recommended tests:

```text
tests/test_team_models.py
tests/test_team_store.py
tests/test_team_mailbox.py
tests/test_team_blackboard.py
tests/test_team_templates.py
tests/test_team_orchestrator.py
tests/test_team_kernel_integration.py
tests/test_team_skill.py
tests/test_team_api.py
```

Key cases:

- Message delivery is FIFO and consume-safe.
- Invalid message JSON is quarantined or marked failed, not silently lost.
- Task dependencies prevent premature execution.
- Required gates block synthesis until required tasks complete.
- Budget exhaustion stops scheduling new workers but preserves completed results.
- Child agents cannot request direct trading/wallet skills.
- Team artifacts redact secrets.
- Existing `dispatch_many` subagent tests still pass.
- Existing trading risk/approval tests still pass unchanged.

## 13. Example: strategy design flow

User request:

```text
帮我针对 BTC 1h 趋势突破设计一个策略，考虑新闻、链上资金流和风控。
```

Runtime flow:

1. Planner matches `strategy.design` and selects `strategy_design_team`.
2. Team run starts with goal and optional strategy id.
3. Lead creates tasks:
   - Market analyst: technical regime and volatility.
   - On-chain watcher: exchange inflow/outflow and whale flow.
   - News interpreter: recent BTC headline/sentiment regime.
   - Risk critic: max drawdown, stale data, leverage/no-trade conditions.
   - Execution planner: venue/order/sizing constraints.
   - Strategy reviewer: testability and replay requirements.
4. Workers write evidence and claims to blackboard.
5. Risk critic flags contradictions or missing data.
6. Lead synthesizes final strategy spec:
   - entry condition
   - exit condition
   - invalidation
   - data requirements
   - risk limits
   - test/replay plan
   - confidence and unresolved gaps
7. Parent AgentKernel receives team context and decides whether to create a strategy proposal, ask for missing data, or run a backtest/replay skill.

## 14. Why this fits Nerya

This design preserves Nerya's existing architecture:

- It keeps `AgentKernel` as the parent decision owner.
- It reuses current subagent roles, allowlists, budgets, LLM gateway, and journals.
- It adds ClawTeam's missing durable team primitives: mailbox, task board, blackboard, snapshots, and gated synthesis.
- It keeps all external calls skill-first.
- It keeps trading and code mutation behind existing risk/approval/proposal controls.

The most important implementation choice is to treat Agent Team as a Nerya-native coordination layer, not as an external process launcher. ClawTeam provides the design pattern; Nerya should implement the pattern inside its runtime.

## 15. Minimal first milestone

The smallest useful milestone is:

1. Implement file-backed `TeamRun`, `TeamTask`, `TeamMessage`, and `BlackboardEntry` storage.
2. Add `market_analysis_team` template.
3. Run existing `market_analyst`, `news_interpreter`, and `risk_critic` subagents against one shared blackboard.
4. Produce `final_context.json` and `final_report.md`.
5. Record the team run id in the parent turn step and strategy history.

That milestone is enough to prove shared-team intelligence without touching live trading behavior or dashboard complexity.

---

# 16. Code-level deep dive and corrected design

This section is the more important implementation analysis. It is based on the actual call paths in `ClawTeam` and `Nerya`, not just the public README/template descriptions.

## 16.1 What ClawTeam actually does in code

### Launch path

`clawteam launch` is implemented in `ClawTeam/clawteam/cli/commands.py::launch_team`.

The real sequence is:

1. Load a TOML template via `load_template(template)`.
2. Create a durable team config through `TeamManager.create_team(...)`.
3. Add every template agent through `TeamManager.add_member(...)`.
4. Materialize initial tasks into `TaskStore(t_name).create(...)`.
5. Resolve a spawn backend via `get_backend(...)`.
6. Render each agent prompt with `render_task(...)`.
7. Spawn every agent process with identity, task, team name, and optional worktree.

So ClawTeam's team is not just a prompt convention. It first persists a team model and task model, then starts agents that use the persisted team primitives.

### Durable task board

`ClawTeam/clawteam/store/file.py::FileTaskStore` stores each task as `task-<id>.json` under a team task directory. Important behavior:

- `create(...)` writes a `TaskItem` and marks it `blocked` if `blocked_by` is non-empty.
- `update(...)` acquires a task lock when status becomes `in_progress`.
- locks are advisory and can be released if the owning agent is no longer alive.
- dependency cycles are rejected by `_validate_blocked_by_unlocked(...)`.
- completing a task resolves dependent tasks via `_resolve_dependents_unlocked(...)`.

This is a major difference from Nerya. Nerya currently has subagent outputs, but no task graph that workers can lock, complete, block, or unblock.

### Mailbox and transport

`ClawTeam/clawteam/team/mailbox.py::MailboxManager` does message semantics while delegating delivery to transport:

- `send(...)` builds a `TeamMessage`, resolves the recipient inbox through `TeamManager.resolve_inbox(...)`, delivers bytes to transport, and writes an event log.
- `broadcast(...)` enumerates recipients and sends a message to everyone except excluded agents.
- `receive(...)` claims raw messages, validates them into `TeamMessage`, acks valid ones, and quarantines invalid ones.

`ClawTeam/clawteam/transport/file.py::FileTransport` gives the mailbox process-safe semantics:

- messages are written as `msg-<ts>-<uid>.json` through temp file + atomic rename.
- receive renames `.json` to `.consumed`, locks the consumed file, then reads it.
- valid messages are acked by deleting consumed files.
- invalid messages go to dead letters with metadata.

For Nerya this means a good TeamRuntime should not be only an in-memory Python list. Market/strategy analysis can be long-running, interrupted, inspected from dashboard, and replayed, so Nerya needs a store-backed mailbox/blackboard.

### Phase gates

`ClawTeam/clawteam/harness/phases.py` has `PhaseRunner` and gates:

- `ArtifactRequiredGate` blocks phase advance until named artifacts exist.
- `AllTasksCompleteGate` checks `TaskStore(state.team_name).list_tasks()` and blocks if any task is not completed.
- `HumanApprovalGate` blocks until an approval artifact exists.
- `PhaseRunner.advance()` calls `can_advance()`, records phase history, changes phase, and emits a phase transition event.

For Nerya, the equivalent should not be a generic dev harness. It should be trading-native gates: required analyst quorum, risk-review required, evidence freshness, strategy spec artifact required, verification artifact required, and parent approval before live trade intent.

## 16.2 What Nerya actually does today

### Parent turn path

`Nerya/nerya/agent/kernel.py::_run_turn_body` has this real order:

1. `plan_turn(trigger, config)` returns `TurnPlan(kind, subagents, skills, tier)`.
2. `select_skills(...)` filters tools for the main turn.
3. If `plan.subagents` is non-empty, `SubAgentDispatcher.dispatch_many(...)` runs those subagents in parallel.
4. `aggregate(outputs)` collapses the results into a dict keyed by subagent name.
5. `build_context(..., subagent_outputs=merged, memory_preview=recall_text, action_catalog=...)` builds the prompt for the main LLM.
6. The main LLM emits actions.
7. `ToolRunner.call(...)` dispatches selected skill actions with budget, timeout, retry, and journaling.

The important insertion point is between steps 2 and 5. The existing subagent block at `kernel.py` lines around 813-887 is where a TeamRuntime should run. A team result can then be inserted into `subagent_outputs` or, better, a new `team_outputs` / `team_context` parameter in `build_context`.

### Subagent runtime path

`Nerya/nerya/subagents/dispatcher.py` resolves each role from workspace prompts and runs it through `SubAgentRuntime`:

- `_resolve_spec(...)` loads `workspace/subagents/*.agent.md` with default allowlists from `registry.py`.
- `_assert_allowed_skills(...)` rejects any denylisted child skills.
- `_run_one(...)` creates `SubAgentRuntime(config, skills, LLMGateway(config))` and calls `runtime.run(...)`.
- `dispatch_many(...)` uses `ThreadPoolExecutor`, caps `max_parallel`, applies a shared USD budget, journals every result, and writes `record_subagent(...)` into strategy history.

`Nerya/nerya/subagents/runtime.py::run` is already a real child runtime:

- it builds role-specific context from `context_policy.build_context(...)`.
- it calls the LLM with task `subagent_analysis`.
- it parses `skill_calls` / `tool_calls` from the LLM output.
- it dispatches only allowed, non-denylisted skills via `self.skills.runtime.call(...)`.
- it records signals, evidence, rejected actions, uncertainty, and step metrics.

This means Nerya should not rewrite subagent execution. The correct design is to wrap/extend it:

- TeamRuntime owns task/message/blackboard state.
- TeamRuntime still calls `SubAgentRuntime.run(...)` for each role.
- TeamRuntime passes a richer payload containing `team_run_id`, `task_id`, `blackboard_preview`, `inbox_messages`, `required_output_schema`, and `open_questions`.
- TeamRuntime captures each returned subagent output into artifacts + blackboard entries.

### Context limitation

`Nerya/nerya/subagents/context_policy.py::build_context` currently builds context from allowed skill capability tags and payload market/topic. It has no team-run awareness. Each subagent sees:

- original payload
- role prompt
- market/news context derived from allowed skills
- its own prior observations in the current child loop

It does not see other analysts' claims, questions, conflicts, or evidence unless those are manually placed in the payload. This is the core code-level reason current subagents are not a Team.

### Aggregator limitation

`Nerya/nerya/subagents/result_aggregator.py::aggregate` only merges outputs by subagent name and averages confidence-like fields. It does not:

- preserve evidence references as first-class artifacts,
- detect disagreement,
- enforce required roles,
- ask follow-up questions,
- require risk sign-off,
- wait on task dependencies,
- produce a final strategy memo.

So the proper design must replace this for team paths with a TeamAggregator that creates `conflict_matrix.json`, `final_context.json`, and `final_report.md`.

## 16.3 Corrected integration design

The previous proposal was directionally right but too abstract. The concrete code-level design should be:

### A. Do not add a parallel system beside Nerya subagents

Bad design:

```text
AgentKernel -> ClawTeam-like external team runner -> independent agents -> text report
```

Problems:

- duplicates LLM routing/budget logic,
- bypasses Nerya's existing subagent denylist,
- makes strategy history harder to explain,
- creates a second runtime ownership model.

Better design:

```text
AgentKernel
  -> plan_turn selects team_template
  -> TeamOrchestrator
      -> TeamStore creates run/tasks/messages/blackboard
      -> TeamRoundScheduler selects runnable tasks
      -> SubAgentRuntime.run(existing) for assigned roles
      -> Blackboard appends evidence/claims/questions/conflicts
      -> TeamGates decide next phase
      -> TeamAggregator builds final_context/final_report
  -> AgentKernel build_context receives team_context
  -> main LLM decides actions through existing ToolRunner
```

### B. Add TeamRuntime as orchestration, not execution

Concrete module responsibilities:

```text
nerya/teams/store.py
  - atomic JSON/JSONL persistence under config.paths.root / "teams"
  - create_run, update_run_status, create_task, update_task, append_event
  - no LLM calls

nerya/teams/mailbox.py
  - send, broadcast, receive, peek
  - can be simpler than ClawTeam initially but must be durable
  - message records are TeamMessage models, not freeform strings

nerya/teams/blackboard.py
  - append_entry, list_entries, preview_for_agent, conflict_candidates
  - writes append-only JSONL entries
  - performs redaction before persistence

nerya/teams/orchestrator.py
  - owns run loop and gates
  - calls existing SubAgentDispatcher._run_one or SubAgentRuntime.run
  - converts subagent outputs to artifacts and blackboard entries
  - never calls trading/wallet directly

nerya/teams/aggregator.py
  - builds final_context.json
  - builds conflict_matrix.json
  - optionally calls a lead/synthesis subagent for final_report.md
```

### C. Minimal code changes inside existing paths

Only small targeted edits are needed in existing code:

1. `nerya/agent/planner.py`
   - Extend `TurnPlan` with `team_template: str | None = None`.
   - Teach `plan_turn` / `explain_plan` to read `route.get("team_template")`.

2. `nerya/agent/kernel.py`
   - After `selected = select_skills(...)` and before the existing `dispatch_many(...)`, branch:

```python
team_result = None
if getattr(plan, "team_template", None):
    team_result = TeamOrchestrator(self.config, self.skills).run(
        template_id=plan.team_template,
        goal=_goal_from_trigger(trigger),
        trigger=trigger,
        trigger_event_id=trigger_event_id,
        strategy_id=strategy_id,
        session_id=session_id,
    )
    _record_step("team", detail=team_result.step_detail(), ...)
```

   - Either skip legacy `plan.subagents` when team is active, or allow templates to run team plus lightweight subagents explicitly. Default should skip to avoid duplicate work.
   - Pass team output into context:

```python
ctx = build_context(..., subagent_outputs=merged, team_context=team_result.final_context)
```

3. `nerya/agent/context_builder.py`
   - Add optional `team_context` rendering section after memory and before action catalog.
   - Mark it as untrusted structured analysis, same safety stance as subagent outputs.

4. `nerya/subagents/runtime.py`
   - Avoid invasive changes. Add optional `team_context` argument only if needed:

```python
def run(..., team_context: dict[str, Any] | None = None)
```

   - Render it as `=== team shared context ===` in `_render_prompt`.
   - Alternatively, encode it into payload first and change no signature. The signature is cleaner for tests.

5. `nerya/subagents/dispatcher.py`
   - Add `dispatch_task(...)` or a TeamOrchestrator-internal helper that can pass `team_context` and `task` to `SubAgentRuntime.run`.
   - Keep `dispatch_many` unchanged for existing tests.

6. `nerya/strategy_history/store.py`
   - Add `record_team_run(...)` and optionally `record_team_artifact(...)` so strategy history can explain why a strategy/trade decision used a team conclusion.

This keeps the implementation surgical.

## 16.4 How TeamOrchestrator should execute rounds

A concrete first implementation can be deterministic and testable:

```python
class TeamOrchestrator:
    def run(self, *, template_id, goal, trigger, trigger_event_id, strategy_id, session_id):
        run = store.create_run(...)
        template = templates.get(template_id)
        store.materialize_members(run.id, template.members)
        store.materialize_tasks(run.id, template.tasks)
        blackboard.seed(run.id, goal=goal, trigger=trigger, memory=recall_preview(...))

        for round_index in range(template.max_rounds):
            runnable = store.list_runnable_tasks(run.id)
            if not runnable:
                break
            results = run_tasks_parallel(runnable, max_parallel=template.max_parallel)
            for task, result in results:
                artifact = artifacts.write_subagent_result(run.id, task.id, result)
                blackboard.append_from_subagent(run.id, task, result, artifact_ref=artifact.id)
                store.update_task(...completed or failed...)
            gate = gates.evaluate(run.id, template.gates)
            if gate.passed:
                break

        final_context = aggregator.build_final_context(run.id)
        report = aggregator.write_final_report(run.id, final_context)
        store.complete_run(run.id, final_context_ref=..., report_ref=...)
        return TeamRunResult(...)
```

The first version does not need true asynchronous background workers. It can be synchronous within `AgentKernel.run_turn`, as current `dispatch_many` is. Later it can move to background supervisor when dashboard needs long-running jobs.

## 16.5 Data model adjustments based on Nerya code

Because Nerya already has `session_id`, `strategy_id`, `trigger_event_id`, and turn journals, TeamRun should include those IDs directly:

```python
@dataclass
class TeamRun:
    id: str
    template_id: str
    turn_id: str | None
    trigger_event_id: str | None
    strategy_id: str | None
    session_id: str | None
    status: str
    phase: str
    goal: str
    created_at: str
    updated_at: str
    final_context_ref: str | None = None
    final_report_ref: str | None = None
```

Each `TeamTask` should bind to a Nerya subagent spec name, not an abstract external process name:

```python
@dataclass
class TeamTask:
    id: str
    run_id: str
    owner: str                 # team member name, e.g. technical-analyst
    subagent_name: str          # Nerya spec, e.g. market_analyst
    subject: str
    payload: dict[str, Any]
    required: bool = True
    status: str = "pending"
    blocked_by: list[str] = field(default_factory=list)
    result_artifact: str | None = None
```

This mapping is essential because Nerya's executable unit is `SubAgentSpec`, not a tmux window.

## 16.6 Better shared-context semantics

The team blackboard should become the missing shared state layer between Nerya subagents.

For each task, TeamOrchestrator should build `team_context` like:

```python
team_context = {
  "run_id": run.id,
  "phase": run.phase,
  "goal": run.goal,
  "task": task.asdict(),
  "member": member.asdict(),
  "blackboard_preview": blackboard.preview_for_agent(
      run.id,
      agent=member.name,
      max_entries=12,
      include_kinds=["evidence", "claim", "risk", "question", "conflict"],
  ),
  "inbox": mailbox.receive(member.name, limit=5),
  "required_output_schema": template.output_schema_for(member.role),
}
```

This is better than just appending all previous outputs into one prompt because:

- it is bounded,
- it is auditable,
- it separates facts from claims,
- it gives each subagent role-relevant shared context,
- it can be rendered in dashboard.

## 16.7 Conflict handling that should exist from day one

Market analysis often fails because agents agree too easily. The aggregator should explicitly detect and surface disagreement:

- Normalize each subagent output to `{signal, confidence, timeframe, market, assumptions, invalidation, evidence_refs}`.
- Build `conflict_matrix.json` when:
  - signals disagree (`bullish` vs `bearish`),
  - timeframes differ (`1h long` vs `1d bearish`),
  - confidence is high but evidence freshness is weak,
  - risk critic rejects a trade that market analyst recommends,
  - execution planner says venue/liquidity constraints make the recommendation impractical.
- If required conflicts exist, either:
  - run one additional `risk_critic` or `strategy_reviewer` task, or
  - mark final context as `actionable: false` with unresolved conflicts.

This is more important for Nerya than ClawTeam's generic engineering workflow because trading decisions need explicit disagreement, not consensus theater.

## 16.8 Recommended first code milestone after this design

The first code PR should not implement every API/dashboard feature. It should prove the runtime primitive:

1. Add `nerya/teams/models.py`, `store.py`, `blackboard.py`, `orchestrator.py`, `aggregator.py`, `templates.py`.
2. Add a synchronous `market_analysis_team` using existing `market_analyst`, `news_interpreter`, and `risk_critic`.
3. Extend `TurnPlan` and `AgentKernel` to run that team when route config has `team_template`.
4. Add `team_context` rendering to `context_builder`.
5. Add tests:
   - `test_team_store.py`
   - `test_team_blackboard.py`
   - `test_team_orchestrator.py`
   - `test_team_kernel_integration.py`
6. Do not add dashboard until the runtime is correct.

That first PR would answer the real question: can Nerya's market/strategy analysis become a shared-state team while preserving current safety gates and journals?

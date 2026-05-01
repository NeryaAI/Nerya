# 08 - Priority Roadmap

## Goal

Make Nerya stop feeling like a brittle trading demo and start feeling like a reliable autonomous operator while preserving its trading safety model.

## Status (2026-04-25)

This document is a meta-plan that maps onto the per-area plans (01–07,
09–31). Per-phase completion is tracked in those documents. As of
2026-04-25:

- **Phase -1 / Phase 0 / Phase 1 / Phase 2 / Phase 3**: BACKEND COMPLETE — see Plans 01, 02, 03, 04, 05, 06, 07.
- **Phase 4 / 5 / 6 / 7**: BACKEND PARTIALLY COMPLETE — see Plans 06, 11, 12, 16, 23, 25, 26, 27, 28, 30, 31.
- Dashboard UX completion is the largest open piece (Plan 05 §1 §3 §4 §5, Plan 21 §1).

## Phase 0 - Truth Reset

Priority: P0

Tasks:

1. Stop claiming or implying full Hermes parity.
2. Mark Nerya as: trading-native runtime real, Hermes-inspired surfaces partial, general operator/coding agent not ready.
3. Add a capability matrix in docs and dashboard with `ready`, `partial`, `stub`, and `not implemented` states.

Acceptance:

- Dashboard and README do not overstate gateway, memory, coding, TUI, MCP client, or skill ecosystem maturity.

## Phase 1 - General Operator Harness

Priority: P0

Tasks:

1. Implement tool registry and operator toolset.
2. Add file/search/patch/terminal/process/web/browser/code tools.
3. Add non-trading approval policies.
4. Add tool-result storage and tool event streaming.
5. Add cancellation/interrupt primitives.

Acceptance:

- Nerya can inspect/edit/test a repo through its own agent loop.

## Phase 2 - Session, Transcript, and UX Cockpit

Priority: P0

Tasks:

1. Persist complete transcript events.
2. Add streaming protocol.
3. Add dashboard chat tool timeline, approvals, interrupt, session resume, and evidence panel.
4. Add session search.
5. Add transcript compaction invariants.

Acceptance:

- A long interrupted task can be resumed and audited without losing tool-call consistency.

## Phase 3 - Skill Ecosystem Parity

Priority: P0/P1

Tasks:

1. Add user/procedural skill discovery and invocation.
2. Add skill list/view/install/enable/disable/sync.
3. Add skill manager UI.
4. Add skill policy and self-improvement proposals.

Acceptance:

- User can install a skill and invoke it from chat; Nerya shows loaded skill and allowed tools.

## Phase 4 - Memory and Self-Learning Loop

Priority: P0/P1

Tasks:

1. Add explicit memory tool and commands.
2. Add FTS session search.
3. Add memory nudge and correction capture.
4. Add user profile memory.
5. Add skill/prompt improvement from repeated failures.

Acceptance:

- Nerya recalls a prior correction with evidence and applies it in a later session.

## Phase 5 - Coding/Subagent Execution

Priority: P1

Tasks:

1. Add coding lanes and role-specialized subagents.
2. Add write-scope isolation and merge protocol.
3. Add verifier contract.
4. Add worktree/snapshot support.

Acceptance:

- Nerya can run two independent subagents and integrate a safe patch with test evidence.

## Phase 6 - Gateway and Scheduled Session Parity

Priority: P1

Tasks:

1. Productionize Telegram plus one second gateway.
2. Add unified session identity across gateway/dashboard/CLI.
3. Finish scheduled-agent-session delivery parity.
4. Add shared slash command registry.

Acceptance:

- A cron job runs a real session and delivers to a gateway; user resumes same session elsewhere.

## Phase 7 - MCP/ACP/Config/Deploy Hardening

Priority: P1/P2

Tasks:

1. Add MCP client integration.
2. Expand ACP adapter.
3. Add config profiles/migrations.
4. Expand doctor and packaging.

Acceptance:

- `nerya doctor` can diagnose the full operator stack, not only trading/runtime basics.

## What Not To Do First

Do not start by adding more trading strategies, wallet providers, or dashboard cards. Those do not fix the core bad experience.

The bad experience comes from lacking a real general-purpose tool harness, durable conversation/session management, user skill ecosystem, memory/search/learning product loop, visible operator UI, and execution-capable subagents.

## Recommended Immediate Implementation Order

1. `operator_tool` registry plus file/search/patch/terminal tools.
2. Transcript event store plus streaming event protocol.
3. Dashboard chat timeline plus interrupt/approval controls.
4. Memory/session search.
5. User skill invocation.
6. Coding subagent lane.
7. Gateway scheduled-session delivery.

## Release Gates

### Gate A - Usable Local Operator

- file/search/patch/terminal tools work,
- dashboard shows tool timeline,
- approvals work,
- session persists and resumes,
- narrow coding E2E passes.

### Gate B - Learning Operator

- memory commands work,
- session search works,
- memory nudge works,
- skill improvement proposal generated from a real failure.

### Gate C - Hermes-Like Daily Use

- TUI or equivalent dashboard UX exists,
- gateways deliver and resume sessions,
- cron scheduled sessions work,
- subagents can execute bounded coding tasks,
- doctor/preflight covers full stack.

## Phase -1 - Security And State Foundations

Priority: P0, must happen before exposing richer tools or gateways.

Tasks:

1. Add actor model and auth modes: loopback dev, dashboard session, service token, gateway actor, schedule actor, admin recovery.
2. Add route authorization matrix and audit journal.
3. Add append-only event store for sessions/turns/tools/approvals/gateway deliveries.
4. Add idempotency keys for gateway inbound, tool calls, approvals, schedule ticks, trading intents, and outbound messages.
5. Add redacted observability and correlation IDs.

Acceptance:

- A request can always be traced from API/gateway entry to auth decision, context selection, tool calls, approvals, delivery, and final result.
- Duplicate webhook/API retries cannot duplicate privileged side effects.

## Phase 1.5 - Gateway Quality Layer

Priority: P0 for Telegram, P1 for second platform.

Tasks:

1. Build normalized inbound pipeline with actor/session resolution.
2. Build queued outbound pipeline with markdown escaping, chunking, message edit fallback, rate-limit handling, and dead-letter storage.
3. Add Telegram `/stop`, `/resume`, `/status`, `/approve`, `/reject`, `/sessions`, `/skills`, and `/memory` commands.
4. Add inline approval buttons and cancellation callback flow.
5. Add fake Telegram transport E2E tests.

Acceptance:

- Telegram long task sends fast ack, compact progress, inline approval/stop controls, and final result without broken markdown or spam.

## Phase 2.5 - Context And Streaming Correctness

Priority: P0.

Tasks:

1. Add `/agent/stream` SSE and `/sessions/{id}/events` replay.
2. Add frontend reconnect via last event sequence.
3. Add context manifest per LLM call.
4. Add lane budgets and dropped-source reasons.
5. Add compression invariants for tool call/result pairs, approvals, user corrections, and unresolved blockers.

Acceptance:

- Dashboard refresh during a turn does not lose progress, and every final answer can explain which context was used or dropped.

## Phase 3.5 - Permissions Before Power

Priority: P0.

Tasks:

1. Add tool permission evaluator before adding shell/write/browser tools.
2. Add grants: one-call, turn, session, skill, schedule.
3. Add subagent permission narrowing and assigned write scopes.
4. Add gateway actor restrictions.
5. Add permission violation tests.

Acceptance:

- More powerful tools can be added without giving every agent, subagent, gateway user, and schedule unrestricted access.

## Evidence-First Correction From Code Reading

The code-backed appendix changes the implementation order slightly:

1. Reuse `nerya/harness/tool_runner.py` and `nerya/skills/runtime.py` as safety chokepoints; do not bolt powerful terminal/file/browser tools around them.
2. Reuse `nerya/messaging/mirror.py` for gateway idempotency/session mirroring; extend it into delivery queue + dead-letter instead of creating a second gateway state model.
3. Reuse `nerya/agent/memory_recall.py::explain_recall` for context source explanation; extend it into full `context_manifest` rather than inventing unrelated context tracing.
4. Keep `nerya/api/local_server.py` loopback-only until auth middleware exists; its current CORS/no-auth shape is explicit local convenience code.
5. Treat `dashboard/components/chat/ChatView.tsx` as a non-streaming prototype; the first frontend fix must be event streaming and server-backed sessions, not visual redesign.
6. Harden `SecretVault` production defaults before exposing secret CRUD through any authenticated remote UI.

So the real first engineering slice should be:

- request context + route auth,
- event store,
- ToolRunner event emission + permissions,
- `/agent/stream`,
- dashboard event timeline,
- Telegram delivery queue built on existing gateway mirror.
## Phase 1.6 - Protocol-Level Telegram Completion

Priority: P0 if Telegram is a primary interface.

Tasks:

1. Add normalized inbound model with `message_id`, `reply_to_message_id`, `thread_id`, `user_id`, `entities`, `caption`, and `attachments[]`.
2. Add attachment cache and safe attachment model for image/document/audio/voice/video.
3. Make Telegram session key include chat type and topic/thread id when applicable.
4. Add outbound renderer with MarkdownV2 escaping, plain-text fallback, long-message chunking, file attachment, and edit-message support.
5. Add inline approval/stop/detail buttons and callback verification.
6. Add per-chat queue, busy debounce, delivery state machine, retry, and dead-letter.
7. Add fake Telegram tests for text, topic reply, attachment, long output, approval callback, duplicate update, unauthorized chat, and failed send.

Acceptance:

- Telegram can preserve reply/thread context, handle attachments safely, avoid broken formatting, avoid message spam, and expose approval/cancel controls without duplicating turns or leaking state.

## Phase 2.6 - Compression, Interrupt, And Busy-Session Semantics

Priority: P0 before long-running tools, gateway-first usage, or coding-agent work.

Tasks:

1. Add explicit turn state machine: queued, thinking, running_tool, awaiting_approval, cancel_requested, cancelled, failed, completed, abandoned, resumable.
2. Add per-session active-turn lock and busy policy: queue_next, interrupt_replace, append_instruction, or reject_busy.
3. Add cooperative cancellation token through AgentKernel, SubAgentDispatcher, LLMGateway, ToolRunner, SkillRuntime, scheduled runner, and gateway status loop.
4. Wire live context compression into turns with context manifest and ReferenceStore refs.
5. Add large tool-result/artifact persistence before prompt inclusion.
6. Add approval wait cancellation, expiry, scoped resolution, and idempotent duplicate handling.
7. Add dashboard and Telegram UX for stop/status/queue/retry/abandon/resume.

Acceptance:

- Stopping a turn cancels backend work, not only the frontend request.
- A second message during active work follows a defined policy and never silently races another turn.
- Compression preserves tool/result pairs, unresolved approvals, latest user corrections, and source refs.
- Large outputs are persisted as artifacts and shown as previews.

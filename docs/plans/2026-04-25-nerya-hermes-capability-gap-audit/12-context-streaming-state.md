# 12 - Context, Streaming, And State Architecture

## Status (2026-04-25)

Implementation evidence (verified file paths):

- **Append-only event store**: per-session JSONL journals written by `Nerya/nerya/agent/kernel.py:run_turn` into `Nerya/workspace/journals/turn_steps.jsonl` (event kinds include `turn.started`, `tool.start/done/error`, `memory.read/write`, `turn.complete`). Replay/search via `Nerya/nerya/agent/session_search.py` and exposed at `GET /agent/session/events` + `POST /agent/session/search` (`Nerya/nerya/api/routes_agent.py:292-378`).
- **Streaming event bus** (Plan 12 — seq+resume contract, COMPLETED 2026-04-25): `Nerya/nerya/agent/streaming.py` now stamps every published event with a monotonic ``seq`` and stable ``event_id`` (`Nerya/nerya/agent/streaming.py:101-131`). The in-memory ring grew to 500 events (default) and ships `recent(after_seq=...)`, `events_since(N)`, `latest_seq()`, `cursor_after(events)`, and `next_seq()` so reconnecting clients can resume from the largest seq they already rendered without duplicating or dropping events. Kernel publishers (`Nerya/nerya/agent/kernel.py:746-761`) automatically inherit the new seq/event_id stamps. HTTP poll endpoint `GET /agent/stream/events` accepts `?after_seq=N` and returns `{events, count, cursor, latest_seq}` (`Nerya/nerya/api/routes_agent.py:326-381`).
- **Cancellation**: `Nerya/nerya/harness/cancellation.py:CancelToken` + process-wide `register_token` / `signal_cancel`; `POST /agent/interrupt` flips a registered token (Plan 05 P0 §1).
- **Context budgeting**: `Nerya/nerya/agent/context_builder.py:build_context` selects/drops sources before LLM call; the harness-level token budget lives in `Nerya/nerya/harness/budget.py`. Long-message compression is `Nerya/nerya/llm/compression.py` (covered by `tests/test_context_compression.py`).
- **Approvals**: skill manifests carry `approval_gate`; the kernel routes those through `Nerya/nerya/harness/tool_runner.py` and journals `approval_gate` decisions (`Nerya/nerya/agent/kernel.py:460-475`).
- **Streaming resume tests**: `Nerya/tests/test_streaming_bus.py` was extended to 21 cases covering seq monotonicity, after-seq filter, ring rotation under load, cursor advancement, clear-resets-seq, persisted-event-id round-trips, and `/agent/stream/events?after_seq=N` HTTP semantics including invalid-cursor fallback and session-id filtering.

Status: PARTIALLY COMPLETED — backend events/streaming/cancellation/journal/search/`seq+resume` are wired; remaining items are
(a) a dedicated approvals queue file (separate from the existing approvals JSONL ledger),
(b) the frontend SSE/event-timeline renderer (Plan 05 P1), and
(c) per-LLM-call context manifest (`turn_id, llm_call_id, included/dropped sources`).

## Goal

Make every turn inspectable, resumable, cancellable, and context-safe.

## Event Store

Nerya should persist append-only events, not just final turn summaries.

Required event types:

- `session.created`
- `turn.started`
- `user.message`
- `context.source_selected`
- `context.source_dropped`
- `llm.requested`
- `llm.completed`
- `assistant.delta`
- `tool.started`
- `tool.progress`
- `tool.output_ref`
- `tool.completed`
- `approval.requested`
- `approval.resolved`
- `memory.read`
- `memory.write`
- `gateway.delivery_requested`
- `gateway.delivery_done`
- `error.raised`
- `turn.completed`
- `turn.cancelled`

Each event needs:

- `event_id`,
- `seq`,
- `ts`,
- `session_id`,
- `turn_id`,
- `actor`,
- `visibility`,
- `payload`,
- `redaction_state`.

## Streaming Contract

### Backend

- `/agent/stream` starts a turn and streams SSE.
- `/sessions/{id}/events?after_seq=N` replays events.
- `/turns/{id}/cancel` requests cancellation.
- `/approvals/{id}/respond` resolves approval.

### Frontend

- Renders events incrementally.
- Reconnects with last seen sequence.
- Shows redacted event details by default.
- Allows full evidence export if authorized.

### Gateway

- Consumes same event stream internally.
- Applies platform-specific delivery policy.
- Does not send every event to chat; compacts progress.

## Context Manifest

Every LLM call should write a manifest:

```yaml
turn_id: ...
llm_call_id: ...
model: ...
max_context_tokens: ...
sources:
  - id: workspace_rules
    kind: instructions
    included: true
    tokens: 900
  - id: old_tool_output
    kind: tool_result
    included: false
    dropped_reason: budget_exhausted
summary_refs:
  - source_ids: [...]
    summary_event_id: ...
```

## Context Lane Budgets

Suggested starting budget:

- system/policy: reserved, never dropped,
- current user request: reserved, never dropped,
- safety/approval history: reserved within session,
- recent transcript tail: 25-35%,
- tool outputs: 15-25%,
- memory: 10-15%,
- skill instructions: 10-20%,
- external untrusted data: capped and fenced,
- subagent outputs: capped and summarized.

## Compression Rules

- Preserve all unresolved approvals.
- Preserve latest user correction verbatim.
- Preserve tool call/result pairs together.
- Preserve final errors and retry decisions.
- Summaries must include source event ids.
- Dropped sources must record why.

## Cancellation And Resume

Cancellation should be cooperative:

- Set cancellation token on turn.
- Long-running tools poll token.
- Non-cancellable tools are marked `cancel_requested` until done.
- Gateway/dashboard immediately show cancellation requested.
- Resume can continue from last stable event.

## Acceptance Tests

1. SSE reconnect replays missed events in order.
2. Context manifest records included and dropped sources.
3. Compression preserves tool call/result pairs.
4. Cancellation interrupts a cancellable tool.
5. Resume after process restart shows previous event timeline.


# 16 - Context Compression, Interrupt, And Concurrency Gap Matrix

## Status (2026-04-25)

Row-by-row remediation (verified file paths only):

- **Segment compression** — COMPLETED. `Nerya/nerya/llm/compression.py` (covered by `Nerya/tests/test_context_compression.py`). Builder side at `Nerya/nerya/agent/context_builder.py:build_context`.
- **Transcript invariants** — COMPLETED. `Nerya/nerya/agent/transcript_compact.py` (covered by `Nerya/tests/test_transcript_compact.py`).
- **Recovery / resumable turns** — COMPLETED. `Nerya/nerya/agent/recovery.py` (covered by `Nerya/tests/test_turn_recovery.py`).
- **FTS / regex session search** — COMPLETED. `Nerya/nerya/agent/session_search.py` exposed via `POST /agent/session/search` + `GET /agent/session/events` (`Nerya/nerya/api/routes_agent.py:292-378`).
- **Per-thread interrupt** — COMPLETED. `Nerya/nerya/harness/cancellation.py:CancelToken` + process-wide registry; `POST /agent/interrupt` flips it (Plan 05 §1).
- **Streaming events** — COMPLETED. `Nerya/nerya/agent/streaming.py` + `GET /agent/stream/events` (Plan 05 §1).
- **Cooperative cancellation in tool runner** — COMPLETED. `Nerya/nerya/harness/tool_runner.py` checks the cancel token between steps.

Open items (tracked under Plan 21):

- ~~Tool-result overflow spool (large output → reference artifact).~~ COMPLETED — runtime-level spool wired in `Nerya/nerya/skills/runtime.py:179-220` (`_maybe_spool_oversized`) backed by `Nerya/nerya/harness/result_store.py:ResultStore`. Threshold knob in `Nerya/nerya/core/config.py:113-120` (`agent.harness.result_overflow_threshold_bytes`, default 64 KiB; 0 disables). Journals add a `skill.call.overflow` row with the `ref_id`. Skills can opt out by adding the `no_overflow_spool` tag. Covered by `Nerya/tests/test_result_overflow_spool.py` (4 tests).
- Iterative rolling summary surfaced as a first-class context source.
- Pending-message queue replay UX in the gateway.
- Session split (`parent_session_id`) on compression overflow.

Status: PARTIALLY COMPLETED — foundational pieces and tool-result overflow spool ship; three open items above are still tracked under Plan 21.

## Why This File Exists

The prior audit still did not fully cover context compression management, message interruption, busy-session behavior, pending messages, cancellation, resume, and transcript invariants. These are core reasons an agent feels unreliable during real use.

## Code Evidence Summary

Nerya evidence:

- `nerya/llm/compression.py` implements dependency-free segment compression with token estimation, priority/droppable segments, truncation, and `ReferenceStore` for dropped or shrunk context segments.
- `tests/test_context_compression.py` covers segment-level compression/reference behavior.
- `tests/test_transcript_compact.py` covers transcript compaction invariants such as preserving tool-use/result pairs, system/pinned messages, skill envelopes, and protected turn ids.
- `nerya/agent/recovery.py` and `tests/test_turn_recovery.py` classify open/resumable turns from `turn_steps.jsonl`; budget-stopped turns can be resumable, LLM-error turns are not.
- `nerya/agent/working_memory.py` is per-turn in-process scratchpad and explicitly not persisted.
- `dashboard/components/chat/ChatView.tsx` has client-side `AbortController`, but this only aborts the HTTP request; it is not a backend cooperative cancellation protocol.

Hermes evidence:

- `agent/context_compressor.py` has a richer `ContextCompressor`: protects head/tail, protects token-budget tail, summarizes middle turns, prunes old tool results, truncates tool-call args, maintains previous summary for iterative compression, has anti-thrashing/cooldown, and generates structured summaries.
- `run_agent.py::_compress_context` performs pre-compression memory flush, calls external memory provider hooks, splits SQLite sessions with `parent_session_id`, propagates title, resets prompt-token estimates, clears file-read dedup cache, and warns on repeated compression.
- `hermes_state.py` stores sessions/messages in SQLite with WAL, FTS5 search, parent session chains, tool calls, reasoning fields, token counts, lock retry, and checkpoint behavior.
- `tools/tool_result_storage.py` persists oversized tool outputs and enforces aggregate per-turn output budget.
- `tools/interrupt.py` provides per-thread interrupt signaling for tools.
- `gateway/run.py` tracks running agents, pending messages, busy acknowledgements, session generations, pending approvals, and active process state.
- `tui_gateway/server.py` has `session.interrupt`, scoped pending prompt release, busy errors requiring interrupt before undo/compress/retry/model switch, and long-handler concurrency handling.

## Difference Matrix


| Surface                      | Nerya Current                                              | Hermes Current                                                                          | Gap                                                                         |
| ---------------------------- | ---------------------------------------------------------- | --------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Segment compression          | Exists in `nerya/llm/compression.py`                       | Exists plus richer runtime compressor                                                   | Nerya segment compression is not fully wired into live conversational turns |
| Transcript invariants        | Tests exist for compact transcript                         | Runtime compressor preserves tool/result detail and summaries                           | Nerya needs runtime enforcement and event manifest, not only tests          |
| Tool result overflow         | No Hermes-like large output persistence found in turn loop | `tools/tool_result_storage.py` persists oversized outputs and enforces aggregate budget | Nerya can still stuff too much into prompt/journal or lose detail           |
| Context manifest             | Not present as first-class artifact                        | Hermes has richer session DB and compressor logs, though not identical manifest         | Nerya needs source-level include/drop/summary refs per LLM call             |
| Iterative summaries          | Not clearly implemented                                    | Hermes stores `_previous_summary` and updates summaries                                 | Nerya compression risks losing long-run continuity                          |
| Pre-compression memory flush | Not clear                                                  | Hermes calls memory manager before compression                                          | Nerya may drop useful context without memory capture                        |
| Session split on compression | Not present                                                | Hermes creates child session with `parent_session_id`                                   | Nerya lacks lineage when compressed/resumed                                 |
| FTS session search           | Not equivalent                                             | Hermes `hermes_state.py` uses FTS5                                                      | Nerya session search is weaker/file-based                                   |
| Reasoning preservation       | Not equivalent                                             | Hermes persists reasoning and reasoning_details                                         | Nerya may lose model reasoning continuity/diagnostics                       |
| Client abort                 | Dashboard has fetch abort                                  | Hermes has agent/tool interrupt                                                         | Nerya abort may leave backend turn running                                  |
| Backend cancellation         | Not a clear cooperative cancellation token                 | Hermes has `tools/interrupt.py` and `session.interrupt` paths                           | Nerya needs cancellation token propagated to ToolRunner/skills/LLM/gateway  |
| Busy session behavior        | Not clearly managed per gateway session                    | Hermes tracks `_running_agents`, `_pending_messages`, busy ack debounce                 | Nerya may run overlapping turns for same chat/session                       |
| Pending message handling     | Not modeled                                                | Hermes queues/merges pending messages around interrupt                                  | Nerya lacks clear follow-up message semantics while a turn is running       |
| Interrupt vs append input    | Not modeled                                                | TUI has interrupt and pending-input semantics                                           | Nerya needs distinguish stop, replace, append-instruction, queue-next       |
| Approval interruption        | Not fully modeled                                          | Hermes/TUI releases pending prompts scoped to session                                   | Nerya needs approval waits to be cancellable and scoped                     |
| Recovery classification      | Has journal-aware open turn classification                 | Hermes has persistent session DB + runtime process state                                | Nerya recovery is useful but not enough to resume long tool sessions        |
| Concurrency locks            | Not clear for per-session turn execution                   | Hermes has per-session running agent maps and SQLite lock retry                         | Nerya needs per-session mutex and queue policy                              |
| Long-running tools           | ToolRunner has thread timeout                              | Hermes tools can check interrupt and process registry exists                            | Nerya timeout cannot kill non-cooperative work safely                       |
| Compression command UX       | Not surfaced                                               | Hermes TUI has `/compress`, busy guards, focus topic                                    | Nerya needs operator-triggered compact with explain/report                  |
| Retry/undo semantics         | Not equivalent                                             | Hermes TUI/gateway has retry/undo busy guards                                           | Nerya needs define retry/undo for turns and tool side effects               |


## Missing Semantics To Specify

### 1. Turn State Machine

Nerya should define explicit states:

- `queued`
- `planning`
- `running_subagents`
- `thinking`
- `awaiting_approval`
- `running_tool`
- `observing`
- `compressing_context`
- `cancel_requested`
- `cancelled`
- `failed`
- `completed`
- `abandoned`
- `resumable`

Each transition should append an event and be visible in dashboard/gateway.

### 2. Message Arrival While Busy

When a new message arrives for a session with an active turn, Nerya needs policy:

- `queue_next`: run after current turn.
- `interrupt_replace`: cancel current turn and run new message.
- `append_instruction`: inject user text into current turn at the next safe point.
- `reject_busy`: send busy ack and ask user to `/stop`.

Policy should depend on channel and command:

- `/stop` -> cancel.
- `/retry` -> reject unless idle or after cancel.
- normal message in DM -> queue or ask.
- reply to approval -> resolve approval.
- urgent correction -> interrupt if explicitly requested.

### 3. Cancellation Token Propagation

Cancellation should propagate through:

- AgentKernel loop,
- SubAgentDispatcher,
- LLMGateway calls where possible,
- ToolRunner,
- SkillRuntime and long-running skills,
- messaging/gateway status loop,
- scheduled-session runner.

Every cancellable function should either poll token or be marked non-cancellable.

### 4. Context Compression Policy

Nerya needs a live policy, not only standalone compression helpers:

- trigger threshold by estimated prompt tokens,
- preserve system/policy + latest user correction,
- preserve unresolved approvals,
- preserve active tool call/result pairs,
- preserve pending TODOs and blockers,
- summarize older turns with source event refs,
- persist dropped full content in ReferenceStore or artifact store,
- write context manifest with include/drop reasons.

### 5. Tool Result Management

For every tool/skill result:

- small result can be included inline,
- medium result should be summarized with artifact ref,
- large result must be persisted and replaced with preview,
- binary/attachment result should become artifact, not prompt text,
- prompt should include only redacted preview and source ref.

### 6. Approval Waits And Interrupts

If a turn is waiting for approval:

- `/stop` cancels the approval and turn,
- `/approve` resolves exact approval id,
- `/reject` sends rejection reason back into agent context,
- timeout marks approval expired,
- duplicate callback is idempotent,
- approval belongs to actor/session and cannot be stolen from another chat.

### 7. Resume And Replay

Nerya should support:

- replay event timeline,
- resume from last stable state,
- mark unsafe-to-resume steps,
- allow operator to abandon a turn,
- preserve compression/session lineage,
- preserve full evidence even after compression.

### 8. Scheduler Concurrency

Scheduled sessions need:

- overlap policy: skip, queue, cancel previous, or parallel allowed,
- per-schedule mutex,
- idempotency per tick,
- TTL cancellation token,
- delivery after cancellation/failure,
- missed tick behavior.

### 9. Frontend UX For These States

Dashboard should show:

- active turn state,
- stop button,
- queue length,
- pending approvals,
- compression happened banner,
- context used/dropped report,
- retry/abandon controls,
- event replay after refresh.

### 10. Gateway UX For These States

Telegram/gateway should support:

- busy ack debounce,
- `/stop`, `/retry`, `/queue`, `/status`, `/approve`, `/reject`, `/resume`,
- edited status message with current state,
- final cancellation notice,
- queued follow-up notice,
- no duplicate final messages after retry.

## Required Tests

### Context Compression Tests

1. Compression preserves tool call/result pairs in live turn context.
2. Compression preserves unresolved approval events.
3. Compression preserves latest user correction verbatim.
4. Dropped segment writes ReferenceStore artifact and manifest ref.
5. Repeated compression updates summary without duplicating stale instructions.
6. Large tool output becomes artifact preview.
7. Context manifest lists included/dropped/shrunk sources with reasons.

### Interrupt And Busy Tests

1. Dashboard stop cancels backend turn, not only fetch.
2. Telegram `/stop` cancels active turn and stops typing/status loop.
3. Normal message during active turn follows configured busy policy.
4. Reply to approval while busy resolves approval, not queued as normal chat.
5. Subagent receives cancellation token and stops.
6. ToolRunner marks non-cooperative timeout distinctly from cancelled.
7. Cancelled turn is not resumed automatically.
8. Retry after cancellation creates a new turn linked to prior turn.
9. Duplicate `/stop` and duplicate `/approve` are idempotent.
10. Scheduled session TTL cancels or marks abandoned consistently.

## Corrected Claim

No, the previous audit did not fully list context compression, interrupt, busy-session, pending-message, and resume semantics. It named context/session gaps, but it did not specify enough of the operational protocol.

This file adds the missing operational matrix. It should be implemented before powerful coding tools and before exposing gateway as a primary interface, because otherwise long-running work will remain fragile and confusing.
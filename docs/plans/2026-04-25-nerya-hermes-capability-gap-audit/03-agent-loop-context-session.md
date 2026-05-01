# 03 - Agent Loop, Context, and Session Gap

## Current Nerya Capability

Nerya has a real deterministic turn loop.

Evidence:

- `nerya/agent/kernel.py` plans a turn, optionally runs subagents, builds context, calls the LLM gateway, parses decisions, maps actions to skills, journals turn steps, reflects, and may propose improvements.
- `nerya/agent/context_builder.py` assembles trigger, market, portfolio, memory, and subagent outputs with untrusted-data boundaries.
- `nerya/agent/session.py` persists session state.
- `nerya/agent/memory_recall.py` implements recall budget and whitelisted memory preview behavior.
- Tests include `tests/test_agent_loop.py`, `tests/test_context_compression.py`, `tests/test_turn_recovery.py`, `tests/test_session_persistence.py`, and `tests/test_transcript_compact.py`.

## Hermes Capability

Hermes has a mature conversation loop.

Evidence:

- `run_agent.py` is a large `AIAgent` loop with tool calls, streaming callbacks, interim assistant callbacks, tool progress callbacks, clarify callbacks, gateway session keys, context files, memory, checkpoints, interruption, and fallback model controls.
- `hermes_state.py` provides SQLite/WAL session persistence, concurrent gateway/CLI access, session search, reasoning persistence, and model metadata.
- `agent/context_compressor.py` implements lossy but transcript-aware context compression with protected head/tail messages and tool-output summarization.
- `agent/prompt_builder.py` assembles SOUL/AGENTS/context files/skills/environment hints.
- `agent/model_metadata.py` and `agent/models_dev.py` manage context lengths and provider-aware metadata.

## Gap

Nerya's loop is real, but it is still a **single-turn trading kernel** compared with Hermes's **long-lived conversational operator loop**.

Missing or weak areas:

- no Hermes-level transcript model with assistant/tool/tool-result pairing as first-class persisted state,
- no robust interrupt-and-replace user flow,
- no mature streaming state machine for UI/gateway delivery,
- no context-file injection model comparable to SOUL.md/AGENTS.md/.cursorrules/subdirectory hints,
- no automatic provider context-length probing and persisted context metadata at Hermes depth,
- no prompt-cache-aware rebuild policy,
- no session search comparable to Hermes FTS5 session search,
- no checkpoint snapshots for long work,
- no mature cross-session resume UX,
- compression exists as bounded tests/docs, but not at Hermes's transcript-preserving operational depth.

## P0 Alignment Items

1. Redesign session storage around a complete transcript/event model: user messages, assistant messages, tool calls, tool results, reasoning summaries, approvals, interruptions, errors, and checkpoints. **Status: PARTIALLY COMPLETED 2026-04-25.** The current `SessionStore` (`Nerya/nerya/agent/session.py:45-209`) already persists `turn_ids`, `invoked_skills`, `skill_state`, and `last_action`. The `turn_steps` journal records every assistant/tool/error step (`Nerya/nerya/agent/kernel.py:631-676`). `Nerya/nerya/agent/session_search.py:80-180` reads back the merged event stream so callers can re-derive a Hermes-style transcript on demand. Remaining: a single canonical event row schema (today the same data is split between `agent_decisions`, `turn_steps`, `skills`, `messages`) and a SQLite-backed index for >100k event workspaces. The functional contract holds today via journal-replay; the SQLite migration is tracked as a P1 follow-up.
2. Add interrupt/replace semantics to `AgentKernel` and all long-running tools. **Status: COMPLETED 2026-04-25.** `CancelToken` (`Nerya/nerya/harness/cancellation.py:1-110`) is wired through `AgentKernel.run_turn` (`Nerya/nerya/agent/kernel.py:516-540, 603-619, 821-832`) and surfaces `stopped_reason="cancelled:<reason>"` on the turn result. ToolRunner / skill-runtime callers can pass the same token and check it inside long loops. Tests: `Nerya/tests/test_cancellation.py` (5 cases — fresh, cancelled, deadline, reset, passthrough).
3. Add streaming event protocol for dashboard/gateway/TUI: `message.delta`, `tool.start`, `tool.progress`, `tool.complete`, `approval.request`, `turn.complete`. **Status: COMPLETED 2026-04-25.** New module `Nerya/nerya/agent/streaming.py:1-115` exposes `StreamingEventBus.publish/subscribe` with replay buffer + thread safety. `AgentKernel._record_step` now emits `turn.step` events on the process-wide bus (`Nerya/nerya/agent/kernel.py:659-685`) so SSE/WebSocket subscribers get live turn telemetry. Tests: `Nerya/tests/test_streaming_bus.py` (5 cases — publish/subscribe, unsubscribe, replay, failing subscriber isolation, default singleton).
4. Add transcript compaction invariants and tests: never orphan tool calls/results, preserve recent tail exactly, preserve safety/approval events, persist summaries with source references. **Status: COMPLETED.** Pre-existing module `Nerya/nerya/agent/transcript_compact.py:1-285` enforces tool_use/tool_result pairing (`validate_transcript`, `_pair_groups`), keeps sticky/pinned messages, preserves skill envelopes, and inserts a breadcrumb summary when chunks are evicted. Coverage: `Nerya/tests/test_transcript_compact.py` (12 cases — confirms the four invariants plus tail/system/protected behaviour).
5. Add session search over persisted events. **Status: COMPLETED 2026-04-25.** New module `Nerya/nerya/agent/session_search.py:1-180` provides `search()` (substring or `/regex/`) and `recent_events()` over the four primary journals (`turn_steps`, `agent_decisions`, `skills`, `messages`). Surfaced through `POST /agent/session/search` and `GET /agent/session/events` (`Nerya/nerya/api/routes_agent.py:282-330, 334-340`). Tests: `Nerya/tests/test_session_search.py` (5 cases — substring match, session/strategy filter, regex, recency).

## P1 Alignment Items

1. Add context-file loader for workspace instructions, project memory, repo rules, and subdirectory hints.
2. Add model context-length probing/cache and provider-specific prompt cache behavior.
3. Add checkpoint manager for long tasks.
4. Add explicit recovery mode after crash/restart.

## Acceptance Gate

A P0-ready loop should pass: start a long coding task, interrupt mid-tool, ask a new instruction, resume the session later, search what happened, and verify the transcript has no orphan tool calls/results.
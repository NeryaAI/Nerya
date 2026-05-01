# 14 - Code Evidence Deep Dive

## Status (2026-04-25)

This document was a code-evidence inventory that drove plans 11/12/15/16. Each finding has been actioned:

1. **Local API auth** — COMPLETED. `Nerya/nerya/api/auth.py` adds `RequestContext` + per-route scope checks consumed by `Nerya/nerya/api/local_server.py:97-118` (Plan 11 status banner).
2. **Dashboard streaming** — BACKEND COMPLETED. `Nerya/nerya/agent/streaming.py` + `GET /agent/stream/events` + `POST /agent/interrupt` (Plan 05 §1, Plan 12 status banner). Frontend SSE renderer tracked under Plan 05 P1.
3. **Gateway acks / approvals** — COMPLETED on backend. `Nerya/nerya/api/gateway_commands.py:80-272` plus mirror outbox (`Nerya/nerya/messaging/mirror.py`) emit ack + progress + approval + final messages. Inline approval buttons tracked under Plan 15.
4. **Tool permissions** — COMPLETED. Per-skill `permissions/risk_gate/approval_gate/agent_query_only` (e.g. `Nerya/nerya/skills/builtin/operator_skill/skill.yml`). Subagent denylist (`Nerya/nerya/subagents/dispatcher.py:33-38`).
5. **Memory** — COMPLETED. `Nerya/nerya/memory/store.py` + `Nerya/nerya/memory/index.py` + `Nerya/nerya/skills/builtin/memory_skill` (Plan 06 §1).
6. **Context budget** — COMPLETED. `Nerya/nerya/agent/context_budget.py` (Plan 16 P0 §1).
7. **Cancellation** — COMPLETED. `Nerya/nerya/harness/cancellation.py` + kernel registration (Plan 12 status banner).

Status: COMPLETED — no further design work remains; remaining items are tracked in plans 15/16/20/21.

## Why This File Exists

The previous addenda were too design-heavy. This file records concrete findings from reading Nerya and Hermes code, so the gap analysis is tied to actual implementation rather than intuition.

## 1. Nerya Local API Is Really A Local Convenience Server

Evidence:

- `nerya/api/local_server.py` uses `BaseHTTPRequestHandler` and `ThreadingHTTPServer` directly.
- `_cors()` sets `Access-Control-Allow-Origin: `* and allows `GET, POST, OPTIONS`.
- `do_GET()` and `do_POST()` route directly to handlers after JSON parsing; there is no auth middleware, actor extraction, route-scope check, CSRF check, or audit event.
- Exception handling returns `500` with traceback tail in POST errors.

Implication:

- My earlier API/auth concern is not theoretical. It is visible in the current server shape.
- Before exposing dashboard/gateway beyond loopback, Nerya needs an auth middleware layer and route authorization matrix.

Concrete gap:

- Add `RequestContext(actor, auth_kind, scopes, request_id, source_ip)`.
- Add route metadata with required scopes.
- Wrap every handler call with auth, redaction, audit, and stable error mapping.

## 2. Nerya Dashboard Chat Is Not Streaming

Evidence:

- `dashboard/components/chat/ChatView.tsx` calls `callApi<TurnPayload>("/agent/run_turn", ...)`.
- The component creates a loading assistant message, waits for final JSON, then replaces the message.
- There is an `AbortController` ref for cancelling the fetch, but this is not a backend turn cancellation protocol.
- Chat threads are loaded/saved through browser local storage helpers from `dashboard/lib/chat.ts`, not through a server-side event/session stream.

Implication:

- The UI cannot show `tool.started`, partial assistant deltas, approval cards, or reconnectable progress from current code.
- The apparent agent freeze during long tasks is expected from the current architecture.

Concrete gap:

- Implement `/agent/stream` SSE and event replay before trying to polish chat UI.
- Move thread/session state from local-only browser storage into runtime session/event store.

## 3. Nerya Gateway Is A Useful Shape But Not A Full Hermes-Like Gateway

Evidence:

- `nerya/api/routes_gateway.py` has startup Telegram command sync and Telegram reply helpers.
- It uses `telegram.set_commands`, `telegram.send`, and `telegram.send_chat_action`.
- It has `_typing_until_done()` for repeated typing while a turn runs.
- `nerya/messaging/platforms.py` defines a Hermes-aligned platform catalog, but many entries are `webhook` or `scaffold` status rather than native adapters.
- `nerya/messaging/mirror.py` records inbound/outbound mirror entries and dedupes repeated message ids.

Implication:

- Nerya has a gateway skeleton and some Telegram affordances, not a mature multi-platform gateway product.
- Deduplication/mirroring exists, so future work should extend it rather than invent a separate event store.

Concrete gap:

- Build around `GatewayMirror`, but add outbound delivery queue, platform renderer, idempotency key, edit-message support, dead-letter queue, and actor/session auth.

## 4. Hermes Gateway Has Much More Runtime Machinery

Evidence:

- `gateway/run.py` tracks running agents per session, pending messages, session timestamps, delivery routing, platform configs, and interrupt logic.
- It has helpers for parsing session keys and preserving platform/thread context.
- It handles media placeholders so media-only events are not silently dropped.
- `tools/send_message_tool.py` includes platform-specific sending logic; Telegram sending has parse-mode handling and fallback to plain text.
- `tools/cronjob_tools.py` preserves origin platform/chat/thread for delivery and explicitly warns that omitting thread id can lose topic targeting.

Implication:

- Hermes's gateway quality is not just more platforms. It has session lifecycle, interrupt handling, media handling, delivery routing, and platform-specific rendering.

Concrete Nerya parity target:

- Match the gateway state machine first for Telegram before adding more platform ids.

## 5. Nerya Context Is Fenced But Not Manifested

Evidence:

- `nerya/agent/context_builder.py` wraps chat payloads and trigger payloads with `wrap_untrusted(...)`.
- It adds strategy limits, known strategies, trade defaults, market snapshot, portfolio snapshot, memory preview, and subagent outputs into one prompt string.
- `_safe_json()` truncates data by character length.
- `nerya/agent/memory_recall.py` has scoring, TTL, top-k, max-char budgets, and `explain_recall()` with dropped reasons.
- `nerya/agent/working_memory.py` is explicit in-process per-turn scratchpad and is intentionally not persisted.

Implication:

- Nerya is doing some correct prompt-injection fencing and recall explanation.
- The missing part is not basic context construction; it is a durable context manifest per LLM call, lane budgets, and transcript/tool-result compaction invariants.

Concrete gap:

- Add `context_manifest.jsonl` recording source id, lane, included, tokens/chars, truncation, dropped reason, and summary refs.
- Reuse `explain_recall()` rather than building a separate memory explanation path.

## 6. Hermes Context/Session Is Operationally Deeper

Evidence:

- `run_agent.py` wires `ContextCompressor`, memory context blocks, prompt builder, context-file prompts, streaming callbacks, checkpoint options, and tool-result storage.
- `tools/tool_result_storage.py` persists oversized outputs and enforces aggregate per-turn output budget.
- `tools/interrupt.py` provides per-thread interrupt signaling.
- `hermes_state.py` persists messages with tool calls, reasoning fields, token counts, and session metadata, and can reconstruct conversation history.

Implication:

- Hermes handles long-running, tool-heavy sessions. Nerya currently has turn-step journals and session summaries, but not this full conversation replay + large tool output discipline.

Concrete Nerya parity target:

- Event-sourced transcript with tool call/result pairs and persisted large outputs is more important than more prompt templates.

## 7. Nerya ToolRunner Is A Good Chokepoint But Too Narrow

Evidence:

- `nerya/harness/tool_runner.py` enforces turn budget, timeout, retries, error classification, and returns structured `ToolCallRecord` instead of raising for budget/timeout.
- It only calls `SkillRuntime.call(...)`; it is not a registry for general terminal/file/browser tools.
- Timeout uses a daemon thread and cannot truly kill non-cooperative work.

Implication:

- Nerya already has a good central place to insert permissions, cancellation tokens, and event emission.
- But if general tools are implemented outside this chokepoint, the safety model will fragment.

Concrete gap:

- Extend `ToolRunner.call()` or add a sibling `OperatorToolRunner` with the same budget/retry/event/permission model.
- Add cooperative cancellation tokens instead of only thread join timeout.

## 8. Nerya Secret Vault Has Scope Checks But Weak Production Defaults

Evidence:

- `nerya/security/secrets.py` encrypts secrets and never exposes reveal via `routes_security.py`.
- `SecretVault.resolve(name, required_scope=...)` checks declared secret scope.
- Default passphrase falls back to `NERYA_VAULT_PASSPHRASE` or `nerya-default-passphrase`.
- `routes_security.py` has CRUD endpoints with no route auth layer in the local server.

Implication:

- Secret handling is better than a plain `.env`, but production mode must forbid the default passphrase and must protect secret CRUD routes.

Concrete gap:

- `preflight(canary_live/full_live)` should fail if the vault uses default passphrase.
- Secret CRUD must require admin/auth scope even in dashboard.

## 9. Nerya Skill Runtime Has Manifest-Level Discipline

Evidence:

- `nerya/skills/runtime.py` loads action specs, validates payload schema, checks `subagent_only`, constructs `SkillCallContext`, journals start/done/error, and records devmode tool calls.
- It passes typed errors through and wraps unexpected exceptions.

Implication:

- The skill runtime is a strength and should be reused for operator tools where possible.
- The gap is not schema validation; it is broader permission classes, user skill lifecycle, and procedural skill invocation.

Concrete gap:

- Add manifest fields for `tool_permissions`, `secret_scopes`, `path_scopes`, `network_scopes`, and `approval_policy`.

## 10. Hermes Tool Registry And Toolsets Are Much Richer

Evidence:

- `tools/registry.py` discovers self-registering tools and stores schema, handler, toolset, availability checks, aliases, and MCP dynamic tools.
- `model_tools.py` filters tools by enabled/disabled toolsets and bridges sync/async tool handlers.
- Hermes has explicit legacy toolset maps and availability checks.

Implication:

- Nerya's skill registry and Hermes's tool registry solve different layers. Nerya needs both: skill actions for domain safety and operator tools for general agency.

Concrete gap:

- Add a Nerya tool capability registry that can expose tools to LLM prompts with schema and availability, while dispatch still goes through Nerya policy.

## 11. Nerya Current Docs Should Distinguish “Shape Exists” From “Product Quality Exists”

Evidence:

- Platform catalog contains many Hermes ids, but statuses include `webhook` and `scaffold`.
- API routes exist for gateway inbound/send, but there is no visible full auth/session/delivery state machine comparable to Hermes.
- Context/memory modules exist, but no durable context manifest or stream replay endpoint exists.

Implication:

- The docs must use states like `native`, `webhook`, `scaffold`, `shape-only`, `production-ready` instead of just `implemented`.

Concrete gap:

- Capability matrix should include a `product_quality` column: `runtime`, `operator_usable`, `production_hardened`.

## Corrected Priority After Code Reading

1. Build request/auth/actor layer before exposing more surfaces.
2. Build event store and streaming before frontend polish.
3. Extend existing `ToolRunner`/`SkillRuntime` safety model instead of bypassing it for new tools.
4. Make Telegram excellent before adding more gateway platform breadth.
5. Add context manifest using existing memory recall explanation and turn-step journal.
6. Add large output persistence and transcript replay before coding-agent ambitions.
7. Harden SecretVault production defaults and route auth.
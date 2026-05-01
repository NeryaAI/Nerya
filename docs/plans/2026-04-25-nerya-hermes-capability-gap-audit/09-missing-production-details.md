# 09 - Missing Production Details Addendum

## Status (2026-04-25)

This document is a requirements addendum that maps onto Plans 11
(auth + tool permissions), 12 (context+streaming+state), 13
(overlooked surfaces), 15 (gateway protocol matrix), 16 (compression
+ interrupt + concurrency), 17–21 (parity backlogs). Section-by-
section status:

- **§1 API Auth + Authz**: PARTIALLY COMPLETED — `Nerya/nerya/api/auth.py` runs `auth_mod.check_request` on every API request (`Nerya/nerya/api/local_server.py:97-109`); route-level scope tagging is tracked in Plan 11.
- **§2 Tool Permission Management**: PARTIALLY COMPLETED — skill manifests ship `risk_gate`/`approval_gate`/`agent_query_only`, the dispatcher denylist (`Nerya/nerya/subagents/dispatcher.py:33-38`) narrows subagents, and operator-skill chroot (`Nerya/nerya/skills/builtin/operator_skill/actions.py:62-88`) enforces path scope. Per-actor caller types tracked in Plan 11.
- **§3 Streaming Protocol**: COMPLETED for the backend — `Nerya/nerya/agent/streaming.py` (`StreamingEventBus`), `GET /agent/stream/events`, `POST /agent/interrupt`, kernel publish on `_record_step` (`Nerya/nerya/agent/kernel.py:659-705`). SSE/WebSocket delivery is tracked.
- **§4 Telegram + Gateway Quality**: PARTIALLY COMPLETED — gateway command registry, mirror-based idempotency, MarkdownV2 escaping in `Nerya/nerya/messaging/`. Inline buttons + chunking tracked in Plan 15.
- **§5 Context Management**: PARTIALLY COMPLETED — `Nerya/nerya/agent/context_builder.py` + `Nerya/nerya/agent/memory_recall.py::explain_recall` produce dropped-reason explanations; structured context manifest tracked in Plan 12.
- **§6 Error/Recovery/Idempotency**: PARTIALLY COMPLETED — `Nerya/nerya/agent/recovery.py` lists open turns, mirror dedupes inbound webhooks, journal writes are append-only.
- **§7 Observability**: PARTIALLY COMPLETED — every turn writes `turn_steps` + `agent_decisions` + `skills` journals (`Nerya/nerya/agent/kernel.py`); `POST /agent/session/search` makes them searchable; `Nerya/nerya/observability/trace.py` renders per-turn timelines.
- **§8 Multi-User/Tenant Boundaries**: PARTIALLY COMPLETED — gateway mirror keys by chat id; isolation between chats is enforced. Full actor model tracked in Plan 11/20.
- **§9 Human Approval UX**: PARTIALLY COMPLETED — `Nerya/nerya/trading/approval_gate.py` writes structured approval records; gateway-side inline buttons tracked in Plan 15.
- **§10 Testing/Release Gates**: PARTIALLY COMPLETED — 1185+ tests, gateway/skill/streaming/cancel coverage all green. Gateway E2E with fake transport tracked.

## Why This Addendum Exists

The first audit identified the big capability gaps, but it was still too high-level. Real usability depends on many lower-level contracts that are easy to miss: authentication, API authorization, Telegram delivery quality, streaming semantics, context budgets, tool permissions, rate limits, observability, recovery, and data retention.

This addendum lists the additional surfaces that must be designed before implementation begins. These are not optional polish. They are what make an agent feel reliable instead of random.

## Missing Detail Areas

### 1. API Authentication And Authorization

Current Nerya local API is a convenience server. `nerya/api/local_server.py` has permissive CORS and no visible request authentication layer. `routes_security.py` protects secret reveal semantics but not route access. That is acceptable for a local prototype, not for a dashboard/gateway/runtime service.

Required details:

- API auth modes:
  - `local_no_auth` for loopback-only development,
  - `dashboard_session` for browser UI,
  - `gateway_webhook_signature` for Telegram/Slack/Discord callbacks,
  - `service_token` for scripts/SDK/automation,
  - `operator_admin` for secret/config/approval routes.
- Route-level authorization matrix:
  - read-only routes,
  - chat/turn routes,
  - trading routes,
  - approval routes,
  - secret management routes,
  - gateway inbound routes,
  - admin/config routes.
- CSRF protection for browser sessions.
- CORS allowlist, not `*`, outside local mode.
- Token rotation and revocation.
- Audit log for every privileged request.

P0 acceptance:

- `/security/secrets/*`, `/trading/*`, `/approvals/*`, `/gateway/inbound`, and `/agent/run_turn` require an explicit auth context outside loopback local mode.
- Every request gets `request_id`, `actor`, `auth_kind`, `scope`, and `decision` in an audit journal.

### 2. Agent Tool Permission Management

Current Nerya has skill caller policy and trading approvals, but it lacks a full tool permission model for general operator tools.

Required details:

- Permission dimensions:
  - caller identity: agent, subagent, script, gateway user, dashboard user, schedule,
  - tool class: read, write, shell, browser, network, secret, trading, deploy,
  - resource path/domain/account,
  - risk level: safe, review, dangerous, forbidden,
  - execution mode: dry-run, proposal-only, apply.
- Policy source order:
  1. hard safety denylist,
  2. workspace policy,
  3. skill manifest policy,
  4. route/session grant,
  5. one-time approval.
- Approval record must include exact tool args hash and expiry.
- Subagents should inherit a narrowed permission set, never the full parent set by default.

P0 acceptance:

- A subagent cannot write outside its assigned scope.
- A gateway user cannot trigger shell/write/trading tools unless explicitly authorized.
- A skill cannot access secrets unless its manifest declares the needed secret scope and the runtime grants it.

### 3. Streaming Protocol For Frontend And Gateways

Current dashboard chat posts to `/agent/run_turn` and receives a final response. That makes long tasks feel frozen.

Required details:

- Transport choices:
  - SSE for dashboard/web first,
  - WebSocket later if bidirectional interrupt/edit is needed,
  - polling fallback for simple gateway workers.
- Event contract:
  - `turn.started`,
  - `message.delta`,
  - `thinking.summary`,
  - `tool.started`,
  - `tool.progress`,
  - `tool.output_ref`,
  - `tool.completed`,
  - `approval.requested`,
  - `approval.resolved`,
  - `memory.used`,
  - `error`,
  - `turn.completed`,
  - `turn.cancelled`.
- Each event must carry `turn_id`, `session_id`, `seq`, `ts`, `actor`, and `visibility`.
- The frontend must be able to reconnect using `Last-Event-ID` and recover missed events from the event store.
- Sensitive events must be redacted before leaving backend.

P0 acceptance:

- Long task UI shows real-time tool progress, can reconnect without duplicating rows, and can display final evidence even after page refresh.

### 4. Telegram And Gateway Message Quality

Current Nerya Telegram support sends replies and typing actions, but a good gateway needs much more than `sendMessage`.

Required details:

- Message rendering:
  - MarkdownV2/HTML escaping,
  - chunking long replies under platform limits,
  - threaded/reply-to behavior,
  - code block preservation,
  - tool progress compaction,
  - final summary vs verbose evidence link split.
- Progressive delivery:
  - send initial acknowledgement quickly,
  - periodically update/edit a status message,
  - send final result with buttons/commands,
  - avoid flooding chat with every tool event.
- Telegram-specific UX:
  - `/help`, `/menu`, `/status`, `/stop`, `/resume`, `/approve`, `/reject`, `/memory`, `/skills`, `/sessions`,
  - inline keyboard for approval and cancel,
  - `sendChatAction` heartbeat while working,
  - message edit fallback when edit fails,
  - media/document attachments for evidence artifacts.
- Reliability:
  - idempotency key per outbound message,
  - retry with backoff,
  - rate limit queue per chat,
  - dead-letter outbox,
  - delivery receipts in journal.
- Security:
  - allowed chat/user list,
  - webhook signature or polling token guard,
  - command authorization by user role.

P0 acceptance:

- A Telegram user sees a fast acknowledgement, compact progress updates, can stop/approve from inline buttons, and receives a final result without broken markdown or message spam.

### 5. Context Management Details

Current Nerya context builder fences untrusted input and includes memory/market/portfolio state. It still needs a formal context budget architecture.

Required details:

- Context lanes:
  - system policy,
  - workspace instructions,
  - user request,
  - session summary,
  - recent transcript tail,
  - tool results,
  - memory recalls,
  - skill instructions,
  - subagent outputs,
  - external untrusted data,
  - verification requirements.
- Budget policy per lane:
  - hard-reserved tokens,
  - max age,
  - source priority,
  - summarization strategy,
  - drop reason.
- Compression invariants:
  - never orphan tool calls and tool results,
  - preserve approvals and safety decisions,
  - preserve user corrections,
  - preserve unresolved TODOs/blockers,
  - attach source refs to summaries.
- Context inspection:
  - operator can view what was included/excluded,
  - each turn stores a context manifest,
  - prompt text can be debug-gated and redacted.

P0 acceptance:

- For every turn, Nerya can explain which context sources were used, which were dropped, why they were dropped, and whether any critical source was missing.

### 6. Error Handling, Recovery, And Idempotency

Required details:

- Stable error taxonomy across API, agent, tools, gateway, and dashboard.
- Idempotency keys for:
  - tool calls,
  - outbound gateway sends,
  - trading intents,
  - approval actions,
  - schedule ticks.
- Retry policy by error class:
  - transient network,
  - rate limit,
  - provider timeout,
  - permission denied,
  - validation error,
  - operator cancelled.
- Crash recovery:
  - resume incomplete turn,
  - mark abandoned tool calls,
  - reconcile outbox deliveries,
  - notify operator.

P0 acceptance:

- Refreshing the dashboard or retrying a gateway webhook cannot duplicate a trade, duplicated approval, or duplicated Telegram final answer.

### 7. Observability And Debuggability

Required details:

- Correlation IDs: `request_id`, `turn_id`, `session_id`, `tool_call_id`, `approval_id`, `gateway_message_id`.
- Journals:
  - API access,
  - auth decisions,
  - agent events,
  - tool calls,
  - gateway sends,
  - memory reads/writes,
  - context manifests,
  - errors.
- Operator surfaces:
  - per-turn timeline,
  - raw event JSON download,
  - redacted prompt/context viewer,
  - dead-letter queue viewer,
  - retry/replay controls.
- Metrics:
  - turn latency,
  - tool latency,
  - provider cost/tokens,
  - gateway delivery success,
  - approval wait time,
  - cancellation count,
  - memory hit rate.

P0 acceptance:

- When a turn fails, the operator can answer: what request came in, who authorized it, what context was used, what tool failed, whether retry happened, and what user saw.

### 8. Multi-User And Tenant Boundaries

Even if Nerya starts as local-first, gateway usage immediately creates multiple actors.

Required details:

- Actor model:
  - owner,
  - admin,
  - operator,
  - viewer,
  - gateway user,
  - service account,
  - schedule.
- Resource ownership:
  - sessions,
  - memories,
  - secrets,
  - strategy permissions,
  - gateway channels,
  - approvals.
- Isolation:
  - one Telegram chat should not see another chat's memory/session,
  - one strategy should not leak another strategy's private memory,
  - scheduled jobs should run under explicit service identity.

P1 acceptance:

- Two gateway users can talk to Nerya without session/memory/approval leakage.

### 9. Human Approval UX

Required details:

- Approval cards show:
  - what action,
  - why needed,
  - exact args diff/summary,
  - risk level,
  - actor,
  - expiry,
  - one-time vs persistent grant.
- Approval channels:
  - dashboard,
  - Telegram inline buttons,
  - CLI/TUI prompt,
  - API.
- Approval scopes:
  - one tool call,
  - one turn,
  - one session,
  - one skill,
  - one route/schedule.
- Rejection feedback loops into the agent context.

P0 acceptance:

- The operator can approve/reject a dangerous action from dashboard or Telegram, and the agent receives the decision without losing the turn.

### 10. Testing And Release Gates

Required details:

- Gateway E2E with fake Telegram transport:
  - inbound command,
  - progress update,
  - approval button,
  - final message,
  - retry/dead-letter.
- Streaming E2E:
  - SSE reconnect,
  - event ordering,
  - cancellation,
  - redaction.
- Auth E2E:
  - unauthenticated blocked,
  - insufficient scope blocked,
  - valid token allowed,
  - audit entry written.
- Context E2E:
  - source manifest,
  - compression invariant,
  - dropped reason.
- Permission E2E:
  - subagent write-scope violation blocked,
  - skill secret-scope violation blocked,
  - gateway user shell attempt blocked.

P0 acceptance:

- These tests become the first release gate before adding more agent features.
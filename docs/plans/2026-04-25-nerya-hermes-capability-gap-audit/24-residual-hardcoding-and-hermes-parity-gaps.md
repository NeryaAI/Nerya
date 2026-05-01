# 24 - Residual Hardcoding And Hermes Parity Gaps

## Status (2026-04-25)

Section status:

1. **Telegram protocol parity** — PARTIALLY COMPLETED. `Nerya/nerya/api/gateway_identity.py` replaces ad-hoc `tg_reply_*` ids; `Nerya/nerya/messaging/platforms.py` carries `support_level=tested` for Telegram and the other platforms have honest support markers. Inline buttons / album buffering / media ingestion → Plan 21 P0/P1.
2. **Context / session details** — COMPLETED foundationally. Compression (`Nerya/nerya/llm/compression.py`), transcript invariants (`Nerya/nerya/agent/transcript_compact.py`), recovery (`Nerya/nerya/agent/recovery.py`), search (`Nerya/nerya/agent/session_search.py`), interrupt (`Nerya/nerya/harness/cancellation.py` + `POST /agent/interrupt`). Session branching on overflow → Plan 16/21.
3. **Hardcoded product behaviour** — COMPLETED for prompt + gateway commands; remaining items (chat suggestions, dashboard wrappers, default planner routes) → Plan 23 P1/P2.
4. **Security / control details** — PARTIALLY COMPLETED. `Nerya/nerya/api/auth.py` enforces tokens; per-route scopes + dashboard proxy auth forwarding → Plan 11 / Plan 20 P1.
5. **Skill-first mismatch** — COMPLETED for prompt and gateway-help; full UI/extension parity → Plan 23 P1/P2.

Status: PARTIALLY COMPLETED — every backend foundation is wired; remaining work is platform-specific renderers + dashboard refactors tracked in Plans 11/16/21/23.

This pass is intentionally **not** a generic wishlist. It lists additional code-backed gaps and hardcoded surfaces that were still under-specified after `00-23`, especially around Telegram/gateway UX, context/session management, permission boundaries, streaming, and skill-driven capability loading.

## Scope Of This Addendum

Earlier files already covered the big categories. This addendum adds smaller but operator-visible details that explain why Nerya still feels much worse than Hermes:

- Gateway protocol details: attachments, reply references, threads, batching, long-message splitting, rich buttons, callback authorization, retry/fallback behavior.
- Context/session details: compression quality, reference retrieval, session branching, interruption, prompt/tool result compaction.
- Hardcoded product behavior: static action routing, static gateway commands, static UI wrappers, hardcoded examples and templates.
- Security/control details: API auth exists but route scopes and tool grants remain coarse; dashboard proxy does not forward auth.
- Skill-first mismatch: many capabilities are described in code/prompt/config rather than declared by builtin skill metadata and loaded into context only when selected.

## 1. Telegram Is Still A Minimal Text Bot, Not A Hermes-Grade Gateway

### Nerya Evidence

- `nerya/api/routes_gateway.py:130` builds outbound Telegram replies through `_reply(client, cfg, chat_id, text)` with only `chat_id` and `text`.
- `nerya/api/routes_gateway.py:132` fabricates `message_id` as `tg_reply_{chat_id}_{time}` instead of using the platform returned message id.
- `nerya/api/routes_gateway.py:205` handles only text commands and a text turn.
- `nerya/api/routes_gateway.py:208` derives session id as `telegram_{chat_id}`.
- `nerya/api/routes_gateway.py:240` mirrors inbound payload as only `text` and `update_id`.
- `nerya/api/routes_gateway.py:259` passes `payload: {text, channel, chat_id}` to the agent.
- `nerya/messaging/telegram.py:26` only defines `sendMessage` plus command/update/chat-action APIs.
- `nerya/messaging/telegram.py:48` sends only `chat_id` and `text`, with optional parse mode and link preview.
- `nerya/messaging/platforms.py:36` marks Telegram as `attachments=True, voice=True`, but the concrete Telegram sender/receiver does not actually implement attachment or voice ingestion.

### Hermes Evidence

- `hermes-agent/gateway/platforms/telegram.py:206` explicitly documents markdown, forum topics, and media messages.
- `hermes-agent/gateway/platforms/telegram.py:212` encodes Telegram's 4096 message length limit.
- `hermes-agent/gateway/platforms/telegram.py:225` has configurable reply-to behavior.
- `hermes-agent/gateway/platforms/telegram.py:227` to `hermes-agent/gateway/platforms/telegram.py:239` buffers photo albums and text splits so rapid platform updates do not self-interrupt turns.
- `hermes-agent/gateway/platforms/telegram.py:244` tracks DM topic thread ids.
- `hermes-agent/gateway/platforms/telegram.py:250` keeps inline approval button state.
- `hermes-agent/gateway/platforms/telegram.py:254` authorizes callback users.
- `hermes-agent/gateway/platforms/telegram.py:989` to `hermes-agent/gateway/platforms/telegram.py:1016` sends replies with `reply_to_message_id` and `message_thread_id` and falls back when markdown/reply fails.
- `hermes-agent/gateway/platforms/telegram.py:1196` to `hermes-agent/gateway/platforms/telegram.py:1206` sends inline yes/no buttons.
- `hermes-agent/gateway/platforms/telegram.py:2552` to `hermes-agent/gateway/platforms/telegram.py:2677` handles photos, voice, and documents by caching media for agent processing.
- `hermes-agent/gateway/platforms/telegram.py:2974` to `hermes-agent/gateway/platforms/telegram.py:2997` preserves reply target id/text into the event.

### Missing Nerya Work

- Replace `_reply(text)` with a platform send envelope supporting `reply_to_message_id`, `thread_id`, `parse_mode`, link-preview policy, inline buttons, edit/delete, and chunk metadata.
- Replace `tg_reply_{chat_id}_{time}` with actual Bot API response ids and a durable platform-id mapping table.
- Parse Telegram updates into a canonical `GatewayEvent` containing platform update id, message id, chat id, user id, thread id, reply target id/text, caption, media group id, attachments, and raw payload reference.
- Add media download/cache support for images, documents, voice/audio, captions, file size limits, MIME allowlists, and secure local reference ids.
- Add text split batching and album batching so Telegram client-side chunking or photo bursts do not become multiple competing turns.
- Add topic/thread-aware sessions for group/forum Telegram chats instead of `telegram_{chat_id}`.
- Add inline approval/action callbacks with callback-user authorization and replay-safe callback ids.
- Make `GatewayPlatformSpec.attachments=True` truthful only when concrete adapter support is present; otherwise expose `declared_only` / `unsupported`.

## 2. Gateway Identity And Dedupe Are Too Weak

### Nerya Evidence

- `nerya/messaging/mirror.py:23` claims idempotency by `message_id`.
- `nerya/messaging/mirror.py:99` and `nerya/messaging/mirror.py:109` accept optional caller-provided ids but do not enforce a platform identity schema.
- `nerya/messaging/mirror.py:215` falls back to `new_id("msg")` when no id is passed.
- `nerya/api/routes_gateway.py:244` records Telegram inbound with `payload={text, update_id}` but no canonical platform message id or reply/thread ids.
- `nerya/api/routes_gateway.py:379` in generic inbound falls back to `conversation_id`, `user_id`, or `default`, which can merge unrelated users into one session.

### Hermes Evidence

- `hermes-agent/gateway/platforms/base.py:25` to `hermes-agent/gateway/platforms/base.py:37` even handles UTF-16 length because platform identity/limits are platform-specific, not generic strings.
- `hermes-agent/gateway/platforms/telegram.py:266` to `hermes-agent/gateway/platforms/telegram.py:277` normalizes thread ids for send/typing.
- `hermes-agent/gateway/platforms/telegram.py:2928` reads `message_thread_id` and `hermes-agent/gateway/platforms/telegram.py:2974` reads reply targets.
- `hermes-agent/tests/gateway/test_yolo_command.py` verifies session-scoped command state for separate Telegram chats.

### Missing Nerya Work

- Introduce `GatewayIdentity` with `platform`, `tenant/workspace`, `chat/conversation`, `thread/topic`, `user`, and `mode` fields.
- Introduce `GatewayEventId` based on platform update/message ids, not wall-clock timestamps.
- Persist bidirectional mapping: platform event -> Nerya event -> turn -> outbound platform message(s).
- Make dedupe operate on platform event id plus idempotency keys, not ad hoc generated ids.
- Stop using `default` as a silent generic inbound session when no user/conversation id exists; reject or require explicit identity.

## 3. Gateway Command Registry Is Still Not Unified Enough

### Nerya Evidence

- `nerya/api/routes_gateway.py:210` hardcodes `/start`, `/help`, `/menu`, `/commands`.
- `nerya/api/routes_gateway.py:214` hardcodes `/status` copy and dashboard URL.
- `nerya/api/routes_gateway.py:224` hardcodes `/new` behavior.
- `nerya/api/routes_gateway.py:234` hardcodes `/trace` behavior.
- `nerya/api/gateway_events.py:136` to `nerya/api/gateway_events.py:164` hardcodes progress text and emojis per agent phase.

### Hermes Evidence

- `hermes-agent/AGENTS.md:150` to `hermes-agent/AGENTS.md:171` documents a central `COMMAND_REGISTRY` where CLI, gateway help, Telegram menu, Slack mapping, and autocomplete derive from one place.
- `hermes-agent/gateway/platforms/telegram.py:843` handles Telegram command menu limits and truncation.

### Missing Nerya Work

- Replace route-local command dispatch with a manifest/registry-driven command system.
- Let commands declare platform availability, aliases, auth scope, handler, help text, menu text, and button/callback behavior.
- Generate Telegram BotCommand menu, gateway help, dashboard command palette, and CLI help from the same registry.
- Move progress text rendering into configurable renderer templates or skill-provided event renderers.

## 4. Context Compression Exists But Is Still Mechanically Shallow

### Nerya Evidence

- `nerya/llm/compression.py:5` uses `~chars / 4` token estimation and avoids tokenizer dependency.
- `nerya/llm/compression.py:77` drops/shrinks segments by priority and rough token budget.
- `nerya/llm/compression.py:95` drops lowest-priority droppable segments first.
- `nerya/llm/compression.py:107` shrinks middle segments by truncation.
- `nerya/llm/compression.py:128` may shrink non-droppable segments as a last resort.
- `nerya/llm/compression.py:12` to `nerya/llm/compression.py:15` stores dropped segments in references, but the prompt/action loop still lacks a Hermes-like retrieval and summarization workflow for those refs.

### Hermes Evidence

- `hermes-agent/hermes_state.py:5` to `hermes-agent/hermes_state.py:13` uses SQLite sessions, FTS5 search, token counters, and compression-triggered session splitting with parent chains.
- `hermes-agent/tui_gateway/server.py:537` to `hermes-agent/tui_gateway/server.py:552` compresses session history in the live TUI session and increments history version.
- `hermes-agent/tools/tool_result_storage.py` is a dedicated surface for large tool result storage and retrieval.
- `hermes-agent/tools/web_tools.py:2065` documents LLM summarization and output caps for large web/PDF extraction.

### Missing Nerya Work

- Replace pure truncation with summarization-aware compression for transcript, tool results, attachments, and retrieved docs.
- Track compression provenance: original ref id, summary model, summary prompt, token delta, and whether user-visible facts were lost.
- Add first-class `load_context_ref` / `search_session` / `expand_tool_result` actions so the model can recover dropped content.
- Add per-model tokenizer/context windows rather than one rough heuristic.
- Add parent/child session splitting when compression fundamentally changes context.
- Add tests for “tool result too large”, “attachment too large”, “resume after compression”, and “reply after compressed reference expansion”.

## 5. Interruption, Replacement, And Queue Semantics Are Not Hermes-Level

### Nerya Evidence

- `dashboard/components/chat/ChatView.tsx:233` has a local `cancel()` UI handler, but this is frontend-local and does not imply runtime-level session interruption.
- `nerya/api/routes_agent.py:128` runs an agent turn synchronously through `kernel.run_turn`.
- `nerya/harness/tool_runner.py:52` to `nerya/harness/tool_runner.py:68` uses a daemon thread timeout, but no cooperative cancellation token is passed into skills/tools.
- `nerya/api/routes_gateway.py:246` starts a typing thread and stops it after `kernel.run_turn`; no interrupt/replacement path is wired into gateway turns.

### Hermes Evidence

- `hermes-agent/tui_gateway/server.py:42` to `hermes-agent/tui_gateway/server.py:49` explicitly separates long handlers so `approval.respond` and `session.interrupt` are not blocked behind long work.
- `hermes-agent/tui_gateway/server.py:351` to `hermes-agent/tui_gateway/server.py:354` scopes prompt cancellation to the interrupted session only.
- `hermes-agent/ui-tui/src/app/useMainApp.ts:383` to `hermes-agent/ui-tui/src/app/useMainApp.ts:385` drains queued messages when a busy session settles after interrupt, shell exec, or recovery.
- `hermes-agent/tools/interrupt.py` provides an explicit interruption surface.

### Missing Nerya Work

- Introduce turn-level cancellation tokens that propagate into `AgentKernel`, `ToolRunner`, `SkillRuntime`, LLM calls, subagents, gateway typing loops, and background jobs.
- Add `/agent/interrupt`, `/agent/replace`, and gateway `/stop` or callback-based cancel endpoints.
- Persist turn state transitions: queued, running, interrupt_requested, interrupted, replaced, completed, failed.
- Define replacement semantics: whether a new message cancels the active turn, queues behind it, or branches a new session.
- Ensure interrupt cannot cancel another user/session's pending approval or secret prompt.

## 6. Dashboard Chat Still Does Not Stream Like Hermes TUI

### Nerya Evidence

- `dashboard/app/api/proxy/[...path]/route.ts:7` to `dashboard/app/api/proxy/[...path]/route.ts:30` forwards requests and returns a single response body; it is not SSE/WebSocket streaming.
- `dashboard/lib/clientApi.ts:548` onward defines `runTurn` style response types with final `reply_text`, `events`, and `tool_trace`, not a streaming event protocol.
- `dashboard/components/chat/ChatView.tsx:58` claims every message drives one real turn, but the user sees mostly request/response state rather than live event deltas.
- `nerya/api/routes_agent.py:147` to `nerya/api/routes_agent.py:159` returns final aggregate result after `kernel.run_turn` finishes.

### Hermes Evidence

- `hermes-agent/tui_gateway/render.py:38` builds a stream renderer.
- `hermes-agent/tui_gateway/server.py:160` emits typed events with `session_id`.
- `hermes-agent/ui-tui/src/app/useMainApp.ts:582` to `hermes-agent/ui-tui/src/app/useMainApp.ts:585` tracks streaming segments, pending stream tools, and tool trail state.
- `hermes-agent/ui-tui/src/app/useMainApp.ts:533` sends `approval.respond` while a session is active.

### Missing Nerya Work

- Add server-side event store and live stream endpoint (`/agent/events`, SSE or WebSocket) with durable cursors.
- Stream model deltas, tool start/progress/finish, approval requests, subagent progress, compression events, attachment ingest, and gateway delivery status.
- Let dashboard render a timeline from events, not from a final `tool_trace` blob.
- Support reconnect/resume from cursor so browser refresh does not lose active turn visibility.
- Add proxy support for streaming and auth headers.

## 7. API Auth Exists, But Authorization Is Still Coarse And Dashboard Proxy Drops Credentials

### Nerya Evidence

- `nerya/api/auth.py:14` to `nerya/api/auth.py:20` defines auth modes `local`, `token`, and `off`.
- `nerya/api/auth.py:160` returns `AuthResult(ok=True, actor=meta["actor"], scope=meta["scope"])` for valid token, but route matching does not enforce per-route scopes.
- `nerya/api/auth.py:174` to `nerya/api/auth.py:181` gives loopback actor `api:all`.
- `nerya/api/local_server.py:97` to `nerya/api/local_server.py:109` checks request auth, but handlers only receive `client` and payload/query, not actor/scope.
- `dashboard/app/api/proxy/[...path]/route.ts:11` sends only `content-type`, so `Authorization` / `X-Nerya-Token` from the browser are not forwarded to the Python API.

### Hermes Evidence

- `hermes-agent/gateway/platforms/telegram.py:254` authorizes callback users for inline actions.
- `hermes-agent/tests/gateway/test_yolo_command.py` verifies gateway command state is session-scoped instead of global.
- Hermes tool approval and YOLO mode are session-scoped through its tool approval surfaces.

### Missing Nerya Work

- Add route-level authorization matrix: read runtime, run agent, call skills, manage triggers, manage secrets, approve trades, edit config, gateway admin.
- Thread `AuthResult.actor` and `scope` into every route handler and then into `SkillRuntime.caller/extras`.
- Enforce action-level permissions at `/skills/call`, not only manifest self-description.
- Add session-scoped tool grants and gateway callback authorization.
- Forward auth headers through the dashboard proxy and add dashboard login/session storage.
- Add audit events for denied route, denied skill action, denied approval callback, and privilege escalation attempts.

## 8. Skill-First Contract Is Still Violated By Prompt And Code Catalogs

### Nerya Evidence

- `nerya/agent/context_builder.py:196` to `nerya/agent/context_builder.py:365` hardcodes reply shapes, action names, query actions, self-improvement actions, and natural-language heuristics inside prompt text.
- `nerya/agent/kernel.py:314` defines a static action map before manifest-driven augmentation.
- `nerya/agent/kernel.py:366` builds manifest-driven action maps but still starts from static fallbacks.
- `dashboard/lib/clientApi.ts:212` through `dashboard/lib/clientApi.ts:305` hardcodes dashboard wrappers for `strategy` and `subagent` skill/action names.
- `dashboard/lib/clientApi.ts:382` through `dashboard/lib/clientApi.ts:417` hardcodes exchange-author skill actions.

### Missing Nerya Work

- Build an `ActionCatalog` entirely from selected skill manifests, including action name, payload schema, natural-language examples, risk level, permissions, and output contract.
- Remove static prompt action lists and render only selected/allowed actions into context.
- Move NL routing examples into skill manifests or builtin skill prompt snippets.
- Make dashboard forms and API wrappers capability-driven from live skill manifests where possible.
- Keep any legacy `ACTION_MAP` behind a debug flag with warnings until all core actions have manifest metadata.

## 9. Static Market And Strategy Templates Still Leak Product Defaults

### Nerya Evidence

- `dashboard/components/chat/ChatView.tsx:22` to `dashboard/components/chat/ChatView.tsx:45` hardcodes suggestions around BTCUSDT, Binance, MACD, portfolio heartbeat, and subagent naming.
- `nerya/skills/builtin/sdk_writer_skill/templates.py:24` onward stores curated strategy templates as Python string literals.
- `nerya/skills/builtin/sdk_writer_skill/templates.py:207` to `nerya/skills/builtin/sdk_writer_skill/templates.py:255` contains default markets/tokens such as `BINANCE:BTCUSDT`, `BINANCE:ETHUSDT`, `BINANCE:SOLUSDT`, BSC token addresses, and Pancake-style examples.
- `nerya/core/market_defaults.py:82` to `nerya/core/market_defaults.py:93` still falls back to `binance` and BTC quote-derived defaults when no workspace preference exists.

### Missing Nerya Work

- Move dashboard suggestions to a runtime capability/example endpoint based on enabled skills, configured markets, and workspace preferences.
- Move SDK templates into skill template files/manifests with explicit tags, required providers, market class, risk class, and supported venue assumptions.
- Mark examples as examples, not defaults, and do not show unsupported venues/assets as if they are runtime-ready.
- Add a first-run setup wizard that asks for preferred market/venue rather than silently anchoring on Binance BTC.

## 10. Tool Execution Permissions Are Budgeted, But Not Actor-Policy Complete

### Nerya Evidence

- `nerya/skills/runtime.py:43` to `nerya/skills/runtime.py:56` dispatches by skill/action/caller.
- `nerya/skills/runtime.py:63` to `nerya/skills/runtime.py:77` validates payload and manifest permissions, but does not appear to evaluate a caller-specific grant policy.
- `nerya/harness/tool_runner.py:94` to `nerya/harness/tool_runner.py:108` has timeout/retry/budget knobs.
- `nerya/harness/tool_runner.py:127` charges turn budget before execution.

### Missing Nerya Work

- Add caller/actor-specific policy checks: dashboard user, gateway user, subagent, script, scheduled job, API token, and system job should not share the same ability set.
- Add allow/deny grants with expiry, per-session YOLO/approval modes, and permission narrowing for subagents.
- Require explicit approval for high-risk tools independent of whether they are first-party skills.
- Log every policy decision with actor, source, session, skill, action, payload hash, and approval id.

## 11. Session Store Is Not Hermes-Grade Searchable Conversation Memory

### Nerya Evidence

- `nerya/api/routes_agent.py:211` to `nerya/api/routes_agent.py:228` lists/loads sessions via `SessionStore`, but the current API surface is direct list/get/delete/skill_state.
- `nerya/messaging/mirror.py:13` stores per-session YAML snapshots under `workspace/messaging/sessions`.
- `nerya/messaging/mirror.py:175` to `nerya/messaging/mirror.py:200` replays mirror rows by channel/session/handle.

### Hermes Evidence

- `hermes-agent/hermes_state.py:5` to `hermes-agent/hermes_state.py:13` stores metadata, full message history, FTS5 search, token counters, and parent session chains.
- `hermes-agent/hermes_state.py:355` to `hermes-agent/hermes_state.py:383` creates sessions with source, user, model, system prompt, and parent id.
- `hermes-agent/hermes_state.py:394` to `hermes-agent/hermes_state.py:399` supports reopening/resuming sessions.
- `hermes-agent/hermes_state.py:412` to `hermes-agent/hermes_state.py:431` tracks token counts.

### Missing Nerya Work

- Add SQLite-backed session/message store or equivalent indexed store with FTS search.
- Store system prompt/action catalog version, model config, token/cost counters, attachments, approvals, gateway ids, and compression refs per session.
- Add session search, reopen/resume, branch, prune, export, and retention policy.
- Keep YAML/JSONL journals as append-only audit logs, but add indexed query surfaces for actual operator UX.

## 12. Capability Catalog Claims Must Be Split Into Declared / Implemented / Tested

### Nerya Evidence

- `nerya/messaging/platforms.py:34` to `nerya/messaging/platforms.py:55` lists many Hermes-aligned platforms and capability booleans.
- Some entries are `webhook` or `scaffold`, while Telegram is marked native with attachments/voice even though concrete support is incomplete.
- `nerya/acp/__init__.py:10` explicitly says editor-native agents lack file diffs, terminal commands, and streamed multi-tool activity.

### Missing Nerya Work

- For every platform/tool/skill/UI feature, expose three booleans: `declared`, `implemented`, `tested`.
- Prevent docs, dashboard, and prompts from showing declared-only scaffold features as available runtime capabilities.
- Add startup health checks that downgrade or hide unsupported platform features.
- Add parity tests per platform feature: text, reply, thread, attachment, voice, long-message split, approval button, retry, dedupe, and auth.

## Priority Alignment List

1. **Gateway event envelope first**: identity, event ids, reply refs, threads, attachments, media refs, batching, and outbox ids.
2. **Streaming event store second**: durable turn events, SSE/WS, dashboard timeline, reconnect cursors.
3. **Interrupt/cancel third**: cooperative cancellation token from UI/gateway/API to LLM/tools/subagents.
4. **Authz/tool grants fourth**: route scopes, actor threading, action grants, approval callbacks, dashboard auth forwarding.
5. **Manifest-driven action catalog fifth**: remove hardcoded prompt actions and dashboard wrappers where possible.
6. **Context/session memory sixth**: indexed session store, compression provenance, reference expansion, branch/resume/search.
7. **UI capability truth seventh**: suggestions, nav, copy, gateway platform matrix, and examples rendered from live capability state.

## Non-Exhaustive But Newly Confirmed Gaps

This is still not an exhaustive proof of parity. It is a code-backed list of additional gaps found after re-reading key Nerya and Hermes files. The main pattern is now clear enough to act on:

```text
Nerya often stores capability knowledge in prompts, route handlers, UI wrappers, and product copy.
Hermes tends to centralize capability knowledge in registries, adapters, session stores, stream events, and platform-specific protocol code.
```

To make Nerya feel like Hermes, the next work should be implementation of these contracts, not more broad audit prose.

# Fine-Grained Hermes Parity Backlog

## Status (2026-04-25)

Section-by-section status:

1. **Telegram / Gateway details** — PARTIALLY COMPLETED. Backend wiring (`Nerya/nerya/messaging/telegram.py`, `Nerya/nerya/messaging/mirror.py`, `Nerya/nerya/messaging/pipeline.py`, `Nerya/nerya/messaging/platforms.py`) supports send + escape + dedup; missing `editMessageText`, callback queries, media inbound/outbound parity, and adaptive flood backoff are tracked under Plan 21 P0/P1.
2. **Streaming and turn cancellation surfaces** — COMPLETED (backend). `Nerya/nerya/agent/streaming.py` + `Nerya/nerya/harness/cancellation.py` + `POST /agent/interrupt` + `GET /agent/stream/events` (Plan 05 §1, Plan 12 status banner).
3. **Tool permission grants** — PARTIALLY COMPLETED. Manifest `permissions/risk_gate/approval_gate` enforced in `Nerya/nerya/skills/runtime.py` and `Nerya/nerya/harness/tool_runner.py`; actor-scoped interactive grant model tracked under Plan 11 / Plan 20 P1.
4. **Context manifest / replay** — PARTIALLY COMPLETED. `POST /agent/session/search` + `GET /agent/session/events` + `Nerya/nerya/agent/transcript_compact.py` cover replay; the per-turn manifest artifact remains as a Plan 21 P1 follow-up.
5. **Tool result overflow / managed tool gateway** — PARTIALLY COMPLETED 2026-04-25.
   - **Tool-result overflow** is COMPLETED via Plan 16 P1 §1 (`Nerya/nerya/skills/runtime.py:179-220` `_maybe_spool_oversized` + `Nerya/nerya/harness/result_store.py:ResultStore` + `agent.harness.result_overflow_threshold_bytes`). Oversized tool outputs are persisted as JSON artifacts and the LLM context only sees a small `{ref, bytes, sha256}` envelope; covered by `Nerya/tests/test_result_overflow_spool.py` (4 tests).
   - **Managed tool gateway / OAuth-style provider auth** remains PENDING and is tracked under Plan 26 §8 (OAuth scaffolding) and Plan 30 follow-ups (managed tool gateway / supply-chain trust).

Status: PARTIALLY COMPLETED — backend foundations all in place; remaining items are platform-specific renderers + actor-scoped grants tracked under Plans 11, 20, 21.

This appendix exists because the earlier audit was still too high level. It is not a final exhaustive proof; it is a code-evidence-backed backlog of small-but-user-visible gaps that explain why Nerya currently feels much worse than Hermes as an everyday agent.

## Evidence Read In This Pass

### Nerya anchors

- Gateway send path is mostly text-only: `nerya/messaging/telegram.py:32` uses Telegram `sendMessage`; there are no `sendPhoto`, `sendDocument`, `editMessageText`, `deleteMessage`, or reply-specific Bot API endpoints in that transport.
- Gateway API has polling and progress hooks: `nerya/api/routes_gateway.py:75`, `nerya/api/routes_gateway.py:90`, `nerya/api/routes_gateway.py:109`, `nerya/api/routes_gateway.py:130`, `nerya/api/routes_gateway.py:164`, `nerya/api/routes_gateway.py:180`, `nerya/api/routes_gateway.py:314`, `nerya/api/routes_gateway.py:423`.
- Gateway event formatting exists but is derived from completed step records: `nerya/api/gateway_events.py:62`, `nerya/api/gateway_events.py:113`, `nerya/api/gateway_events.py:136`.
- Platform registry advertises many platforms and attachment flags: `nerya/messaging/platforms.py:16`, `nerya/messaging/platforms.py:36`, `nerya/messaging/platforms.py:37`, `nerya/messaging/platforms.py:38`, but most are scaffold/generic inbound, not full protocol adapters.
- Chat frontend uses `AbortController` client-side: `dashboard/components/chat/ChatView.tsx:86`, `dashboard/components/chat/ChatView.tsx:181`, `dashboard/components/chat/ChatView.tsx:233`; this does not prove backend turn cancellation.
- API run turn is synchronous request/response: `nerya/api/routes_agent.py:119`, `nerya/api/routes_agent.py:145`, `nerya/api/routes_agent.py:244`; there is no `/agent/stream` or event replay endpoint in the evidence read.
- Tool permission is manifest-string based: `nerya/security/permissions.py:1`, `nerya/security/permissions.py:32`, `nerya/skills/permissions.py:8`; it is not yet an actor-scoped interactive grant system.
- Tool runner has timeout/budget/retry records: `nerya/harness/tool_runner.py:65`, `nerya/harness/tool_runner.py:94`, `nerya/harness/tool_runner.py:128`, `nerya/harness/tool_runner.py:159`, but no thread-aware interrupt token like Hermes.
- Context builder and transcript compaction exist: `nerya/agent/context_builder.py:1`, `nerya/agent/transcript_compact.py:122`, `nerya/llm/compression.py`, but they are not yet exposed as a live manifest/report/replay product layer.

### Hermes anchors

- Telegram adapter is a full platform adapter: `hermes-agent/gateway/platforms/telegram.py:1`, with commands, callbacks, message types, media caching, reply ids, processing lifecycle, editing, flood backoff, and finalization behavior.
- Hermes Telegram parses incoming attachments: `hermes-agent/gateway/platforms/telegram.py:2551`, `hermes-agent/gateway/platforms/telegram.py:2577`, `hermes-agent/gateway/platforms/telegram.py:2610`, `hermes-agent/gateway/platforms/telegram.py:2631`, `hermes-agent/gateway/platforms/telegram.py:2707`, `hermes-agent/gateway/platforms/telegram.py:2996`.
- Hermes Slack shows similar multi-chunk/thread/file behavior: `hermes-agent/gateway/platforms/slack.py:268`, `hermes-agent/gateway/platforms/slack.py:314`, `hermes-agent/gateway/platforms/slack.py:697`, `hermes-agent/gateway/platforms/slack.py:758`, `hermes-agent/gateway/platforms/slack.py:1196`, `hermes-agent/gateway/platforms/slack.py:1427`.
- Hermes session store has gateway/session reset, retry/undo/compress and token tracking: `hermes-agent/gateway/session.py:101`, `hermes-agent/gateway/session.py:143`, `hermes-agent/gateway/session.py:361`, `hermes-agent/gateway/session.py:470`, `hermes-agent/gateway/session.py:529`, `hermes-agent/gateway/session.py:1135`.
- Hermes frontend/TUI exposes busy guards, approvals, streaming and tools: `hermes-agent/ui-tui/src/app/useMainApp.ts:383`, `hermes-agent/ui-tui/src/app/useMainApp.ts:533`, `hermes-agent/ui-tui/src/app/useMainApp.ts:582`, `hermes-agent/ui-tui/src/app/useMainApp.ts:623`.
- Hermes has thread-aware interrupts: `hermes-agent/tools/interrupt.py:1`, `hermes-agent/tools/interrupt.py:39`, `hermes-agent/tools/interrupt.py:62`.
- Hermes has tool result persistence as a first-class context control: `hermes-agent/tools/tool_result_storage.py:5`, `hermes-agent/tools/tool_result_storage.py:9`.
- Hermes managed tool gateway has OAuth/user-token concepts: `hermes-agent/tools/managed_tool_gateway.py:30`, `hermes-agent/tools/managed_tool_gateway.py:75`, `hermes-agent/tools/managed_tool_gateway.py:132`.

## Capability Gaps To Close

### 1. Telegram / Gateway Details


| Detail               | Current Nerya                                      | Hermes-like target                                                                           | Priority |
| -------------------- | -------------------------------------------------- | -------------------------------------------------------------------------------------------- | -------- |
| Text delivery        | `sendMessage` only                                 | format-aware send with Markdown/HTML fallback, table/code handling, safe escaping            | P0       |
| Message editing      | no edit API in `telegram.py`                       | stream into one editable message, finalize without stuck cursor                              | P0       |
| Chunking             | no robust Telegram 4096/UTF-16 chunker             | chunk by platform limits, preserve code fences, avoid tiny cursor-only messages              | P0       |
| Flood/rate backoff   | no adaptive edit/send backoff in evidence          | detect 429/flood, slow edits, fallback to final tail send                                    | P0       |
| Reply threading      | no `reply_to_message_id` send contract             | maintain reply target, thread/session binding, quoted context                                | P0       |
| Attachments inbound  | registry says attachments, but text path dominates | download/cache photo/document/voice/audio/video/sticker, attach artifact refs to turn        | P0       |
| Attachments outbound | no photo/document sending APIs                     | send files/images/audio with captions and provenance                                         | P1       |
| Media groups         | no album batching                                  | coalesce Telegram media groups before invoking agent                                         | P1       |
| Edited user messages | no evidence                                        | update/replace queued prompt or annotate correction event                                    | P1       |
| Deleted messages     | no evidence                                        | mark session event deleted; avoid acting on revoked input when possible                      | P2       |
| Inline approvals     | no callback workflow                               | approve/reject buttons tied to approval id and actor/session                                 | P0       |
| Typing/status        | has `sendChatAction` and hook text                 | debounce typing, edit status, stop on cancellation, no duplicate finals                      | P0       |
| Commands             | basic command sync                                 | `/stop`, `/retry`, `/undo`, `/compress`, `/status`, `/queue`, `/resume`, `/tools`, `/memory` | P0       |
| Unauthorized chat    | not enough evidence                                | allowlist/pairing, group mention policy, per-chat role                                       | P0       |
| Idempotency          | offset state exists but weak semantic contract     | dedupe by platform update/message id and delivery id                                         | P0       |
| Observability        | journal plus dashboard outbox                      | delivery attempts, platform raw ids, retry schedule, error class, next retry                 | P1       |


### 2. Frontend Streaming / Operator UX


| Detail             | Current Nerya                   | Hermes-like target                                                               | Priority |
| ------------------ | ------------------------------- | -------------------------------------------------------------------------------- | -------- |
| Streaming response | chat appears request/response   | SSE/WebSocket `/agent/stream` with token/tool/subagent events                    | P0       |
| Stop button        | client `AbortController` only   | backend cancellation token, tool cancellation, final cancelled event             | P0       |
| Timeline           | static events after result      | live timeline: plan, think, tool start/output/end, approvals, compression, retry | P0       |
| Tool visibility    | tool trace returned after turn  | collapsible live tool cards with args preview, result artifact, duration, status | P0       |
| Approval UX        | evolution page exists           | inline per-tool approval modal with risk, command, affected files/secrets        | P0       |
| Auth UI            | no clear login flow in evidence | login/session management, token expiry, role labels, logout, audit trail         | P0       |
| Artifacts          | no strong artifact browser      | files/images/audio/tool-output refs, preview, download, redaction status         | P1       |
| Subagents          | CRUD page exists                | live subagent lanes, messages, tool use, cancellation, cost/budget               | P1       |
| Context report     | not surfaced                    | show what context was included, compressed, dropped, retrieved                   | P1       |
| Replay/resume      | trace/open_turns APIs exist     | replay whole event stream after refresh, resume/abandon controls                 | P0       |
| Busy sessions      | local `busy` flags in pages     | per-session queue state, interrupt/replace/append policy UI                      | P0       |


### 3. API Auth / Gateway Auth / Multi-User Boundaries


| Detail             | Required behavior                                                                                                       |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| Actor model        | every request/event has `actor_id`, `actor_type`, `source`, `session_id`, `tenant/workspace` if multi-user is supported |
| API auth           | local dev may be open, but non-local routes require token/session/OAuth; dashboard proxy must forward actor safely      |
| Route scopes       | define scopes for read, run, tool, approve, secret, trading, evolution, gateway admin                                   |
| Gateway pairing    | Telegram/Slack/Discord chats must be paired or allowlisted before running tools                                         |
| Group policy       | require mention/reply or command prefix in groups; isolate sessions per user/thread when configured                     |
| Approval ownership | only the actor/session that owns an approval can resolve it, unless operator role overrides                             |
| Subagent narrowing | subagents inherit a narrowed permission set; no privilege escalation via delegated prompt                               |
| Audit trail        | auth failures and denied permission checks must be evented and inspectable                                              |


### 4. Tool Permission And Approval Details


| Detail                 | Current risk                        | Target                                                                       |
| ---------------------- | ----------------------------------- | ---------------------------------------------------------------------------- |
| Permission granularity | manifest strings are coarse         | permission class + risk level + resource scope + actor grant                 |
| Dynamic approvals      | not enough evidence                 | tool call blocks pending operator approval with exact call diff              |
| Filesystem tools       | not Hermes-like yet                 | read/write/patch/search guarded by path scopes and dirty-tree checks         |
| Shell tools            | not Hermes-like yet                 | command classifier, cwd scope, network/destructive/escalation policy         |
| Browser/web tools      | not Hermes-like yet                 | URL allow/deny policy, screenshot/artifact capture, sensitive-site warning   |
| MCP tools              | exposed but not full approval layer | server/tool scopes, OAuth status, per-call timeout/interruption              |
| Long-running tools     | timeout only                        | cooperative cancellation plus kill tree for processes where safe             |
| Result size            | result can bloat prompt             | persist large result, prompt gets preview + artifact ref                     |
| Secret handling        | vault exists                        | redact in events, deny secret egress, reveal only via explicit approved flow |


### 5. Context Compression / Session Management


| Detail              | Required behavior                                                                                        |
| ------------------- | -------------------------------------------------------------------------------------------------------- |
| Context manifest    | every model call writes included/dropped/summarized artifacts, reasons, token estimate, source event ids |
| Compression trigger | automatic threshold by model context and user-visible `/compress` command                                |
| Latest correction   | latest user correction is preserved verbatim and cannot be summarized away                               |
| Tool pairs          | active `tool_use`/`tool_result` pairs stay valid after compression                                       |
| Pending approvals   | unresolved approval context never dropped                                                                |
| Artifact references | dropped large content remains retrievable by ref, not lost                                               |
| Session resume      | resume from last stable event, with unsafe states marked non-resumable                                   |
| Message replacement | edited/new interrupting user messages become replacement events, not ambiguous appended text             |
| Busy policy         | configure queue, interrupt_replace, append_instruction, or reject_busy per channel/session               |
| Scheduler overlap   | each scheduled agent session needs skip/queue/cancel/parallel policy, TTL, idempotency                   |


### 6. Memory / Self-Learning Details


| Detail                | Gap                                                                                      |
| --------------------- | ---------------------------------------------------------------------------------------- |
| Explicit memory tools | need `/remember`, `/forget`, `/memsearch`, memory source citations, and dashboard review |
| Memory provenance     | every recalled memory needs source, age, confidence, scope, and why it was injected      |
| Memory isolation      | separate global, workspace, strategy, user, chat, and temporary memory scopes            |
| Reflection quality    | reflection should produce proposed durable rules, not silently mutate behavior           |
| Self-evolution loop   | proposals need eval gates, rollback, confidence, and operator signoff                    |
| Mistake registry      | recurring failures should become actionable checks, not only journal text                |
| Gateway memory        | gateway sessions should summarize long chats and preserve file/media refs                |


### 7. Coding-Agent Capability Details


| Detail             | Gap                                                                               |
| ------------------ | --------------------------------------------------------------------------------- |
| General file tools | Nerya is trading-skill-first; Hermes has broad file/search/patch/session tools    |
| Patch workflow     | need diff preview, apply, revert, dirty-tree protection, and test command capture |
| Build/test loop    | need project command discovery, narrow verification, failure summarization        |
| Background jobs    | need process registry, logs, kill/restart, long-running status                    |
| Browser testing    | need browser app integration and screenshots for frontend debugging               |
| Git workflows      | status, branch, blame/log, PR-ready summary and no accidental commit policy       |
| Code index         | symbol/context search should be first-class tool, not ad hoc shell usage          |
| Sandboxing         | per-tool env/cwd/path/network policy, not just Python skill runtime assumptions   |


### 8. Gateway Product Polish That Matters In Daily Use

These are small details but high impact:

- Send a short immediate ack when work starts, then edit it rather than spamming new messages.
- Keep final answer in the same thread/reply as the user message.
- Preserve tables and code blocks instead of mangling Markdown.
- Show a compact tool trail: `plan -> search -> patch -> test -> done`.
- When a tool is slow, update status at a low frequency and include elapsed time.
- If cancelled, stop typing/status loops and send exactly one cancellation message.
- If a retry occurs, link it to the prior turn and avoid duplicate final answers.
- If a message contains an image/document, say whether it was attached, ignored, too large, unsupported, or cached.
- If the gateway cannot authenticate the chat, respond with pairing instructions, not a silent failure.
- If a platform API fails, persist the raw platform message id and a redacted error class for debugging.

## 追平 Hermes 的建议顺序

1. **P0 event backbone**: append-only event store + `/agent/stream` + replay endpoint + actor/session ids.
2. **P0 cancellation**: real backend cancellation token through agent loop, subagents, LLM, ToolRunner, skills, gateway status loops.
3. **P0 gateway rebuild**: Telegram first-class adapter: edit/chunk/reply/attachments/commands/callback approvals/idempotency.
4. **P0 auth and permissions**: route scopes, gateway pairing, actor-bound approvals, tool grants.
5. **P0 frontend timeline**: live stream, stop/retry/abandon/resume, tool cards, approval modals.
6. **P1 context product layer**: context manifest, compression report, artifact-backed large result handling.
7. **P1 coding harness**: file/search/patch/shell/browser/process tools with approval and path policy.
8. **P1 memory cockpit**: explicit memory CRUD/search/provenance, reflection proposals, mistake registry.
9. **P2 platform breadth**: Slack/Discord/WeChat/Feishu/Matrix parity after Telegram protocol is correct.

## Acceptance Tests To Add

1. Telegram sends a long Markdown table, chunks safely, preserves code fences, and edits one status message into final text.
2. Telegram image + caption becomes one agent event with cached image artifact and source message id.
3. Telegram document too large returns a user-visible unsupported/too-large message and logs a redacted event.
4. Replying `/stop` to an active turn cancels backend work and stops typing/status updates.
5. Duplicate Telegram update id does not create duplicate turns or duplicate final replies.
6. Inline approval callback can approve only the matching approval id and actor/session.
7. Dashboard stop cancels backend ToolRunner, not just the HTTP request.
8. Dashboard refresh replays the live turn event timeline from the event store.
9. New user message during busy turn follows configured busy policy and is visible in queue state.
10. Context compression preserves latest user correction, pending approval, and active tool pairs.
11. Large tool output becomes an artifact ref and is not injected fully into the next prompt.
12. Subagent cannot call a tool outside its narrowed permission grant.
13. Unauthorized gateway chat receives pairing instructions and cannot trigger tools.
14. Authenticated API user without `tool.execute` scope cannot run `/agent/run_turn` with tool execution.
15. Retry after cancellation links to the previous turn and does not reuse unsafe partial tool state.

## Correction To Earlier Claim

The earlier documents named many categories, but they still under-specified these daily-use details: attachment lifecycle, reply threading, edited/deleted messages, chunking/edit backoff, status-loop cancellation, actor-bound approvals, backend cancellation, context manifests, result artifacting, busy-session policy, and frontend replay. Those details are not optional polish; they are what make Hermes feel usable and Nerya currently feel unreliable.
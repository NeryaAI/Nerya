# Nerya End-to-End Streaming Implementation Plan

Status: implementation plan  
Date: 2026-04-27  
Owner: Nerya runtime / API / dashboard / gateway integration  
Reference material: Hermes gateway streaming consumer, provider official streaming APIs, Nerya current event bus  

## 1. Goal

Make Nerya stream user-visible progress and assistant output in real time across:

1. Dashboard chat and future web UI surfaces.
2. Gateway platforms such as Telegram, Discord, Slack, webhook/API clients, and Hermes-aligned platform catalog entries.
3. Provider-backed LLM calls for OpenAI, OpenAI-compatible/OpenRouter, Anthropic, Gemini, and mock/test providers.
4. Agent loop progress, tool execution progress, approvals, cancellation, errors, and final turn completion.

The target architecture is:

```text
Provider native stream
  -> Nerya provider chunk normalizer
  -> LLMGateway / ModelRouter stream API
  -> AgentKernel turn event stream
  -> process event bus + persisted event journal
  -> HTTP SSE / polling replay / gateway stream consumer
  -> Dashboard realtime UI / platform send-edit updates / webhook delta events
```

This is not just a frontend change. Frontend streaming only becomes reliable if the runtime emits stable, resumable, redacted events and gateways consume the same event protocol.

## 2. Non-goals

- Do not make Hermes a runtime dependency. Hermes is reference material only.
- Do not stream plaintext secrets, raw private prompts, vault values, or unredacted tool arguments.
- Do not expose provider chain-of-thought. Only expose visible answer text and approved reasoning summaries/progress.
- Do not execute tools from partial provider JSON. Tool calls must be complete and validated before execution.
- Do not break the existing blocking `POST /agent/run_turn` contract.
- Do not require every gateway platform to support token-level edits. Some platforms will receive progress + final message only.

## 3. Current Nerya State

### 3.1 Existing pieces to reuse

- `nerya/agent/streaming.py` already defines a process-local event bus with `seq`, `event_id`, replay buffer, and event kinds such as `message.delta`, `tool.start`, `tool.progress`, `tool.complete`, `approval.request`, `turn.step`, and `turn.complete`.
- `nerya/api/routes_agent.py` already exposes `GET /agent/stream/events`, which returns replay/polling results from the bus using `after_seq`, `session_id`, and `limit`.
- `nerya/agent/kernel.py` already publishes `turn.step` to the event bus when journaling turn steps.
- `dashboard/components/chat/ChatView.tsx` already creates a pending assistant bubble before making the request.
- `nerya/api/routes_gateway.py` already emits gateway progress status at coarse hook boundaries and records inbound/outbound gateway mirror entries.
- `nerya/messaging/platforms.py` already has a Hermes-aligned gateway platform catalog.

### 3.2 Gaps to close

- `POST /agent/run_turn` is synchronous and returns only after `AgentKernel.run_turn()` finishes.
- `GET /agent/stream/events` is polling replay, not a long-lived SSE endpoint.
- Dashboard chat waits for the complete `TurnPayload` before updating the assistant bubble.
- Provider adapters are primarily blocking/non-streaming.
- `ModelRouter` / `LLMGateway` do not expose a streaming call path.
- Kernel does not publish provider token deltas as real `message.delta` events.
- Tool progress is not uniformly emitted from scripts/skills into the stream bus.
- Gateway platform delivery uses final messages/status updates, not Hermes-style progressive send/edit consumers.

## 4. Hermes Reference Pattern

Hermes' `gateway/stream_consumer.py` is the main design reference:

- The agent emits synchronous token deltas through a callback.
- The gateway consumer receives deltas in a thread-safe queue.
- An async task buffers text and rate-limits delivery.
- It sends an initial platform message and progressively edits the same message when possible.
- It uses `edit_interval` and `buffer_threshold` to avoid platform flood control.
- It handles long messages by chunking and segment boundaries.
- It falls back to final-only send when edits fail repeatedly.
- It separates assistant commentary, tool boundaries, and final response state.

Nerya should adopt the pattern, not copy the dependency:

```text
Nerya stream event bus
  -> GatewayStreamConsumer-like adapter
  -> platform transport send_initial/edit/finalize
```

## 5. Provider Official API Plan

### 5.1 OpenAI native

Preferred path:

- Use the Responses API for native OpenAI models where available.
- Enable streaming and consume SSE events.
- Map visible output deltas to `ProviderStreamChunk(kind="text_delta")`.
- Map final response metadata and usage to `ProviderStreamChunk(kind="usage" | "complete")`.
- Keep Chat Completions streaming as compatibility path for models/routes that require it.

Relevant provider concepts:

- Responses streaming emits event objects such as text delta and completion events.
- Chat Completions streaming emits `chat.completion.chunk` objects with `choices[].delta.content`, optional tool-call deltas, and finish reasons.

Implementation notes:

- Do not expose raw reasoning items unless explicitly configured as a provider-supported summary.
- For JSON schema/action planning, either use a non-streaming call or stream only status text while buffering JSON until valid.
- Preserve current `max_completion_tokens` / reasoning-model behavior in `nerya/llm/adapters/openai.py`.

### 5.2 OpenRouter and OpenAI-compatible providers

Preferred path:

- Use OpenAI-compatible Chat Completions `stream: true`.
- Parse SSE `data:` chunks.
- Map `choices[].delta.content` to visible text deltas.
- Map `choices[].delta.tool_calls` to buffered tool-call deltas.
- Respect provider-routing metadata and OpenRouter provider preferences already represented in Nerya config.

Implementation notes:

- Treat OpenRouter as `openai_compat_stream` unless a provider-specific extension is required.
- Keep parsing tolerant: `[DONE]`, empty keepalive lines, unknown vendor metadata, and usage blocks should not crash the turn.

### 5.3 Anthropic

Preferred path:

- Use Messages API streaming.
- Parse SSE events such as `message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, and `message_stop`.
- Map text deltas from `content_block_delta` to visible text.
- Buffer tool-use / input-json deltas until complete before emitting a validated tool request.
- Map usage information from `message_delta` / final metadata.

Implementation notes:

- Preserve prompt caching support in `nerya/llm/adapters/anthropic.py`.
- Do not surface chain-of-thought. If Anthropic exposes summary-like metadata, map it only to approved `reasoning.summary` events.
- Tool input JSON must be closed and validated before reaching `ToolRunner`.

### 5.4 Gemini

Preferred path:

- Use `streamGenerateContent` / `generate_content_stream` depending on SDK/REST path.
- Parse streamed `GenerateContentResponse` chunks.
- Map `candidates[].content.parts[].text` to visible text deltas.
- Separate `thought` parts from visible content and expose only allowed summaries.
- Map function-call parts to buffered tool-call chunks.

Implementation notes:

- Existing Hermes Code Assist adapter demonstrates wrapping Gemini stream events into OpenAI-shaped chunks; Nerya should instead normalize directly into `ProviderStreamChunk`.
- Preserve Gemini `thinkingConfig` behavior currently in `nerya/llm/adapters/gemini.py`.
- Handle Gemini finish reasons and safety blocks explicitly as `error` or `complete` stream events.

### 5.5 Mock/test provider

Preferred path:

- Add deterministic slow-stream support for tests and local demos.
- Emit predictable chunks with configurable delay and optional tool/progress events.
- Use this provider in dashboard/gateway tests so CI does not need real provider keys.

## 6. Unified Data Model

### 6.1 Provider chunk model

Add a small provider-normalized model, for example in `nerya/llm/streaming.py`:

```python
@dataclass(frozen=True)
class ProviderStreamChunk:
    kind: Literal[
        "start",
        "text_delta",
        "message_snapshot",
        "reasoning_summary_delta",
        "tool_call_delta",
        "tool_call_complete",
        "usage",
        "error",
        "complete",
    ]
    text: str = ""
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments_delta: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    raw_event_type: str | None = None
    provider: str = ""
    model: str = ""
```

Rules:

- Provider adapters may include raw event type names for diagnostics.
- Raw provider bodies should not be sent to UI by default.
- Tool-call argument deltas are internal until complete.
- Usage may arrive at the end or in incremental blocks depending on provider.

### 6.2 Runtime event model

Runtime stream events should be provider-independent:

```json
{
  "kind": "message.delta",
  "event_id": "...",
  "seq": 42,
  "ts": 1770000000.0,
  "turn_id": "trn_...",
  "session_id": "dashboard:...",
  "source": "provider|kernel|tool|gateway",
  "visibility": "user|operator|internal",
  "message_id": "msg_...",
  "delta": "partial visible text",
  "snapshot": "optional accumulated visible text",
  "provider": "openai",
  "model": "gpt-..."
}
```

Required event kinds:

| Event kind | Visibility | Purpose |
| --- | --- | --- |
| `turn.started` | user/operator | Turn accepted and has IDs. |
| `turn.step` | operator | Existing journaled kernel step. |
| `message.delta` | user | Visible assistant content delta. |
| `message.snapshot` | user | Full current assistant content after replay/reconnect. |
| `message.final` | user | Final assistant content. |
| `tool.start` | user/operator | Tool/skill started. |
| `tool.progress` | user/operator | Long-running tool progress. |
| `tool.complete` | user/operator | Tool finished. |
| `approval.request` | operator | Approval card/action required. |
| `usage.delta` | operator | Token/cost metadata. |
| `error` | user/operator | Recoverable or terminal error. |
| `turn.cancelled` | user/operator | User interrupted the turn. |
| `turn.complete` | user/operator | Final result and stop reason. |

## 7. Backend/API Implementation Phases

### Phase 1 — Streaming data contracts and tests

Files to add/change:

- Add `nerya/llm/streaming.py`.
- Extend `nerya/agent/streaming.py` if the bus needs typed helper builders.
- Add tests under `tests/` for event ordering, replay, cursor behavior, and redaction.

Acceptance criteria:

- Events always have monotonic `seq` and stable `event_id`.
- `events_since(after_seq)` / `recent(after_seq=...)` returns no duplicates.
- Session filtering works for multiple simultaneous turns.
- Redaction helpers prevent secrets and raw provider API keys from entering event payloads.

### Phase 2 — Provider streaming adapters

Files to change:

- `nerya/llm/adapters/_base.py`
- `nerya/llm/adapters/openai.py`
- `nerya/llm/adapters/anthropic.py`
- `nerya/llm/adapters/gemini.py`
- OpenAI-compatible adapter path used by OpenRouter.
- Mock provider implementation in `nerya/llm/model_router.py` or a dedicated adapter.

Implementation tasks:

1. Define optional `stream(...)` on adapter classes.
2. Keep `__call__` as blocking fallback.
3. Add SSE line parser shared by OpenAI-compatible and Anthropic where practical.
4. Add Gemini chunk parser.
5. Add final-result accumulator that converts chunks into existing `ProviderResult`.
6. Add provider-specific error classification for stream failures.

Acceptance criteria:

- OpenAI-compatible mock SSE chunks produce expected text deltas and final result.
- Anthropic text delta + final usage fixture is parsed correctly.
- Gemini streamed text chunks are parsed correctly.
- Tool-call deltas are not executed until complete.
- Blocking path remains unchanged for non-streaming providers.

### Phase 3 — `ModelRouter` and `LLMGateway` stream path

Files to change:

- `nerya/llm/model_router.py`
- `nerya/llm/gateway.py`
- `docs/llm-gateway.md`
- `nerya/llm/capability_matrix.py`

Implementation tasks:

1. Add `ModelRouter.dispatch_stream(...) -> Iterator[ProviderStreamChunk]`.
2. Add `LLMGateway.call_stream(...)` that resolves keys, tier config, timeouts, reasoning config, and budget metadata.
3. Add fallback behavior:
   - if provider supports streaming, stream directly;
   - if provider does not support streaming and fallback is enabled, emit status + blocking final snapshot;
   - if fallback is disabled, fail fast with a typed error.
4. Update capability matrix statuses from `experimental` / `metadata-only` to exact runtime truth after implementation.

Acceptance criteria:

- `dispatch_stream()` chooses the same provider/model/tier config as `dispatch()`.
- Missing key behavior matches current production truth gates.
- Stream errors preserve partial visible output and return a typed stopped reason.
- Cost/token accounting still lands in final turn budget.

### Phase 4 — Agent kernel streaming turn execution

Files to change:

- `nerya/agent/kernel.py`
- `nerya/harness` / `ToolRunner` progress hooks as needed.
- Skill runtime interfaces where long-running scripts need progress callback.

Implementation tasks:

1. Add a `stream_sink` or `TurnStreamContext` object to the kernel.
2. Emit `turn.started` before first plan/think step.
3. During LLM think/planning calls, publish `message.delta` for visible text when safe.
4. For structured action planning, buffer partial JSON internally and publish user-safe progress text instead.
5. Emit `tool.start`, `tool.progress`, and `tool.complete` around every skill/tool execution.
6. Emit `message.final` and `turn.complete` at the end.
7. Emit `error` and `turn.complete` with stopped reason for provider/tool failures.
8. Ensure current `run_turn()` can call streaming internals and still return the same final payload.

Acceptance criteria:

- A slow mock provider produces real-time `message.delta` events before `run_turn` completes.
- Existing blocking tests still pass.
- Tool failures stream a visible error/progress event and still journal full error detail privately.
- Approval-required actions stream an `approval.request` event before waiting.

### Phase 5 — HTTP/SSE API

Files to change:

- `nerya/api/local_server.py`
- `nerya/api/routes_agent.py`
- `nerya/api/route_scopes.py`
- `dashboard/app/api/proxy/[...path]/route.ts`

Recommended API shape:

```http
POST /agent/turns
Content-Type: application/json

{
  "source": "user_chat",
  "kind": "user.chat",
  "target": "main",
  "payload": {"text": "...", "channel": "dashboard"},
  "session_id": "..."
}
```

Response:

```json
{"ok": true, "turn_id": "trn_...", "session_id": "..."}
```

SSE stream:

```http
GET /agent/turns/{turn_id}/events?after_seq=0
Accept: text/event-stream
```

SSE frame:

```text
event: message.delta
id: 42
data: {"kind":"message.delta","seq":42,"delta":"hello"}

```

Compatibility options:

- Keep `POST /agent/run_turn` blocking.
- Add `POST /agent/run_turn?stream=1` only if easy, but prefer two-step start + stream because it handles reconnects better.
- Keep `GET /agent/stream/events` as polling replay fallback for clients that cannot hold SSE.

Acceptance criteria:

- SSE sends heartbeat comments every 15-30 seconds.
- SSE reconnect with `after_seq` replays missed events without duplicates.
- Auth scopes distinguish `write:chat`, `read:sessions`, and stream access.
- Next.js proxy does not buffer SSE; it pipes the upstream stream.

### Phase 6 — Dashboard chat realtime UI

Files to change:

- `dashboard/components/chat/ChatView.tsx`
- `dashboard/components/chat/ChatMessage.tsx`
- `dashboard/lib/clientApi.ts` or a new `dashboard/lib/streamApi.ts`
- `dashboard/lib/chat.ts`

Implementation tasks:

1. Replace blocking `callApi<TurnPayload>("/agent/run_turn")` with:
   - create pending user + assistant messages;
   - start turn through `POST /agent/turns`;
   - subscribe to SSE events;
   - append `message.delta` to assistant text;
   - render `tool.*` events as progress timeline/tool cards;
   - finalize on `turn.complete`.
2. Store `turn_id`, `session_id`, and `last_seq` in the thread state.
3. On page reload, call replay endpoint with `after_seq` and then resume SSE if turn is still active.
4. Add clear states: connecting, streaming, running tool, waiting approval, cancelled, failed, complete.
5. Stop button calls `/agent/interrupt` and closes local SSE after `turn.cancelled` or timeout.

Acceptance criteria:

- First visible progress appears within 1 second for slow mock provider/tool tests.
- Assistant bubble grows token-by-token or chunk-by-chunk.
- Tool progress is visible while the tool is running.
- Final `TurnPayload` remains available for trace/detail rendering.
- Cancelled turns keep partial output and show a truthful stopped reason.

### Phase 7 — Gateway platform streaming

Files to add/change:

- Add `nerya/messaging/stream_consumer.py`.
- Extend `nerya/messaging/telegram.py` with send/edit/typing primitives.
- Extend `nerya/messaging/discord.py` with webhook message edit support where webhook token/message id is available.
- Add Slack transport if/when Slack credentials are configured.
- Extend `nerya/messaging/generic_platform.py` for `delta/progress/final` webhook event payloads.
- Update `nerya/api/routes_gateway.py` to start a streaming turn and attach a gateway consumer.

Gateway delivery modes:

| Platform type | Delivery mode | Behavior |
| --- | --- | --- |
| Dashboard/API client | SSE | Raw normalized events. |
| Telegram | send + edit | Send placeholder, edit every interval/threshold, final edit. |
| Discord webhook | send + edit when possible | Send placeholder, edit via webhook message endpoint, fallback to final continuation. |
| Slack | `chat.postMessage` + `chat.update` | Same as send/edit with Slack rate limits. |
| Generic webhook | event push | Send JSON `delta`, `progress`, `final` events. |
| Send-only webhooks | progress + final | Typing/progress webhook if configured, final message otherwise. |
| Catalog-only platforms | no realtime stream | Report unsupported until native transport exists. |

Default stream consumer config:

```yaml
gateway:
  streaming:
    enabled: true
    edit_interval_s: 1.0
    buffer_threshold_chars: 40
    max_flood_strikes: 3
    cursor: " ▉"
    fallback_final_send: true
```

Acceptance criteria:

- Telegram mock transport receives send, several edits, then final edit.
- Discord mock transport obeys edit throttling and fallback on simulated 429.
- Generic webhook receives typed JSON events with seq and turn id.
- Platforms that cannot edit do not spam token-level messages.

### Phase 8 — Persistence, replay, and observability

Files to change/add:

- Event journal path helper in config/paths if needed.
- `nerya/agent/streaming.py` replay/hydration helpers.
- Trace endpoints in `nerya/api/routes_agent.py`.
- Dashboard trace UI if needed.

Implementation tasks:

1. Persist stream events to a turn-scoped journal, not only in-memory bus.
2. On API restart, allow replay from persisted event journal for recent turns.
3. Add metrics:
   - time to first event,
   - time to first token,
   - stream duration,
   - provider stream errors,
   - gateway edit attempts/failures,
   - fallback-to-blocking count,
   - cancelled stream count.
4. Add a debug endpoint or trace view for stream health.

Acceptance criteria:

- Dashboard reload can reconstruct completed turn stream from persisted events.
- Gateway mirror records final user-visible text and selected progress markers.
- Observability can identify whether latency came from provider, kernel, tool, or platform edit limits.

## 8. Safety and Redaction Rules

- Apply existing redaction before publishing any event.
- Never publish API keys, vault refs resolved values, exchange credentials, wallet secrets, or raw headers.
- Do not stream raw provider request bodies to UI.
- Do not stream full tool arguments if they may contain secrets; use summarized progress.
- Chain-of-thought is internal. If provider emits `thought` or reasoning content, default behavior is discard or summarize only if provider offers a safe summary field and config enables it.
- Tool-call JSON deltas are internal until complete, schema-validated, and approved by policy.
- Approval-gated actions must stream a clear pending approval state and must not execute until approval gate succeeds.

## 9. Cancellation and Interrupt Semantics

Required behavior:

1. Frontend Stop button calls `/agent/interrupt` with `turn_id/session_id`.
2. Gateway `/stop` maps to the same cancel signal.
3. Kernel checks cancel state between provider chunks, between tool steps, and before approvals.
4. Provider stream HTTP request is closed where supported.
5. Long-running tools get a best-effort cancellation context.
6. UI receives `turn.cancelled` and then final `turn.complete` with stopped reason.

Acceptance criteria:

- Cancelling during provider stream stops further deltas.
- Cancelling during a tool emits a tool cancelled/final event.
- Partial assistant output is preserved and labelled as partial.

## 10. Testing Strategy

### Unit tests

- Event bus ordering, replay, cursor, session filter.
- SSE parsers for OpenAI-compatible, Anthropic, Gemini.
- Provider chunk accumulator to final `ProviderResult`.
- Redaction of secrets in events.
- Stream consumer throttling and fallback logic.

### Integration tests

- Slow mock provider through `LLMGateway.call_stream()`.
- `AgentKernel` emits turn/progress/message/tool/final events.
- Blocking `run_turn()` still returns a correct final payload.
- `GET /agent/turns/{turn_id}/events` streams valid SSE frames.
- `GET /agent/stream/events` polling replay still works.

### Dashboard tests

- Assistant bubble updates before final completion.
- Tool progress cards render in order.
- Reconnect with `after_seq` avoids duplicate text.
- Stop button marks partial output correctly.

### Gateway tests

- Telegram send/edit/final mock flow.
- Discord send/edit/final mock flow and 429 fallback.
- Generic webhook delta/progress/final payloads.
- Send-only platform final-message behavior.

### Manual smoke tests

```powershell
# Start API
nerya service start

# Start dashboard
cd dashboard
pnpm dev

# Run slow mock streaming chat
# Verify: first progress/token appears before final completion.
```

Recommended pytest slices:

```powershell
pytest tests/test_streaming_event_bus.py tests/test_llm_streaming_adapters.py -q
pytest tests/test_agent_streaming_turn.py tests/test_api_streaming_sse.py -q
pytest tests/test_gateway_stream_consumer.py -q
```

## 11. Rollout Plan

### Milestone A — Web-visible mock streaming

Scope:

- Event contracts.
- Slow mock provider.
- Kernel publishes `message.delta`.
- SSE endpoint.
- Dashboard streaming UI.

Exit criteria:

- Local dashboard visibly streams mock assistant text and tool progress.
- Existing blocking chat still works.

### Milestone B — OpenAI-compatible/OpenRouter streaming

Scope:

- OpenAI-compatible SSE parser.
- OpenRouter route support.
- Stream fallback to blocking.
- Basic token/cost final accounting.

Exit criteria:

- A real OpenAI-compatible provider streams into dashboard.
- Partial output survives provider stream failure.

### Milestone C — Anthropic and Gemini streaming

Scope:

- Anthropic Messages streaming parser.
- Gemini streamGenerateContent parser.
- Tool-call buffering and JSON validation.
- Reasoning/thinking safety behavior.

Exit criteria:

- Provider fixture tests pass for text/tool/error/usage paths.
- Live provider smoke tests pass when keys are configured.

### Milestone D — Gateway streaming

Scope:

- Nerya `GatewayStreamConsumer` equivalent.
- Telegram progressive edit.
- Discord progressive edit where supported.
- Generic webhook event stream.
- Send-only fallback for other catalog platforms.

Exit criteria:

- Gateway users see progress before final answer.
- Platform flood-control does not break final delivery.

### Milestone E — Persistence and production hardening

Scope:

- Persisted event journal.
- Restart/reconnect replay.
- Metrics and trace diagnostics.
- Documentation updates.

Exit criteria:

- Reload/restart can recover completed stream history.
- Operators can diagnose stream failures from trace/metrics.

## 12. Risk Register

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Provider stream shapes differ or change | Broken parsing | Fixture tests per provider and tolerant unknown-event handling. |
| Partial JSON tool calls execute too early | Unsafe tool execution | Buffer and validate tool JSON before tool runner sees it. |
| Platform edit rate limits | Gateway spam/failure | Hermes-style interval/threshold throttle and fallback final send. |
| Dashboard duplicate deltas after reconnect | Corrupt UI text | Use seq cursor and optional message snapshots. |
| Reasoning leaks | Privacy/safety issue | Treat reasoning as internal unless approved summary. |
| API local server struggles with SSE | Unstable streams | Implement minimal stdlib SSE first; migrate production to FastAPI/Starlette if needed. |
| Long tools cannot report progress | Poor UX | Add optional progress callback to ToolRunner and script protocol. |
| Stream failure hides final result | User confusion | Preserve partial output and send typed error + final stopped reason. |

## 13. Documentation Updates

Update or add:

- `docs/llm-gateway.md`: streaming provider support matrix and config.
- `docs/gateway-platforms.md`: platform streaming modes and limitations.
- `docs/dashboard-chat-streaming.md`: frontend event handling and reconnect contract.
- `docs/api-streaming.md`: `POST /agent/turns`, SSE endpoint, replay endpoint, event schemas.
- `docs/security.md` or relevant security docs: streaming redaction and reasoning visibility policy.

## 14. Definition of Done

Nerya can claim complete streaming support only when all of these are true:

1. Dashboard receives real-time `message.delta` and progress events before turn completion.
2. At least one real provider and the mock provider stream through the same normalized path.
3. OpenAI-compatible/OpenRouter, Anthropic, and Gemini have parser tests and documented capability status.
4. Gateway platforms use the same event stream, with send/edit streaming where supported and final-only fallback where not.
5. Cancellation works from dashboard and gateway.
6. Replay/reconnect works using `seq` / `after_seq` without duplicated text.
7. Existing blocking API behavior remains compatible.
8. Sensitive data and raw reasoning are not streamed to user surfaces.
9. Tests cover provider parsers, kernel event emission, API SSE, dashboard rendering, and gateway consumer behavior.
10. Documentation names unsupported or fallback-only platforms honestly.

## 15. External References

- OpenAI streaming responses: https://platform.openai.com/docs/guides/streaming-responses
- Anthropic Messages streaming: https://docs.anthropic.com/en/docs/build-with-claude/streaming
- Gemini text generation and streaming: https://ai.google.dev/gemini-api/docs/text-generation
- OpenRouter streaming: https://openrouter.ai/docs/api-reference/streaming
- Telegram Bot API: https://core.telegram.org/bots/api
- Discord API rate limits: https://discord.com/developers/docs/topics/rate-limits

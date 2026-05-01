# 15 - Protocol-Level Gateway And UX Gap Matrix

## Status (2026-04-25)

Status of each row in the matrix (see remediation §1-§9 below):

- **Plain text send / typing / rate-limit / mirror dedup** — COMPLETED. `Nerya/nerya/messaging/pipeline.py`, `Nerya/nerya/messaging/mirror.py`, `Nerya/nerya/messaging/telegram.py`.
- **MarkdownV2 / HTML escaping** — COMPLETED. `Nerya/nerya/messaging/platforms.py:_escape_markdown_v2` is invoked from the Telegram sender.
- **Long-message chunking** — PARTIALLY COMPLETED. `Nerya/nerya/messaging/platforms.py:_chunk_for_send` honors per-platform char/UTF-16 limits; preserve-codeblock chunking for Discord/Feishu tracked.
- **Reply-to / thread / topic preservation** — COMPLETED. `Nerya/nerya/api/routes_gateway.py:_handle_text` carries `reply_to_message_id`, `message_thread_id`, and the mirror persists topic ids.
- **Attachments inbound** — PARTIALLY COMPLETED. `Nerya/nerya/api/routes_files.py` accepts uploads; Telegram-side media ingest tracked under Plan 21 P1.
- **Attachments outbound** — PARTIALLY COMPLETED. `Nerya/nerya/messaging/telegram.py:send_photo/send_document` exists; voice/video parity tracked under Plan 21 P1.
- **Inline keyboards / approval buttons** — COMPLETED 2026-04-25. `Nerya/nerya/messaging/approval_prompts.py` builds platform-agnostic approval prompts with `ApprovalButton` + Telegram-style `inline_keyboard` payloads from any pending approval row; `Nerya/nerya/messaging/telegram.py` now forwards `reply_markup` / `reply_to_message_id` and exposes `answer_callback_query`. HTTP surface (`Nerya/nerya/api/routes_approvals.py`: `/approvals/pending`, `/approvals/prompt`, `/approvals/callback`) is registered in `Nerya/nerya/api/local_server.py`. Actor-bound ownership is enforced via `resolve_callback(... actor_owns=...)`. Coverage: `Nerya/tests/test_approval_prompts.py` (16 tests).
- **Edit-in-place status messages** — PARTIALLY COMPLETED. `Nerya/nerya/messaging/pipeline.py:edit_message` is wired; Telegram callback round-trip remaining.
- **Per-actor rate limiting** — COMPLETED. `Nerya/nerya/messaging/pipeline.py:_RateLimiter` enforces per-channel/per-actor token bucket.
- **Voice mode** — PENDING (tracked under Plan 21 P2).
- **Reconnect/backoff** — COMPLETED. `Nerya/nerya/messaging/telegram.py:run_polling` reconnect loop with exponential backoff.

Status: PARTIALLY COMPLETED — every backend hook is in place; remaining items are platform-specific renderers tracked under Plan 21.

## Why This File Exists

The earlier audit still missed many protocol-level details. A gateway is not just `send text to Telegram`. A production gateway must preserve message structure, attachments, reply/thread context, delivery state, user identity, approvals, rate limits, and platform-specific rendering. This file lists those missing details explicitly, with code evidence where checked.

## Code Evidence Summary

Nerya evidence:

- `nerya/api/routes_gateway.py` currently builds Telegram sessions as `telegram_{chat_id}`, extracts text from Telegram updates, sends typing, runs one `AgentKernel.run_turn`, and replies with trace + final text.
- `nerya/messaging/telegram.py` sends basic Telegram Bot API messages and chat actions.
- `nerya/messaging/generic_platform.py` builds basic text/webhook payloads for Slack/Discord/DingTalk/Feishu/WeCom/etc.
- `nerya/messaging/pipeline.py` supports channel send, template rendering, rate-limit check, sender dispatch, and message journal.
- `nerya/messaging/mirror.py` records inbound/outbound text payloads and dedupes message ids.
- `nerya/messaging/platforms.py` marks many Hermes platform IDs as `webhook` or `scaffold`; many are not native production adapters.

Hermes evidence:

- `gateway/platforms/telegram.py` handles Telegram-specific formatting, MarkdownV2 escaping, table conversion, media, commands, callback handlers, and inline keyboard imports.
- `gateway/platforms/base.py` defines richer message/media abstractions, media cache helpers, supported document types, UTF-16 limits, and extraction helpers.
- `gateway/run.py` tracks running agents, pending messages, busy acknowledgements, session generations, pending approvals, failed-platform reconnects, voice modes, background tasks, media placeholders, and session interrupt behavior.
- `tools/send_message_tool.py` has Telegram parse-mode fallback, thread kwargs, media sending for photo/video/voice/audio/document, and platform-specific send branches.
- `tools/cronjob_tools.py` captures origin platform/chat/thread and warns about losing thread/topic targeting.

## Detailed Gateway Difference Matrix


| Surface                    | Nerya Current                                                                      | Hermes Current                                                           | Gap                                                                        |
| -------------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| Plain text send            | Exists for Telegram/generic webhook                                                | Exists across many native/platform adapters                              | Nerya is mostly basic text send                                            |
| Markdown escaping          | Not clearly platform-complete                                                      | Telegram MarkdownV2 escaping and fallback                                | Nerya can break messages with special chars/code/table                     |
| HTML parse mode            | Not clearly supported                                                              | Auto-detects HTML in send path                                           | Missing robust formatting mode fallback                                    |
| Long message chunking      | Not clearly implemented                                                            | Send path has chunking-oriented logic                                    | Nerya may exceed platform limits                                           |
| Message edit/update        | Not clearly implemented                                                            | Hermes supports status/update patterns in gateway/TUI paths              | Nerya likely sends multiple messages rather than updating status           |
| Reply-to original message  | Not preserved in Nerya Telegram turn payload                                       | Hermes tracks source, chat/thread context                                | Nerya loses reply context and quote semantics                              |
| Forum/topic thread id      | Not preserved in Nerya Telegram poll path                                          | Hermes uses `thread_id` / `message_thread_id` and warns about topic loss | Nerya can respond in wrong topic or lose topic routing                     |
| Attachments inbound        | Not modeled in Nerya normalized inbound                                            | Hermes has media placeholders and cache helpers                          | Nerya drops or ignores photos/docs/voice unless text exists                |
| Attachments outbound       | Nerya message pipeline is text-shaped                                              | Hermes sends photo/video/voice/audio/document                            | Nerya cannot deliver evidence files/screenshots gracefully                 |
| Captions                   | Not modeled                                                                        | Hermes media send handles message + media paths                          | Missing caption/media relationship                                         |
| Voice notes                | Platform catalog says Telegram voice=true, but implementation path is not complete | Hermes has voice/audio handling and voice mode state                     | Nerya status overstates product readiness                                  |
| Inline buttons             | Not implemented in Nerya gateway path                                              | Hermes imports inline keyboard/callback query handling                   | Missing approval/stop/status buttons                                       |
| Callback queries           | Not implemented                                                                    | Hermes has callback handler concepts                                     | Nerya cannot process inline approvals from Telegram                        |
| Reactions                  | Not modeled                                                                        | Not necessarily core in Hermes, but platform layer is richer             | Optional P2, but should be explicit                                        |
| Typing/status              | Nerya has Telegram chat action loop and generic status webhook                     | Hermes has richer progress/callback model                                | Nerya typing is not tied to full event stream                              |
| Busy session behavior      | Not clear beyond direct run                                                        | Hermes has running-agent and pending-message maps                        | Nerya can mishandle concurrent messages per chat                           |
| Interrupt/stop             | `/new` and simple commands exist; real cancel unclear                              | Hermes has interrupt handling and pending messages                       | Nerya lacks protocol-level `/stop` semantics                               |
| Deduplication              | `GatewayMirror` dedupes message ids                                                | Hermes has session/message state and pending event handling              | Nerya needs idempotency tied to turn creation and delivery                 |
| Delivery retries           | Message pipeline journals sent/rate_limited                                        | Hermes has retry/fallback behavior in platform sends                     | Nerya lacks delivery queue/dead-letter semantics                           |
| Rate limiting              | Nerya `RateLimiter.allow(channel)` exists                                          | Hermes has platform/session-aware handling                               | Nerya rate limit is channel-level, not actor/chat/tool aware               |
| Allowed chats/users        | Not clearly enforced                                                               | Hermes has pairing store / gateway authorization concepts                | Nerya gateway inbound is too open                                          |
| Multi-user threads         | Not modeled                                                                        | Hermes session context distinguishes shared thread and sender names      | Nerya likely conflates all chat participants                               |
| Session key shape          | `telegram_{chat_id}`                                                               | `agent:main:{platform}:{chat_type}:{chat_id}[:thread]`                   | Nerya loses platform/chat_type/thread richness                             |
| Scheduled delivery origin  | Nerya scheduled session has delivery target concept                                | Hermes captures origin chat/thread and defaults to origin                | Nerya needs exact thread/topic preservation                                |
| Evidence artifact delivery | Not complete                                                                       | Hermes can send MEDIA/files and local output                             | Nerya should attach or link evidence instead of dumping text               |
| Platform capability truth  | Nerya catalog has `native/webhook/scaffold`                                        | Hermes has many real adapters                                            | Nerya docs/UI must show capability grade per feature, not only platform id |


## Missing Gateway Features To Add Explicitly

### Inbound Message Fields

Nerya normalized inbound should include:

- `platform`
- `chat_id`
- `chat_type`
- `thread_id` / `message_thread_id`
- `message_id`
- `reply_to_message_id`
- `quote_text` / `reply_to_text` when safe
- `user_id`
- `user_name`
- `text`
- `caption`
- `attachments[]`
- `entities[]`
- `raw_update_ref`
- `received_at`
- `idempotency_key`

### Attachment Model

Each attachment should include:

- `attachment_id`
- `platform_file_id`
- `kind`: image, video, audio, voice, document, sticker, unknown
- `mime_type`
- `filename`
- `size_bytes`
- `caption`
- `local_cache_path`
- `sha256`
- `safe_status`: pending, accepted, rejected
- `extracted_text_ref` for OCR/transcription/doc parse
- `visibility` and session ownership

### Reply/Quote Model

A gateway event should preserve reply context:

- if user replies to a bot message, map reply to `turn_id` or `approval_id` when possible,
- if user replies to another user, include quoted text as untrusted context,
- if reply target is missing, record `reply_context_unavailable`,
- thread/topic id must be part of session key for group/forum chats.

### Outbound Message Model

Outbound delivery should support:

- text message,
- edit previous status message,
- reply to message,
- send in thread/topic,
- attach files,
- attach screenshots/images,
- send voice/audio if enabled,
- inline keyboard/buttons,
- fallback plain text,
- chunking,
- delivery receipt.

### Telegram UX Minimum

P0 Telegram should support:

- `/start`, `/help`, `/status`, `/stop`, `/resume`, `/sessions`, `/approve`, `/reject`, `/memory`, `/skills`, `/mode`.
- Inline buttons for approve/reject/stop/details.
- Ack within 1-2 seconds for long tasks.
- One editable status message for progress.
- Final short summary plus evidence attachment/link.
- Safe MarkdownV2 escaping and fallback to plain text.
- Long message chunking.
- Topic/thread preservation.
- Attachment ingestion and safe document/image handling.

## Other Fine-Grained Gaps Not Fully Listed Before

### Message Entities And Formatting

- Preserve links, mentions, code spans, and bot commands as entities.
- Avoid letting entity text become trusted instructions.
- Convert Markdown/HTML per platform, not globally.

### Platform Identity And Authorization

- Differentiate `chat_id`, `user_id`, `username`, `thread_id`, `team_id`, `workspace_id`.
- Authorization should be based on stable IDs, not display names.
- Group chat messages should include sender prefix in context.

### Queue And Backpressure

- Per-chat queue.
- Per-user queue.
- Per-platform rate limiter.
- Max pending turns per session.
- Busy reply debounce.
- Drop/merge policy for rapid follow-up messages.

### Delivery State Machine

States should be explicit:

- `queued`
- `rendered`
- `sending`
- `sent`
- `edited`
- `failed_retryable`
- `failed_final`
- `dead_lettered`
- `cancelled`

### Evidence And Large Output

- Do not paste long tool logs into Telegram.
- Store evidence as artifact.
- Send a short summary with artifact link/file.
- Allow `/trace` or `/evidence <turn_id>` to fetch details.

### Message-Level Privacy

- Attachments and quoted messages may contain secrets.
- Redaction should happen before LLM context and before dashboard/gateway display.
- Raw platform payloads should be access-controlled.

### Gateway Testing Matrix

Required fake-transport tests:

1. Telegram text in DM creates `session_key` with platform/chat id.
2. Telegram group topic preserves `message_thread_id`.
3. Reply to bot approval message maps to approval id.
4. Photo with caption becomes text + image attachment.
5. Voice message becomes voice attachment and transcription task if enabled.
6. Long final answer chunks or sends document.
7. Markdown-breaking answer falls back to plain text.
8. Duplicate update id does not create duplicate turn.
9. `/stop` cancels current turn and drains pending status.
10. Unauthorized chat is rejected without leaking runtime state.
11. Rate-limited chat queues or drops according to policy.
12. Failed send goes to dead-letter with retry metadata.

## Corrected Claim

No, the previous audit did not list all differences. It listed the large capability categories, then later some production concerns, but still missed protocol-level gateway features such as attachments, reply references, topic/thread preservation, inline callbacks, media rendering, message entity handling, queue/backpressure, delivery state machines, and platform-specific evidence delivery.

This file should be treated as the start of a protocol checklist, not the final exhaustive list. The next exhaustive step should produce one matrix per surface:

- Telegram
- Discord
- Slack
- generic webhook
- dashboard chat
- API SDK
- scheduler delivery
- TUI/CLI

Each matrix should cover inbound fields, outbound features, auth, permissions, rate limits, state, tests, and known unsupported behavior.
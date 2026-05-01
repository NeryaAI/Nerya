# 10 - Detailed Gateway Design Notes

## Status (2026-04-25)

Implementation evidence:

- **Inbound pipeline**: `Nerya/nerya/api/routes_gateway.py:_handle_text` already verifies source (token/allowed chat) and normalises events. `gateway_session_id`/`gateway_message_id` (`Nerya/nerya/api/routes_gateway.py:gateway_session_id`) provide stable session keys.
- **Shared command registry**: `Nerya/nerya/api/gateway_commands.py:80-272` (Plan 23 §3).
- **Outbound pipeline / mirror**: `Nerya/nerya/messaging/mirror.py` keeps idempotency. MarkdownV2 escaping lives in `Nerya/nerya/messaging/platforms.py`.
- **Acceptance**: PARTIALLY COMPLETED — long-message chunking, inline approval buttons, dead-letter outbox tracked under Plan 15.

## Goal

Make gateway behavior feel like a real assistant channel, not an HTTP adapter that dumps final text.

## Gateway Architecture To Add

### Inbound Pipeline

1. Verify source:
  - Telegram polling token or webhook secret,
  - allowed chat/user,
  - platform signature where available.
2. Normalize event:
  - `platform`, `channel_id`, `thread_id`, `user_id`, `message_id`, `text`, `attachments`, `reply_to`, `timestamp`.
3. Resolve actor and session:
  - stable `gateway_session_key`,
  - role and scopes,
  - memory namespace.
4. Parse command:
  - shared command registry,
  - slash command fast path,
  - regular message to agent.
5. Enqueue turn:
  - idempotency key from platform message id,
  - immediate acknowledgement if turn may take more than 2 seconds.

### Outbound Pipeline

1. Convert agent events to platform delivery policy:
  - ack,
  - compact progress,
  - approval prompt,
  - final result,
  - evidence artifact.
2. Render safely:
  - Markdown/HTML escape,
  - split long messages,
  - preserve code blocks,
  - attach files when too long.
3. Send through queued transport:
  - per-chat rate limit,
  - retry/backoff,
  - idempotency,
  - dead-letter.
4. Journal delivery:
  - requested,
  - sent,
  - edited,
  - failed,
  - retried.

## Telegram Specific Requirements

### Commands

- `/start` - pair and show capabilities.
- `/help` - show command menu and safety modes.
- `/status` - current turn/session status.
- `/stop` - cancel current turn.
- `/resume` - resume last session.
- `/sessions` - list recent sessions.
- `/memory` - show/search memory commands.
- `/skills` - list/invoke skills.
- `/approve <id>` and `/reject <id>` - fallback for inline buttons.
- `/mode` - switch read-only/dev/trading-safe mode.

### Message Style

- Send immediate ack: `Got it, working on <short task>...`.
- For long turns, edit one status message instead of spamming:
  - planning,
  - running tool,
  - waiting approval,
  - verifying,
  - done.
- Final message should be short and include:
  - result,
  - changed/affected artifacts,
  - verification,
  - link or command to inspect full evidence.
- Full logs go as document attachment or dashboard evidence link.

### Approval UX

Inline keyboard buttons:

- Approve once,
- Reject,
- Details,
- Stop turn.

Approval details must show redacted args and risk class. The callback must verify user role and approval id.

### Attachments

- Images/screenshots should be sent as photos where safe.
- Long markdown/evidence should be sent as document.
- Audio/voice input should be transcribed only if enabled and authorized.

## Gateway State Model

Per platform message:

- inbound event id,
- normalized event,
- actor id,
- session id,
- turn id,
- ack message id,
- status message id,
- final message ids,
- delivery state.

## Failure Modes To Handle

- Telegram markdown parse error: retry plain text.
- Message too long: chunk or attach file.
- Edit message failed: send new status message.
- Rate limit: queue and update delivery state.
- Duplicate webhook/poll event: return cached turn mapping.
- Bot removed from chat: mark channel disabled and notify dashboard.
- Unauthorized chat: ignore or send minimal rejection without leaking state.

## Acceptance Tests

1. Fake Telegram inbound produces one session and one turn.
2. Duplicate inbound does not create duplicate turn.
3. Long response is chunked or attached.
4. Markdown-breaking text is escaped or retried as plain text.
5. Approval inline callback changes pending approval state.
6. `/stop` cancels an in-flight turn and notifies the agent loop.
7. Unauthorized chat cannot trigger agent tools.
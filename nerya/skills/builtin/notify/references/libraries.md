# Notify — channels and libraries

Pick a transport before you write a script. Each has its quirks.

## Channels

| Channel | Library | Notes |
|---|---|---|
| Operator chat (default) | internal `nerya.notify.operator` | Use this unless told otherwise. |
| Slack | `slack_sdk` | App-level token; respect `chat:write` scope. |
| Discord | `discord-webhook` (sync) / `aiohttp` to webhook URL | Webhook URLs are secrets — store in env, not in code. |
| Telegram | `python-telegram-bot` | Bot token + chat id; the bot must be added to the chat first. |
| Email | `smtplib` (built-in) / `sendgrid` / `resend` | TLS only; use App Passwords on Gmail. |
| Generic webhook | `httpx` | Sign the payload (HMAC) when the receiver expects auth. |

## Choosing format per channel

- **Slack / Discord** — short, with a clear leading verb. Avoid
  multi-paragraph walls; use thread replies for detail.
- **Telegram** — supports markdown but skip code blocks > 30 lines;
  Telegram truncates messages above ~4k chars.
- **Email** — fine for digests. Subject is part of the message;
  spend effort on it.
- **Webhook** — JSON only; never a free-text body unless the
  contract says so.

## Idempotency

If a trigger may fire twice for the same event, include a stable
event id in the message and have the receiver dedupe by id. Do not
rely on humans to spot duplicates.

## Patterns

1. **Summary line first.** Whatever the channel, the first line is
   the headline.
2. **Action at the end.** "What should I do" is the last sentence,
   not buried in paragraph two.
3. **Quiet hours.** Default to off-hours suppression unless the
   user opted in. Trading desks have one schedule; humans have
   another.

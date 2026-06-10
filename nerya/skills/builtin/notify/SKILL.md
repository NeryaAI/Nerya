<!-- nerya-skill-frontmatter-start -->
---
name: notify
description: "Use when the agent must send a message out to an operator, channel, webhook, or email."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Notify

Use for outbound delivery. Do not use it for the current chat reply.

## Flow

IDENTIFY recipient, channel, urgency, and allowed content.
IF the channel, webhook, severity route, or trade-notification fan-out is not
already configured, use `evolve_core_config_patch` for `messages/channels.yml`
or `triggers/routes.yml`; do not edit live config or claim delivery is active.
KEEP message short, structured, and actionable.
REDACT secrets and noisy raw logs.
SEND through the narrowest script.
RETURN delivery status and any retry/fallback result.

## channels.yml patch shapes

Channel/bot/webhook setup requests are config-proposal work, not send
work: always land them as one `evolve_core_config_patch` call with
`target: "messages/channels.yml"` carrying the full desired config.
Secrets stay vault refs (`vault://...`) — never plaintext tokens.

- Telegram bot: `channels.telegram` with `kind: telegram`,
  `bot_token_ref: vault://...`, and a numeric `chat_id` or
  `chat_id_ref: vault://...`.
- Discord / generic webhook: `channels.<id>` with `kind: discord` or
  `kind: webhook` plus `webhook_url_ref`/`url_ref`, and `topics`
  (for example `[critical_risk]`, `[trade_execution]`) for the events
  to push.
- Severity routing: top-level `severity_routes`, for example
  `{info: [telegram], critical: [telegram, discord], silent: []}`.
  Do not invent shapes like `notifications.routing`.

The proposal stays pending operator review: report the proposal id and
say the channel is *not connected yet* — never claim the connection is
established or delivery is active before approval and a real send.

## Scripts

- `scripts/send_message.py`
- `scripts/post_channel.py`
- `scripts/send_email.py`
- `scripts/send_digest.py`

## Lazy References

- `references/full-playbook.md` for notification policy and formats.
- `references/libraries.md` for outbound libraries.

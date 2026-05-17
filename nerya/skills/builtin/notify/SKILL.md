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
KEEP message short, structured, and actionable.
REDACT secrets and noisy raw logs.
SEND through the narrowest script.
RETURN delivery status and any retry/fallback result.

## Scripts

- `scripts/send_message.py`
- `scripts/post_channel.py`
- `scripts/send_email.py`
- `scripts/send_digest.py`

## Lazy References

- `references/full-playbook.md` for notification policy and formats.
- `references/libraries.md` for outbound libraries.

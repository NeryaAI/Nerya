<!-- nerya-skill-frontmatter-start -->
---
name: notify
description: "Use whenever the agent needs to push a message *out* \u2014 to the operator, to a chat channel, to a webhook, to email \u2014 rather than just replying in the current chat. Triggers on \"send a message\", \"ping me when\", \"notify the team\", \"post to Slack / Discord / Telegram\", \"email this to\", \"alert\", \"broadcast\". Read this skill so messages stay short, structured, and easy to act on; long unprioritised pings get tuned out and become useless."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Notify playbook

A notification is competing for the user's attention with everything
else in their day. The job is to make the notification *worth the
interruption* and easy to act on.

## Anatomy of a good notification

Every outbound message has three parts, in this order:

1. **One-line summary.** What happened, in plain words. The user
   should be able to decide whether to read further from this line
   alone.
2. **Why it matters.** One short paragraph: the state, the threshold
   crossed, the implication.
3. **What to do next.** Concrete options (link, reply command,
   "no action needed"). Vague endings get ignored.

If you can't write part 3 honestly, the notification probably
shouldn't be sent — the user can't act on it anyway.

## Channel choice

- **Operator chat** — default for everything strategy- or
  trade-related. The operator is the auditor.
- **Team channel** — group-relevant alerts only; do not cross-post
  noise.
- **Email** — long-form digests, end-of-day summaries.
- **Webhook** — machine-to-machine; never user-facing as a primary
  surface.

Pick one channel per message. Cross-posting trains people to ignore
notifications.

## Frequency

- A trigger that fires every minute is a setup mistake; tune the
  cooldown.
- Daily digests are fine and often *less* annoying than per-event
  pings, even though they're longer.
- "I'll just send one more for context" is the path to noise; if
  the original message was unclear, edit and re-send instead of
  appending.

## Bundled scripts

| Script | Purpose |
|---|---|
| `scripts/send_message.py` | Send to the operator chat. |
| `scripts/post_channel.py` | Post to a named team channel / webhook. |
| `scripts/send_email.py` | Send a short email. |
| `scripts/send_digest.py` | Compose + send a daily / weekly digest. |

All read JSON payload from `--json` / `--payload-file` / stdin.

## Failure modes

- **All-caps urgency.** Use it for genuine incidents; otherwise it
  reads as panic.
- **Missing context.** "Done" is not a notification.
- **Action-less alerts.** If there is nothing to do, say so explicitly
  ("no action needed, recording for the journal") so the recipient
  doesn't search for a hidden ask.

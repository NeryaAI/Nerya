# Conversation tool authorization

The runtime owns choices, request identity, rules, and continuation. The browser
shows the actual command, file content, or structured arguments inline in the
conversation bottom dock, shared with supplemental questions. Blocking interactions
hide the ordinary composer while preserving its draft. History retains a compact
decision summary. The dock shows numbered permission choices and optional rejection
feedback. Tool authorization never opens a modal or overlay. Leaving the
request unanswered keeps it pending. Keyboard submission must not
interfere with IME composition. Failure keeps the request and feedback visible.

- Allow once consumes the existing exact, actor/session/strategy-bound verdict.
- Allow in this conversation authorizes the listed tools at the reviewed risk
  and scope for that actor/session/strategy. It survives durable-session recovery.
- Allow matching requests in this workspace authorizes only identical tool
  arguments for the same actor/strategy in this workspace. No shell prefix rules.
- Dangerous or secret-access calls offer only allow once and deny. Legacy
  records without reviewed risk/scope also offer only those choices.
- Deny never runs the pending tool or automatically starts a revised task.
  Feedback is retained in the durable decision and canonical chat history so the
  next turn receives it. Draft feedback survives navigation and reload.
- Reusable grants are derived from durable approved decisions and can be revoked.
  They are consulted only after the permission engine returns ASK, so plan mode,
  deny rules, capability restrictions, and domain gates remain authoritative.
- New decisions carry the server's request version. The transition checks it
  under the pending-record lock; a changed batch must be reviewed again.
- A duplicate callback cannot broaden the original permission choice or change
  feedback. It can reconcile the original continuation.

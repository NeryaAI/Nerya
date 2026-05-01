# Fifth-Pass Residual Micro And Ecosystem Gaps

## Status (2026-04-25)

This document captures **micro-UX residuals**. Most items are runner-up product polish, tracked at P1/P2.

Section status:

1. **Onboarding / first-run** — PARTIALLY COMPLETED. `Nerya/nerya/install/` covers basic bootstrap; full first-run wizard → Plan 27.
2. **Command discoverability / help** — COMPLETED. `Nerya/nerya/api/gateway_commands.py:BUILTIN_COMMANDS` is the unified registry consumed by CLI, dashboard, and gateway (`Nerya/nerya/messaging/telegram.py`).
3. **Error message quality** — PARTIALLY COMPLETED. Tool-runner / kernel emit structured errors; operator-friendly copy + error catalog → Plan 21 P2.
4. **Notification preferences** — PENDING. Per-channel notification preferences → Plan 21 P2.
5. **Search / history** — COMPLETED. `Nerya/nerya/agent/session_search.py` + `POST /agent/session/search` + `GET /agent/session/events` (Plan 06).
6. **Voice / multimodal renderers** — PENDING (Plan 21 P2).
7. **Inline approvals / buttons** — COMPLETED 2026-04-25 (backend). Inline approval prompts (text + Telegram-style `inline_keyboard`) are now produced from any pending approval row by `Nerya/nerya/messaging/approval_prompts.py:build_prompt` (with `ApprovalButton` / `ApprovalPrompt`/`parse_callback_data` / `resolve_callback`). The Telegram transport now forwards `reply_markup` and `reply_to_message_id` from the message envelope (`Nerya/nerya/messaging/telegram.py:54-66`) and exposes `answer_callback_query` (`Nerya/nerya/messaging/telegram.py:130-159`). HTTP surface lives in `Nerya/nerya/api/routes_approvals.py` (`/approvals/pending`, `/approvals/prompt`, `/approvals/callback`) and is registered in `Nerya/nerya/api/local_server.py:16-44`. Actor-bound ownership (Hermes parity) is enforced via `actor_owns` in `resolve_callback`. Tests: `Nerya/tests/test_approval_prompts.py` (16 tests).
8. **Long-message editing / streaming** — PARTIALLY COMPLETED. `Nerya/nerya/messaging/pipeline.py` supports edit; flood-aware backoff → Plan 21 P1.
9. **Profile / multi-account UX** — PENDING (Plan 28).
10. **Plugin / extension surface** — PENDING (Plan 30).

Status: PARTIALLY COMPLETED — every backend hook exists; remaining items are polish/UI tracked under Plans 21/27/28/30.

This fifth pass captures residual gaps that are easy to dismiss as small details but compound into poor operator experience. Some are direct Hermes parity gaps; others are supporting product surfaces required if Nerya wants to feel like a polished Hermes-class operator agent.

## Residual Gaps Still Worth Listing

### 1. Onboarding And First-Run Experience

- First-run wizard that asks what the user wants: trading runtime, coding agent, gateway bot, dashboard-only, or hybrid.
- Guided provider setup with test call and clear failure messages.
- Guided gateway setup with live send/receive test and pairing confirmation.
- Guided workspace creation with sane defaults and sample strategy/session.
- “What can I do?” command cards driven by actual enabled tools/skills.
- First-run safety explanation: live trading off, approval gates, secret storage, data retention.
- Broken setup recovery: missing Node/Python/browser/Docker/git/provider key explains exact fix.
- Demo mode that exercises streaming/tool/approval/gateway without real credentials.

### 2. Command Discoverability And Help

- Unified command registry shared by CLI, TUI, dashboard, gateway, and docs.
- Per-context help: chat help differs from cron help, gateway help, coding help, trading help.
- Command aliases and deprecation warnings.
- “Why is this command unavailable?” when missing scope/tool/provider/credential.
- Searchable command palette in dashboard/TUI.
- Gateway `/help` that reflects current platform and permissions.
- Examples next to every risky command.
- Auto-generated docs from command metadata.

### 3. Error Message Quality

- Stable error codes with user-facing explanation and operator-facing details.
- Every error includes next action: retry, configure, approve, install, inspect logs, contact operator.
- Separate model/provider/tool/gateway/auth/policy/user-input failures.
- No raw Python tracebacks in user channels unless debug mode.
- Redacted raw details available in trace/debug bundle.
- Error copy localized and platform-friendly.
- Repeated error suppression to avoid spam.
- Error-to-doc deep links.

### 4. Notification Preferences

- Per-user/channel notification levels: silent, failures-only, approvals, progress, all.
- Quiet hours and do-not-disturb windows.
- Digest mode for cron/background jobs.
- Escalation routing for urgent approvals or failures.
- Threaded notifications instead of channel spam.
- Notification dedup and coalescing.
- Final-only mode for noisy platforms.
- Separate settings for trading, coding, cron, memory, gateway, and system alerts.

### 5. Mobile And Low-Bandwidth UX

- Short status messages suitable for mobile chat clients.
- Long output converted to artifact/link instead of wall-of-text.
- Image/document previews optimized for mobile.
- Fallback when platform cannot render tables/Markdown/buttons.
- Low-bandwidth mode: fewer edits, fewer images, compressed summaries.
- Resumable links to dashboard trace from gateway message.
- Small-screen dashboard layout for approvals and incident response.
- Offline/poor-network resend and idempotency handling.

### 6. Accessibility

- Keyboard navigation for dashboard.
- Screen-reader labels for tool cards, approvals, status, diff views.
- Color contrast for risk/status badges.
- Non-color-only status indicators.
- Reduced motion for streaming/progress UI.
- Font size and density settings.
- Accessible error and form validation copy.
- Gateway-friendly plain-text alternative for rich outputs.

### 7. Template And Recipe Library

- Templates for common workflows: daily briefing, PR review bot, Telegram assistant, trading monitor, risk review, backtest report, cron digest.
- Workspace templates for coding-only, trading-only, gateway-only, team bot, local private mode.
- Prompt templates with variables and validation.
- Approval policy templates.
- Gateway formatting templates per platform.
- Postmortem and incident templates.
- Eval scenario templates.
- Migration templates from Hermes/OpenClaw setups.

### 8. Operator Education And Self-Diagnosis

- “Why did Nerya do that?” explain mode in plain Chinese/English.
- “What context did you use?” answer with manifest and citations.
- “What are you waiting on?” status answer for approvals/tools/queues/providers.
- “What can I safely ask?” examples based on current mode.
- “How do I fix this setup?” doctor-guided remediation.
- “Why was this denied?” policy explanation with override path.
- “What changed since yesterday?” changelog digest.
- “How reliable is this feature?” support-level badge.

### 9. Human Feedback Loop

- Thumbs up/down or correction capture per answer/tool run.
- Feedback reason taxonomy: wrong, slow, too verbose, unsafe, ignored instruction, bad formatting, duplicate message.
- Feedback routes into eval/memory/reflection without instantly mutating behavior.
- Operator-visible feedback inbox.
- Regression test generation from negative feedback.
- “Never do this again” scoped rule proposal with approval.
- Satisfaction metrics by surface: gateway, dashboard, coding, trading, cron.
- Feedback export for model/eval analysis.

### 10. Conversation And Instruction Hierarchy UX

- Visible active instructions: system, workspace, session, channel, user memory, latest correction.
- Conflict explanation when instructions disagree.
- Latest user correction pinned until turn completes.
- Per-session “pinned constraints” editor.
- Temporary instruction expiry.
- Channel-specific instruction overlays.
- Safe reset of stale instructions.
- Prompt-injection warning shown when untrusted input attempts override.

### 11. Memory UX Details

- Memory inbox for proposed memories before durable write.
- Memory edit/delete/merge UI.
- Memory source preview and confidence/age/scope display.
- Memory conflict resolution: old rule vs new correction.
- Memory stale warning.
- Memory disable per channel/session.
- Sensitive-memory classification.
- Memory import/export with redaction.

### 12. Search And Index Quality

- Incremental indexing for sessions, artifacts, code, docs, memory, gateway messages.
- Search result ranking with source type and recency.
- Facets: session, actor, tool, platform, strategy, artifact type, date.
- Snippet redaction and permission checks.
- Reindex/repair command.
- Search explain: why this result matched.
- Query suggestions and typo tolerance.
- Performance budget for large workspaces.

### 13. File And Artifact Preview UX

- Diff viewer for patches and config changes.
- Image/audio/document preview with metadata.
- CSV/JSON/table preview with truncation.
- Browser screenshot gallery.
- Tool output folding by severity and section.
- Safe rendering for HTML/Markdown to prevent script injection.
- Artifact pinning and favorites.
- Artifact comparison across runs.

### 14. Trading-Specific Operator UX Still Missing Relative To Nerya Ambition

Even if Hermes is general-purpose, Nerya must exceed it in trading UX:

- Explain every trade intent with data sources, risk checks, approval status, and expected failure modes.
- Live/paper/sim distinction visible everywhere.
- Account/venue/chain credential readiness matrix.
- Kill-switch state and last-change actor visible in dashboard/gateway.
- Trade replay and counterfactual “what would have happened?” report.
- Strategy change diff with risk impact.
- PnL attribution and anomalous result warning.
- Compliance-friendly audit trail for every live action.

### 15. Gateway Message Formatting Microdetails

- Preserve nested lists, blockquotes, code fences, tables, links, mentions, emojis.
- Platform-specific escaping for MarkdownV2/HTML/Slack mrkdwn/Discord Markdown/Feishu cards.
- Avoid accidental mentions like `@everyone` unless explicitly approved.
- Long code output as file attachment instead of chat spam.
- Inline buttons degrade gracefully to text commands.
- Reply-to original message by default; thread where possible.
- Include compact trace footer only when configured.
- Final message should not repeat progress text already sent.

### 16. Gateway Identity And Presence

- Bot display name/avatar/status per workspace/platform.
- Presence/typing indicators that match actual active work.
- Human-readable session title in gateway threads.
- Channel topic/status updates for long-running incidents.
- Multi-bot or multi-workspace identity separation.
- “Who am I talking to?” command showing model/mode/workspace.
- Prevent bot loops when multiple bots are in same channel.
- Mention/reply heuristics that avoid hijacking unrelated group chats.

### 17. Admin And Operator Governance UI

- Admin dashboard for users, roles, workspaces, gateways, secrets, policies, queues, jobs.
- Approval queue with filters and bulk actions.
- Audit log search and export.
- Kill switches and maintenance mode.
- Provider health/cost dashboard.
- Plugin/skill risk review dashboard.
- Queue dead-letter replay UI.
- Retention and privacy controls.

### 18. Compatibility With External Operator Habits

- GitHub issue/PR workflow conventions.
- Slack/Discord thread norms.
- Telegram group/private chat norms.
- Enterprise SSO/SCIM-like expectations if team use is claimed.
- Local-first/private-mode expectations for sensitive coding/trading work.
- Backup/export expectations before upgrades.
- “No surprise live action” expectation for trading and shell tools.
- Consistent Chinese/English mixed-language behavior.

### 19. Abuse, Spam, And Cost Attack Protection

- Gateway spam throttling and abuse detection.
- Prompt bombing/token exhaustion guard.
- Attachment zip bomb/large file guard.
- Webhook replay attack protection.
- Approval callback forgery prevention.
- Model/tool retry storm prevention.
- Per-channel spend caps.
- Automatic quarantine of suspicious inbound content.

### 20. Supportability

- One-click support bundle with redacted config, logs, traces, versions, health, recent errors.
- Reproduction script generator for a failed turn.
- “Copy debug info” button in dashboard and gateway command.
- Version and capability report.
- Known-issues page generated from failing checks.
- Minimal diagnostic mode that does not call paid providers.
- Test gateway message command.
- Health snapshot command for remote operators.

## Fifth-Pass Priority Additions

1. First-run/onboarding wizard and demo mode.
2. Unified command registry and generated help.
3. User-facing error-code system with next actions.
4. Notification preferences and quiet-hour routing.
5. Mobile/low-bandwidth/accessibility pass.
6. Template/recipe library for common workflows.
7. Feedback-to-eval loop with regression generation.
8. Instruction hierarchy and memory UX cockpit.
9. Search/index quality layer.
10. Admin/supportability surfaces.

## Correction To Previous Files

The earlier files list core capability, platform, ecosystem, and production-governance gaps. This file adds the remaining user-experience and supportability layer: onboarding, command discoverability, error copy, notification controls, mobile/accessibility, templates, feedback, instruction hierarchy, memory/search UX, artifact previews, trading-specific operator UX, message formatting microdetails, identity/presence, admin UI, abuse protection, and support bundles.

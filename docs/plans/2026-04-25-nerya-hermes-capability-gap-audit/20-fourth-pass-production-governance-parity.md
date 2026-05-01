# Fourth-Pass Production And Governance Parity Gaps

## Status (2026-04-25)

This document is a **production-governance backlog**. Most items are P1/P2 platform work that lands after the runtime parity sprint (Plans 01-08) and the gateway/protocol sprint (Plans 15/17/21).

Section status:

1. **Multi-tenancy / workspace isolation** — PARTIALLY COMPLETED. Workspace path scoping in `Nerya/nerya/workspace/manager.py`; per-actor token + scope checks in `Nerya/nerya/api/auth.py`. Encryption boundaries / role catalog → Plan 20 P1.
2. **Queueing / backpressure** — PARTIALLY COMPLETED. Inbound queue + dedup in `Nerya/nerya/messaging/mirror.py`; LLM rate limits in `Nerya/nerya/llm/rate_limits.py` + `Nerya/nerya/llm/budget.py`; dead-letter for triggers in `Nerya/nerya/triggers/dead_letter.py`. Priority lanes / queue-depth UI → Plan 20 P1.
3. **High availability / crash recovery** — PARTIALLY COMPLETED. `Nerya/nerya/agent/recovery.py` covers open-turn classification (covered by `Nerya/tests/test_turn_recovery.py`); leader election → Plan 20 P2.
4. **Release engineering / supply-chain** — PENDING. Tracked under Plan 27 (install/db/frontend lifecycle).
5. **Cost controls / billing meters** — PARTIALLY COMPLETED. LLM usage in `Nerya/nerya/llm/usage.py`; per-actor cost roll-up → Plan 20 P1.
6. **Data lifecycle / retention** — PARTIALLY COMPLETED. Mirror TTL + journal pruning. Per-actor purge tools → Plan 20 P1.
7. **Observability / SLOs** — PARTIALLY COMPLETED. `Nerya/nerya/observability/` exposes traces; SLO dashboards → Plan 20 P1.
8. **Auth / authorization model** — PARTIALLY COMPLETED. Loopback + token check in `Nerya/nerya/api/auth.py`; full actor model + per-token scopes → Plan 20 P1.
9. **Plugin / supply-chain trust** — PENDING (Plan 30 follow-up).
10. **Documentation / runbooks** — PENDING (out of scope for capability sprint).

Status: PARTIALLY COMPLETED — production foundations exist; advanced governance (multi-tenant, leader election, signed plugins, full SLO dashboards) tracked as P1/P2 platform work.

This fourth pass covers another class of gaps that were still underrepresented: production governance, multi-user operation, release engineering, compatibility, data lifecycle, cost controls, supply-chain trust, and operational SLOs. These are not flashy agent features, but they decide whether Nerya can be safely used as a persistent operator system instead of a local prototype.

## Additional Missing Surfaces

### 1. Multi-Tenancy And Workspace Isolation

Nerya needs explicit rules for whether it is single-user local only or multi-user/team capable.

- Tenant/workspace id on every session, gateway event, tool call, artifact, memory, approval, and cron job.
- Per-tenant encryption boundaries for vault, tokens, media, logs, and memory.
- Workspace quota: storage, active turns, background jobs, token spend, gateway messages, browser sessions.
- Cross-workspace read prevention for memory, artifacts, session search, and file references.
- Admin/operator/viewer roles per workspace.
- Ownership transfer for sessions, jobs, skills, and gateway channels.
- Team audit logs with actor identity and impersonation controls.
- “Shared channel” semantics: one group chat may map to multiple users and multiple workspaces.

### 2. Queueing, Backpressure, And Load Shedding

Earlier docs mention busy policy, but not full queue architecture:

- Durable queues for inbound gateway messages, scheduled jobs, tool jobs, delivery jobs, and retries.
- Queue depth visibility in dashboard/gateway/status command.
- Priority lanes: interactive chat > approval callbacks > cron > background eval > low-priority memory jobs.
- Backpressure messages when model/tool/gateway is overloaded.
- Dead-letter queues with replay controls.
- Per-actor and per-channel rate limits.
- Fanout controls for subagents and multi-platform delivery.
- Load shedding that fails safely instead of silently losing events.

### 3. High Availability And Crash Recovery

Need to define how Nerya behaves across process crashes/restarts:

- Leader election or single-writer lock for cron/gateway workers.
- Idempotent event application after restart.
- Open turn recovery policy: resume, cancel, abandon, or operator review.
- Gateway adapter reconnect state and missed update catch-up.
- Durable tool/process state for long-running jobs.
- Health checks for API, dashboard proxy, gateway workers, cron workers, model providers, MCP servers.
- Liveness/readiness probes if deployed under Docker/systemd/Kubernetes.
- Graceful shutdown: stop accepting new turns, finish/cancel current jobs, flush journals.

### 4. Data Model, Storage, And Migration Discipline

Nerya has file/journal foundations, but Hermes-like product maturity needs storage contracts:

- Versioned event schema with compatibility rules.
- Migration scripts for workspace layout, sessions, journals, memory, artifacts, and config.
- Schema validation at read/write boundary.
- Corruption detection and repair for JSONL/state files.
- Indexes for session search, artifacts, memory, gateway message ids, approval ids, tool calls.
- Storage compaction and archival policy.
- Backward-compatible readers for old event versions.
- Test fixtures that pin older schema versions.

### 5. API Contract Versioning And Client Compatibility

Missing API maturity details:

- Stable public API version namespace.
- OpenAPI or generated typed client contract.
- Error envelope standard across every route.
- Pagination/filter/sort conventions for sessions, logs, artifacts, jobs, messages.
- Idempotency keys for mutating API calls.
- Request correlation id propagation.
- Deprecation policy and compatibility tests for dashboard/SDK/CLI.
- WebSocket/SSE protocol versioning and reconnect semantics.

### 6. Cost Governance And Spend Controls

Nerya has LLM budget foundations, but daily operator use needs stronger cost controls:

- Per-user/per-workspace/per-session/per-job token and dollar budgets.
- Live cost meter in frontend/TUI/gateway status.
- Cost attribution by model, tool, gateway, subagent, cron job, and strategy.
- Alert thresholds and hard stops.
- Budget override workflow with approval and expiry.
- Fallback-to-cheaper-model policy with visible explanation.
- Cost anomaly detection: sudden loop, runaway subagents, repeated retries.
- Historical cost reports and export.

### 7. Performance Budgets And Latency UX

Not only correctness; Nerya needs latency targets:

- Time-to-first-ack for gateway and dashboard chat.
- Time-to-first-token/status event for streaming.
- Tool-start latency and max idle gap before progress update.
- Gateway edit frequency caps per platform.
- Context build/compression latency budget.
- Dashboard load time and large session virtualization.
- Search/indexing response budgets.
- Load tests for many simultaneous sessions/channels/subagents.

### 8. Supply Chain And Marketplace Trust

Skill/plugin/tool ecosystems create trust problems:

- Skill signing or trusted-source provenance.
- Dependency lockfile verification and vulnerability scan.
- Permission manifest review before install.
- Sandbox risk labels and install-time warnings.
- Update diff review for installed skills/plugins.
- Revocation/disable list for malicious skills.
- Reproducible builds for packaged releases.
- Plugin isolation so dashboard plugins cannot steal tokens or workspace data.

### 9. Policy Engine And Centralized Risk Decisions

Current permissions/risk logic is spread across trading, skills, and security. Need a central policy layer:

- One policy decision API for tool, skill, gateway, file, shell, browser, trading, memory, secret, and network actions.
- Decision record includes actor, resource, action, risk, allow/deny/approval, reason, expiry.
- Policy-as-data config with tests.
- Dry-run policy evaluation endpoint.
- Channel-specific policy overlays.
- Emergency kill switch by workspace/provider/gateway/tool class.
- Policy audit dashboard.
- Regression tests for approval bypass and policy drift.

### 10. Secret Lifecycle Management

Vault exists, but full lifecycle is broader:

- Secret creation/update/rotation/revocation workflows.
- Secret usage inventory: which skills/gateways/jobs use which secret refs.
- Expiry reminders and stale secret detection.
- Scoped secret grants to tools/subagents/jobs.
- Just-in-time secret reveal for approved operations.
- Redacted support bundle and logs.
- Secret leakage scanner across journals, artifacts, memory, and gateway outputs.
- Provider-specific credential validation without exposing secret value.

### 11. Artifact Governance

Previous docs mention artifacts, but not full governance:

- Artifact type registry: text, diff, image, audio, document, browser screenshot, model transcript, tool output, trade report.
- Artifact access control by actor/workspace/session.
- Artifact provenance: tool, input event, source URL/file, redaction state, checksum.
- Preview generation and safe rendering.
- Retention class and delete policy.
- Large artifact streaming/download with auth.
- Virus/malware scanning for inbound files where relevant.
- Artifact references stable across compression, replay, and export/import.

### 12. Human Handoff And Escalation

Hermes-like operator systems need handoff paths:

- Escalate to human operator when approval times out, tool fails repeatedly, risk is high, or auth is missing.
- Home-channel incident summary with exact next actions.
- Assign incident to operator/user/team.
- Pause/resume workspace or gateway channel.
- Manual override with audit trail.
- Postmortem template for failed autonomous actions.
- User-visible “I am blocked because…” status instead of silent failure.

### 13. Compliance / Legal / Data Residency

If Nerya handles chat, trading, files, and credentials:

- Data residency setting for logs/artifacts/media.
- PII classification and redaction policy.
- Right-to-delete per chat/user/workspace.
- Exportable audit logs for compliance.
- Trading-specific compliance records for live execution approvals.
- Consent and disclosure messages for group chats/gateways.
- Retention overrides for regulated trading artifacts.
- Policy for using third-party model providers with sensitive data.

### 14. Release Engineering And CI/CD

Missing release discipline details:

- Matrix tests for Windows/macOS/Linux and Python/Node versions.
- Smoke tests for packaged CLI, dashboard, gateway, service start.
- Migration tests from older workspace versions.
- Golden transcript tests for agent loop changes.
- Gateway fake transport integration tests per platform.
- Canary release and rollback plan.
- Changelog generation tied to user-visible capabilities.
- Versioned docs published with releases.

### 15. Observability Deepening

Earlier observability notes were still broad. More precise needs:

- Trace id propagation from inbound message -> event -> model -> tool -> artifact -> outbound message.
- Structured span model for agent loop phases.
- Metrics for retries, cancellations, approvals, queue age, gateway edit failures, model fallback, compression count.
- Log sampling and redaction at source.
- Error taxonomy shared across backend, frontend, gateway, CLI.
- Debug bundle for a single turn/session/job.
- Operator “why did this happen?” page with correlated timeline and raw sanitized records.
- Alert routing by severity and component.

### 16. UX Quality Gates

Make “feels bad” measurable:

- Time-to-ack and time-to-progress acceptance thresholds.
- No duplicate final messages acceptance tests.
- No silent dropped attachments acceptance tests.
- Cancellation must produce one clear final state.
- Every pending approval has visible owner, expiry, and risk reason.
- Every tool failure gives next action.
- Long answers preserve code blocks/tables on each platform.
- Latest user correction must override stale context/memory in test cases.

### 17. Agent Behavior Configuration

Need product-level behavior knobs:

- Autonomy level: ask-first, balanced, autonomous, yolo-like per session/channel.
- Verbosity level: compact, normal, debug.
- Evidence mode: brief, tool trail, full trace.
- Model routing mode: cheap, balanced, high-quality, local-only.
- Gateway response mode: edit-single-message, progressive-messages, final-only.
- Memory mode: off, recall-only, write-after-approval, auto-write.
- Coding mode: read-only, proposal-only, patch-allowed, shell-allowed.
- Trading mode: disabled, paper, approval-gated live, restricted live.

### 18. Governance Of Self-Evolution

Earlier self-evolution notes need governance:

- Change proposal categories: prompt, config, skill, tool, policy, model routing, trading strategy.
- Required evidence for each proposal category.
- Risk scoring and mandatory test gates.
- Rollout plan, rollback plan, owner, expiry.
- Shadow-mode evaluation before activating behavior changes.
- Compare-before/after metrics.
- Prevent self-evolution from weakening security, auth, risk, or approval policies.
- Record rejected proposals and why.

### 19. Ecosystem Compatibility Matrix

Need a machine-readable matrix for what Nerya claims:

- Hermes parity claim by surface: none/partial/compatible/superset.
- Claude Code/Codex-like coding parity claim by surface.
- Gateway platform support level: scaffold/send-only/full-duplex/full-media/full-approval.
- Tool support level: unavailable/manual/internal/user-facing/streaming/cancellable.
- Frontend support level: hidden/static/live/interactive/replayable.
- Test status per claim.
- Docs status per claim.
- Owner and target milestone.

### 20. “Do Not Claim” List

To avoid misleading future docs, explicitly do not claim these until implemented and tested:

- Hermes gateway parity.
- Full Telegram parity.
- Streaming frontend parity.
- Backend cancellation parity.
- Multi-user/team readiness.
- Secure plugin marketplace.
- Full coding-agent parity.
- Full MCP OAuth lifecycle parity.
- Production HA readiness.
- Self-evolving safe agent without operator governance.

## Fourth-Pass Priority Additions

Add these after the first three backlog files:

1. Product support matrix with claim/test/doc status.
2. Event schema versioning and migration discipline.
3. Durable queue/backpressure/dead-letter design.
4. Multi-tenant actor/workspace isolation model.
5. Central policy engine for all risky actions.
6. Secret/artifact lifecycle governance.
7. Cost/performance SLOs and dashboards.
8. Release/CI/CD/migration/golden transcript gates.
9. Supply-chain trust for skills/plugins/tools.
10. UX quality gates that quantify “not terrible to use”.

## Correction To Previous Addenda

The previous files focus on feature parity. This file adds the missing governance and production-readiness layer: multi-tenancy, queues, HA, schema migrations, API compatibility, cost and latency SLOs, supply-chain trust, central policy, secret/artifact lifecycle, human handoff, compliance, release engineering, and explicit claim discipline.
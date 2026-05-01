# 13 - Additional Overlooked Surfaces

## Status (2026-04-25)

Section-by-section status (verified file paths only; remaining work tracked under the listed downstream plans):

1. **Rate limits / quotas** — PARTIALLY COMPLETED. Messaging side covered by `Nerya/nerya/messaging/rate_limits.py`. LLM side covered by `Nerya/nerya/llm/rate_limits.py` + `Nerya/nerya/llm/budget.py` + `Nerya/nerya/llm/credential_pool.py`. Per-actor API rate limit + per-session cost cap → Plan 20 P1.
2. **Secret lifecycle** — PARTIALLY COMPLETED. `Nerya/nerya/security/vault.py` provides the vault primitives. Rotation/import workflow + non-default passphrase preflight → Plan 20 P1.
3. **Data retention** — PARTIALLY COMPLETED. Inbound/outbound dedupe + TTL inside `Nerya/nerya/messaging/mirror.py`. Per-actor purge + transcript retention → Plan 20 P1 §6.
4. **Attachments / multimodal** — PARTIALLY COMPLETED. `Nerya/nerya/api/routes_workspace.py` accepts uploads; OCR / image-context pipeline → Plan 21.
5. **Command registry** — COMPLETED. `Nerya/nerya/api/gateway_commands.py:BUILTIN_COMMANDS` is the single registry consumed by CLI, dashboard palette, and gateways (Plan 23 P0 §3).
6. **Background jobs** — COMPLETED. `Nerya/nerya/skills/builtin/operator_skill/actions.py` `process_start/list/status/output/stop` + `_PROCESS_REGISTRY` (Plan 04 P0 §1).
7. **Scheduling semantics** — PARTIALLY COMPLETED. `Nerya/nerya/triggers/runtime.py` + `Nerya/nerya/triggers/scheduled_session.py` + `Nerya/nerya/triggers/cooldown.py` + `Nerya/nerya/triggers/dead_letter.py`. Pause/resume + dry-run UX → Plan 07 P1 §2.
8. **API versioning** — PARTIALLY COMPLETED. Routes mount through `Nerya/nerya/api/local_server.py`; all `routes_*.py` modules. OpenAPI generation + `/api/v1` namespace → Plan 20 P1 §3.
9. **Frontend auth UX** — PARTIALLY COMPLETED. Header-token enforcement in `Nerya/nerya/api/auth.py`; dashboard SSE login UX → Plan 05 P1.
10. **Prompt injection / untrusted data** — COMPLETED. Trust fencing inside `Nerya/nerya/agent/prompt_firewall.py` + `Nerya/nerya/agent/context_builder.py`; safety net in `Nerya/nerya/agent/kernel.py` (Plan 22).
11. **Human handoff** — COMPLETED. Skill manifests carry `approval_gate`; the kernel routes risky skills through `Nerya/nerya/harness/tool_runner.py` and emits `approval.request` events on the streaming bus.
12. **Evaluation harness** — PARTIALLY COMPLETED. `Nerya/tests/test_streaming_bus.py`, `Nerya/tests/test_cancellation.py`, `Nerya/tests/test_operator_skill.py`, `Nerya/tests/test_context_compression.py`, `Nerya/tests/test_transcript_compact.py`, `Nerya/tests/test_turn_recovery.py` form the smoke matrix. Golden gateway traces → Plan 20 P1 §5.
13. **Deployment modes** — PARTIALLY COMPLETED. `nerya doctor` exists (`Nerya/nerya/cli/`); per-mode preflight expansion → Plan 20 P1 §1.

## Purpose

This file lists important surfaces not fully covered by the first audit or by the gateway/auth/context documents. These are the kinds of details that create reliability gaps even after the obvious features are built.

## 1. Rate Limits, Quotas, And Cost Controls

Missing details:

- Per-actor turn rate limit.
- Per-gateway chat rate limit.
- Per-tool concurrency limit.
- Per-session token/cost budget.
- Per-schedule budget.
- Provider fallback and circuit breaker.
- Dashboard display for remaining budget.

Acceptance:

- A spammy Telegram chat cannot exhaust LLM/tool budget or block owner sessions.

## 2. Secret Lifecycle

Current `SecretVault` protects plaintext reveal, but production needs lifecycle controls.

Missing details:

- non-default vault passphrase requirement outside local mode,
- secret rotation workflow,
- secret last-used metadata,
- stale secret detection,
- per-scope secret access audit,
- import/export/backup story,
- environment variable fallback policy.

Acceptance:

- `nerya doctor` fails production mode if vault uses default passphrase or secret scopes are too broad.

## 3. Data Retention And Privacy

Missing details:

- retention policy for transcripts, tool outputs, gateway messages, memories, and attachments,
- delete/export user data,
- redact before persistence vs redact before display distinction,
- local-only vs cloud/deployed mode defaults.

Acceptance:

- Operator can prune a gateway user's sessions and memories without deleting unrelated strategy history.

## 4. Attachments And Multimodal Inputs

Missing details:

- gateway attachment ingestion,
- safe file type allowlist,
- OCR/transcription policy,
- image context budget,
- evidence artifact storage,
- malware/size checks.

Acceptance:

- Telegram image/document input is stored safely, summarized, and linked to the turn without leaking to unrelated sessions.

## 5. Command Registry And Help Consistency

Missing details:

- one canonical command registry for CLI/dashboard/gateway/TUI,
- aliases,
- per-surface visibility,
- per-role availability,
- generated help/menu/autocomplete.

Acceptance:

- Adding a command updates CLI help, dashboard command palette, Telegram menu, and gateway help from one source.

## 6. Background Jobs And Process Lifecycle

Missing details:

- process registry,
- job logs,
- job cancellation,
- restart policy,
- owner/session association,
- dashboard/gateway status.

Acceptance:

- A long-running job can be started, inspected, cancelled, and resumed/retried with logs.

## 7. Scheduling Semantics

Missing details:

- timezone handling,
- missed tick behavior,
- overlap policy,
- idempotency per tick,
- schedule owner identity,
- delivery targets,
- schedule pause/resume and dry-run.

Acceptance:

- A scheduled job cannot overlap itself and cannot run under ambiguous actor permissions.

## 8. API Versioning And SDK Compatibility

Missing details:

- `/api/v1` stable route namespace,
- OpenAPI generation,
- SDK compatibility matrix,
- deprecation policy,
- contract tests for Python and TS SDKs.

Acceptance:

- Dashboard and SDKs cannot silently drift from backend route contracts.

## 9. Frontend Auth And Session UX

Missing details:

- login/pairing flow,
- session expiry,
- role display,
- route guards,
- CSRF token handling,
- secure storage of tokens,
- logout/revoke all sessions.

Acceptance:

- Browser refresh preserves safe session state, but stolen localStorage alone is not enough for privileged actions.

## 10. Prompt Injection And Untrusted Data

Nerya already fences untrusted text in context. The missing part is applying that consistently to all new surfaces.

Missing details:

- attachments are untrusted,
- gateway messages are untrusted,
- web/browser output is untrusted,
- tool output may be untrusted,
- memory can contain stale or malicious text,
- skill instructions have trust levels.

Acceptance:

- A Telegram message or website cannot override tool permission policy through prompt injection text.

## 11. Human Handoff And Escalation

Missing details:

- when agent should stop and ask,
- escalation to owner/admin,
- unresolved approval expiry,
- failed scheduled job notification,
- live trading emergency stop UX.

Acceptance:

- Dangerous ambiguity produces a structured clarification/approval event, not silent action or generic failure.

## 12. Evaluation And Regression Harness

Missing details:

- golden conversation traces,
- gateway fake transports,
- auth permission matrix tests,
- context compression fixtures,
- tool permission violation tests,
- UI event rendering tests,
- end-to-end smoke for local operator mode.

Acceptance:

- Before each release, one command runs the local operator smoke, gateway smoke, auth matrix, context invariant tests, and dashboard typecheck.

## 13. Deployment Modes

Missing details:

- local-only,
- LAN dashboard,
- VPS with gateway,
- team/shared workspace,
- live trading mode.

Each mode needs explicit defaults for auth, CORS, vault, gateway, memory retention, and tool permissions.

Acceptance:

- `nerya preflight --mode vps_gateway` can tell the operator exactly what is unsafe before starting.
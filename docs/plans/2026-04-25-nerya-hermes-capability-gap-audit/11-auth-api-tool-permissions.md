# 11 - Auth, API, And Tool Permission Design

## Status (2026-04-25)

Implementation evidence:

- **Auth kinds + route scoping**: `Nerya/nerya/api/auth.py` enforces `loopback_dev` mode, validates `X-Nerya-Token` against the configured token hash, and routes hit `auth_mod.check_request` (`Nerya/nerya/api/local_server.py:98-118`) before dispatch. **Per-route scope mapping is now enforced**: `Nerya/nerya/api/route_scopes.py:1-260` declares the full route → minimum-scope matrix (longest-prefix match, method-aware overrides, anonymous paths, `admin:ops` fallback for unknown routes). `auth_mod.authorize_route` (`Nerya/nerya/api/auth.py:213-256`) consults the matrix after authentication and journals `permission.allowed` / `permission.denied` for every request. The capability matrix surfaces the rules at `GET /runtime/capability_matrix.route_scopes` (`Nerya/nerya/api/routes_capability.py:265-289`). `nerya doctor` adds an `api.token_scopes` check (`Nerya/nerya/ops/diagnostics.py:680-786`) that flags wildcard grants in token mode and typoed scope names. Coverage: `Nerya/tests/test_api_route_scopes.py` (38 cases — required-scope lookups, parse_scopes, authorize, end-to-end auth + scope enforcement with token grants, journaling, capability-matrix integration); diagnostic coverage: `Nerya/tests/test_diagnostics.py::test_check_token_scopes_flags_wildcard_in_token_mode` and adjacent.
- **Tool permission gates**: every skill action declares `permissions`, `risk_gate`, `approval_gate`, and (now) `agent_query_only`/`agent_hint`/`agent_payload_hint` (e.g. `Nerya/nerya/skills/builtin/operator_skill/skill.yml:33-340`). The kernel's safety net (Plan 22) enforces `agent_query_only` semantics, and `Nerya/nerya/skills/runtime.py:111-168` journals every call with `loaded_via` + `permissions`.
- **Subagent narrowing**: `Nerya/nerya/subagents/dispatcher.py:33-38` (denylist) and `Nerya/nerya/subagents/registry.py:DEFAULT_SUBAGENT_SKILLS` (per-role allowlist) keep coding/critic lanes off trading skills.
- **Audit**: every API request is logged via `Nerya/nerya/api/auth.py`'s decision path; every tool call writes `kind: skill.call.start/done/error` to `Nerya/workspace/journals/skills.jsonl`. Permission decisions (allow/deny) write `permission.allowed`/`permission.denied` to `journals/security_events.jsonl`.
- **Dashboard → backend auth forwarding**: `Nerya/dashboard/app/api/proxy/[...path]/route.ts` now forwards `Authorization`, `X-Nerya-Token`, and cookie headers from the browser to the backend, falls back to a server-side `NERYA_API_TOKEN` when the client did not send one, strips hop-by-hop headers, and supports the full HTTP verb set (GET/POST/PUT/PATCH/DELETE/HEAD). This means per-actor route scoping (`Nerya/nerya/api/auth.py`) actually works end-to-end through the dashboard proxy.

Status: PARTIALLY COMPLETED — actor model groundwork + per-route scope enforcement + token grant audit shipped 2026-04-25; full per-actor cost cap + secret CRUD UI tracked under Plan 20.

## Current Risk

Nerya's API and dashboard are currently shaped like local development surfaces. That is dangerous once gateways, browser UI, schedules, and external SDKs all call the same runtime.

## Authentication Model

### Auth Kinds

- `loopback_dev`: allowed only from `127.0.0.1` and only when `runtime.auth.mode=local`.
- `dashboard_session`: cookie or bearer token for browser UI.
- `service_token`: long-lived scoped token for SDK/scripts/automation.
- `gateway_actor`: derived from verified platform inbound event.
- `schedule_actor`: derived from schedule config and service identity.
- `admin_recovery`: local-only emergency mode for owner.

### Token Storage

- Store token hashes, not plaintext.
- Show token preview and fingerprint only.
- Support revocation and expiry.
- Tie tokens to scopes and actor id.

### Route Scopes

- `read:runtime`
- `read:sessions`
- `write:chat`
- `write:tools`
- `write:memory`
- `write:skills`
- `write:config`
- `write:secrets`
- `trade:paper`
- `trade:live`
- `admin:`*

## API Authorization Matrix


| Route Family          | Minimum Scope                   | Notes                                  |
| --------------------- | ------------------------------- | -------------------------------------- |
| `/health`             | none or `read:runtime`          | no secrets, no workspace data          |
| `/workspace/`*        | `read:runtime`                  | write actions need `write:config`      |
| `/agent/run_turn`     | `write:chat`                    | tool scopes are checked later          |
| `/agent/stream`       | `write:chat`                    | emits redacted events only             |
| `/security/secrets/*` | `write:secrets`                 | no reveal route                        |
| `/trading/*`          | `trade:paper` or `trade:live`   | live still requires risk/approval gate |
| `/skills/*`           | `read:runtime` / `write:skills` | install/enable needs write             |
| `/gateway/inbound`    | `gateway:webhook`               | actor resolved per platform            |
| `/gateway/send`       | `gateway:send`                  | should usually be internal-only        |
| `/approvals/*`        | `approve:*`                     | approval id and actor must match       |
| `/ops/preflight`      | `read:runtime`                  | may include redacted sensitive state   |


## Tool Permission Model

### Policy Record

A tool call should be evaluated against:

```yaml
actor: gateway:telegram:123
session_id: sess_x
turn_id: turn_x
tool: terminal
operation: shell
risk: dangerous
resources:
  paths: ["repo/**"]
  domains: []
  accounts: []
requested_args_hash: sha256:...
decision: allow | require_approval | deny
reason: ...
```

### Tool Classes

- `read_file`: safe but path-scoped.
- `write_file` / `patch`: approval unless session has write grant.
- `terminal`: approval by default; deny destructive commands unless explicit.
- `browser`: allow public GET; approval for login/session/click actions.
- `network`: domain allowlist.
- `secret`: explicit secret scope.
- `memory`: actor namespace and operation-specific scope.
- `trading`: existing risk and approval gate.
- `gateway_send`: actor/channel scoped.

### Grants

- One-call grant: exact args hash, expires after use.
- Turn grant: same class/resource for one turn.
- Session grant: scoped to session and actor.
- Skill grant: scoped to skill id and manifest permissions.
- Schedule grant: scoped to schedule id and time window.

## Subagent Permission Rules

- Subagents receive narrowed grants.
- Default subagent mode is read-only.
- Write subagents need assigned paths.
- Subagents cannot approve their own escalations.
- Subagent outputs are untrusted until parent verifies.

## Security Events

Every auth and permission decision should append a redacted event:

- `auth.accepted`
- `auth.rejected`
- `permission.allowed`
- `permission.denied`
- `permission.approval_required`
- `approval.created`
- `approval.resolved`

## Acceptance Tests

1. Unauthenticated `/security/secrets/list` is blocked outside local mode.
2. Gateway actor with `write:chat` cannot call terminal without approval.
3. Subagent cannot write outside assigned path.
4. Skill cannot resolve a secret outside declared scope.
5. Revoked service token cannot call API.
6. Approval args hash mismatch is rejected.


# 07 - Config, Gateways, Scheduler, MCP/ACP, and Deployment Gap

## Current Nerya Capability

Nerya has real config and integration surfaces, especially for local trading operation.

Evidence:

- `nerya/core/config.py` and workspace `nerya.yml` drive runtime behavior.
- `nerya/cli/commands/core.py` exposes `doctor`, `preflight`, `certify`, and service commands.
- `nerya/ops/preflight.py` and `nerya/ops/certification.py` define production readiness checks and evidence gates.
- `nerya/api/routes_gateway.py` exposes gateway platform matrix, inbound, send, and Telegram poll surfaces.
- `nerya/mcp/server.py` exposes Nerya tools over FastMCP.
- `nerya/acp/server.py` implements a minimal JSON-RPC ACP adapter.
- `nerya/triggers/cron.py`, `nerya/triggers/schedule.py`, and `nerya/triggers/scheduled_session.py` implement schedule lifecycle and agent scheduled sessions.

## Hermes Capability

Hermes has a much broader configuration and gateway product.

Evidence:

- `hermes_cli/config.py` owns default config, optional env vars, migration, and runtime config behavior.
- `hermes_cli/doctor.py` diagnoses tools, providers, config, platform readiness, and requirements.
- `gateway/platforms/` contains adapters for Telegram, Discord, Slack, WhatsApp, Signal, Email, Matrix, Mattermost, Feishu, DingTalk, WeCom, Weixin, SMS, HomeAssistant, webhook, API server, and more.
- `gateway/run.py`, `gateway/session.py`, and `gateway/config.py` handle multi-platform sessions and delivery.
- `cron/` plus `tools/cronjob_tools.py` support scheduled jobs with platform delivery.
- `tools/mcp_tool.py` is a large MCP client; `mcp_serve.py` exposes tools; `acp_adapter/` implements IDE/editor integration.
- `nix/`, `docker/`, `packaging/`, `setup-hermes.sh`, and release docs support deployment/packaging breadth.

## Gap

Nerya has local runtime gates and API stubs, but Hermes has an ecosystem-grade deployment/config/gateway layer.

Missing or weak areas:

- gateway adapters are shallow compared with Hermes's real platform adapters,
- no full multi-platform conversation continuity layer,
- no shared slash-command registry for gateway/CLI/dashboard,
- scheduler delivery/session parity is still incomplete relative to Hermes,
- MCP support is mostly server/export; not a full client/tool integration ecosystem,
- ACP adapter is minimal compared with Hermes editor integration,
- doctor/preflight are useful but less comprehensive for general operator agent dependencies,
- config migration/profile/provider UX is narrower,
- service packaging/deployment options are less mature.

## P0 Alignment Items

1. Make gateways real for at least two primary platforms: Telegram, Discord or Slack. **Status: PARTIALLY COMPLETED.** Telegram and webhook adapters are real (`Nerya/nerya/api/routes_gateway.py`, `Nerya/nerya/messaging/platforms.py`); Discord/Slack are stubs declared via `support_level: "stub"` in `GatewayPlatformSpec` so the dashboard never overstates support. Tracked.
2. Add unified session identity across dashboard/CLI/gateway. **Status: COMPLETED.** `gateway_session_id` and `gateway_message_id` helpers (`Nerya/nerya/api/routes_gateway.py`) standardise session keys; mirror state lives in `Nerya/nerya/messaging/mirror.py`. Coverage: `Nerya/tests/test_gateway_session_identity.py`.
3. Add shared command registry for slash/help/menu across all surfaces. **Status: COMPLETED.** `Nerya/nerya/api/gateway_commands.py:80-272` defines the canonical registry; CLI, dashboard chat, telegram, and webhook all dispatch through `handle_command`. Coverage: `Nerya/tests/test_gateway_commands.py`.
4. Finish scheduled-agent-session parity: fresh session, attached skills, delivery targets, TTL/cancellation, evidence journal. **Status: COMPLETED.** `Nerya/nerya/triggers/scheduled_session.py` runs scheduled agent sessions through `AgentKernel.run_turn` with `attached_skills`, delivery targets, and journal entries; `CancelToken` registration (`Nerya/nerya/agent/kernel.py:548-571`) plus `POST /agent/interrupt` (`Nerya/nerya/api/routes_agent.py:342-360`) gives operator cancellation.
5. Expand doctor to cover general operator dependencies: terminal backend, file permissions, browser backend, MCP servers, gateway credentials, memory DB, dashboard reachability. **Status: PARTIALLY COMPLETED.** `Nerya/nerya/cli/commands/skills.py:cmd_skill_doctor` adds skill-side diagnostics; `Nerya/nerya/ops/preflight.py` + `Nerya/nerya/ops/certification.py` cover trading/runtime. Remaining: terminal/browser/MCP/gateway-credential checks merged into `nerya doctor`. Tracked.

## P1 Alignment Items

1. Add MCP client tools, not only MCP server export. **Status: NOT STARTED.** `Nerya/nerya/mcp/server.py` exposes Nerya skills as MCP tools, but there is no `MCPClient` consuming external servers. Tracked under Plan 30.
2. Expand ACP adapter to real IDE workflow: session create/resume, file patch, approvals, tool events. **Status: PARTIALLY COMPLETED.** `Nerya/nerya/acp/server.py` ships a JSON-RPC ACP adapter; full file-patch / approval surface remains.
3. Add config profiles and migrations. **Status: NOT STARTED.** `Nerya/nerya/core/config.py` reads a single `nerya.yml`; profile/migration helpers are tracked under Plan 28.
4. Add service install/runbook parity for Windows/Linux. **Status: PARTIALLY COMPLETED.** `Nerya/nerya/install/` ships service helpers for Linux; Windows install is still tracked.

## Acceptance Gate

A P0-ready integration layer should pass: a scheduled job starts a fresh Nerya session, uses a user skill, runs tools, delivers a summarized result to Telegram, and the same session can be resumed from dashboard with full transcript evidence.
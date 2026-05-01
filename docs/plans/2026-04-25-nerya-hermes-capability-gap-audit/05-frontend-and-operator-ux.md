# 05 - Frontend and Operator UX Gap

## Current Nerya Capability

Nerya has a Next.js dashboard and a CLI.

Evidence:

- `dashboard/` is a Next.js operator surface.
- `dashboard/components/chat/ChatView.tsx` posts user input to `/agent/run_turn`.
- Dashboard pages exist for triggers, orders, settings, integrations, LLM ops, and other runtime surfaces.
- `nerya/cli/commands/core.py` exposes commands such as `doctor`, `preflight`, `certify`, service helpers, and dashboard startup.
- API routes in `nerya/api/` expose runtime surfaces for dashboard use.

## Hermes Capability

Hermes has a mature terminal and gateway UX.

Evidence:

- `ui-tui/` is a full Ink/React TUI with multiline editing, history, slash completion, tool activity, prompts, approvals, session picker, and theme support.
- `tui_gateway/` provides a Python JSON-RPC backend for TUI sessions, tools, model calls, and slash commands.
- `cli.py` and `hermes_cli/commands.py` implement rich CLI command dispatch, aliases, shared gateway command registry, and autocomplete.
- `agent/display.py` formats tool previews, spinners, progress, and activity.
- Gateway platforms reuse slash commands and shared command metadata.

## Gap

Nerya's dashboard is a control panel, not a full agent operating cockpit.

Missing or weak areas:

- no TUI equivalent,
- no rich streaming transcript with tool activity,
- no approval prompts in the primary chat UX,
- no interrupt button/redirect semantics,
- no session picker/resume/search comparable to Hermes,
- no slash-command registry shared across CLI/dashboard/gateway,
- no visible skill browser/installer,
- no live tool progress or background process panels,
- dashboard examples are still trading/demo prompt oriented, which makes general agent use feel constrained,
- no browser-level debugging/testing workflow documented for dashboard regressions.

## P0 Alignment Items

1. Upgrade dashboard chat to an event-driven operating cockpit: streaming assistant deltas, tool start/progress/complete rows, approval cards, error cards, interrupt/stop/replace controls, session picker and resume. **Status: PARTIALLY COMPLETED 2026-04-25.** Backend support is in: `StreamingEventBus` (`Nerya/nerya/agent/streaming.py:1-127`) emits `turn.step` / `tool.start` / `tool.complete` / `approval.request` events; new `GET /agent/stream/events` (`Nerya/nerya/api/routes_agent.py:314-340`) replays the bus for poll-based dashboards; `POST /agent/interrupt` (`Nerya/nerya/api/routes_agent.py:342-360`) flips the per-session `CancelToken` registered in `Nerya/nerya/harness/cancellation.py:96-130` and wired through `AgentKernel.run_turn` (`Nerya/nerya/agent/kernel.py:548-571,610-619`). Session picker / resume is already covered by `GET /agent/sessions` + `GET /agent/session`. Coverage: `Nerya/tests/test_streaming_bus.py:50-65`, `Nerya/tests/test_cancellation.py:53-72`. Remaining: dashboard React components to render the streamed feed + approval cards.
2. Add shared slash-command registry for CLI, dashboard chat, and gateway. **Status: COMPLETED.** `Nerya/nerya/api/gateway_commands.py:80-272` defines the canonical `BUILTIN_COMMANDS` registry (`/help`, `/menu`, `/status`, `/new`, `/trace`, `/skills`, `/skill`) with `CommandSpec` metadata; the dashboard chat, telegram gateway, and webhook gateway all dispatch through `handle_command`. Coverage: `Nerya/tests/test_gateway_commands.py`.
3. Add tool evidence panel per turn. **Status: PARTIALLY COMPLETED.** Per-turn evidence is exposed via `Nerya/nerya/observability/trace.py` + `POST /agent/trace`/`/explain` (`Nerya/nerya/api/routes_agent.py`); the dashboard already renders the trace summary. Remaining: a dedicated "evidence" tab that surfaces commands run, files read/written, gaps — tracked under Plan 16 EvidenceBundle.
4. Add skill manager page. **Status: PARTIALLY COMPLETED.** `GET /skills`, `POST /skills/install/enable/disable`, `GET /skills/<id>` and the new `/skill view`/`/skill doctor` chat commands (`Nerya/nerya/api/gateway_commands.py:_handle_skills,_handle_skill_subcommand`) cover the runtime side. Remaining: dedicated `dashboard/app/skills` page that drives those endpoints.
5. Add memory/session search page. **Status: BACKEND COMPLETED.** `POST /agent/session/search` and `GET /agent/session/events` (`Nerya/nerya/api/routes_agent.py:292-324`) drive `Nerya/nerya/agent/session_search.py`; the dashboard component is the only remaining piece.

## P1 Alignment Items

1. Add TUI or reuse Hermes-style TUI architecture with a Nerya backend. **Status: NOT STARTED.** Tracked.
2. Add notifications and background job panels. **Status: PARTIALLY COMPLETED 2026-04-25.** Background jobs surfaced through `operator.process_*` (`Nerya/nerya/skills/builtin/operator_skill/skill.yml:218-340`). Notification side is covered by `Nerya/nerya/messaging/`.
3. Add dashboard UI tests for chat/tool/approval flows. **Status: NOT STARTED.** Tracked under dashboard sprint.
4. Make dashboard all runtime-backed; remove local-only state where it affects operator truth. **Status: PARTIALLY DONE.** All routes already proxy to `:18317`; `dashboard/lib/api.ts` only caches UI prefs. Tracked.

## Acceptance Gate

A P0-ready UX should allow: user sends a complex task, watches tools stream, approves a risky patch, interrupts a wrong path, resumes the session, and opens a per-turn evidence report.
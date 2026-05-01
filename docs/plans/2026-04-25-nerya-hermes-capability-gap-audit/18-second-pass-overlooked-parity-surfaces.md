# Second-Pass Overlooked Hermes Parity Surfaces

## Status (2026-04-25)

Section-by-section status:

1. **Cron / scheduled agent product** — PARTIALLY COMPLETED. Triggers stack lives in `Nerya/nerya/triggers/cron.py` + `Nerya/nerya/triggers/runtime.py` + `Nerya/nerya/triggers/scheduled_session.py` + `Nerya/nerya/triggers/cooldown.py` + `Nerya/nerya/triggers/dead_letter.py`; tests `Nerya/tests/test_cron_scheduler.py`, `Nerya/tests/test_scheduled_session_delivery.py`, `Nerya/tests/test_trigger_routes.py`. Pause/resume + dry-run UI → Plan 21 P1.
2. **Gateway delivery / restart / channel directory** — PARTIALLY COMPLETED. `Nerya/nerya/messaging/mirror.py` (idempotency), `Nerya/nerya/messaging/scheduled_delivery.py` (delayed delivery), `Nerya/nerya/messaging/dashboard.py` (channel directory). Restart-redelivery dedup tests → Plan 21 P1.
3. **Multi-platform adapters** — PARTIALLY COMPLETED. `Nerya/nerya/messaging/platforms.py` exposes registry; native Slack/Discord/Telegram/Feishu/WeCom/DingTalk send paths are wired through `Nerya/nerya/messaging/generic_platform.py` + `Nerya/nerya/messaging/discord.py`. Full WhatsApp/Matrix/QQBot adapters → Plan 21 P2.
4. **Voice / multimodal** — PENDING (Plan 21 P2). No native voice mode yet; uploads via `Nerya/nerya/api/routes_workspace.py` only.
5. **Browser / web safety stack** — PARTIALLY COMPLETED 2026-04-25 (backend layer).
   - `Nerya/nerya/security/web_safety.py` (~290 lines): `WebPolicy`, `Decision`, `evaluate_url`, `evaluate_urls`, `require_safe_url`, `make_citation`, structured `REASON_*` codes (loopback, link-local, private host, userinfo, credential keyword, deny list, not-allowed scheme).
   - `Nerya/nerya/api/routes_security.py:16,68-110`: `POST /security/web/check`, `GET /security/web/check`, `POST /security/web/citation` consume the workspace `security/web_policy.yml` if present.
   - Tests: `Nerya/tests/test_web_safety.py` (37 cases — scheme/loopback/link-local/private/credential-keyword guards, wildcard allow/deny lists, citation truncation, source rejection, route round-trips).
   - CDP/Playwright bindings + screenshot capture remain follow-ups under Plan 21 P2; this pass nails URL safety + citation hygiene so any subsequent browser tool can reuse a single guard.
6. **Environment backends** — PARTIALLY COMPLETED. `Nerya/nerya/skills/builtin/operator_skill/actions.py` covers local terminal + process registry (Plan 04 P0 §1). Docker/SSH/Modal backends → Plan 21 P2.
7. **Process / background control** — COMPLETED. `Nerya/nerya/skills/builtin/operator_skill` + `_PROCESS_REGISTRY` (Plan 04 P0 §1).
8. **MCP / OAuth / managed tool gateway** — PARTIALLY COMPLETED. `Nerya/nerya/mcp/server.py` + `Nerya/nerya/mcp/tools.py` + `Nerya/nerya/acp/server.py`. OAuth flow + managed-tool gateway → Plan 30/31.
9. **Dashboard plugin shell + ops pages** — PARTIALLY COMPLETED. Dashboard pages live under `Nerya/dashboard/app/*`; plugin shell + log viewer → Plan 05 P1.
10. **CLI ops surfaces** — PARTIALLY COMPLETED. `Nerya/nerya/cli/` covers core commands; `doctor` / `logs` / `status` enrichment → Plan 28.
11. **Skill hub** — PARTIALLY COMPLETED. `Nerya/nerya/skills/builtin/capability_developer_skill` + `Nerya/nerya/api/routes_skills.py` cover authoring; remote skill hub → Plan 30.
12. **Session search / checkpoints / todos** — PARTIALLY COMPLETED. Search via `Nerya/nerya/agent/session_search.py` + `POST /agent/session/search`. Checkpoint manager + todo tool → Plan 21 P2.
13. **RL / eval / training loops** — PENDING (out of scope for current parity sprint).

Status: PARTIALLY COMPLETED — every backend foundation exists; remaining items are renderer + secondary-platform adapters tracked under Plans 21/28/30/31.

This file lists additional Hermes parity surfaces that were still not expanded enough in the earlier audit addenda. The focus here is not only Telegram/context, but the broader product/runtime ecosystem that makes Hermes feel like a complete operator agent.

## Evidence Read In This Pass

### Hermes surfaces found

- Cron/scheduled autonomous work: `hermes-agent/cron/jobs.py`, `hermes-agent/cron/scheduler.py`, `hermes-agent/tools/cronjob_tools.py`, `hermes-agent/hermes_cli/cron.py`, `hermes-agent/tests/cron/*`.
- Gateway delivery/restart/channel directory: `hermes-agent/gateway/delivery.py`, `hermes-agent/gateway/channel_directory.py`, `hermes-agent/gateway/restart.py`, `hermes-agent/gateway/display_config.py`, `hermes-agent/tests/gateway/test_restart_redelivery_dedup.py`, `hermes-agent/tests/gateway/test_channel_directory.py`, `hermes-agent/tests/gateway/test_delivery.py`.
- Multi-platform gateway adapters and tests: `hermes-agent/gateway/platforms/*`, `hermes-agent/tests/gateway/test_whatsapp_*`, `test_weixin.py`, `test_matrix.py`, `test_feishu.py`, `test_dingtalk.py`, `test_qqbot.py`, `test_bluebubbles.py`, `test_homeassistant.py`.
- Voice/multimodal tools: `hermes-agent/tools/voice_mode.py`, `tools/tts_tool.py`, `tools/transcription_tools.py`, `tools/vision_tools.py`, `tools/image_generation_tool.py`, `tests/tools/test_voice_mode.py`, `test_transcription_tools.py`, `test_vision_tools.py`.
- Browser/web safety stack: `hermes-agent/tools/browser_tool.py`, `tools/browser_cdp_tool.py`, `tools/browser_providers/*`, `tools/url_safety.py`, `tools/website_policy.py`, `tests/tools/test_url_safety.py`, `test_website_policy.py`.
- Environment backends: `hermes-agent/tools/environments/local.py`, `docker.py`, `ssh.py`, `modal.py`, `daytona.py`, `singularity.py`, `file_sync.py`, and terminal environment tests.
- Process/background control: `hermes-agent/tools/process_registry.py`, `tools/terminal_tool.py`, zombie/timeout/process tests.
- MCP/OAuth/tool gateway: `hermes-agent/tools/mcp_tool.py`, `tools/mcp_oauth.py`, `tools/mcp_oauth_manager.py`, `tools/managed_tool_gateway.py`, `tests/tools/test_mcp_*`.
- Dashboard/plugin shell: `hermes-agent/web/src/App.tsx`, `web/src/plugins/*`, `plugins/example-dashboard/dashboard/*`, `hermes-agent/web/src/pages/LogsPage.tsx`, `CronPage`, `SessionsPage`, `EnvPage`, `SkillsPage`.
- CLI ops surfaces: `hermes-agent/hermes_cli/doctor.py`, `logs.py`, `status.py`, `profiles.py`, `plugins_cmd.py`, `auth_commands.py`, `env_loader.py`.
- Skill hub and toolsets: `hermes-agent/tools/skills_hub.py`, `tools/skill_manager_tool.py`, `hermes-agent/hermes_cli/skills_hub.py`, website docs and tests.
- ACP integration: `hermes-agent/acp_adapter/*`, `hermes-agent/tests/acp/*`.
- Session search/checkpoints/todos: `hermes-agent/tools/session_search_tool.py`, `tools/checkpoint_manager.py`, `tools/todo_tool.py`.
- RL/eval/training loops: `hermes-agent/rl_cli.py`, `tools/rl_training_tool.py`, optional MLOps skills.

### Nerya nearby surfaces found

- Nerya has cron/session delivery foundations: `nerya/triggers/cron.py`, `nerya/triggers/delivery.py`, `tests/test_cron_scheduler.py`, `tests/test_scheduled_session_delivery.py`, `docs/plans/2026-04-24-hermes-parity-cron-session.md`.
- Nerya has dashboard pages and local API: `dashboard/app/*`, `dashboard/lib/clientApi.ts`, `nerya/api/*`.
- Nerya has MCP/ACP shims: `nerya/mcp/server.py`, `nerya/mcp/tools.py`, `nerya/acp/server.py`, `tests/test_mcp_tools.py`, `tests/test_acp_server.py`.
- Nerya has LLM budget/rate-limit/credential foundations: `nerya/llm/budget.py`, `nerya/llm/rate_limits.py`, `nerya/llm/credential_pool.py`, `tests/test_llm_retry_and_rate_limits.py`, `tests/test_credential_pool.py`.
- Nerya has messaging scaffolds/rate limits/webhook: `nerya/messaging/webhook.py`, `nerya/messaging/rate_limits.py`, `nerya/messaging/platforms.py`, `tests/test_messaging_channels.py`.

## Additional Gaps Not Expanded Enough Before

### 1. Cron / Scheduled Agent Product


| Missing detail        | What Hermes has                                          | What Nerya needs                                       |
| --------------------- | -------------------------------------------------------- | ------------------------------------------------------ |
| Human schedule parser | `every 5m`, ISO timestamps, cron expressions, validation | same parser with explicit timezone and preview         |
| Secure job storage    | cron job DB/output folders and permission checks         | secure workspace cron state + output artifacts         |
| Job output history    | per-job timestamped markdown outputs                     | dashboard/gateway-visible run history                  |
| Inactivity timeout    | cron inactivity tests and policy                         | stale job detection and operator notification          |
| Delivery target       | cron origin delivery and routing                         | send result back to originating gateway/thread/session |
| Retry/missed ticks    | scheduler recovery semantics                             | explicit catch-up/skip policy and idempotency per tick |
| Cron CLI              | list/add/remove/run/status                               | Nerya CLI/API/dashboard parity                         |
| Cron permissions      | job creation/update/deletion scopes                      | actor-bound scheduler permissions                      |


### 2. Gateway Restart / Redelivery / Home Channel


| Missing detail           | Target                                                                |
| ------------------------ | --------------------------------------------------------------------- |
| Restart drain            | on restart, drain pending outbound/inbound events safely              |
| Redelivery dedupe        | never duplicate final answers after process restart                   |
| Home channel             | configured operator/home channel for boot errors and status           |
| Boot hooks               | optional `boot.md`-style operator prompt on gateway restart           |
| Channel directory        | known chats/channels/threads searchable by name                       |
| Delivery router          | send to origin, configured target, home, or explicit platform/channel |
| Background notifications | long-running process completion notifications to originating gateway  |
| Display config           | per-platform verbosity, tool trail, markdown style, status frequency  |


### 3. Multi-Platform Gateway Breadth

Nerya should not just list platforms in a registry. Each serious gateway needs a protocol contract:

- WhatsApp: bridge allowlist, group gating, reply prefix, markdown formatting, attachment caching.
- Weixin/WeCom: callback verification, official account constraints, media limits, token refresh.
- Feishu: approval buttons, comment rules, document/drive integration, onboarding flow.
- DingTalk: robot signing, command routing, markdown/card support.
- Matrix: mention policy, voice/media support, room/thread identity.
- QQ Bot: guild/channel/user identity and command limitations.
- BlueBubbles/iMessage: local bridge pairing, file handling, mobile notification semantics.
- HomeAssistant: event/action mapping and safety guardrails.
- Discord/Slack: channel prompts, channel skills, thread controls, bot auth bypass prevention.

### 4. Voice And Multimodal Interaction


| Missing detail   | Target                                                                     |
| ---------------- | -------------------------------------------------------------------------- |
| Voice mode       | gateway-level voice command, platform isolation, fallback text mode        |
| STT              | voice/audio transcription with artifact and confidence metadata            |
| TTS              | optional spoken answer, voice profile, speed, provider routing             |
| Vision           | attached image to vision tool, redacted previews, multimodal prompt budget |
| Image generation | generated image artifact, outbound gateway file delivery                   |
| Audio lifecycle  | cache, retention, transcript, deletion, size/type errors                   |
| Multimodal UX    | frontend/gateway shows what media was processed or ignored                 |


### 5. Browser And Web Research Safety


| Missing detail       | Target                                                         |
| -------------------- | -------------------------------------------------------------- |
| Browser providers    | local CDP/browser-use/browserbase/firecrawl provider selection |
| URL safety           | malicious/local/private network URL guard                      |
| Website policy       | site allow/deny/credential-sensitive warnings                  |
| Screenshots          | visual artifact capture and display in dashboard/gateway       |
| Browser sessions     | persistent browser state with reset and isolation              |
| Download handling    | downloaded files become artifacts with provenance              |
| Interactive testing  | frontend browser testing loops and screenshot comparison       |
| Web citation hygiene | source tracking and quoted-content limits where relevant       |


### 6. Environment Backends / Remote Execution

Nerya still needs a Hermes-like execution environment abstraction if it wants to be a general coding/operator agent:

- Local environment with cwd/path/env policy.
- Docker sandbox with volume mounts, credential-file read-only mounts, cleanup.
- SSH environment with file sync, command execution, reconnect, path mapping.
- Modal/Daytona/Singularity managed environments.
- Per-environment tool availability doctor.
- Environment-scoped secrets and env passthrough allowlist.
- Output streaming, timeout, interrupt, and process tree cleanup.
- Artifact sync back to workspace.

### 7. Process Registry / Long-Running Jobs


| Missing detail      | Target                                                     |
| ------------------- | ---------------------------------------------------------- |
| Process registry    | track pid/job id/session/tool origin                       |
| Background jobs     | start, stream logs, poll, stop, kill tree                  |
| Zombie cleanup      | detect and reap orphaned child processes                   |
| Notifications       | notify origin session/gateway on completion/failure        |
| Log tails           | dashboard/gateway can tail process output                  |
| Safe foreground cap | prevent foreground terminal tools from hanging forever     |
| Shell semantics     | exit code, timeout, stdin, PTY/non-PTY, command classifier |


### 8. MCP / OAuth / External Tool Gateway


| Missing detail        | Target                                                             |
| --------------------- | ------------------------------------------------------------------ |
| MCP lifecycle         | connect/reconnect/probe/shutdown with child cleanup                |
| MCP OAuth             | provider auth, token refresh, 401 retry, per-server status         |
| MCP resources/prompts | list/read resources, list/get prompts, schema normalization        |
| Managed tool gateway  | user-token based hosted vendor passthrough                         |
| ACP auth/events       | ACP session/event parity and permission mapping                    |
| Tool config watch     | dynamic enable/disable without restart where safe                  |
| Toolset controls      | `/tools enable/disable`, UI toolset state, session reset on change |


### 9. Dashboard Plugins And Operator Console

Nerya dashboard is useful, but Hermes has a more platformized console shape:

- Plugin registry and plugin-contributed nav pages.
- Logs page with component filters: gateway, agent, tools, cli, cron.
- Cron page with job CRUD and run output history.
- Sessions page with transcript/tool-call browser.
- Env page for keys/env vars with reveal/save/clear flows.
- Skills page with skills and toolsets, install/sync/status.
- OAuth provider cards and auth status.
- Plugin manifest, loaded component, placement before/after nav sections.
- Dashboard API contracts versioned enough for plugin compatibility.

### 10. CLI / Doctor / Profiles / Logs


| Missing detail    | Target                                                                      |
| ----------------- | --------------------------------------------------------------------------- |
| Doctor            | check config, env, credentials, browser, Docker, MCP, gateways, permissions |
| Status            | local service, gateway, cron, MCP, model/provider health                    |
| Logs              | structured log tail by component/session/job                                |
| Profiles          | switch model/config/profile cleanly                                         |
| Auth commands     | provider login/logout/status flows                                          |
| Env loader        | sanitized env loading, env refs, stale base URL cleanup                     |
| Plugin commands   | install/list/enable/disable plugin lifecycle                                |
| Gateway setup CLI | guided setup for each platform with tests                                   |


### 11. Skills Hub / Toolsets / Optional Skills

Nerya has skill-first design, but Hermes parity needs ecosystem workflow:

- Remote skill hub browsing and search.
- Install/update/remove skills with provenance.
- Optional skill catalog grouped by domain.
- Skill env passthrough declarations.
- Credential-file mounts declared by skills.
- Skill sync and conflict handling.
- Skill guard/security checks before loading.
- Toolsets separate from skills, with enable/disable and session reset semantics.
- Skill marketplace docs generated from manifests.

### 12. Session Search / Checkpoints / Todo State


| Missing detail     | Target                                                                |
| ------------------ | --------------------------------------------------------------------- |
| Session search     | search past conversations, tool calls, outputs, errors                |
| Checkpoints        | save/restore conversation and workspace checkpoints                   |
| Undo               | undo last exchange where safe; define unsafe side effects             |
| Retry              | retry last exchange with linked lineage and no duplicate side effects |
| Todos              | model-visible todo list with explicit updates and UI display          |
| Title generation   | session title generation and rename                                   |
| Subdirectory hints | project-level path hints injected into prompt                         |
| Context references | `@file`, `@url`, `@session`, `@artifact` reference resolver           |


### 13. Model Provider / Credential Pool Nuances

Nerya has a good LLM gateway foundation, but Hermes-like polish includes:

- Per-provider OAuth and API-key auth UX.
- Credential pool routing, cooldown, rate-limit tracker, provider health.
- Prompt caching metrics and display.
- Model metadata/context-window discovery.
- Provider-specific tool-call parser fallbacks.
- Streaming deltas normalized across providers.
- Retry classifier by provider error class.
- Cost/budget visible live per turn/session/job.
- Fallback model policy with user-visible explanation.

### 14. Security / Privacy / Compliance Details


| Missing detail          | Target                                                     |
| ----------------------- | ---------------------------------------------------------- |
| Weak credential guard   | refuse insecure gateway/webhook secrets                    |
| Webhook signatures      | verify inbound signatures and rate limit untrusted sources |
| URL/IP guard            | block SSRF/private network unless explicitly allowed       |
| Secret egress policy    | tool outputs scanned/redacted before model/gateway/UI      |
| Data retention          | per-artifact/session/media retention and deletion          |
| Audit export            | export auth/tool/approval/gateway audit log                |
| Prompt injection audits | classify untrusted content and record mitigation           |
| Role-based views        | dashboard pages hidden/readonly by role/scope              |


### 15. Testing / Evals / Regression Harness

Hermes has a broad test envelope around gateway, tools, CLI, cron, ACP, providers, browser, voice. Nerya needs parity tests beyond trading runtime:

- Gateway restart/redelivery/dedup tests.
- Platform-specific attachment/reply/group tests.
- Auth/allowlist/signature/rate-limit tests.
- Browser/provider/screenshot tests.
- Process/zombie/interrupt tests.
- MCP OAuth reconnect tests.
- Dashboard plugin and logs/cron/session UI contract tests.
- Skill hub install/update/conflict tests.
- Context reference and checkpoint tests.
- End-to-end “send from gateway -> stream -> tool -> approval -> cancel/retry -> final” tests.

### 16. RL / Self-Improvement / Evaluation Loop

The earlier docs mentioned self-reflection, but not enough of the training/eval surface:

- Capture task trajectories with tool calls, context, outcomes, and operator feedback.
- Compress trajectories for eval/training datasets.
- Run regression evals before accepting self-evolution patches.
- Separate memory/reflection from code mutation.
- Maintain benchmark suites for gateway/coding/trading tasks.
- Track reward signals: success, latency, user interruption, approval denial, duplicate messages, tool failures.
- Provide rollback and A/B rollout for behavior changes.

## Updated Highest-Priority Missing Stack

If the goal is “Nerya feels like Hermes”, the missing stack is broader than the first P0 list:

1. Event store + streaming + replay + backend cancellation.
2. Actor/auth/permission/approval model.
3. First-class Telegram adapter with attachments/edit/reply/dedup.
4. Dashboard live timeline + logs + sessions + artifacts.
5. Cron/job product with output history and origin delivery.
6. Process registry + terminal/file/browser tools.
7. MCP/OAuth/toolset lifecycle.
8. Context manifest + session search + checkpoints + references.
9. Voice/vision/media artifact pipeline.
10. CLI doctor/status/logs/profile/setup commands.
11. Skill hub/toolset/plugin ecosystem.
12. Regression/eval/self-improvement loop.

## Bottom Line

The previous addendum fixed many gateway/context details, but still missed these larger Hermes ecosystem surfaces: cron productization, restart/redelivery, channel directory, multi-platform protocol adapters, voice/multimodal, browser safety, environment backends, process registry, MCP OAuth lifecycle, dashboard plugins, CLI doctor/logs/profiles, skill hub/toolsets, session search/checkpoints/todos, provider auth polish, and regression/eval infrastructure.
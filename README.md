# Nerya

Nerya is a **skill-first, trading-native, self-evolving autonomous agent
runtime**. It owns its own agent loop, LLM gateway, subagent runtime,
trigger plane, skill runtime, trading kernel and evolution pipeline — it
does **not** attach to an external agent runtime at execution time.

Every capability (market data, trading, risk, messaging, script generation,
strategy review, evolution) is exposed as a **Skill** inside a secure
runtime, never as a raw tool call against an exchange API.

## Design pillars

1. **Skill-first** — the agent never talks to an exchange, a provider key, or
  a bot token directly. It always goes through a registered skill action
   that is bounded by a manifest, permissions, a Risk Gate and an Approval
   Gate.
2. **Trading-native** — Risk Gate, Approval Gate, paper/live separation,
  virtual ledger, strategy history, trade review, kill switch and
   reconciliation are first-class citizens.
3. **Self-evolving** — the agent can write learning notes, propose prompt
  patches, new scripts, new skills, new trigger routes and strategy config
   patches — but only as **proposals**. Active limits, live-trading flags,
   signer policies and secret policies are immutable from Agent side.
4. **Nerya-native runtime** — the agent loop (`nerya/agent/kernel.py`),
  LLM gateway (`nerya/llm/gateway.py`), subagents (`nerya/subagents/`),
   triggers (`nerya/triggers/`) and skill runtime (`nerya/skills/runtime.py`)
   are all implemented in-repo. Sibling projects (Hermes among them) are
   used only as **reference material** for behaviors and maturity targets;
   Nerya never imports or attaches to any of them at runtime. See
   `docs/reference-capability-map.md`.

## Layout

See `docs/nerya-architecture.md` for the full layout, and the other
`docs/*.md` files for deep-dives on triggers, SDKs, LLM Gateway, Risk Gate,
strategy history, security and evolution.

## One-liner install

```bash
# macOS / Linux
curl -LsSf https://example.com/install.sh | sh

# Windows PowerShell
iwr https://example.com/install.ps1 -UseBasicParsing | iex
```

The installer is idempotent and does the following:

1. installs `[uv](https://github.com/astral-sh/uv)` if missing,
2. clones Nerya into `~/.nerya/src` and runs `uv sync --extra trading`,
3. drops a `nerya` shim into `~/.local/bin` (POSIX) or `%USERPROFILE%\.local\bin` (Windows),
4. initialises a workspace at `~/nerya-ws`,
5. registers a host service (`systemd --user` on Linux, `launchd` on macOS,
  NSSM on Windows) so the local API boots with the machine on port 18317.

After install, self-check with:

```bash
nerya doctor            # python / uv / node / package versions, workspace path
nerya service status    # is the background service running?
```

### Manage the service yourself

```bash
nerya service install --port 18317   # registers and starts the unit
nerya service status
nerya service uninstall
```

These are thin wrappers around `systemctl --user`, `launchctl`, and `nssm` —
no sudo/UAC required for the default per-user path. On Windows you need
`nssm` on `PATH` (`winget install nssm` or `choco install nssm`).

On Windows this service path manages the **API only**. It does not start the
Next.js dashboard.

### Ports at a glance

Nerya separates the interactive local daemon from the background host
service so they can coexist on one machine:

- `8787` — local daemon default (`nerya serve`, `nerya/api/local_server.py`,
dashboard proxy, TypeScript SDK `NERYA_BASE_URL` default).
- `18317` — background host-service default (`nerya service install`).

If you run both at once, point the dashboard / SDKs at whichever port
you are actually developing against. Everything else (CLI, skills, SDKs,
dashboard) reads the same `NERYA_BASE_URL` so you only configure it once.

## Quick start (manual, without installer)

```bash
# 1. create a workspace
python -m nerya.cli.app init --workspace ~/.nerya

# 2. inspect installed skills
python -m nerya.cli.app skill list

# 3. run the vertical slice demo (paper trading via PAPER:* markets, no live keys)
#    (Mock markets/chain are opt-in; seeded defaults are paper, not mock.)
python sdk/python/examples/price_tracker.py --workspace ~/.nerya

# 4. review the resulting strategy session
python -m nerya.cli.app strategy history btc_momentum --workspace ~/.nerya

# 5. reflect + generate evolution proposals
python -m nerya.cli.app reflect --workspace ~/.nerya
python -m nerya.cli.app evolve --workspace ~/.nerya
python -m nerya.cli.app proposals list --workspace ~/.nerya
```

## Local dashboard workflow on Windows

The current repo-accurate local workflow for Windows is:

1. Start the local API on `:18317`.
2. Start the dashboard dev server on `:3001`.
3. Open `http://127.0.0.1:3001/dashboard`.

Use the checked-in launcher instead of manually retyping the commands:

```powershell
pwsh -File .\scripts\windows\start-local.ps1 -OpenDashboard
```

The launcher is idempotent:

- if the API is already listening on `18317`, it leaves it running,
- if the dashboard is already listening on `3001`, it leaves it running,
- logs go to `~/.nerya/logs/api.out.log`, `~/.nerya/logs/api.err.log`,
`~/.nerya/logs/dashboard.out.log`, `~/.nerya/logs/dashboard.err.log`,
`~/.nerya/logs/telegram-poller.out.log`, and
`~/.nerya/logs/telegram-poller.err.log`.

Useful variants:

```powershell
pwsh -File .\scripts\windows\start-local.ps1
pwsh -File .\scripts\windows\start-local.ps1 -ApiOnly
pwsh -File .\scripts\windows\start-local.ps1 -NoTelegramPoller
pwsh -File .\scripts\windows\start-local.ps1 -Workspace "$HOME\.nerya"
```

The dashboard launcher sets:

- `NERYA_API=http://127.0.0.1:18317`
- `NERYA_BASE_URL=http://127.0.0.1:18317`

The launcher also starts a local Telegram poller by default. Bot menus are
synchronised by Nerya itself during API startup whenever a Telegram channel with
`bot_token_ref` or `token_ref` exists in `~/.nerya/messages/channels.yml`; the
poller only calls `/gateway/telegram/poll` so user messages reach the local
agent without manual curl commands. The menu is generated from the same gateway
command registry used by `/help` and `/menu`, keeping the Bot API menu aligned
with Hermes-style gateway command behavior. Use `-NoTelegramPoller` when you
only want API + dashboard.

If you want real e2e LLM calls after boot, make sure the machine or user
environment already has a valid `NERYA_E2E_LLM_KEY`. Without it the API still
starts, but real model calls will fail at runtime.

## Windows autostart

If you want the local API + dashboard + Telegram poller to come up automatically
when you log in, install the current-user Startup entry:

```powershell
pwsh -File .\scripts\windows\install-autostart.ps1
```

This writes a small launcher command file into the current user's Windows
Startup folder so the same local bootstrap runs after logon, without requiring
`nssm`, Task Scheduler admin setup, or a machine-wide service.

Custom file name / ports:

```powershell
pwsh -File .\scripts\windows\install-autostart.ps1 `
  -ShortcutName "NeryaLocal.cmd" `
  -Workspace "$HOME\.nerya" `
  -ApiPort 18317 `
  -DashboardPort 3001
```

Remove the autostart task:

```powershell
pwsh -File .\scripts\windows\install-autostart.ps1 -Remove
```

Live trading is **disabled by default**. Paper trading is the only mode
reachable from Agent skills until the operator edits `accounts/accounts.yml`,
enables `live_trading_enabled: true` and provisions signed approvals.

## Gateway platform alignment

Nerya tracks the same gateway platform ids as Hermes and exposes a universal
platform contract:

- `GET /gateway/platforms` returns the platform matrix and support status.
- `POST /gateway/inbound` accepts normalized inbound messages from any platform.
- `POST /gateway/send` sends outbound messages through native or webhook-backed channels.
- Agent turn responses include `events`, a user-visible decision trail (`plan`, `think`, `act`, `observe`, `close`).
- Telegram keeps `typing` active until the turn finishes; other platforms can use native status adapters or `status_webhook_url`.

For platform adapter development, use the built-in `capability_developer` skill.
Its reference lives at `nerya/skills/builtin/capability_developer_skill/references/gateway-platform-development.md`; the operator-facing overview is in `docs/gateway-platforms.md`.

## Safety summary

- Agent context never sees raw secrets; only `secret_ref` and redacted previews.
- Agent skills never call an exchange, a wallet, Telegram, or Discord directly —
they go through `connectors/`, `security/signer.py`, `messaging/pipeline.py`.
- Scripts must be approved before they can run, and they cannot bypass
Trading SDK + Risk Gate.
- `evolution` can mutate prompts, scripts, skills, routes, strategy configs —
never `limits.yml`, `live_trading_enabled`, signer policy or secret policy.

## Reference material (shape only, not source)

Nerya draws on Hermes and other sibling projects for **capability shape
and boundary ideas** — never as a runtime dependency. "Reference" means
we re-implemented the same boundary natively, not that we reach feature
parity with the reference project. Concepts Nerya has a native
implementation of, with the shape borrowed from Hermes:

- agent loop / turn model (`nerya/agent/kernel.py`) — subset of Hermes's
orchestrator; no compactor parity.
- skill runtime with per-skill manifests and sandbox (`nerya/skills/runtime.py`,
`nerya/skills/builtin/`*).
- subagent registry + dispatcher (`nerya/subagents/`*) — tiered dispatch,
no ad-hoc spawning parity.
- cron / schedule primitives with operator lifecycle
(`nerya/triggers/cron.py`, `nerya/triggers/router.py`).
- LLM gateway + model catalog + OpenRouter-style provider routing
(`nerya/llm/gateway.py`, `nerya/llm/model_router.py`,
`nerya/llm/provider_routing.py`) — covers the routing preferences set
(`sort`, `only`, `ignore`, `order`, `require_parameters`,
`data_collection`); does not cover every edge of Hermes's router.
- strategy history journaling + trade review
(`nerya/strategy_history/*`) — session ledger rather than full
conversational memory compaction.
- self-reflection and evolution pipeline (`nerya/agent/reflection.py`,
`nerya/evolution/*`).

Nerya does **not** attach to an external agent runtime at execution
time, and no Hermes bridge package exists. External agents plug into
Nerya through `nerya/mcp/` (Model Context Protocol) and `nerya/acp/`
(Agent Communication Protocol). See `docs/reference-capability-map.md`
for the full, per-capability reference-to-native mapping (including
explicit "implemented" / "partial" / "shape only" labels).

## What Nerya implements natively

Every trading-critical, security-critical and orchestration-critical
capability is Nerya-native:

- `nerya/trading/` — TradeIntent, RiskGate, ApprovalGate, paper execution,
VirtualLedger, positions, PnL, reconciliation, conflict resolution.
- `nerya/security/` — SecretVault, Signer, PolicySigner, redaction,
prompt firewall, script sandbox, audit log.
- `nerya/strategy_history/` — per-strategy JSONL ledgers and session
artifacts (trigger, context, decision, intent, risk, execution, messages,
outcome, review, reflection).
- `nerya/evolution/` — reflection, learning writer, skill/script/prompt
proposal generators, `promotion.py` with operator-signed apply,
`rollback.py` with snapshot restore.
- `nerya/sdk/` — internal in-process client + Trigger / Trading / LLM /
Strategy / Message / Skill surfaces used by the CLI, file-mode SDK and
local HTTP API.
- `nerya/connectors/` — CEX (Binance / Bybit / OKX / Hyperliquid) and DEX
(BSC / PancakeSwap, Solana / Jupiter, generic EVM) connectors with real
signed order placement / cancellation.
- `nerya/skills/builtin/` — 20+ built-in skills covering market_data,
trading, portfolio, risk, trigger, llm, script, message, strategy,
strategy_review, evolution, onchain, news_social, exchange,
exchange_author, sdk_writer, wallet, subagent, trace, creative,
data_science, devops.
- `nerya/install/` — cross-platform service installer (systemd / launchd / NSSM).

## Running the demos

```bash
python sdk/python/examples/price_tracker.py        # trigger → subagent → trading skill → paper fill
python sdk/python/examples/news_alpha_watcher.py   # light+high LLM filtering → main agent
python sdk/python/examples/direct_order_strategy.py  # Trading SDK direct order (still via Risk Gate)
```

Trigger routing, subagents, Risk Gate, paper execution, strategy history,
strategy review and reflection all run in every demo.

## Running the tests

```bash
python -m pytest tests/
```

The regression suite covers skill manifests and YAML flow DSL, trigger
router (dedupe, dry-run, subagent routing), schedule operator lifecycle
(create/edit/pause/resume/run-now/tick/status), Trading SDK (risk gate,
kill switch, over-size, low-confidence, paper fill, strategy session
creation), LLM gateway and OpenRouter-style provider routing, model
catalog refresh, secret redaction, script sandbox, reflection and
evolution, strategy history and explain-trade, subagent / agent kernel
turn, dynamic connector discovery and hot-load, certification evidence
gates, CEX live-signed order placement (Binance/Bybit/OKX/Hyperliquid),
DEX live-signed swaps (BSC PancakeSwap, Solana Jupiter), MCP and ACP
adapters, dev-mode journaling, and the service installer.

## Status

- Vertical slice complete: `nerya init → skill list → demo trigger → subagent turn → TradeIntent → Risk Gate → paper fill → strategy history → review → reflect → proposals` runs end-to-end.
- Real CEX order placement / cancellation for Binance, Bybit, OKX,
Hyperliquid. Real DEX swaps for BSC (PancakeSwap v2) and Solana (Jupiter).
- Dev mode captures HTTP / tool / error traces; accessible via
`nerya dev status|tail|clear` and the local HTTP API.
- One-line installer with service registration on Linux / macOS / Windows.


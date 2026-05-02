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
   are all implemented in-repo. Nerya never imports or attaches to an
   external agent runtime at execution time.

## Layout

The main source layout is documented in `AGENTS.md`; runtime behavior is
covered by the code and tests checked into this repository.

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

Nerya uses one local API port by default:

- `18317` — local daemon, background host service, dashboard proxy, and SDK
defaults.

If you need a temporary alternate port, pass `--port` to `nerya serve` or
`nerya service install`, and point the dashboard / SDKs at that same URL via
`NERYA_API`.

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
`~/.nerya/logs/dashboard.out.log`, and
`~/.nerya/logs/dashboard.err.log`.

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

The API starts configured Telegram pollers internally during startup. Bot menus
are synchronised by Nerya itself whenever a Telegram channel with
`bot_token_ref` or `token_ref` exists in `~/.nerya/messages/channels.yml`, and
the poller dispatches updates through the same handler as
`/gateway/telegram/poll` so user messages reach the local agent without manual
curl commands. The menu is generated from the same gateway command registry used
by `/help` and `/menu`, keeping the Bot API menu aligned with the gateway
command behavior. Use `-NoTelegramPoller` when you want API + dashboard without
starting Telegram long-polling in a newly launched API process.

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

## Gateway Platform

Nerya exposes a universal platform contract:

- `GET /gateway/platforms` returns the platform matrix and support status.
- `GET /gateway/status` returns configured-channel and poller liveness state
  without exposing secret values.
- `POST /gateway/inbound` accepts normalized inbound messages from any platform.
- `POST /gateway/send` sends outbound messages through native or webhook-backed channels.
- Agent turn responses include `events`, a user-visible decision trail (`plan`, `think`, `act`, `observe`, `close`).
- Telegram keeps `typing` active until the turn finishes; other platforms can use native status adapters or `status_webhook_url`.

For platform adapter development, use the built-in `capability_developer` skill
and the gateway APIs above.

## Safety summary

- Agent context never sees raw secrets; only `secret_ref` and redacted previews.
- Agent skills never call an exchange, a wallet, Telegram, or Discord directly —
they go through `connectors/`, `security/signer.py`, `messaging/pipeline.py`.
- Scripts must be approved before they can run, and they cannot bypass
Trading SDK + Risk Gate.
- `evolution` can mutate prompts, scripts, skills, routes, strategy configs —
never `limits.yml`, `live_trading_enabled`, signer policy or secret policy.

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

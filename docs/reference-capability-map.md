# Reference capability map

Nerya borrows *ideas* and *boundaries* from sibling projects. It does
**not** import their runtime, SDK or source code. Each row names a
capability Nerya learned from, plus where it lands in the native tree
and an explicit parity label:

- **implemented** — Nerya has a native, covered-by-tests implementation
  that matches the reference boundary.
- **partial** — Nerya has a working implementation but does not match
  every edge of the reference project yet.
- **shape only** — Nerya uses the reference as inspiration for the
  boundary; concrete feature parity is not claimed.

## hermes-agent (`../hermes-agent/`)

Hermes is a reference for **generalist agent-runtime maturity** (agent
loop, provider routing, cron, subagents, gateway, session/memory,
self-improvement). Nerya has a native implementation of the *boundary*
of each of those pieces, but does not claim Hermes-level feature parity
across the board:

| Capability | Parity | Where it lands in Nerya |
|---|---|---|
| Agent turn loop | implemented | `nerya/agent/kernel.py` |
| LLM gateway | implemented | `nerya/llm/gateway.py`, `nerya/llm/adapters/*` |
| Model catalog + refresh | implemented | `nerya/llm/model_catalog.py` |
| Provider routing (sort / only / ignore / order / require_parameters / data_collection) | implemented | `nerya/llm/provider_routing.py`, `nerya/llm/model_router.py` |
| Subagent dispatch (tiered) | implemented | `nerya/subagents/*` |
| Cron / schedules (create / pause / resume / run-now / tick / status) | implemented | `nerya/triggers/cron.py`, `nerya/triggers/router.py` |
| Strategy session history | implemented | `nerya/strategy_history/*` |
| Conversational memory compaction | shape only | not a goal for Nerya; strategy-centric ledger is kept instead |
| Self-reflection / evolution | partial | `nerya/agent/reflection.py`, `nerya/evolution/*` — proposal-only for live-trading-critical files |
| Certification gates + evidence | implemented | `nerya/ops/certification*.py` |

Nerya does **not** attach to Hermes at runtime and does **not** ship
any Hermes bridge package — external agents plug in via `nerya/mcp/`
and `nerya/acp/` only.

## agent-trade-kit (`../agent-trade-kit/`)

| Capability | What we learn | Where it lands in Nerya |
|---|---|---|
| Trading tool boundary | Trading is a declared skill with a narrow surface (submit / cancel / status / portfolio), not a raw exchange SDK. | `nerya/skills/builtin/trading_skill/*` defines the surface; `nerya/trading/*` is the implementation. |
| API key permission shape | Keys are scoped (read / trade / withdraw) and stored outside the agent context. | `nerya/security/secrets.py` + `nerya/security/permissions.py` + `accounts/exchanges.yml`. |
| Strategy config layout | `packages/*/configs/*.toml` — per-strategy config, limits and metadata. | `workspace/strategies/{id}/strategy.yml|config.yml|limits.yml`. |

We **do not** take agent-trade-kit as a runtime SDK dependency. Key scoping
is reimplemented in `nerya/security/permissions.py`.

## goat (`../goat/`)

| Capability | What we learn | Where it lands in Nerya |
|---|---|---|
| On-chain action provider abstraction | `plugins` expose chain actions behind a typed interface. | `nerya/skills/builtin/onchain_skill/*` declares actions (`get_balance`, `simulate_swap`, `prepare_signed_tx`, `broadcast_tx`) and `nerya/connectors/evm_native.py` + `solana_native.py` implement them. |
| Wallet abstraction | Wallets are providers with a uniform sign/balance interface. | `nerya/security/signer.py` — the agent never holds a private key, it asks the signer to sign a *policy-validated* prepared transaction. |
| Chain-agnostic plugin surface | Plugins declare `getTools` with strongly typed parameters. | Nerya's skill manifest (`skill.yml`) serves the same purpose. |

## onchainos-skills (`../onchainos-skills/`)

| Capability | What we learn | Where it lands in Nerya |
|---|---|---|
| Skill-as-capability | Each skill is a directory with a `skill.yml`, `actions`, `README.md`, `tests`, permissions and an optional installer. | Mirrored 1:1 in `nerya/skills/builtin/*`, enforced by `nerya/skills/manifest.py`. |
| Skill install flow | Download → verify → stage → approve → enable. | `nerya/skills/installer.py` + `workspace/skills/{pending,installed,rejected}/`. |
| Skill audit | Separate audit log for every skill invocation. | `nerya/skills/audit.py`. |

## hummingbot (`../hummingbot/`)

| Capability | What we learn | Where it lands in Nerya |
|---|---|---|
| Connector base class | One class per venue, lifecycle hooks (`start`, `stop`, `place_order`, `cancel_order`, `on_order_update`, etc.). | `nerya/connectors/base.py`, with mock + native stubs per venue. |
| Order lifecycle | `Created → Submitted → Partially filled → Filled → Cancelled → Failed`, with reconciliation. | `nerya/trading/orders.py`, `nerya/trading/execution.py`, `nerya/trading/reconciliation.py`. |
| Paper trading / virtual ledger | In-process simulated fills with fees and slippage. | `nerya/trading/paper.py` + `nerya/trading/virtual_ledger.py`. |
| Controllers / strategies | Strategies are classes with `on_tick`, `place_order`, `on_fill` that never touch transport. | Nerya exposes this as TriggerEvent → Skill actions so that scripts and agents share the same boundary. |

**Note**: Nerya does **not** embed Hummingbot and does not depend on it.
The connector base is a thin re-implementation. Hummingbot is the
reference for connector semantics and order lifecycle, not for source code.

## Common principle

Every sibling project teaches the same lesson: the agent must call a
*declared* action with a *bounded* permission, not the raw network API. In
Nerya we implement this lesson across LLM, triggers, trading, messaging,
scripting, on-chain and evolution.

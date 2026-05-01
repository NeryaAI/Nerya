# AGENTS.md

Ground-truth orientation for AI agents and
human contributors working inside the **Nerya** repository.

Read this file *first*. It answers the "where does X live, and how do
I run things" questions so you don't grep through the whole tree on
every session.

---

## 1. What Nerya is

Nerya is a **skill-first, trading-native, self-evolving autonomous
agent runtime**. It runs as:

- `nerya.api.local_server` — FastAPI-like HTTP server on `:18317`
(launched by `nerya service start`).
- `nerya.agent.kernel.AgentKernel` — the main decision loop.
- `dashboard/` — Next.js 14 control panel on `:3001`, talking to the
API server through `/api/proxy/`*.

The runtime never calls exchanges or LLMs directly — **every external
call is mediated by a Skill and its approved scripts**. A Skill is the
model/operator-facing playbook declared by `SKILL.md`. Executable logic
belongs under `scripts/`, not in `actions.py` or YAML manifests. Do not
create new `skill.yml`, `skill.yaml`, `manifest.yml`, `manifest.yaml`, or
`actions.py` files to define skills. Legacy YAML/action files in existing
skill directories are migration artifacts only and must be removed or
converted when touching that capability.

## 2. Absolute rules (read before editing code)

1. **Never log or return plaintext secrets.** Use
  `nerya.security.secrets.SecretVault`; values resolved via
   `vault://<ref>` stay in memory for a single call.
2. **Live trading** is off unless `runtime.live_trading_enabled: true`
  in `nerya.yml` *and* the Approval Gate signs off. Do not bypass
   `nerya.trading.risk_gate` or `approval_gate`.
3. **All agent-authored changes are proposals.** Writing a strategy,
  a trigger, a script, or a skill goes through
   `nerya.evolution.PatchProposal`. Never mutate `workspace/` state
   directly from an action.
4. **Don't introduce native CEX connectors.** All CEX venues are
  handled by `nerya.connectors.ccxt_adapter.CcxtConnector`. If you
   want a new venue, add an `ExchangeProviderSpec` (see
   `nerya/connectors/provider_spec.py`).
5. **Dashboard state is not authoritative.** The browser's
  `localStorage` caches UI prefs only; every action hits the API
   proxy, which talks to the Python runtime.
6. **Research knowledge is lazy-loaded skill content.** Professional
  research frameworks, data-source decision trees, report templates,
   factor/quant methodology, and market-analysis checklists belong in
   `SKILL.md` or files under `references/`. Keep always-on prompts,
   default subagent prompts, route manifests, and team templates small:
   they should name the role, preferred skills, and output contract only.
   Load the relevant skill (`Skill`, `skill_view`, or script docs) only
   when a task actually needs that research capability.

## 3. Repo layout (top-level)

```
Nerya/
├── nerya/                 # Python runtime
│   ├── agent/             # AgentKernel, Planner, ContextBuilder, Memory, Reflection
│   ├── api/               # local_server + HTTP routes (/chat, /strategies, /integrations, /wallet, /exchanges)
│   ├── cli/               # `nerya ...` CLI (cli/app.py)
│   ├── connectors/        # ccxt_adapter, polymarket, evm/solana/bsc native, registry, provider_spec
│   ├── core/              # config, paths, errors, yaml_io, devmode
│   ├── data/              # news / social / tvl / funding / whale-event feeds (real APIs)
│   ├── evolution/         # reflection_engine, strategy_mutation, learning_writer, patch proposals
│   ├── install/           # service / host-process helpers
│   ├── llm/               # ModelRouter, adapters/, credential_pool, compression, session, budget
│   ├── mcp/               # FastMCP server exposing skills as MCP tools
│   ├── messaging/         # Telegram / Discord / webhook transports + outbound pipeline
│   ├── sdk/               # trading + trigger SDKs (user-authored scripts import these)
│   ├── security/          # SecretVault, prompt firewall, redaction, structured output validation
│   ├── skills/            # skill kernel + builtin/<name>_skill/*
│   ├── subagents/         # spawn/run sub-agents per strategy
│   ├── trading/           # risk_gate, approval_gate, accounts, ledger, reconciliation
│   ├── triggers/          # cron + trigger router, schedules.yml parser
│   └── wallet/            # WalletProvider abstraction + self_custody / okx_os / bitget / binance_agentic / coinbase
├── dashboard/             # Next.js 14 control panel (app router + Tailwind)
├── scripts/               # CLI helpers (install / doctor / e2e)
├── tests/                 # pytest suite — 500+ tests
├── workspace_template/    # reference layout (the actual layout is built at runtime by `workspace/layout.py::required_dirs`)
└── pyproject.toml         # extras: [trading], [dashboard], [dev]
```

### Skill layout (`nerya/skills/builtin/<name>_skill/`)

Every skill is a directory with `SKILL.md` as the required entry point:

```
<name>_skill/
├── SKILL.md       # required: when to use, workflow, examples, references
├── scripts/       # optional: runnable helpers / executable adapters
├── references/    # optional: detailed implementation docs loaded on demand
└── templates/     # optional: code/templates loaded on demand
```

Rules:

- `SKILL.md` is the only skill-definition surface. It describes when to
use the skill, the workflow, gotchas, examples, and which files under
`references/`, `scripts/`, or `templates/` to load when needed.
- Put detailed research / analysis / report-writing methodology in
`SKILL.md` or `references/` and lazy-load it on demand. Do not paste
large research playbooks into system prompts, default subagent prompts,
team templates, or route config just to make them "available".
- Do **not** encode skill instructions, action catalogs, or large schemas
in YAML. Do **not** add new `skill.yml`, `skill.yaml`, `manifest.yml`,
or `manifest.yaml` files for skills.
- If a skill needs executable behavior, put it under `scripts/` with a
small, documented command interface. Scripts are executable helpers,
not the skill definition itself.
- Scripts that mutate state must be deterministic, accept structured input
such as JSON/stdin or CLI flags, return JSON-serialisable output, and
route side effects through the trading SDK, the vault, or
`PatchProposal`.
- Legacy YAML action manifests and `actions.py` files may be read only for
backwards-compatible migration. Do not extend them; convert the touched
capability to `SKILL.md` + `scripts/` instead.

### LLM adapters (`nerya/llm/adapters/`)

Split as of this milestone:

- `_base.py` — dataclasses, `UrllibTransport`, retry + rate-limit
capture, pricing patterns, prompt splitter.
- `openai.py` — `OpenAIAdapter`, `OpenAICompatAdapter`,
`DEFAULT_BASE_URLS`.
- `anthropic.py` — `AnthropicAdapter` (with prompt caching).
- `gemini.py`, `ollama.py` — self-explanatory.
- `nerya/llm/providers.py` is now a thin compat shim that re-exports
from the adapters package — keep it that way.

### Skill shared helpers (`nerya/skills/_connector_helpers.py`)

Use these instead of duplicating boilerplate in each skill:

- `venue_of(market)` — parse `BINANCE:BTCUSDT` → `"binance"`.
- `mock_exchange(ctx)` / `mock_chain(ctx)` — per-ctx cached mocks.
- `public_connector(ctx, market, cache_key=...)` — resolves public CEX
connector with mock fallback when offline.
- `public_chain_connector(ctx, chain, cache_key=...)` — same for chains.
- `account_connector(ctx, account_id, cache_key=...)` — credentialed
connector from the workspace account registry.

## 4. Daily workflow

```bash
# Python tests (pytest)
cd Nerya
python -m pytest tests/ -q

# A single module
python -m pytest tests/test_connector_helpers.py -v

# Dashboard type-check
cd Nerya/dashboard
npx tsc --noEmit

# Dev servers (two terminals)
python -m nerya.api.local_server       # :18317
NERYA_API=http://127.0.0.1:18317 npm run dev --prefix Nerya/dashboard  # :3001

# CLI smoke
python -m nerya.cli.app --help
python -m nerya.cli.app doctor
python -m nerya.cli.app service status
```

`tests/e2e/` runs 20 end-to-end scenarios with deterministic
`<<MOCK_DECISION:{…}>>` hooks — no network required.

## 5. Adding a new capability

### A new skill

1. `mkdir nerya/skills/builtin/<name>_skill` and create `SKILL.md`.
2. Put detailed guidance in `references/` when it would bloat `SKILL.md`;
  link to those files from `SKILL.md` instead of embedding everything.
3. Add files under `scripts/` only if the capability needs executable
  helpers. Keep script usage documented in `SKILL.md` and avoid hidden
  runtime entry points.
4. Add matching tests in `tests/test_<name>_skill.py` for loading,
  script invocation if any, and context-selection behavior.
5. If a script needs a connector, **reuse `_connector_helpers`** or the
  existing SDK bridge — don't write new connector boilerplate.

### A new exchange venue

- CCXT already supports it → add/adjust an alias in
`nerya/connectors/provider_spec.py::_register_builtins`.
- Unsupported by ccxt → use the `exchange_author` skill:
`POST /api/exchanges/author` with the venue's HTTP docs — Nerya
drafts an `ExchangeProviderSpec` as a proposal. Human approval
lands it under `workspace/providers/`.

### A new LLM provider

Drop a new file in `nerya/llm/adapters/<provider>.py` inheriting
`OpenAICompatAdapter` (if OpenAI-compatible) or a fresh adapter that
returns `ProviderResult`. Register it in
`nerya/llm/adapters/__init__.py::builtin_providers`.

## 6. Testing guarantees

- **No network at test time.** All tests use `FakeTransport` / `_FakeHttp`
stubs. CI fails fast if a test tries to hit the real internet.
- **Structured output is validated.** Any LLM-driven action that
returns structured JSON validates against the schema in
`nerya/security/structured_output.py`. Breaking the schema breaks
the action.
- **Pattern-based pricing.** Provider pricing in
`nerya/llm/adapters/_base.py::_PRICE_PATTERNS` is matched first;
`nerya.yml:llm.tiers.<t>.prices` overrides win when present.

## 7. Known intentional mocks (safe)

These are *deliberately* mocks — not missing features:

- `nerya/connectors/mock_exchange.py` and `mock_chain.py` — used by
paper runs and tests. Do **not** try to "replace with real data".
- Dashboard test scripts under `dashboard/tests/` use
`<<MOCK_DECISION:{…}>>` hooks to deterministically drive the agent.

## 8. When in doubt

Look at, in this order:

1. This file.
2. The nearest implementation file in `nerya/` or `dashboard/`.
3. The nearest test in `tests/test_<area>.py` — tests are the
  executable spec for everything in the runtime.

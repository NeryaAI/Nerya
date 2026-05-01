# Nerya Production Runbook

This runbook is the operator's source of truth for taking Nerya
through the four operating states defined in the production alignment
plan (`local_dev -> prod_paper -> canary_live -> full_live`).

Every section is evidence-oriented: every stage has a command, a
check, and an artifact that must exist before the next stage.

---

## 1. First run (local_dev)

```bash
# Install
cd Nerya
pip install -e .

# Create a workspace
python -m nerya.cli.app init --workspace ~/.nerya

# Inspect
python -m nerya.cli.app skill list --workspace ~/.nerya
python -m nerya.cli.app portfolio --workspace ~/.nerya
```

Preflight (expect `status == "ok"` in `local_dev` mode):

```bash
curl http://127.0.0.1:8787/ops/preflight?mode=local_dev
```

---

## 2. Paper-trading smoke (still local_dev)

```bash
python sdk/python/examples/price_tracker.py --workspace ~/.nerya
python sdk/python/examples/direct_order_strategy.py --workspace ~/.nerya

python -m nerya.cli.app strategy history btc_momentum --workspace ~/.nerya
python -m nerya.cli.app review strategy btc_momentum --workspace ~/.nerya
python -m nerya.cli.app reflect --workspace ~/.nerya
python -m nerya.cli.app proposals list --workspace ~/.nerya
```

**Required artifacts**

- `strategies/btc_momentum/history/fills.jsonl` has at least one fill,
- `memory/global.md` contains a reflection note,
- `evolution/proposals/*/proposal.yml` exists.

---

## 3. prod_paper bring-up

`prod_paper` means **real providers, real connectors, but still paper
execution**. Mock LLM tiers are not allowed in this mode.

### 3.1 Secret provisioning

```bash
nerya vault create-secret --kind exchange_api_key \
    --name binance_spot_trade_key --scope read
nerya vault create-secret --kind llm_api_key \
    --name openai_key --scope llm
```

### 3.2 Connector & provider validation

```bash
# live endpoint for the operator dashboard
curl -sX POST http://127.0.0.1:8787/llm/capabilities | jq '.tiers'

# route discovery (no accidental mock routes?)
curl -s http://127.0.0.1:8787/triggers/routes | jq
```

### 3.3 Preflight (this is the hard gate)

```bash
curl "http://127.0.0.1:8787/ops/preflight?mode=prod_paper" | jq
```

`prod_paper` preflight **fails loudly** if any of:

- `runtime.require_talib=true` and TA-Lib is missing,
- any LLM tier still points at provider `mock`,
- `runtime.live_trading_enabled=true` while in a non-live mode,
- workspace root not writable,
- kill switch is armed.

### 3.4 End-to-end paper cycle evidence

Run one strategy through one full cycle and capture the trace:

```bash
curl -sX POST http://127.0.0.1:8787/agent/run_turn \
     -d '{"trigger":{"kind":"tick.minute","payload":{}},"strategy_id":"btc_momentum"}'
curl -sX POST http://127.0.0.1:8787/agent/explain \
     -d '{"strategy_id":"btc_momentum","session_id":"<from_run>"}' > paper_explain.json
curl -sX POST http://127.0.0.1:8787/strategy/attribution \
     -d '{"strategy_id":"btc_momentum","session_id":"<from_run>"}' > paper_attribution.json
```

**Required artifacts** (filed under the paper-ready evidence package):

- `paper_explain.json` — full trace
- `paper_attribution.json` — attribution bundle
- `paper_preflight.json` — from `/ops/preflight?mode=prod_paper`
- one `evolution/proposals/*/strategy_versions.json` snapshot

---

## 4. canary_live promotion

Canary is the first time a strategy touches **real live execution**.

### 4.1 Pin the strategy version

```bash
curl -sX POST http://127.0.0.1:8787/strategy/versions \
     -d '{"strategy_id":"btc_momentum"}' > canary_versions.json
```

The active `version_id` must match the one you intend to promote.
Compare against the previous paper-ready version:

```bash
curl -sX POST http://127.0.0.1:8787/strategy/versions/compare \
     -d '{"strategy_id":"btc_momentum","left":"<paper_vid>","right":"<canary_vid>"}'
```

### 4.2 Scenario replay (what-if)

Before exposing real capital, run the worst-case scenarios through
scenario replay using the last paper session:

```bash
curl -sX POST http://127.0.0.1:8787/strategy/scenario_replay \
     -d '{"strategy_id":"btc_momentum","session_id":"<sid>",
          "slippage_bps_cap":30,"daily_loss_cap_usd":50}' | jq
```

Confirm the projected PnL clips at the configured daily loss cap.

### 4.3 Live kill-switch smoke

```bash
nerya risk kill-switch on   # should stop new intents within one tick
nerya risk kill-switch off  # re-open only after the operator acknowledges
```

### 4.4 Preflight in canary mode

```bash
curl "http://127.0.0.1:8787/ops/preflight?mode=canary_live" | jq
```

Must be `status == "ok"` before cut-over.

### 4.5 Required canary evidence package

- `canary_preflight.json`
- `canary_versions.json` (active & parent)
- `canary_compare.json`
- `canary_scenario.json`
- one real `paper_vs_live_divergence` pull after 24 hours:
  `POST /strategy/divergence {"strategy_id":"btc_momentum"}`
- one rollback rehearsal: `nerya proposals rollback <id>` against a
  dry-run target.

---

## 5. full_live promotion

`full_live` requires operator sign-off in addition to all of canary.

```bash
curl "http://127.0.0.1:8787/ops/preflight?mode=full_live" | jq
```

**Go-live checklist** (all must be explicitly checked by the operator):

- [ ] preflight returned `status == "ok"` for `full_live`
- [ ] `/llm/capabilities` shows no `experimental` entry on any
      business-critical tier
- [ ] `/triggers/routes` contains no proposal-only routes on the live
      path
- [ ] live trading flag (`runtime.live_trading_enabled`) set **and**
      policy signed via `nerya vault sign-policy`
- [ ] kill switch tested within the last rehearsal window
- [ ] incident and rollback path rehearsed within the last week
- [ ] two operators have approved the release record

---

## 6. Common ops

- Rotate a secret: `nerya vault rotate-secret <name>`
- Reject a pending approval: `nerya approvals reject <id> --reason <text>`
- Roll back a proposal: `nerya proposals rollback <id>`
- Inspect LLM spend: `nerya llm usage --tier all --range 24h`
- List open/halted turns: `curl http://127.0.0.1:8787/agent/open_turns`
- Inspect one halted turn:
  `curl -sX POST http://127.0.0.1:8787/agent/turn_state -d '{"turn_id":"..."}'`
- List schedules:  `curl http://127.0.0.1:8787/triggers/schedules`
- Disable a schedule without deleting it:
  `curl -sX POST /triggers/schedules/enable -d '{"id":"<sid>","enabled":false}'`

## 7. Recovery

- Truncated journal row: replay with `workspace.replay` against the
  last good `turn_steps.jsonl`.
- A halted turn: `load_turn_state` reports `is_resumable()` — resumable
  turns were stopped for budget/max-steps, not permission or LLM error.
- Lost vault passphrase: secrets are unrecoverable; follow the operator
  backup procedure referenced by `workspace/vault/keyring.ref`.

## 8. Non-production behaviours (must never silently happen)

Per the production alignment plan, the following are **explicit
failure modes** in `prod_paper`/`canary_live`/`full_live`:

- Connector/provider failure → degraded envelope, never mock fallback.
- Unsupported provider capability → explicit error, never "best-effort".
- Scheduler accepting `cron` while operator surface only exposes
  `every_seconds` — preflight catches this drift.
- Any live intent bypassing Risk Gate or Approval Gate.

If the runtime silently does any of the above, treat it as an
incident and file an evolution proposal with the trace attached.

---

## 9. Adding a new account (CEX, broker, data source, on-chain wallet)

All account onboarding follows the same shape: discover the provider
spec, install missing dependencies, store secrets in the vault, then
write the account row. Plaintext credentials never reach the Agent —
either the Agent submits placeholders captured by the secret scanner,
or the operator pre-provisions vault refs and only passes
`vault://<name>` strings.

### 9.1 Discover provider spec and credential schema

```bash
# Full catalog (~50 first-class venues, brokers, data sources)
curl -sX POST http://127.0.0.1:8787/exchanges/list | jq '.specs[] | {id, kind, runtime, supports}'

# Per-venue field schema (use for forms / agent prompts)
curl -sX POST http://127.0.0.1:8787/exchanges/credential_schema \
     -d '{"id":"binance"}' | jq

# Wallet equivalents
curl -sX POST http://127.0.0.1:8787/wallet/list | jq
curl -sX POST http://127.0.0.1:8787/wallet/credential_schema \
     -d '{"id":"bitget"}' | jq
```

Each schema response includes a `credential_fields[]` array describing
every slot (label, kind, sensitive, vault_scope) plus an
`install_command` and `install_alternatives[]` listing how to make the
runtime work (pip wheel, git-cloned Node skill, npm package).

### 9.2 Install optional runtime dependencies (opt-in)

`nerya.yml` ships with `runtime.allow_auto_install: false`. Flip it
once, then dispatch installs through the API so the journal records
each one:

```bash
# pip-based connector (e.g. ib_async for IBKR)
curl -sX POST http://127.0.0.1:8787/exchanges/install \
     -d '{"id":"ibkr"}' | jq

# pick a specific install alternative (e.g. npm wallet skill)
curl -sX POST http://127.0.0.1:8787/wallet/install \
     -d '{"id":"bitget","command":"npm:@bitget/wallet-sdk"}' | jq

# inspect installed Node.js skills
curl -sX POST http://127.0.0.1:8787/wallet/installed | jq
```

The response includes `configure_patch` — the exact `provider_config`
fields the runtime needs (`skill_path`, `entry`, etc.). Forward those
into the upsert payload below.

### 9.3 Store secrets in the vault

Direct route (operator pre-provisions, then references by name):

```bash
curl -sX POST http://127.0.0.1:8787/security/secrets/put \
     -d '{
       "name":"binance_api_key",
       "value":"<paste once>",
       "kind":"exchange_api_key",
       "scope":["exchange"]
     }'
# returns {"ref": "vault://binance_api_key", ...}
```

Agent-driven route (the user pastes a secret in chat / dashboard, the
scanner replaces it with `<<NERYA_SECRET:xxxxxxxx>>`):

```bash
# 1. agent or operator opens an intake
curl -sX POST http://127.0.0.1:8787/accounts/intake/create \
     -d '{"venue":"binance","account_id":"binance_main","kind":"cex"}'

# 2. user pastes plaintext into the gateway / dashboard input
#    -> secret_scanner intercepts -> SecretBuffer holds plaintext
#    -> agent receives only "<<NERYA_SECRET:tok>>"

# 3. agent submits the placeholders
curl -sX POST http://127.0.0.1:8787/accounts/intake/submit \
     -d '{
       "intake_id":"...",
       "values":{
         "api_key":"<<NERYA_SECRET:abc123>>",
         "api_secret":"<<NERYA_SECRET:def456>>"
       },
       "profile":{"mode":"paper","initial_balance_usd":10000}
     }'
```

The submit step resolves placeholders, encrypts plaintext into the
vault, drops the buffer entries, and writes the account row. The
agent never sees the raw key material.

### 9.4 Upsert the account row directly (advanced)

When credentials already exist as `vault://...` refs:

```bash
curl -sX POST http://127.0.0.1:8787/accounts/upsert \
     -d '{
       "id":"ibkr_paper",
       "mode":"paper",
       "venue":"IBKR",
       "kind":"broker",
       "provider_spec":"ibkr",
       "provider_config":{"host":"127.0.0.1","port":7497,"client_id":7},
       "credentials":{}
     }'

curl -sX POST http://127.0.0.1:8787/accounts/upsert \
     -d '{
       "id":"alpaca_paper",
       "mode":"paper",
       "venue":"ALPACA",
       "kind":"broker",
       "provider_spec":"alpaca",
       "provider_config":{"paper":true},
       "credentials":{
         "api_key":"vault://alpaca_paper_key",
         "api_secret":"vault://alpaca_paper_secret"
       }
     }'
```

Public, non-secret config goes under `provider_config`; anything
sensitive must be a `vault://...` reference. The route refuses
plaintext in `credentials`.

### 9.5 Data sources with custom HTTP auth headers

Some upstreams need bespoke headers (`Authorization: Bearer <token>`,
`X-Custom-Token`, ...). Park them on `provider_config.headers`; vault
refs are resolved per request:

```bash
# 1. Store the token
curl -sX POST http://127.0.0.1:8787/security/secrets/put \
     -d '{"name":"cmc_pro_key","value":"<token>",
          "kind":"exchange_api_key","scope":["exchange"]}'

# 2. Patch the headers
curl -sX POST http://127.0.0.1:8787/accounts/headers/patch \
     -d '{
       "account_id":"cmc_paper",
       "headers":{
         "X-CMC_PRO_API_KEY":"vault://cmc_pro_key",
         "Accept":"application/json"
       }
     }'

# 3. Inspect (returns masked metadata only)
curl -sX POST http://127.0.0.1:8787/accounts/headers/list \
     -d '{"account_id":"cmc_paper"}' | jq
```

The route refuses any value that looks like a plaintext secret
without a `vault://` prefix.

### 9.6 Verify in Telegram / gateway

Once the row is written, gateway commands surface the account:

```
/accounts                # roster + health summary
/accounts <id>           # one account detail (mode, venue, NAV, headers)
/wallets                 # on-chain wallet providers + install state
```

Same data is available via:

```bash
curl -sX POST http://127.0.0.1:8787/accounts/list | jq
curl -sX POST http://127.0.0.1:8787/accounts/get \
     -d '{"account_id":"cmc_paper"}' | jq
```

### 9.7 Promote to live

Live trading still goes through the existing canary / full_live gate
described in §4–5. Adding an account does **not** flip
`live_trading_enabled` — that requires a signed policy plus an
operator approval.

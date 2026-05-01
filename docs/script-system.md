# Script system

Scripts in Nerya are trusted-*enough* to run on the operator's box, but
not trusted to call an exchange, wallet, or messaging channel directly.

**Capability shape (narrow script model):** Approved strategy scripts
run in-process with a deliberately small surface. They cannot submit
trades, fire triggers, or call the LLM gateway from inside the sandbox.
All decisions that can move money or spend LLM budget stay with the
agent loop, subagents, or external (non-sandboxed) callers using
`nerya_sdk`. This is a conscious trade-off: richer scripting lives
outside the sandbox, and auditability lives inside it.

## Lifecycle

```
proposal drafted (by evolution or operator)
      │
      ▼
workspace/scripts/pending/<script_id>/
      │ static_analyzer
      │ manifest validation
      ▼
approved (`nerya scripts approve`)
      │
      ▼
workspace/scripts/approved/<script_id>/
      │ runner / scheduler
      ▼
execution inside scripts.sandbox
      │
      ▼
returns dict → caller (skill, cron, operator) decides what to do next
```

A script can never move from `pending` to `approved` via code — only
operator action (CLI or dashboard with admin token).

## Script manifest

`workspace/scripts/<state>/<script_id>/manifest.yml`:

```yaml
id: price_tracker
version: 0.1.0
entrypoint: script.py
schedule: "*/5 * * * *"    # optional
permissions:
  - ctx.skill_call            # only gate currently enforced by the runner
llm_policy:                   # reserved for future broader model
  allowed_tiers: [light, medium]
  allowed_tasks: [news_filtering, classify]
  max_calls_per_run: 20
  max_tokens_per_run: 10000
  max_cost_usd_per_day: 0.50
  high_tier_requires_approval: true
network:
  allow_http_domains: []       # empty means no direct HTTP; use ctx.skill_call
filesystem:
  workspace_read: [data/cache]
  workspace_write: [artifacts/generated]
```

> The `llm_policy` block is honoured if a future runtime adopts the
> broader script model. The sandbox as shipped today does **not**
> expose an LLM facade to the script entrypoint, so the policy only
> applies to non-sandboxed callers (for example, a script that is
> imported directly by a skill action, which is not the default path).

## Static analyzer

`nerya/scripts/static_analyzer.py` rejects at proposal time:

- Any import of `os`, `sys`, `subprocess`, `socket`, `threading`,
  `multiprocessing`, `ctypes`, `importlib`, `requests`, `urllib3`, `http`,
  `httpx` (must go through SDK), `ccxt`, `web3`, `solana`, `solders`,
  `telegram`, `discord`, `boto3`, `google.*`, unless explicitly allow-listed.
- Any read of `~/.env`, `~/.ssh`, `~/Library`, `~/.config`, `AppData`.
- Any access to `workspace/vault`, `workspace/accounts/secrets.refs.yml`,
  `workspace/approvals`, `workspace/security/**`.
- Any `exec`, `eval`, `compile`, `__import__`.
- Any direct wallet/chain RPC call patterns.

The sandbox enforces these at runtime too, so a post-approval tampering
still fails.

## Runtime sandbox

- Scripts run in-process through `nerya/scripts/runner.py`, wrapped in
  the `scripts.sandbox` context manager. The sandbox monkey-patches
  `builtins.open` / `os.environ.get` to reject reads against known
  secret paths/keys (see `nerya/scripts/sandbox.py`).
- Each run gets a fresh `LLMSession` with the manifest's `llm_policy`
  attached as the *active* session. Nothing inside the sandbox exposes
  an LLM facade to the script, but if a future broader model opts in,
  the session is already budget-tracked.
- The script entrypoint may accept a `ctx` keyword argument. If it does,
  the runner hands it a :class:`nerya.scripts.script_context.ScriptContext`
  that exposes exactly one method: `ctx.skill_call(skill_id, action, **payload)`.
  Any `(skill_id, action)` pair that is **not** on
  `SCRIPT_ALLOWED_SKILLS` raises `PermissionError` and is journaled.

## What approved scripts can do (narrow model, shipping today)

- Call `ctx.skill_call("market_data", "get_mark_price", ...)`
- Call `ctx.skill_call("market_data", "get_ticker" | "get_candles" | "summarize_market" | "calculate_features", ...)`
- Call `ctx.skill_call("onchain", "get_onchain_price" | "get_token_balance" | "get_whale_events" | "summarize_onchain_activity", ...)`
- Call `ctx.skill_call("news_social", "get_recent_news" | "get_social_pulse", ...)`
- Return a plain dict / list — the caller (skill action, cron tick,
  operator) decides what to do with it, including whether to emit a
  `TriggerEvent` or submit a `TradeIntent`.

The complete allowlist lives at
`nerya/scripts/script_context.py::SCRIPT_ALLOWED_SKILLS`. Adding a new
capability to approved scripts means one entry there plus a matching
test scenario.

## What approved scripts cannot do

- Call `client.trading.submit_intent(...)` or any other trading action.
- Emit triggers directly; triggers must come from a skill, a subagent,
  or a non-sandboxed `nerya_sdk` consumer (see below).
- Call the LLM gateway (`client.llm.*`) from inside the sandbox.
- Read secrets, `.env`, SSH keys, wallet files.
- Call exchange / chain / messaging transport directly.
- Sign transactions.
- Send Telegram / Discord messages.
- Modify `limits.yml`, `accounts.yml`, `nerya.yml`.
- Install new skills or approve other scripts.

## Non-sandboxed callers — `nerya_sdk`

External scripts running **outside** the approved-script sandbox use
the `nerya_sdk` package (see `docs/trading-sdk.md`). Those callers are
explicitly not sandboxed and *can* reach `client.triggers.emit(...)`,
`client.trading.submit_intent(...)` and `client.llm.*`. Such calls
still flow through the same Risk Gate, Approval Gate and LLM Gateway
as any agent action. The distinction is:

- **Inside the sandbox** (runner → `ctx`): narrow, read-only
  feature-extraction surface.
- **Outside the sandbox** (operator-run `nerya_sdk`): full SDK surface,
  gated by the same runtime primitives the agent uses.

This keeps approved strategy scripts auditable and reviewable while
still giving operators a full-power SDK when they need it.

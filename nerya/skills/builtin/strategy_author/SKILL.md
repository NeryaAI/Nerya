<!-- nerya-skill-frontmatter-start -->
---
name: strategy_author
description: "Use whenever the user asks to author / generate / scaffold / refactor a Nerya strategy package: scalping, trend-following, news-tracking, sentiment, on-chain, sector rotation, mean reversion, A-share / US / crypto / Polymarket, or any prompt-driven or code-driven strategy. Triggers on \"create a strategy\", \"write a scalping strategy\", \"generate a trend follower\", \"build a news strategy\", \"make a polymarket strategy\", \"design an A-share strategy\", \"author a strategy package\". Tells the model how to structure strategy.yml, main.py, subagent prompts, triggers, schedules, account/SDK access, and the *required* backtest harness when historical data is available."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Strategy Author playbook

This skill is the canonical brief for writing Nerya strategies. Read
it before calling `strategy_generate_proposal` or hand-editing a
package under `<workspace>/strategies/<id>/`.

## Mental model

A Nerya strategy is *agent-generated code* that the runtime executes
on a schedule or in response to events. The package is the contract
between the agent and the kernel:

```
<workspace>/strategies/<strategy_id>/
├── strategy.yml          # manifest (id, mode, accounts, markets, triggers, subagents, tuning)
├── strategy.md           # human-readable description (entry/exit rules, risk, references)
├── main.py               # `def run(ctx) -> dict` — the only callable entry point
├── limits.yml            # per-strategy trading limits (USD caps, drawdown, kill switch)
├── subagents/<name>.agent.md   # optional: per-strategy role prompts
├── tests/test_main.py    # REQUIRED backtest / smoke when data is available (see below)
├── runs/<run_id>.json    # auto-written: every tick's decision record
└── logs/                 # auto-written: per-tick stderr / stdout
```

Nothing else. No top-level imports of trading SDKs, no hand-rolled
HTTP calls, no `requests.post()` to the Nerya API — *everything*
flows through the `ctx` facade `main.run` receives, because the
runtime decides what `ctx` is allowed to do based on `mode`,
`accounts`, and `limits`.

## strategy.yml (manifest)

Minimum viable manifest:

```yaml
strategy_id: ashare_600519_sma_crossover
title: 600519 SMA 5/20 Crossover
mode: paper                # paper | live
accounts: [paper_main]
markets: ["cn:600519"]
trigger_kinds: [schedule]
subagents: []              # leave empty unless you actually need subagent analysis
tuning:
  enabled: false
```

Add only what the strategy actually uses:

- `triggers:` — list of `{kind, schedule|event, payload}`. Use
  `kind: schedule` with a `cron:` field for time-based runs (`*/1 *
  * * *` for one-minute scalpers, `0 9 * * 1-5` for daily-open
  trend strategies). Use `kind: event` for price/news/on-chain
  webhook routes. *Cross-reference the* `triggers` *skill before
  writing this section.*
- `subagents: [name1, name2]` — only listed if `main.py` actually
  calls `ctx.subagents.dispatch("...")`. Listing roles you don't use
  costs the operator nothing but adds noise.
- `tuning.enabled: true` — only if you also write a per-strategy
  `tuner` prompt and want the auto-evolution lane to look at this
  strategy.
- `live_trading_enabled: false` (default) — must be explicitly
  flipped by the operator after a paper run window proves out the
  package.

## main.py

`main.py` exports exactly one function:

```python
def run(ctx) -> dict:
    """One tick. Return a decision dict the runtime persists to runs/."""
    ...
```

`ctx` exposes the only blessed surfaces:

| Surface              | Use for                                                    |
| -------------------- | ---------------------------------------------------------- |
| `ctx.market_data`    | Price / candle / orderbook reads (cached when stale)       |
| `ctx.news`           | News / social / announcements feed                         |
| `ctx.onchain`        | On-chain reads (balances, transfers, contract state)       |
| `ctx.llm`            | Tiered LLM calls (`tier="light"` for filtering, `"medium"` |
|                      | for analysis, `"high"` for hard reasoning)                 |
| `ctx.trading`        | Read-only by default. `submit_intent({...})` to place an   |
|                      | order — the kernel fans out approvals + risk gates.        |
| `ctx.portfolio`      | Current positions, unrealised PnL, last fills              |
| `ctx.subagents`      | `dispatch(name, payload)` for in-tick subagent analysis    |
| `ctx.memory`         | Strategy-scoped memory append/read                         |
| `ctx.account`        | Resolved account record (id, venue creds, base currency)   |
| `ctx.limits`         | The package's `limits.yml` snapshot                        |
| `ctx.now()`          | UTC timestamp the tick is operating against                |
| `ctx.logger`         | Structured logger (writes under `runs/<run_id>.json`)      |

Forbidden patterns:

- Importing `nerya.api.*` or `nerya.tools.*` directly — the runtime
  hides everything for a reason.
- Reading `os.environ` for secrets — use `ctx.account` (which reads
  from the encrypted vault) or `ctx.config.get("...")`.
- Spawning threads / processes — return a decision and let the
  runtime schedule the next tick.
- Holding state in module globals — strategies are reloaded across
  ticks; persist via `ctx.memory.append_strategy(...)` instead.

## Account / SDK access

Account credentials live in the encrypted secret vault. **Never** ask
the model to read `accounts.json` or paste a private key into a file.
The flow is:

1. Operator provisions `<workspace>/accounts/<id>.yml` with non-secret
   fields (`venue`, `base_currency`, `paper`).
2. Secrets land in `<workspace>/vault.enc` via the secret vault tools.
3. The runtime resolves both into `ctx.account` for the running tick.
4. `ctx.trading.submit_intent({...})` uses the resolved account
   automatically — strategies never see raw keys.

If your strategy needs a venue / API the runtime doesn't model yet,
add a small adapter in `main.py` that takes credentials from
`ctx.account.extra` (operator-supplied, vault-resolved). Document
the schema in `strategy.md` so the operator knows what to put in
the vault.

## Triggers & schedules

Read the `triggers` skill first. The most common patterns:

- **Scalping** — `cron: "*/1 * * * *"` (one minute) or
  `cron: "*/15 * * * * *"` (15s, requires the runtime's high-cadence
  mode). Tick must be *fast*: read price, decide, submit, return.
- **Trend / swing** — `cron: "0 9 * * 1-5"` (daily 09:00 weekdays) or
  `cron: "0 */4 * * *"` (every 4h). Tick reads daily candles, runs a
  longer analysis, optionally calls a subagent for confirmation.
- **News-driven** — `kind: event` with `match.kind: news.*`. The
  trigger router invokes the strategy with the event payload as
  `ctx.trigger.payload`.

Each tick should be **idempotent**: rerunning the same tick on the
same data must not double-submit. Use `ctx.memory.has_seen(event_id)`
or a deterministic `dedup_key` in `submit_intent`.

## Subagents inside a strategy

Add a subagent only when the analysis is too expensive or too
qualitative for in-tick logic. Workflow:

1. Author `subagents/<role>.agent.md` *inside the strategy package*
   (per-strategy prompt, narrower than the workspace defaults).
2. List the role under `strategy.yml > subagents`.
3. In `main.py`, call:
   ```python
   verdict = ctx.subagents.dispatch("market_analyst",
                                    payload={"signal": signal,
                                             "candles": last_50})
   ```
4. The harness merges per-strategy roles on top of workspace
   defaults; operators can override either.

For *low-frequency* strategies (daily, news-driven), prefer
`ctx.team.run({...})` (delegates to the `team_run` native tool, see
the `team` skill) so multiple roles vote before the strategy commits
to an order.

## Backtest is required when data permits

If `ctx.market_data` (or your custom adapter) can return historical
candles, the package **must** ship a backtest under `tests/`:

```python
# tests/test_main.py
import pytest
from main import run

def test_backtest_no_blowup(make_ctx):
    """Replay 30d of 1h candles; assert no negative drawdown beyond limits."""
    ctx = make_ctx(window_days=30, tf="1h")
    out = run(ctx)
    assert out["decision"] in {"ENTRY", "HOLD", "EXIT"}
    assert ctx.portfolio.max_drawdown_pct() <= ctx.limits.max_drawdown_pct
```

Use `ctx.backtest_replay({...})` in tests to drive the strategy over
historical bars — the helper handles tick-time, fill simulation, and
fee modelling. Backtest output lands under `tests/reports/<run>.json`
and `strategy_validate` reads it before allowing `strategy_promote`
to flip a paper strategy live.

When historical data is **not** available (Polymarket new market, a
brand-new on-chain pool, an arbitrary news feed), call this out
explicitly in `strategy.md` under a `## Backtesting` section and
ship a paper-trade plan instead. Don't leave the section blank — the
operator must know which guarantees the strategy carries.

## Reference archetypes

`ref/` carries three complete walkthroughs the model should consult
before writing similar code:

| File                                | Archetype                       |
| ----------------------------------- | ------------------------------- |
| `ref/scalping_cron.md`              | Short-cycle cron + SDK tick     |
| `ref/trend_follow_subagent.md`      | Long-cycle cron + subagent vote |
| `ref/news_track_filter.md`          | News fetch → LLM filter → team  |

Read the archetype that most closely matches the user's request,
adapt it; do not copy verbatim — the user always wants a tailored
strategy.

## Workflow

1. Confirm the archetype + triggers + market with the user.
2. Read `ref/<archetype>.md` and the existing manifests under
   `<workspace>/strategies/` for stylistic consistency.
3. **Draft the package files in your own context** (the actual
   `main.py`, `tests/test_main.py`, `strategy.md`, optional
   `subagents/<name>.agent.md`). The generator's stock templates
   are a *fallback* — they only fire for files you don't supply,
   and they will not encode the user's logic on their own.
4. Call `strategy_generate_proposal` with the drafted files
   inline. **Every non-trivial strategy must wire** a real
   `schedule_cron` and a `tuning_prompt` + `tuning_cron` (the
   generator defaults `create_tuning=true` for you, but you
   still owe a real tuner rubric). **Subagents are different**:
   only list a role under `subagents` when `main.py` actually
   calls `ctx.subagents.run("<role>", ...)` or
   `ctx.team.run({...})` at runtime. A pure indicator-driven
   scalper / trend follower does not need a subagent and should
   ship with `subagents: []`. A news / sentiment / event-driven
   strategy that wants a second opinion before the order goes in
   should list the role(s) it dispatches and provide either a
   per-strategy `subagents/<role>.agent.md` override under
   `files`, or rely on the workspace persona from the Agents
   library by name. Example:

   ```jsonc
   strategy_generate_proposal({
     "strategy_id": "btcusdt_intraday_long_zh",
     "title": "BTCUSDT 日内做多 (EMA + RSI)",
     "prompt": "<full operator brief, lands in strategy.md>",
     "strategy_class": "trend",
     "mode": "paper",
     "markets": ["binance:BTCUSDT"],
     "accounts": ["paper_main"],
     "schedule_cron": "*/5 * * * *",
     "subagents": ["market_analyst"],
     "create_tuning": true,
     "tuning_cron": "0 */6 * * *",
     "tuning_prompt": "You are the strategy_tuner for `<id>`. \nReview the last 200 ticks under runs/, the closed trades, and \nthe strategy.md objectives. Propose at most one parameter \nchange per cycle (e.g. RSI band, EMA period, max_single_order_usd). \nAlways write a one-paragraph rationale into the proposal note. \nNever flip live_trading_enabled.",
     "tuning_objectives": [
       "drawdown < 5%",
       "win_rate > 55%",
       "respect max_single_order_usd"
     ],
     "files": {
       "main.py": "# real implementation, not the template ...",
       "tests/test_main.py": "# real backtest using ctx.backtest_replay ...",
       "strategy.md": "# overrides the prompt-derived playbook if needed",
       "subagents/market_analyst.agent.md": "# per-strategy market_analyst prompt ..."
     }
   })
   ```

   The runtime writes the result under
   `proposals/<id>/after/strategies/<id>/`. Any path you don't
   override falls back to the stock template; any path you
   *do* supply replaces the template wholesale.
5. Call `strategy_validate({proposal_id})`. Fix any blocker by
   calling `strategy_generate_proposal` again with corrected
   `files` (overwriting the proposal) until the validator says
   `ok=true`.
6. Run `strategy_run_tick({proposal_id, dry_run: true})` if the
   archetype is paper-runnable; or run the included backtest.
7. If clean, call `strategy_promote({proposal_id})` to write the
   package to `strategies/<id>/`. Promotion now also installs
   the trading + tuning rows in `triggers/schedules.yml`
   automatically — you don't need a separate
   `strategies/runtime/schedule` call unless the operator paused
   it later.
8. Send a short summary to the operator including the trigger
   shape, expected cadence, and any unverified risks the
   validator flagged as warnings.

### Subagent / persona reuse

Before writing a fresh subagent prompt, check the workspace's
**Agents library** (top-level `<workspace>/subagents/<name>.agent.md`
+ `<name>.role.yaml`). If a persona that already matches the
strategy's needs exists there (e.g. a curated `market_analyst`),
just list its `name` under `subagents` and skip
`subagents/<name>.agent.md` in `files` — the dispatcher will
fall back to the workspace persona automatically. Only ship a
per-strategy override prompt when the role needs to be specialised
to *this* strategy / market / mission. Operators can also create
new personas from the dashboard's Agents page; the agent should
prefer those over inventing one-off prompts in every package.

## Failure modes

- **Missing the backtest.** Live promotion is blocked when
  `tests/` is empty *and* historical data was reachable. Add it.
- **Trigger but no schedule.** A strategy without a trigger never
  runs; the dashboard shows it as "registered, idle". Always wire
  at least one trigger.
- **Asking the operator for keys.** Never. Use the vault flow.
- **Hard-coded venue endpoints.** Use `ctx.account.extra.endpoint`;
  the operator may rotate venues per environment.
- **Generic prompt subagents.** A per-strategy subagent prompt
  should reference *this* strategy, *this* market, *this* mission.
  Otherwise just use a default workspace role.

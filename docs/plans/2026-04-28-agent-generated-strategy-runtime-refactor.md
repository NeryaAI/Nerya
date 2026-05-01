# Agent-Generated Strategy Runtime Refactor Plan

Status: proposal / implementation blueprint  
Date: 2026-04-28  
Goal: let Nerya agents generate strategy code packages that can be run by cron, call Nerya SDK surfaces, invoke subagents for analysis, and place orders only through the existing risk-gated trading kernel.

---

## 1. Target Outcome

Nerya should generate a full strategy package, not a loose script:

```text
workspace/strategies/btc_scalper/
├── strategy.md
├── strategy.yml
├── main.py
├── subagents/
│   ├── market_analyst.agent.md
│   └── risk_critic.agent.md
├── tests/
├── runs/
├── reviews/
└── versions/
```

The package is staged as a `PatchProposal` first. It becomes runnable only after static analysis, contract tests, policy checks, optional shadow run, and operator approval. Runtime execution calls Nerya SDK surfaces (`triggers`, `trading`, `llm`, `strategy`, `messages`), so orders still pass Risk Gate, Approval Gate, journals, strategy history, and reconciliation.

---

## 2. Repo Facts Rechecked

| Area | Evidence | Design impact |
|---|---|---|
| Skill-first rule | `AGENTS.md` requires external calls to be mediated by Skills and approved scripts, with skill logic under `SKILL.md + scripts/`. | Strategies must not call exchanges, wallets, or LLM providers directly. |
| Python SDK | `sdk/python/nerya_sdk/client.py` exposes `triggers.emit`, `trading.submit_intent`, `llm.classify`, `llm.analyze_signal`, `strategy.review`, `messages.send`. | Generated code can use a stable SDK/context facade. |
| TypeScript SDK | `sdk/typescript/src/client.ts` exposes trigger/trading/LLM/strategy HTTP wrappers. | JS/TS strategies can follow later with the same contract. |
| Cron/schedules | `nerya/triggers/schedule.py` supports interval, 5-field cron, `session_kind`, `attached_skills`, delivery targets, and TTL. | Strategy schedules should compile into existing trigger schedules. |
| Trigger API | `nerya/sdk/trigger_api.py` and `nerya/api/routes_triggers.py` expose schedule CRUD, run-now, dry-run, explain, replay. | Strategy activation can reuse trigger routes. |
| Trading API | `nerya/sdk/trading_api.py` routes `submit_intent` through `trading_skill.submit_trade_intent`. | Direct-order strategy code is safe only through Trading SDK. |
| Subagents | `nerya/subagents/dispatcher.py` deny-lists direct subagent use of `trading`, `wallet`, and `script_runtime`. | Subagents analyze and recommend; runner/main agent submits final trade intents. |
| Examples | `direct_order_strategy.py`, `price_tracker.py`, `news_alpha_watcher.py` show direct orders, market triggers, tiered LLM/news flow. | Productize these examples as generated strategy templates. |
| Script sandbox | `nerya/scripts/script_context.py` is narrow; prior docs noted approved scripts are mostly read-only / trigger-emitting. | Do not overload legacy script sandbox; add dedicated strategy runtime. |

Doc correction: `docs/skill-first-trading.md` still describes legacy `skill.yml + actions.py`, while current repo rules require `SKILL.md + scripts/`. This refactor must follow `AGENTS.md`.

---

## 3. Strategy Classes

### 3.1 Scalping

- short cron/interval: `*/1 * * * *` or `every_seconds: 30`;
- deterministic market/orderbook/spread/volatility logic;
- direct `ctx.trading.submit_intent(...)` allowed in paper/shadow when policy passes;
- light-tier LLM only for anomaly classification, not every tick by default;
- strict per-run, per-minute, and daily caps.

Flow:

```text
cron tick -> StrategyRunner -> ctx.market/ctx.trading -> Risk Gate -> Approval Gate -> Paper/Live Execution -> Strategy History
```

### 3.2 Trend-following

- longer cron: `*/15 * * * *`, hourly, or daily;
- code computes features first;
- candidate setup triggers strategy-scoped `market_analyst` or `risk_critic`;
- trade submits only after structured subagent result passes schema, confidence, and policy thresholds.

Flow:

```text
cron tick -> strategy signal -> subagent analysis -> StrategyRunner merges result -> Trading SDK submit_intent
```

### 3.3 News-following

- code fetches news/social sources through Nerya data/skill surfaces;
- light-tier LLM filters noise;
- useful items escalate to medium/high analysis or `news_interpreter` subagent;
- structured, source-attributed, deduped signals can become trade intents;
- uncertain items notify operator/subagent and skip trade.

Flow:

```text
cron/webhook -> news fetch -> llm.classify(light) -> dedupe/source score -> subagent/analysis -> trade or message
```

---

## 4. Core Design

Do not make generated strategies ordinary approved scripts. Add first-class `strategy_runtime`:

```text
Agent request
  -> nerya/evolution/strategy_code_generator.py creates PatchProposal
  -> proposal contains new_strategy/<strategy_id>/ files
  -> static analyzer + contract tests + dry-run + optional backtest/shadow run
  -> operator approval promotes to workspace/strategies/<strategy_id>/
  -> existing trigger schedule calls StrategyRunner
  -> StrategyRunner injects StrategyContext
  -> main.py calls ctx.market / ctx.news / ctx.llm / ctx.subagents / ctx.trading / ctx.messages
```

Reasons:

1. legacy script sandbox is intentionally narrow;
2. trading strategies need controlled order submission, not unrestricted Python;
3. current Trading SDK already enforces the kernel path;
4. strategies need package lifecycle, versioning, shadow runs, backtests, and attribution;
5. subagents are deny-listed from direct trading, so runner/main agent owns final order submission.

---

## 5. New Runtime Components

### 5.1 `nerya/strategies/package.py`

Owns package loading and manifest validation.

Responsibilities:

- parse `strategy.yml`;
- validate required files;
- normalize markets, accounts, schedules, subagent prompts, LLM budgets, risk limits;
- expose `StrategyPackage`, `StrategyManifest`, `StrategySchedule`, `StrategyPolicy` models;
- compute content hash for versions/replay.

Minimum manifest:

```yaml
version: 1
strategy_id: btc_scalper
title: BTC short-cycle scalper
mode: paper
entrypoint: main.py:run
markets: [PAPER:BTCUSDT]
accounts: [paper_main]
schedule:
  type: cron
  cron: "*/1 * * * *"
policy:
  max_single_order_usd: 100
  max_daily_notional_usd: 1000
  max_open_positions: 1
  min_confidence: 0.55
  allow_direct_order: true
  require_subagent_before_order: false
llm_policy:
  default_tier: light
  allowed_tiers: [light]
  max_calls_per_run: 2
subagents: []
```

### 5.2 `nerya/strategies/context.py`

Only injected object for generated code:

```python
def run(ctx: StrategyContext) -> StrategyResult:
    candles = ctx.market.candles("PAPER:BTCUSDT", timeframe="1m", limit=50)
    signal = compute_signal(candles)
    if not signal.buy:
        return ctx.result.hold(reason=signal.reason)
    return ctx.trading.submit_intent(
        market="PAPER:BTCUSDT",
        side="buy",
        size=50,
        size_unit="usd",
        confidence=signal.confidence,
        reasoning=signal.reason,
    )
```

Facade surfaces:

- `ctx.market`: ticker, candles, orderbook, funding, features.
- `ctx.news`: configured news/social reads with source metadata.
- `ctx.llm`: tier-aware `classify`, `extract_json`, `analyze_signal`, `compress` with strategy policy enforcement.
- `ctx.subagents`: `run(name, payload, schema=...)`, `run_many(...)`, scoped to this strategy.
- `ctx.trading`: `submit_intent`, `cancel_order`, positions/history via current Trading SDK.
- `ctx.messages`: notify operator or configured delivery target.
- `ctx.state`: strategy-local key/value state with optimistic locking.
- `ctx.clock`: deterministic time source for tests/replay.
- `ctx.audit`: structured run-journal events.

Rules:

- no raw `Config`, vault, connector, wallet, raw `SkillKernel`, or provider adapter;
- every side effect goes through the facade;
- facade injects `strategy_id`, `source="strategy_runtime"`, `_caller="strategy:<id>"`, and trigger attribution.

### 5.3 `nerya/strategies/runner.py`

Responsibilities:

- load strategy by id/version;
- enforce mode: `paper`, `shadow`, `live`;
- enforce timeout, memory, max SDK calls, LLM cost;
- instantiate `StrategyContext`;
- import `main.py` entrypoint safely;
- collect `StrategyRunResult`;
- journal run details and write strategy history;
- trigger review after accepted/filled orders;
- emit messages for skipped, rejected, errored, or escalated decisions.

### 5.4 `nerya/evolution/strategy_code_generator.py`

Agent-facing generator that returns proposal files:

- `strategy.md`;
- `strategy.yml`;
- `main.py`;
- `subagents/*.agent.md` when needed;
- `tests/test_contract.py`;
- optional fixtures/backtest config.

Expose it through `strategy_skill` or `evolution_skill` as `generate_strategy_package_proposal`. It must never directly mutate `workspace/strategies/*`.

### 5.5 `nerya/strategies/validator.py`

Validation gates before promotion:

1. manifest schema validation;
2. import smoke test with fake `StrategyContext`;
3. AST/static policy scan;
4. dependency allowlist;
5. SDK facade contract test;
6. dry-run with fixture data;
7. schedule dry-run / trigger explain;
8. optional backtest or shadow-run readiness check.

Hard-block examples: direct `ccxt`/`web3`/private HTTP imports, reading secrets/env keys directly, writing outside strategy state, `subprocess`, shell, sockets, filesystem traversal, placing orders without `ctx.trading.submit_intent`, or converting a subagent result into an order without confidence/schema checks.

### 5.6 `nerya/strategies/scheduler_bridge.py`

Compile strategy schedules into existing trigger schedules:

```yaml
schedules:
  - id: strategy_btc_scalper_tick
    kind: strategy.tick
    cron: "*/1 * * * *"
    target: skill:strategy.run_tick
    strategy_id: btc_scalper
    payload:
      strategy_id: btc_scalper
      reason: cron
```

This avoids a second scheduler and keeps `/triggers/schedules/*` as the operator control plane.

---

## 6. Strategy-Scoped Subagents

Current subagent registry is workspace-global. Add strategy overlay resolution:

1. `workspace/strategies/<strategy_id>/subagents/<name>.agent.md`
2. `workspace/subagents/<name>.agent.md`
3. `nerya/workspace/_prompt_bundles/default/subagents/<name>.agent.md`

Subagents return recommendations, not orders:

```json
{
  "recommendation": "buy|sell|hold|reduce|avoid",
  "confidence": 0.0,
  "time_horizon": "minutes|hours|days",
  "market": "PAPER:BTCUSDT",
  "thesis": "...",
  "invalidation": "...",
  "risk_flags": [],
  "evidence": [{"source": "candles", "summary": "..."}]
}
```

Then `StrategyRunner` or the main agent decides whether to call `ctx.trading.submit_intent`.

Risk to fix: `DEFAULT_SUBAGENT_SKILLS` currently lists `trading` for `execution_planner` and `portfolio_manager`, while `SubAgentDispatcher` denies `trading`. Split `trading_read` / `trading_write`; subagents get read/planning only, runner/main agent owns write.

---

## 7. Strategy Self-Evolution and Scheduled Tuning

Each strategy must support an independent self-evolution loop. This loop is separate from the trading tick: it runs on its own cron/interval, reviews live/paper performance, lets a strategy-specific tuning subagent research market changes, proposes code/config/prompt updates, and validates those updates before promotion.

### 7.1 Per-strategy tuning config

Add a `tuning` block to `strategy.yml`:

```yaml
tuning:
  enabled: true
  schedule:
    type: cron
    cron: "0 */6 * * *"
  lookback:
    runs: 200
    min_closed_trades: 20
    max_age_hours: 72
  subagent:
    name: strategy_tuner
    prompt_file: subagents/strategy_tuner.agent.md
    tier: high
  objectives:
    primary: risk_adjusted_return
    secondary: [drawdown, win_rate, slippage, execution_quality]
  guardrails:
    max_patch_files: 5
    max_position_size_change_pct: 25
    require_backtest: true
    require_shadow_run: true
    require_operator_approval: true
  proposal_policy:
    allowed_targets: [strategy.yml, main.py, subagents/*.agent.md]
    forbidden_targets: [accounts/*, limits.yml, secrets/*, live_trading_enabled]
```

The trading schedule and tuning schedule are independent. A scalping strategy may trade every minute but tune every 6 hours; a news strategy may tune daily; a long-cycle trend strategy may tune weekly.

### 7.2 Tuning subagent prompt contract

When creating a strategy, the Agent can also create `subagents/strategy_tuner.agent.md`. The prompt is strategy-specific and should include strategy intent, non-goals, allowed markets/accounts/mode, current policy/risk caps, objectives, guardrails, evidence sources, and output schema.

The tuning subagent returns proposals, not live mutations:

```json
{
  "summary": "reduce false breakout entries during low volatility",
  "evidence": [{"source": "strategy_runs", "finding": "..."}],
  "proposed_changes": [
    {"file": "main.py", "kind": "code_patch", "rationale": "..."},
    {"file": "strategy.yml", "kind": "config_patch", "rationale": "..."}
  ],
  "expected_effect": {"return": "neutral_or_better", "drawdown": "lower"},
  "validation_plan": ["unit", "fixture_replay", "backtest", "shadow_run"],
  "risk_flags": []
}
```

### 7.3 Self-evolution runtime flow

```text
tuning cron -> StrategyEvolutionRunner
  -> collect performance snapshot and market context
  -> run strategy-scoped strategy_tuner subagent
  -> create PatchProposal with code/config/prompt changes
  -> run validator + backtest + optional shadow run
  -> write evolution report and recommendation
  -> require operator approval before promotion
```

This loop may research and reason autonomously, but it must never directly apply strategy code, live-trading flags, account settings, signer policy, secrets, or global limits.

### 7.4 Components

Add:

- `nerya/strategies/evolution.py`: `StrategyEvolutionRunner`, performance snapshot builder, tuning result envelope.
- `nerya/strategies/performance.py`: metrics from strategy runs, orders, PnL, drawdown, risk rejects, slippage, LLM/subagent cost.
- `nerya/evolution/strategy_tuning_generator.py`: creates tuning prompt/config files when the Agent creates a strategy.
- `nerya/subagents/strategy_registry.py`: resolves `strategy_tuner.agent.md` before global defaults.
- journals: `workspace/journals/strategy_evolution.jsonl` and `workspace/strategies/<id>/reviews/tuning_<run_id>.md`.

### 7.5 Creation-time behavior

`generate_strategy_package_proposal` should accept:

```json
{
  "create_tuning": true,
  "tuning_prompt": "Focus on reducing drawdown and avoiding news whipsaws.",
  "tuning_schedule": "0 */6 * * *",
  "tuning_objectives": ["risk_adjusted_return", "drawdown", "execution_quality"]
}
```

When `create_tuning=true`, the Agent must generate both trading files and tuning files in the same proposal: tuning config, `subagents/strategy_tuner.agent.md`, tests for tuning schema, and schedule bridge config.

---
## 8. SDK, API, CLI, Dashboard

### Python SDK

Add:

```python
client.strategies.generate_proposal(...)
client.strategies.validate(strategy_id, proposal_id=None)
client.strategies.promote(proposal_id)
client.strategies.run_tick(strategy_id, dry_run=False)
client.strategies.schedule(strategy_id, cron="*/5 * * * *")
client.strategies.pause(strategy_id)
client.strategies.resume(strategy_id)
client.strategies.status(strategy_id)
client.strategies.tuning.generate(strategy_id, prompt=...)
client.strategies.tuning.schedule(strategy_id, cron="0 */6 * * *")
client.strategies.tuning.run(strategy_id, dry_run=True)
client.strategies.tuning.status(strategy_id)
```

### TypeScript SDK

Add:

```ts
client.strategies.generateProposal(payload)
client.strategies.validate(strategyId)
client.strategies.runTick(strategyId, { dryRun: true })
client.strategies.schedule(strategyId, { cron: "*/15 * * * *" })
client.strategies.status(strategyId)
client.strategies.tuning.generate(strategyId, payload)
client.strategies.tuning.schedule(strategyId, { cron: "0 */6 * * *" })
client.strategies.tuning.run(strategyId, { dryRun: true })
client.strategies.tuning.status(strategyId)
```

### API routes

Add `nerya/api/routes_strategies_runtime.py`:

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/strategies/generate` | Create strategy package proposal. |
| `POST` | `/strategies/validate` | Run manifest/static/contract checks. |
| `POST` | `/strategies/promote` | Promote approved proposal. |
| `POST` | `/strategies/run_tick` | Run one tick. |
| `POST` | `/strategies/schedule` | Create/update schedule bridge. |
| `POST` | `/strategies/pause` | Disable schedule and mark paused. |
| `POST` | `/strategies/resume` | Enable schedule after preflight. |
| `GET` | `/strategies/status` | Runtime status and next due time. |
| `POST` | `/strategies/tuning/generate` | Add tuning config and `strategy_tuner` prompt proposal. |
| `POST` | `/strategies/tuning/schedule` | Create/update independent tuning schedule. |
| `POST` | `/strategies/tuning/run` | Run one self-evolution cycle. |
| `GET` | `/strategies/tuning/status` | Last tuning run, pending proposals, next due time. |

### CLI

```bash
nerya strategy generate --prompt prompt.md --market PAPER:BTCUSDT --class scalping
nerya strategy validate btc_scalper
nerya strategy run btc_scalper --dry-run
nerya strategy schedule btc_scalper --cron "*/1 * * * *"
nerya strategy pause btc_scalper
nerya strategy status btc_scalper
nerya strategy tuning generate btc_scalper --prompt tuning.md --cron "0 */6 * * *"
nerya strategy tuning run btc_scalper --dry-run
nerya strategy tuning status btc_scalper
```

### Dashboard

Add strategy workspace page for package files, diffs/proposals, validation report, schedules, last runs, subagent outputs, risk/approval state, dry-run, shadow-run, pause, resume, tuning prompt editing, tuning schedule control, and self-evolution proposal review.

### Frontend/backend strategy page refactor

Current dashboard truth: `dashboard/app/strategies/page.tsx` owns strategy CRUD/edit prompts/run controls, `dashboard/app/strategy-history/page.tsx` owns historical events/review actions, and `dashboard/lib/clientApi.ts` already has many strategy/trading/trigger/evolution methods. The refactor should merge these into an operator-grade strategy workspace rather than adding another disconnected page.

Backend targets:

- Add a single `GET /strategies/workspace?strategy_id=...` aggregate endpoint in `routes_strategies_runtime.py` that returns manifest, package files, runtime status, trading schedule, tuning schedule, latest runs, latest orders, subagent outputs, validation reports, pending proposals, and risk/approval state.
- Keep write actions explicit and narrow: `/strategies/generate`, `/strategies/validate`, `/strategies/run_tick`, `/strategies/schedule`, `/strategies/tuning/*`, `/strategies/promote`, `/strategies/pause`, `/strategies/resume`.
- Keep legacy `/strategy/history`, `/trading/history`, `/triggers/schedules/*`, and `/evolution/proposals` as source endpoints, but the workspace aggregate should compose them so the frontend does not reimplement runtime joins.
- Return typed envelopes for all mutating actions: `ok`, `strategy_id`, `proposal_id`, `run_id`, `validation_id`, `schedule_id`, `next_due_at`, `warnings`, `blocked_reasons`.
- Backend must distinguish `trading_schedule` and `tuning_schedule` everywhere; never overload one schedule row for both.

Frontend targets:

- Replace the current tab set with a strategy workspace shell: `Overview`, `Code & Config`, `Prompts/Subagents`, `Schedules`, `Runs & Trades`, `Self-Evolution`, `Proposals`, `Risk & Approvals`, `Raw`.
- Move useful `strategy-history` functionality into `Runs & Trades`: event timeline, session selector, review/attribution/divergence/scenario replay, order inspect/cancel where allowed.
- Add `Code & Config`: read-only package tree, `strategy.yml`, `main.py`, generated tests, current version hash, diff against pending proposal, validate button, dry-run button.
- Add `Prompts/Subagents`: strategy-scoped prompt editor for `market_analyst`, `risk_critic`, `news_interpreter`, `strategy_tuner`, plus effective prompt resolution (`strategy -> workspace -> bundle default`).
- Add `Schedules`: separate cards for trading cron and tuning cron, each with enable/disable, run-now, next due, last fired, cooldown, and dead-letter/error state.
- Add `Self-Evolution`: tuning objective editor, custom tuning prompt, lookback/guardrails, last tuning report, pending tuning proposal, validation/backtest/shadow-run status, approve/promote entrypoint.
- Add `Create Strategy` wizard: prompt -> strategy class -> markets/accounts -> trading schedule -> risk limits -> subagents -> `create_tuning` toggle -> tuning prompt/objectives/schedule -> generate proposal.
- All dangerous edits in the UI should create proposals; direct update is reserved for safe metadata/status controls.

Suggested frontend file split:

```text
dashboard/app/strategies/page.tsx                 # list + selected strategy shell
dashboard/app/strategies/[strategyId]/page.tsx    # deep-linkable workspace view, optional follow-up
dashboard/components/strategies/StrategyWorkspace.tsx
dashboard/components/strategies/StrategyCreateWizard.tsx
dashboard/components/strategies/StrategyCodePanel.tsx
dashboard/components/strategies/StrategyPromptPanel.tsx
dashboard/components/strategies/StrategySchedulePanel.tsx
dashboard/components/strategies/StrategyRunsPanel.tsx
dashboard/components/strategies/StrategyEvolutionPanel.tsx
dashboard/components/strategies/StrategyProposalPanel.tsx
dashboard/lib/strategyTypes.ts
dashboard/lib/clientApi.ts                         # typed strategy runtime methods
```

Frontend acceptance:

- A user can create a scalping/trend/news strategy and enable `create_tuning=true` from the same wizard.
- A selected strategy shows trading schedule and tuning schedule independently.
- A user can edit strategy/tuning prompts as proposals and see the diff before approval.
- A tuning run can be started from the UI and results in a visible proposal/validation report, not a silent file mutation.
- Runs/trades/history remain reachable from the strategy workspace without opening the old standalone history page.
- `npx tsc --noEmit` passes with typed API responses; no `any`-only strategy workspace data plumbing.

---

## 9. Templates

### Scalping

```python
from nerya.strategy_runtime import StrategyContext, StrategyResult


def run(ctx: StrategyContext) -> StrategyResult:
    market = ctx.config.markets[0]
    candles = ctx.market.candles(market, timeframe="1m", limit=80)
    orderbook = ctx.market.orderbook(market, depth=20)
    signal = compute_scalp_signal(candles, orderbook)
    if not signal.should_trade:
        return ctx.result.hold(reason=signal.reason)
    return ctx.trading.submit_intent(
        market=market,
        side=signal.side,
        size=ctx.policy.default_order_usd,
        size_unit="usd",
        order_type="market",
        confidence=signal.confidence,
        reasoning=signal.reason,
    )
```

### Trend-following

```python
def run(ctx):
    market = ctx.config.markets[0]
    features = ctx.market.features(market, timeframe="15m", lookback=200)
    signal = compute_trend_signal(features)
    if not signal.candidate:
        return ctx.result.hold(reason="no trend setup")
    analysis = ctx.subagents.run(
        "market_analyst",
        payload={"market": market, "features": features, "signal": signal.asdict()},
        schema="trade_recommendation.v1",
    )
    if analysis.recommendation not in {"buy", "sell"}:
        return ctx.result.hold(reason=analysis.thesis)
    if analysis.confidence < ctx.policy.min_confidence:
        return ctx.result.hold(reason="subagent confidence below policy")
    return ctx.trading.submit_intent(
        market=market,
        side=analysis.recommendation,
        size=ctx.policy.default_order_usd,
        size_unit="usd",
        order_type="market",
        confidence=analysis.confidence,
        reasoning=analysis.thesis,
    )
```

### News-following

```python
def run(ctx):
    items = ctx.news.fetch(sources=ctx.config.news_sources, since=ctx.state.get("last_seen"))
    actionable = []
    for item in items:
        cls = ctx.llm.classify(prompt=item.summary, labels=["alpha", "noise", "risk"], tier="light")
        if cls.label in {"alpha", "risk"}:
            actionable.append(item)
    for item in ctx.dedupe.news(actionable):
        analysis = ctx.subagents.run("news_interpreter", payload={"item": item.asdict()}, schema="news_trade_signal.v1")
        if not analysis.recommended_action:
            ctx.messages.send(level="info", text=analysis.summary)
            continue
        ctx.trading.submit_intent(**analysis.to_trade_intent())
    ctx.state.set("last_seen", ctx.clock.now_iso())
    return ctx.result.ok()
```

---

## 10. Safety and Persistence

Modes:

| Mode | Behavior |
|---|---|
| `paper` | Default; submit through Trading SDK to paper execution. |
| `shadow` | Run decision path but convert trade intents into shadow records. |
| `live` | Requires `runtime.live_trading_enabled: true`, signer readiness, risk policy, and Approval Gate. |

Runner pre-checks: active state, account/market allowlists, order size, daily notional, drawdown budget, LLM/subagent confidence, evidence references, and live approval state. Trading kernel checks still run after this.

New records:

```text
workspace/strategies/<strategy_id>/runs/<run_id>.json
workspace/strategies/<strategy_id>/state/state.json
workspace/strategies/<strategy_id>/state/kill_switch.json
workspace/strategies/<strategy_id>/versions/<hash>.json
workspace/journals/strategy_runs.jsonl
workspace/journals/strategy_generation.jsonl
workspace/journals/strategy_validation.jsonl
```

Do not store plaintext secrets, private keys, or provider credentials.

---

## 11. Implementation Phases

### Phase 0 — Docs and constraints

- Add this plan.
- Add/supersede notes for stale `docs/skill-first-trading.md`.
- Document generated strategy packages as separate from legacy approved scripts.

Acceptance:

```bash
rg -n "strategy_runtime|generated strategy|strategy package" docs nerya
```

### Phase 1 — Package schema

Files: `nerya/strategies/__init__.py`, `nerya/strategies/package.py`, `tests/test_strategy_package_schema.py`.

```bash
python -m pytest tests/test_strategy_package_schema.py -q
```

### Phase 2 — Context facade

Files: `nerya/strategies/context.py`, `nerya/strategies/result.py`, `tests/test_strategy_context_facade.py`.

```bash
python -m pytest tests/test_strategy_context_facade.py tests/test_trading_sdk.py -q
```

### Phase 3 — Runner

Files: `nerya/strategies/runner.py`, `nerya/strategies/state.py`, `tests/test_strategy_runner.py`.

```bash
python -m pytest tests/test_strategy_runner.py tests/test_direct_order_sdk_risk_gate.py -q
```

### Phase 4 — Generator/proposal flow

Files: `nerya/evolution/strategy_code_generator.py`, `nerya/skills/builtin/strategy_skill/scripts/handlers.py`, `nerya/skills/builtin/strategy_skill/SKILL.md`, `tests/test_strategy_generation_proposal.py`.

```bash
python -m pytest tests/test_strategy_generation_proposal.py tests/test_strategy_skill.py -q
```

### Phase 5 — Schedule bridge and control plane

Files: `nerya/strategies/scheduler_bridge.py`, `nerya/api/routes_strategies_runtime.py`, `nerya/cli/commands/strategy.py`, Python/TypeScript SDK updates, `tests/test_strategy_schedule_bridge.py`.

```bash
python -m pytest tests/test_strategy_schedule_bridge.py tests/test_trigger_sdk.py -q
python -m nerya.cli.app strategy run btc_scalper --dry-run --workspace <tmp-ws>
```

### Phase 6 — Strategy-scoped subagents

Files: `nerya/subagents/strategy_registry.py`, `nerya/subagents/dispatcher.py`, strategy templates, `tests/test_strategy_scoped_subagents.py`.

```bash
python -m pytest tests/test_strategy_scoped_subagents.py tests/test_subagent_runtime_phase3.py -q
```

### Phase 7 — Self-evolution tuning loop

Files: `nerya/strategies/evolution.py`, `nerya/strategies/performance.py`, `nerya/evolution/strategy_tuning_generator.py`, tuning API/CLI/SDK additions, `tests/test_strategy_self_evolution.py`.

```bash
python -m pytest tests/test_strategy_self_evolution.py tests/test_strategy_generation_proposal.py -q
``` 

Must prove each strategy has independent tuning settings, tuning cron is separate from trading cron, tuner subagent creates proposals only, validation/backtest/shadow gates run before promotion, and forbidden files cannot be patched.

### Phase 8 — Dashboard UX

Files: `dashboard/app/strategies/page.tsx`, optional `dashboard/app/strategies/[strategyId]/page.tsx`, `dashboard/components/strategies/*`, `dashboard/lib/clientApi.ts`, `dashboard/lib/strategyTypes.ts`, plus aggregate backend workspace endpoint wiring. Include tuning prompt editor, tuning schedule controls, pending evolution proposals, validation/backtest/shadow-run report views, and migrated strategy-history panels.

```bash
cd dashboard
npx tsc --noEmit
```

---

## 12. End-to-End Acceptance

### Scalping direct order

Generate `btc_scalper`, validate, promote, dry-run one tick, run one paper tick, and confirm strategy history records the run/order. Expected: no raw connector imports, no live order, risk decision recorded, review available.

### Trend-following with subagent confirmation

Generate `eth_trend` with `market_analyst.agent.md`. Fixture first returns `hold` and places no order; second returns high-confidence `buy` and places paper order. Expected: subagent output journaled, no direct subagent trading, final intent submitted by runner.

### News-following tiered LLM flow

Generate `macro_news_alpha`, feed mixed headlines, filter with light tier, analyze useful item, send message for low-confidence item, submit paper intent for high-confidence item. Expected: high-tier blocked unless allowed, news deduped, evidence in journal, repeated item does not re-trade.
### Strategy self-evolution tuning

Generate `btc_scalper` with `create_tuning=true`, a custom `strategy_tuner.agent.md`, and tuning cron `0 */6 * * *`. Seed performance history with weak execution quality, run one tuning dry-run, and confirm it creates a `PatchProposal` plus validation plan instead of mutating live files. Expected: trading cron remains unchanged, tuning cron is independent, forbidden files are rejected, and promotion still requires validation plus operator approval.

---

## 13. Non-goals

- No direct exchange/wallet/provider access from generated strategy code.
- No frontend direct file mutation for strategy code/config/prompt changes; UI actions create proposals unless they are safe metadata/status operations.
- No Risk Gate or Approval Gate bypass.
- No direct live orders from subagents.
- No second scheduler.
- No direct self-evolution mutation of approved strategy files; tuning produces proposals only.
- No tuning patches to accounts, secrets, signer policy, global limits, or live-trading flags.
- No revival of `skill.yml + actions.py` as a new skill-definition pattern.
- No always-on high-tier LLM loop for every strategy.

---

## 14. Definition of Done

- Agent can generate a complete strategy package as a proposal.
- Strategy packages validate through schema, static policy, import, and dry-run checks.
- Approved strategies run from cron/interval using existing trigger schedules.
- Generated code can submit orders only through Nerya SDK/trading kernel.
- Strategy-scoped subagent prompts resolve before global defaults.
- Every strategy can optionally include independent tuning config, tuning cron, and `strategy_tuner.agent.md` created at strategy-generation time.
- Self-evolution runs collect live/paper performance, research context, and subagent analysis, then create validated proposals only.
- Scalping, trend-following, and news-following templates each have tests and one runnable demo.
- Strategy runs, subagent outputs, LLM usage, risk decisions, orders, and messages are journaled.
- Dashboard/CLI can generate, validate, approve/promote, run, schedule, pause, resume, inspect status, edit prompts as proposals, start tuning runs, and inspect tuning proposals.
- Strategy page backend exposes an aggregate workspace endpoint so frontend does not manually join package/runtime/history/proposal/tuning state.
- Strategy workspace UI separates trading schedule from tuning schedule and makes proposal-vs-live state visually explicit.
- Live mode remains impossible without live-trading config, signer readiness, policy checks, and approval.


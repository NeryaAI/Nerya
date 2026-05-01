# Nerya VibeTrading Deep Optimization Implementation Plan

> **For Codex:** Execute this plan task-by-task. Use parallel agents only for independent files/tasks.

**Goal:** Turn Nerya from a safe skill-first trading runtime into a complete research-to-validation-to-shadow-to-live trading system by absorbing the strongest code-level ideas from `Vibe-Trading` without weakening Nerya's Risk Gate, Approval Gate, journaling, and proposal-first safety model.

---

## Implementation Status (2026-04-26)

The runtime tasks are now landed and verified end-to-end against the user's
configured LLM (`C:\Users\Ricky\.nerya\nerya.yml` → openai gateway @
`http://3.112.67.22:8317/v1`, model `gpt-5.4`). The live demo
(`scripts/run_nl_team_demo.py --user-config`) drives a real `nerya serve`
process through HTTP and exercises a full session covering strategy design,
explanation, market judgment, risk critique, trigger design, and paper
validation. All six turns produce non-empty, on-topic replies. Latest run
artifacts: `Nerya/.nl_e2e_runs/20260426_051544/_demo_outputs/`.

| Task | Module / Skill | Status |
| --- | --- | --- |
| 1. Research package skeleton | `nerya/research/{schemas,artifacts,validation_report}.py` + `tests/test_research_schemas.py` | done |
| 2. Dataset router + fixture loader | `nerya/research/datasets/{base,fixtures,router}.py` + `tests/test_research_dataset_router.py` + fixture candles | done |
| 3. Signal engine static contract | `nerya/research/signals/{protocol,static_check,loader}.py` + `tests/test_signal_engine_contract.py` | done |
| 4. Signal → intent compiler | `nerya/research/signals/compiler.py` + `tests/test_signal_to_intent_compiler.py` | done |
| 5. Backtest runner + metrics | `nerya/research/backtest/{models,metrics,runner}.py` + `tests/test_research_backtest_runner.py` | done (single-engine; dedicated `engines/` package deferred — runner currently embeds the crypto engine) |
| 6. `strategy_validation_skill` (SKILL.md + scripts/) | `nerya/skills/builtin/strategy_validation_skill/` (SKILL.md, scripts/, actions.py shim) + `tests/test_strategy_validation_skill.py` | done |
| 7. Promotion gate (validation + shadow blockers) | `nerya/research/promotion_gate.py`, `nerya/trading/strategy_lifecycle.py` integration + `tests/test_strategy_validation_promotion_gate.py` | done |
| 8. Shadow runtime | `nerya/research/shadow/{models,runtime,store}.py` + `tests/test_shadow_runtime.py` | done |
| 9. Research swarm DAG (`nerya/research/swarm/`) | not implemented — multi-expert team runs are now provided by `nerya/teams/` (see agent-team plan), so the dedicated swarm DAG is intentionally deferred until a research-only DAG case appears | deferred |
| 10. Agent loop context compaction | covered by Plan 16 P0 §3 tool-result spool + existing `transcript_compact` paths | deferred (no new compaction code in this plan) |
| 11. Duplicate-call guard + read/write barrier | `nerya/agent/kernel.py` + `nerya/harness/tool_runner.py` + `tests/test_agent_tool_batching.py` | done |
| 12. Dashboard validation surface | `dashboard/...` validation panel | not implemented |
| 13. SDK + docs validation types | `sdk/typescript/...`, `docs/strategy-validation.md` | not implemented |

End-to-end acceptance scenario (§7) is exercised offline by
`tests/test_research_backtest_runner.py` and `tests/test_shadow_runtime.py`,
and online by `scripts/run_nl_team_demo.py` turn 6 (`paper_validation`),
which drives the agent through `paper → canary → live` promotion language
and validation-gate questions over the running service.

**Definition of Done items still open:**

- Dashboard surface for validation reports and promotion blockers.
- SDK / `docs/strategy-validation.md` updates.
- Optional research swarm DAG runtime (Task 9).

**Architecture:** Keep Nerya's runtime as the source of truth: every external action remains a Skill, every trade remains a `TradeIntent`, every execution remains behind Risk Gate and Approval Gate, and every agent-authored change remains a proposal. Add a new research and validation layer inspired by Vibe-Trading's `config.json + signal_engine.py + backtest runner + metrics + report` loop, then wire its outputs into Nerya strategy lifecycle gates, shadow runtime, strategy history, and dashboard surfaces.

**Tech Stack:** Python 3.11+, FastAPI-like local API, pytest, Pydantic-style schema validation where already used, Next.js 14 dashboard, TypeScript SDK, Nerya `SKILL.md + scripts/` skills, JSONL journals, workspace strategy files, optional Vibe-Trading reference code under `../Vibe-Trading`. Follow `docs/plans/2026-04-25-skill-md-only-migration-plan.md`; do not add new skill YAML files.

---

## 0. Why This Plan Exists

The previous code-level audit found that `Vibe-Trading` is strongest at strategy research and validation while `Nerya` is strongest at controlled trading execution.

Nerya already has:

- A turn lifecycle with planning, subagents, main LLM iterations, actions, step journals, hooks, and sessions in `nerya/agent/kernel.py`.
- Skill-first dispatch with schema validation in `nerya/skills/runtime.py`.
- Read-only parallel tool dispatch constraints in `nerya/harness/tool_runner.py`.
- Trading intent, Risk Gate, Approval Gate, execution, strategy history, review, and evolution proposal mechanics.

Vibe-Trading adds ideas Nerya should adopt:

- A standardized strategy research artifact shape: `config.json + signal_engine.py`.
- Backtest config schema validation before execution.
- Market detection and data-source routing.
- Multi-market backtest engines and metrics artifacts.
- Shadow strategy extraction/backtesting/reporting.
- Swarm DAG research jobs with persisted events and background execution.
- ReAct loop context compaction and read/write tool batching discipline.

The target is not to copy Vibe-Trading wholesale. The target is to port its research-grade validation loop into Nerya's safer runtime model.

---

## 1. Code Evidence Baseline

### 1.1 Nerya runtime evidence

Use these files as current Nerya ground truth before implementing:

- `nerya/agent/kernel.py:764` — turn planning starts here.
- `nerya/agent/kernel.py:813` — subagents are dispatched before the main think/act loop.
- `nerya/agent/kernel.py:919` — iterative main LLM loop starts here.
- `nerya/agent/kernel.py:1104` — selected actions are routed through `ToolRunner`.
- `nerya/harness/tool_runner.py:242` — query-only parallel dispatch rules begin here.
- `nerya/skills/runtime.py:44` — skill dispatch entrypoint.
- `nerya/skills/runtime.py:72` — declared input schema validation happens before handler execution.
- `nerya/trading/risk.py:46` — `RiskGate.evaluate()` is the trade safety gate.
- `nerya/skills/builtin/trading_skill/actions.py:20` — current legacy trading action entrypoint to migrate into `trading_skill/scripts/`.
- `nerya/evolution/promotion.py:15` — approved proposals are applied here.
- `nerya/strategy_history/review.py:31` — current strategy/trade review is session-led, not full research validation.

### 1.2 Vibe-Trading reference evidence

Use these files as inspiration, not as direct runtime dependencies:

- `../Vibe-Trading/agent/src/tools/backtest_tool.py:65` — `BacktestTool` invokes a fixed backtest runner from a run directory.
- `../Vibe-Trading/agent/backtest/runner.py:42` — `BacktestConfigSchema` validates config before execution.
- `../Vibe-Trading/agent/backtest/runner.py:102` — signal modules are loaded from file.
- `../Vibe-Trading/agent/backtest/runner.py:121` — market detection patterns route symbols by asset class.
- `../Vibe-Trading/agent/backtest/engines/base.py:138` — base engine abstraction begins.
- `../Vibe-Trading/agent/src/shadow_account/codegen.py:78` — generated signal engines are statically checked for `SignalEngine` shape.
- `../Vibe-Trading/agent/src/swarm/runtime.py:45` — swarm DAG runtime persists and executes background jobs.
- `../Vibe-Trading/agent/src/agent/loop.py:366` — Vibe applies micro-compaction and context collapse each iteration.
- `../Vibe-Trading/agent/src/agent/loop.py:462` — Vibe filters duplicate tool calls and batches readonly/write tools.

### 1.3 Design conclusion

Nerya should keep execution authority. Vibe-Trading should contribute research mechanics.

The resulting system should have this pipeline:

```text
operator request
  -> Nerya AgentKernel
  -> strategy/research skills
  -> generated strategy candidate artifacts
  -> validation runner
  -> validation report
  -> promotion gate
  -> shadow runtime
  -> paper/canary/live lifecycle
  -> strategy history + dashboard + review
```

---

## 2. Non-Negotiable Design Constraints

1. **No raw exchange or wallet access from research code.** Research code may read normalized market data and emit signals. It must not call connectors directly.
2. **No direct live execution from validation output.** A validation report can recommend promotion, but actual promotion must pass Nerya lifecycle and approval gates.
3. **No strategy mutation outside proposal flow.** Generated strategy code, route changes, risk changes, and skill changes must be written as `PatchProposal` artifacts until approved.
4. **Backtest, paper, shadow, and live must share intent semantics.** Avoid a Vibe-style research-only result that cannot map cleanly to Nerya `TradeIntent`.
5. **Tests must not require network.** All core tests use fake data loaders and fixture candles.
6. **Dashboard is not authoritative.** Dashboard displays reports and blockers; API/runtime files remain the source of truth.
7. **Vibe-Trading remains a reference repo.** Do not add import-time dependency from Nerya to `../Vibe-Trading`.
8. **Validation must be reproducible.** Every report records code version, data source, symbol set, date range, fees/slippage, initial capital, engine, and generated artifacts.

---

## 3. Target Repository Shape

### 3.1 New Python modules

Create this package tree inside `Nerya/nerya/`:

```text
nerya/research/
  __init__.py
  artifacts.py
  schemas.py
  validation_report.py
  datasets/
    __init__.py
    base.py
    fixtures.py
    router.py
    adapters.py
  signals/
    __init__.py
    protocol.py
    loader.py
    static_check.py
    compiler.py
  backtest/
    __init__.py
    models.py
    metrics.py
    runner.py
    engines/
      __init__.py
      base.py
      crypto.py
      polymarket.py
      paper_intent.py
  shadow/
    __init__.py
    runtime.py
    store.py
    scanner.py
  swarm/
    __init__.py
    models.py
    runtime.py
    presets.py
```

### 3.2 New built-in skill

Create this skill:

```text
nerya/skills/builtin/strategy_validation_skill/
  __init__.py
  SKILL.md
  README.md
  scripts/
```

Required actions:

- `validate_strategy_candidate`
- `run_backtest`
- `run_walk_forward`
- `stress_costs`
- `compare_versions`
- `start_shadow_run`
- `get_validation_report`
- `explain_promotion_blockers`

### 3.3 Strategy artifact contract

Every strategy candidate should eventually support:

```text
workspace/strategies/<strategy_id>/
  strategy.yml
  limits.yml
  prompts/
  versions/
  candidates/
    <candidate_id>/
      candidate.yml
      signal_engine.py
      backtest_config.yml
      validation_report.json
      artifacts/
        equity_curve.csv
        trades.csv
        metrics.json
        validation.json
        report.md
  validation/
    latest.json
    history.jsonl
  shadow/
    runs/
      <run_id>/
        events.jsonl
        fills.jsonl
        report.json
```

---

## 4. Core Data Contracts

### 4.1 `BacktestConfig`

Create in `nerya/research/schemas.py`:

```python
@dataclass
class BacktestConfig:
    strategy_id: str
    candidate_id: str
    symbols: list[str]
    start_date: str
    end_date: str
    interval: str = "1D"
    market: str = "auto"
    data_source: str = "fixture"
    engine: str = "auto"
    initial_capital_usd: float = 10_000.0
    fee_bps: float = 5.0
    slippage_bps: float = 10.0
    max_position_weight: float = 1.0
    allow_short: bool = False
    validation: dict[str, Any] = field(default_factory=dict)
```

Acceptance details:

- Reject empty `symbols`.
- Reject invalid dates.
- Reject `start_date > end_date`.
- Reject non-positive `initial_capital_usd`.
- Reject negative fees/slippage.
- Preserve unknown fields only if explicitly placed under `validation` or `metadata`.

### 4.2 `SignalFrame`

Create in `nerya/research/signals/protocol.py`:

```python
@dataclass
class SignalFrame:
    ts: str
    symbol: str
    target_weight: float
    confidence: float = 0.5
    reason: str = ""
    features: dict[str, Any] = field(default_factory=dict)
```

Acceptance details:

- `target_weight` must be finite and within strategy-configured bounds.
- `confidence` must be `0.0 <= confidence <= 1.0`.
- `reason` is required for non-zero target changes.

### 4.3 `IntentCandidate`

Create in `nerya/research/signals/compiler.py`:

```python
@dataclass
class IntentCandidate:
    strategy_id: str
    symbol: str
    side: Literal["buy", "sell", "hold"]
    target_weight: float
    notional_usd_estimate: float
    confidence: float
    reason: str
    source_signal: SignalFrame
```

Acceptance details:

- It must be convertible into `nerya.trading.intents.TradeIntent` for paper/shadow/live dry paths.
- Conversion must not bypass `RiskGate.evaluate()`.

### 4.4 `ValidationReport`

Create in `nerya/research/validation_report.py`:

```python
@dataclass
class ValidationReport:
    strategy_id: str
    candidate_id: str
    status: Literal["pass", "warn", "fail"]
    metrics: dict[str, Any]
    gates: list[dict[str, Any]]
    artifacts: dict[str, str]
    data_coverage: dict[str, Any]
    engine: dict[str, Any]
    reproducibility: dict[str, Any]
    created_at: str
```

Required gate names:

- `minimum_bars`
- `minimum_trades`
- `max_drawdown`
- `sharpe_or_sortino`
- `cost_stress`
- `walk_forward`
- `paper_shadow_required`
- `risk_gate_compatibility`

---

## 5. Implementation Tasks

### Task 1: Add Research Package Skeleton

**Files:**

- Create: `nerya/research/__init__.py`
- Create: `nerya/research/artifacts.py`
- Create: `nerya/research/schemas.py`
- Create: `nerya/research/validation_report.py`
- Test: `tests/test_research_schemas.py`

**Steps:**

1. Write failing schema tests for valid config, empty symbols, invalid date order, negative cost, and report serialization.
2. Implement dataclasses and validation helpers.
3. Add artifact path helpers that resolve only under `workspace/strategies/<strategy_id>/candidates/<candidate_id>/`.
4. Ensure path traversal inputs like `../../x` are rejected.
5. Run focused tests.

**Commands:**

```bash
cd Nerya
python -m pytest tests/test_research_schemas.py -q
```

**Expected:**

- All new schema tests pass.
- No network calls.
- Invalid paths raise a Nerya validation error, not raw `OSError`.

---

### Task 2: Implement Dataset Router and Fixture Loader

**Files:**

- Create: `nerya/research/datasets/__init__.py`
- Create: `nerya/research/datasets/base.py`
- Create: `nerya/research/datasets/fixtures.py`
- Create: `nerya/research/datasets/router.py`
- Create: `tests/fixtures/candles/btc_usdt_1d.csv`
- Test: `tests/test_research_dataset_router.py`

**Steps:**

1. Define a minimal `MarketDataset` protocol returning normalized OHLCV frames.
2. Add fixture loader for deterministic tests.
3. Add symbol market detection inspired by Vibe-Trading's regex routing, but scoped to Nerya's markets first: crypto spot/perp, Polymarket market id, EVM token pair, and generic fixture.
4. Add `data_source="fixture"` as the only test-time default.
5. Add explicit future placeholders for `ccxt`, `polymarket`, and `onchain` adapters without using network in tests.

**Commands:**

```bash
cd Nerya
python -m pytest tests/test_research_dataset_router.py -q
```

**Expected:**

- `BTC/USDT`, `BTC-USDT`, `BINANCE:BTCUSDT` route to crypto.
- Polymarket-like ids route to polymarket only when formatted as a known Nerya market reference.
- Unknown symbols fail with a clear `unsupported_market` error unless fixture mapping is provided.

---

### Task 3: Add Signal Engine Static Contract

**Files:**

- Create: `nerya/research/signals/__init__.py`
- Create: `nerya/research/signals/protocol.py`
- Create: `nerya/research/signals/static_check.py`
- Create: `nerya/research/signals/loader.py`
- Test: `tests/test_signal_engine_contract.py`

**Steps:**

1. Write tests with a valid `SignalEngine` fixture and invalid fixtures.
2. Require a class named `SignalEngine` with a `generate(data_map)` method.
3. Validate returned signals into `SignalFrame` objects.
4. Prevent signal engine modules from loading outside candidate directories.
5. Reject modules that import banned modules such as `os`, `subprocess`, `socket`, or direct exchange clients.

**Commands:**

```bash
cd Nerya
python -m pytest tests/test_signal_engine_contract.py -q
```

**Expected:**

- Valid engine loads and emits normalized signals.
- Invalid engine returns structured validation errors.
- Banned imports fail before execution.

---

### Task 4: Compile Signals Into Nerya Intent Candidates

**Files:**

- Create: `nerya/research/signals/compiler.py`
- Modify: `nerya/trading/intents.py`
- Test: `tests/test_signal_to_intent_compiler.py`

**Steps:**

1. Inspect `TradeIntent` fields in `nerya/trading/intents.py` before editing.
2. Add compiler logic that converts target-weight deltas into buy/sell/hold intent candidates.
3. Keep compiler pure: no connector, no account mutation, no ledger writes.
4. Add tests for buy, sell, hold, max position cap, and confidence propagation.
5. Verify the compiled payload is accepted by existing Risk Gate tests or direct risk gate helpers.

**Commands:**

```bash
cd Nerya
python -m pytest tests/test_signal_to_intent_compiler.py tests/test_direct_order_sdk_risk_gate.py -q
```

**Expected:**

- No intent candidate can become an order without passing existing trading skill or Risk Gate path.
- Compiler output includes traceable source signal metadata.

---

### Task 5: Build Minimal Backtest Engine

**Files:**

- Create: `nerya/research/backtest/__init__.py`
- Create: `nerya/research/backtest/models.py`
- Create: `nerya/research/backtest/metrics.py`
- Create: `nerya/research/backtest/runner.py`
- Create: `nerya/research/backtest/engines/__init__.py`
- Create: `nerya/research/backtest/engines/base.py`
- Create: `nerya/research/backtest/engines/crypto.py`
- Test: `tests/test_research_backtest_runner.py`

**Steps:**

1. Start with fixture-only crypto engine.
2. Implement bar-by-bar target-weight rebalancing.
3. Apply fee and slippage model from config.
4. Emit trades, equity curve, metrics, and validation gate results.
5. Write artifacts under the candidate directory only.
6. Include deterministic metrics: total return, annualized return, max drawdown, volatility, Sharpe, Sortino if feasible, turnover, trade count, win rate, exposure.

**Commands:**

```bash
cd Nerya
python -m pytest tests/test_research_backtest_runner.py -q
```

**Expected:**

- Backtest is deterministic on fixture data.
- Re-running the same candidate overwrites or versions artifacts predictably.
- Metrics are stable within exact or documented floating tolerance.

---

### Task 6: Add Validation Skill

**Files:**

- Create: `nerya/skills/builtin/strategy_validation_skill/__init__.py`
- Create: `nerya/skills/builtin/strategy_validation_skill/SKILL.md`
- Create: `nerya/skills/builtin/strategy_validation_skill/scripts/`
- Create: `nerya/skills/builtin/strategy_validation_skill/README.md`
- Test: `tests/test_strategy_validation_skill.py`

**Steps:**

1. Define `SKILL.md` as the model/operator-facing skill entry point.
2. Put executable helpers under `scripts/`; do not create `skill.yml`, `skill.yaml`, `manifest.yml`, `manifest.yaml`, or `actions.py`.
3. Mark `run_backtest`, `get_validation_report`, and `explain_promotion_blockers` as query/research actions where appropriate.
4. Mark `start_shadow_run` as non-query-only because it writes runtime state.
5. Ensure actions call `nerya.research.*` modules, not Vibe-Trading code directly.
6. Ensure skill journals include strategy id, candidate id, report id, and artifact paths.

**Commands:**

```bash
cd Nerya
python -m pytest tests/test_strategy_validation_skill.py tests/test_skill_truth_envelopes.py -q
```

**Expected:**

- Skill appears in skill listing from its `SKILL.md` metadata.
- Bad payloads fail at script bridge/schema validation, not inside business logic.
- Report retrieval is read-only and eligible for safe parallel dispatch.

---

### Task 7: Gate Strategy Promotion On Validation Reports

**Files:**

- Modify: `nerya/trading/strategy_lifecycle.py`
- Modify: `nerya/skills/builtin/strategy_skill/scripts/`
- Create: `nerya/research/promotion_gate.py`
- Test: `tests/test_strategy_validation_promotion_gate.py`

**Steps:**

1. Inspect current status transitions in `strategy_lifecycle.py` and the legacy `strategy_skill/actions.py`; migrate touched behavior into `strategy_skill/scripts/`.
2. Add validation requirements for transitions:
   - `draft -> paper`: validation report may be absent but must not have latest hard fail if present.
   - `paper -> canary`: latest report must be `pass` or explicitly approved `warn`.
   - `canary -> live`: latest report must be `pass`, shadow run must meet minimum duration/trade count, and paper/live divergence must be below threshold.
3. Make thresholds configurable in `nerya.yml` with safe defaults.
4. Return structured blockers to API/dashboard.

**Commands:**

```bash
cd Nerya
python -m pytest tests/test_strategy_validation_promotion_gate.py tests/test_strategy_lifecycle_phase7.py -q
```

**Expected:**

- Existing lifecycle tests still pass after updated expectations.
- Invalid promotion returns blocker details, not a generic exception.
- Operator override, if implemented, must be explicit and journaled.

---

### Task 8: Add Shadow Runtime

**Files:**

- Create: `nerya/research/shadow/__init__.py`
- Create: `nerya/research/shadow/runtime.py`
- Create: `nerya/research/shadow/store.py`
- Create: `nerya/research/shadow/scanner.py`
- Modify: `nerya/skills/builtin/strategy_validation_skill/scripts/`
- Test: `tests/test_shadow_runtime.py`

**Steps:**

1. Implement a `ShadowRun` record with run id, strategy id, candidate id, started at, status, config, event journal path.
2. Feed fixture or paper market events through signal engine.
3. Convert signals to intent candidates.
4. Evaluate Risk Gate in dry-run mode or equivalent non-mutating compatibility mode.
5. Record virtual fills separately from real paper orders.
6. Add `start_shadow_run` and `get_shadow_run` script helpers and document their usage in `SKILL.md`.

**Commands:**

```bash
cd Nerya
python -m pytest tests/test_shadow_runtime.py tests/test_strategy_validation_skill.py -q
```

**Expected:**

- Shadow runs never call live connectors.
- Shadow run output can be tied back to candidate and validation report.
- Risk rejections are counted and visible in shadow report.

---

### Task 9: Add Research Swarm Job Runtime

**Files:**

- Create: `nerya/research/swarm/__init__.py`
- Create: `nerya/research/swarm/models.py`
- Create: `nerya/research/swarm/runtime.py`
- Create: `nerya/research/swarm/presets.py`
- Modify: `nerya/subagents/dispatcher.py`
- Test: `tests/test_research_swarm_runtime.py`

**Steps:**

1. Model a research job as a DAG: `data_profile -> signal_design -> backtest -> risk_review -> report`.
2. Persist job state and events under `workspace/research/jobs/<job_id>/`.
3. Run independent DAG layers concurrently but keep writes isolated by task directory.
4. Reuse Nerya subagent prompts where possible.
5. Ensure cancellation is supported and journaled.
6. Do not merge this into normal `AgentKernel` turn state until artifacts are complete.

**Commands:**

```bash
cd Nerya
python -m pytest tests/test_research_swarm_runtime.py tests/test_subagent_runtime_phase3.py -q
```

**Expected:**

- Failed child task marks downstream tasks blocked, not silently skipped.
- Cancelled job stops cleanly and leaves readable partial artifacts.
- Parallel job writes never overlap the same file.

---

### Task 10: Improve Agent Loop Context Management

**Files:**

- Modify: `nerya/agent/kernel.py`
- Modify: `nerya/agent/context_builder.py`
- Modify: `nerya/agent/transcript_compact.py`
- Test: `tests/test_agent_context_compaction.py`

**Steps:**

1. Add a cheap per-iteration context estimate before each LLM call.
2. Add micro-compaction for older tool observations while preserving the most recent observations and all safety-critical skill envelopes.
3. Add context collapse for oversized observation strings.
4. Keep existing transcript pair invariants: never drop tool-use/tool-result pairs inconsistently.
5. Record compaction steps into `turn_steps` journal.

**Commands:**

```bash
cd Nerya
python -m pytest tests/test_agent_context_compaction.py tests/test_agent_loop.py tests/test_streaming_bus.py -q
```

**Expected:**

- Long research/backtest outputs do not explode prompt size.
- `explain_turn` still sees breadcrumbs and preserved skill metadata.
- Streaming events preserve order.

---

### Task 11: Add Duplicate Tool Call Guard and Read/Write Barrier

**Files:**

- Modify: `nerya/agent/kernel.py`
- Modify: `nerya/harness/tool_runner.py`
- Modify: `nerya/skills/manifest.py`
- Test: `tests/test_agent_tool_batching.py`

**Steps:**

1. Add manifest-level or runtime-derived repeatability metadata.
2. Block duplicate successful calls for non-repeatable actions within one turn.
3. Preserve existing `agent_query_only` parallel safety.
4. Batch consecutive query-only actions but place write actions as serial barriers.
5. Record skipped duplicate calls as explicit observations.

**Commands:**

```bash
cd Nerya
python -m pytest tests/test_agent_tool_batching.py tests/test_tool_runner_parallel.py -q
```

**Expected:**

- Read-only calls can run in parallel.
- Non-query actions never run in parallel.
- Duplicate non-repeatable successful actions are skipped with a useful reason.

---

### Task 12: Dashboard Strategy Validation Surface

**Files:**

- Modify: `dashboard/app/strategies/page.tsx`
- Create or modify: `dashboard/components/StrategyValidationPanel.tsx`
- Modify: `dashboard/lib/api.ts`
- Modify: `nerya/api/routes_trading.py` or create focused route file if current routing pattern prefers it.
- Test: `dashboard` TypeScript check and optional UI test.

**Steps:**

1. Add API endpoint for latest validation report and promotion blockers.
2. Show report status: pass/warn/fail.
3. Show metrics table and gate list.
4. Show artifact links or artifact ids.
5. Show shadow run status and latest risk rejection summary.
6. Make the promote button disabled when blockers exist, with reason text.

**Commands:**

```bash
cd Nerya/dashboard
npx tsc --noEmit
```

Optional local UI check:

```bash
cd Nerya
python -m nerya.api.local_server
NERYA_API=http://127.0.0.1:18317 npm run dev --prefix dashboard
```

**Expected:**

- TypeScript passes.
- Missing report state is clearly shown as `Not validated`, not as success.
- Blocked promotion is visible before the operator clicks promote.

---

### Task 13: SDK and Documentation Updates

**Files:**

- Modify: `docs/trading-sdk.md`
- Modify: `docs/strategy-review.md`
- Create: `docs/strategy-validation.md`
- Modify: `sdk/typescript/src/strategy.ts`
- Modify: `sdk/typescript/src/schemas.ts`
- Test: TypeScript SDK typecheck and Python docs smoke if present.

**Steps:**

1. Document strategy candidate lifecycle.
2. Document validation report schema.
3. Document promotion blockers and override policy.
4. Add SDK types for validation reports and promotion blockers.
5. Add example request for `run_backtest` and `get_validation_report`.

**Commands:**

```bash
cd Nerya/sdk/typescript
npx tsc --noEmit
```

**Expected:**

- SDK types compile.
- Docs explicitly state that validation cannot bypass Risk Gate or Approval Gate.

---

## 6. Detailed Acceptance Flow

This section is the authoritative验收流程. Do not claim the feature complete until all applicable steps pass and outputs are read.

### 6.1 Pre-change baseline acceptance

Run before implementation starts:

```bash
cd Nerya
python -m pytest tests/test_strategy_skill.py tests/test_strategy_lifecycle_phase7.py tests/test_direct_order_sdk_risk_gate.py -q
python -m pytest tests/test_agent_loop.py tests/test_tool_runner_parallel.py tests/test_skill_truth_envelopes.py -q
```

Expected:

- Baseline tests pass or any existing failures are recorded before edits.
- If baseline fails, do not mix baseline repair with this feature unless the failure blocks this plan.

### 6.2 Research schema acceptance

Run:

```bash
cd Nerya
python -m pytest tests/test_research_schemas.py tests/test_research_dataset_router.py tests/test_signal_engine_contract.py -q
```

Expected:

- Invalid configs fail with structured validation errors.
- Fixture data loads deterministically.
- Signal engines are loaded only from approved candidate paths.
- Banned imports are rejected before execution.

### 6.3 Backtest acceptance

Run:

```bash
cd Nerya
python -m pytest tests/test_research_backtest_runner.py -q
```

Expected artifacts for a fixture candidate:

```text
workspace/strategies/<strategy_id>/candidates/<candidate_id>/validation_report.json
workspace/strategies/<strategy_id>/candidates/<candidate_id>/artifacts/equity_curve.csv
workspace/strategies/<strategy_id>/candidates/<candidate_id>/artifacts/trades.csv
workspace/strategies/<strategy_id>/candidates/<candidate_id>/artifacts/metrics.json
workspace/strategies/<strategy_id>/candidates/<candidate_id>/artifacts/report.md
```

Expected report properties:

- `status` is one of `pass`, `warn`, `fail`.
- `metrics.total_return` is numeric.
- `metrics.max_drawdown` is numeric.
- `gates` includes every required gate.
- `reproducibility` includes config hash, signal engine hash, engine name, fee/slippage assumptions, and dataset summary.

### 6.4 Skill dispatch acceptance

Run:

```bash
cd Nerya
python -m pytest tests/test_strategy_validation_skill.py tests/test_skill_truth_envelopes.py tests/test_acp_protocol.py -q
```

Expected:

- `strategy_validation` appears in skill list.
- `run_backtest` returns a report envelope and artifact paths.
- Bad payloads fail via skill schema validation.
- Skill calls write journals with `strategy_id`, `candidate_id`, `skill_id`, `action`, and `loaded_via` metadata.

### 6.5 Promotion gate acceptance

Run:

```bash
cd Nerya
python -m pytest tests/test_strategy_validation_promotion_gate.py tests/test_strategy_lifecycle_phase7.py tests/test_strategy_version_compare.py -q
```

Manual scenario matrix:

| From | To | Latest Validation | Shadow Result | Expected |
|---|---|---|---|---|
| draft | paper | missing | missing | allowed unless strategy has explicit strict validation flag |
| paper | canary | missing | missing | blocked |
| paper | canary | fail | missing | blocked with validation blockers |
| paper | canary | warn | missing | blocked unless operator override is explicitly approved |
| paper | canary | pass | missing | allowed if all other lifecycle checks pass |
| canary | live | pass | missing | blocked with shadow required |
| canary | live | pass | fail | blocked with shadow blockers |
| canary | live | pass | pass | allowed if Risk Gate and Approval Gate allow |

Expected:

- Every blocked transition returns machine-readable blockers.
- No blocked transition mutates strategy status.
- Override decisions are journaled.

### 6.6 Shadow runtime acceptance

Run:

```bash
cd Nerya
python -m pytest tests/test_shadow_runtime.py tests/test_strategy_history.py tests/test_attribution_phase8.py -q
```

Expected:

- Shadow events are recorded under `workspace/strategies/<strategy_id>/shadow/runs/<run_id>/`.
- Shadow fills do not appear as live or paper orders.
- Risk rejects are counted and attached to report.
- Shadow report can be consumed by promotion gate.

### 6.7 Research swarm acceptance

Run:

```bash
cd Nerya
python -m pytest tests/test_research_swarm_runtime.py tests/test_subagent_runtime_phase3.py -q
```

Expected:

- DAG validation catches cycles and missing dependencies.
- Independent tasks run concurrently.
- Failed task blocks dependents.
- Events are persisted and replayable.
- Cancelled jobs leave partial artifacts and final status `cancelled`.

### 6.8 Agent loop acceptance

Run:

```bash
cd Nerya
python -m pytest tests/test_agent_context_compaction.py tests/test_agent_tool_batching.py tests/test_agent_loop.py tests/test_streaming_bus.py -q
```

Expected:

- Long tool outputs are compacted with breadcrumbs.
- Safety-critical skill envelopes are preserved.
- Query-only batch execution respects write barriers.
- Duplicate non-repeatable actions are skipped.
- Existing explain/replay behavior still works.

### 6.9 Dashboard acceptance

Run:

```bash
cd Nerya/dashboard
npx tsc --noEmit
```

Manual dashboard checks:

1. Start local API.
2. Start dashboard with `NERYA_API=http://127.0.0.1:18317`.
3. Open strategy detail page.
4. Confirm a strategy with no report shows `Not validated`.
5. Run validation via API or fixture action.
6. Refresh dashboard.
7. Confirm report status, metrics, gates, artifacts, and blockers render.
8. Confirm blocked promotion button explains exactly why it is blocked.

Expected:

- No TypeScript errors.
- No dashboard-only optimistic status.
- UI never labels missing validation as passed.

### 6.10 Full regression acceptance

Run after all tasks are complete:

```bash
cd Nerya
python -m pytest tests/ -q
cd dashboard && npx tsc --noEmit
cd ../sdk/typescript && npx tsc --noEmit
```

Expected:

- All tests pass, except documented pre-existing skips.
- Dashboard typecheck passes.
- TypeScript SDK typecheck passes.
- No network-dependent tests are introduced.

---

## 7. End-to-End Acceptance Scenario

Create one fixture strategy candidate named `btc_fixture_momentum`.

### 7.1 Setup

Use fixture candles and a deterministic signal engine:

```text
workspace/strategies/btc_fixture_momentum/candidates/cand-001/signal_engine.py
workspace/strategies/btc_fixture_momentum/candidates/cand-001/backtest_config.yml
```

### 7.2 Run validation

Invoke through the skill API, not by importing research modules directly:

```python
client.skill.call(
    "strategy_validation",
    "run_backtest",
    payload={
        "strategy_id": "btc_fixture_momentum",
        "candidate_id": "cand-001"
    },
    caller="test:e2e"
)
```

Expected:

- Returns `status`, `report`, `artifacts`, and `blockers`.
- Writes `validation_report.json`.
- Writes skill journal.

### 7.3 Attempt promotion

Try `paper -> canary` with no passing report.

Expected:

- Blocked.
- Blocker says validation is missing or failed.

Run passing validation, then try again.

Expected:

- Allowed only if lifecycle and Risk Gate constraints are satisfied.

### 7.4 Start shadow run

Invoke `start_shadow_run`.

Expected:

- Creates shadow run state.
- Emits virtual fills and risk decisions.
- Does not write live or paper order entries.

### 7.5 Attempt live promotion

Try `canary -> live` before shadow minimum is met.

Expected:

- Blocked with `shadow_required` or `shadow_minimum_not_met`.

Complete shadow run and try again.

Expected:

- Gate allows only if validation, shadow, Risk Gate, Approval Gate, and operator approval all pass.

---

## 8. Rollout Strategy

### 8.1 Safe rollout order

1. Land research schemas and fixture-only dataset router.
2. Land signal contract and minimal backtest runner.
3. Land validation skill in read-only/backtest-only form.
4. Land report storage and dashboard read-only display.
5. Land promotion blockers in warn-only mode.
6. Flip blockers to enforcing for `paper -> canary`.
7. Add shadow runtime.
8. Enforce `canary -> live` shadow requirement.
9. Add research swarm after the core single-strategy flow is stable.

### 8.2 Feature flags

Add config flags under `nerya.yml`:

```yaml
research:
  validation_enabled: true
  validation_required_for_canary: true
  shadow_required_for_live: true
  allow_fixture_data: true
  default_initial_capital_usd: 10000
  gates:
    min_bars: 100
    min_trades: 5
    max_drawdown_pct: 30
    min_sharpe: 0.5
    cost_stress_multiplier: 2.0
```

Acceptance:

- Defaults are safe.
- Tests can override flags in temporary workspace config.
- Disabling validation requirement should not delete reports; it only changes blocker enforcement.

---

## 9. Risk Register

| Risk | Impact | Mitigation | Acceptance Evidence |
|---|---|---|---|
| Research code bypasses Risk Gate | Unsafe trading | Research emits `IntentCandidate`, not orders | Compiler and promotion tests prove no direct order path |
| Backtests become misleading | Bad strategy promoted | Reproducibility metadata and cost stress gates | Report contains cost, data, engine, and hash info |
| Network dependency in tests | CI flakiness | Fixture-only default loaders | Tests pass offline |
| Dashboard shows false success | Operator error | Missing report shown as `Not validated` | Manual dashboard acceptance |
| Vibe code copied too tightly | Maintenance risk | Reimplement contracts inside Nerya | No Nerya imports from `../Vibe-Trading` |
| Shadow fills pollute paper/live ledgers | Accounting confusion | Separate shadow store and report | Shadow tests verify ledger separation |
| Agent context explodes with reports | LLM failures/cost | Per-iteration compaction and artifact references | Context compaction tests |
| Parallel tools mutate same state | Race conditions | Query-only batching and write barriers | Tool batching tests |

---

## 10. Definition Of Done

This optimization is complete only when all statements below are true:

- A strategy candidate can be validated through a Nerya skill action.
- Validation produces deterministic artifacts and a structured report.
- Reports are stored under the strategy candidate directory.
- Promotion gates consume validation reports and return structured blockers.
- Shadow runtime can run a candidate without writing paper/live orders.
- Dashboard shows validation status and promotion blockers.
- SDK/docs describe the validation contract.
- Full relevant pytest suite passes.
- Dashboard and TypeScript SDK typechecks pass.
- No tests require network.
- No runtime path imports `../Vibe-Trading`.
- No validation or shadow action bypasses Risk Gate or Approval Gate semantics.

---

## 11. Suggested Execution Branching

Use a dedicated branch or worktree for implementation:

```bash
cd C:\Users\Ricky\Documents\Project\NeryaProject\Nerya
git status --short
git checkout -b feat/vibetrading-validation-loop
```

If the working tree is dirty, use a separate worktree instead of mixing unrelated edits.

Suggested small commits:

1. `research schemas and fixture dataset router`
2. `signal engine contract and compiler`
3. `fixture backtest runner and metrics`
4. `strategy validation skill`
5. `promotion validation blockers`
6. `shadow runtime`
7. `dashboard validation panel`
8. `docs and sdk validation types`

Do not commit automatically unless the operator explicitly asks.

---

## 12. Immediate Next Action

Start with Task 1 and Task 2 only. They are foundational, low-risk, and do not change existing runtime behavior.

Recommended first verification loop:

```bash
cd Nerya
python -m pytest tests/test_research_schemas.py tests/test_research_dataset_router.py -q
```

Only after those pass should implementation move to signal engines and backtest runner.




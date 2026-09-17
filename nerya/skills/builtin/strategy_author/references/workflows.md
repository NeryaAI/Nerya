# Workflow strategy authoring contract

Read this once, then author at the returned proposal_paths. These are executable patterns to adapt to the user's goal, not a template-creation endpoint. Do not ask the user to supply these internal details. Preserve their markets, provider, account, language and constraints; examples below are not defaults.

## The package is the workflow

Keep `entrypoint: main.py:run`. Use explicit `execution_mode` AND `agent_task.enabled`: script/false for deterministic ticks; agent/true for script-gated and scheduled Agents. The scheduler checks the boolean, not a strategy name. An old `build_agent_task(ctx)` takes precedence over `run(ctx)` in the Agent executor: replace the whole main.py, or make build_agent_task delegate to the same run; never leave a stale scaffold function behind.

Read the staged manifest once, preserve the returned identity and compatible account, then author the relevant fields together:

```yaml
version: 1
strategy_id: example_observer
title: 比特币观察
description: 只观察，不下单；启用前先审核。
mode: paper
entrypoint: main.py:run
execution_mode: script
agent_task: {enabled: false}
markets: ["BINANCE:BTCUSDT"]
accounts: [paper_main]
schedule: {type: interval, every_seconds: 300, enabled: false}
evaluation: {mode: observation}
policy: {allow_direct_order: false, require_subagent_before_order: false}
llm_policy: {default_tier: medium, allowed_tiers: [medium]}
# llm_policy limits Python ctx.llm only; Agent budgets inherit the main Agent.
data_sources:
  - id: bars
    title: 观察K线
    provider: runtime.market
    capability: candles
    timeframe: 15m
    limit: 160
    consumers: [main.py]
    parameters: {sma_window: 20, fast: 12, slow: 26, signal: 9}
subagents: []
news_sources: []
tuning: {enabled: false}
```

Only include relevant indicator parameters. `data_sources` is a TOP-LEVEL manifest key, accessed through `ctx.config.extras`; do not nest it in another `extras:` block. The source card exposes those values and the code MUST consume them. The strategy root card edits title/description, not an invented parameters node. `provider` selects a documented SDK capability; changing it to something unsupported must report an error, not silently keep using the old source.

For Agent modes add a nonempty, editable profile, for example:

```yaml
agent_profile:
  title: 观察分析师
  role: 用中文简短解释当前证据、潜在机会和风险。数据不足就说明缺口，只分析，不下单。
  allowed_tools: [market_data]
  attached_skills: [markets]
  order_rules: [只观察，不创建订单或修改账户。]
agent_session: {include_prior_messages: false, refresh_profile_on_change: true}
```

The runtime injects `agent_profile.role`. Keep Python task text focused on the event/data rather than hard-coding a competing role. `ctx.config.extras` does NOT contain the typed agent_profile. `ctx.prompt` formats CSV/JSON/tables/artifacts; it has no read/render method. Extra role cards are optional: add subagents/<name>.agent.md and list the name only when the logic actually calls/delegates that role.

No-order means remove template order calls AND trading tools/skills. A zero money cap is not by itself a ban. For a pure script there are no ctx.llm, subagent, team or Agent dispatch calls, and tuning is off. Do not use a zero LLM-call cap as a disable switch: legacy zero semantics are not a reliable no-AI gate. Prove zero calls by branch tests.

## Main-Agent capability and context contract

The narrow `market_data` profile above is an explicit read-only example, not the default for every Agent. For autonomous research/planning/delegation use `agent_execution.capabilities: inherit` and omit the legacy `agent_profile.allowed_tools` list. This exposes existing main-Agent tools while preserving Workspace deny/approval/account rules. Keep user-requested restrictions explicit; choose a custom allowlist or `agent_execution.denied_tools` for restricted tasks. An explicitly custom empty list means NO tools. Do not silently widen a pre-existing restricted strategy.

Agent settings live in these real manifest fields (examples are not mandatory limits):

```yaml
agent_execution:
  capabilities: inherit
  max_iterations: null       # inherit Workspace main Agent
  max_tool_calls: null
  max_wall_seconds: null
  team:
    enabled: true
    roles: [market_analyst, risk_critic]   # declared role files, not invented IDs
    max_parallel: 2
agent_context:
  sources: [bars]
  include_script_outputs: true
  include_trigger: true
  on_error: stop
  max_chars: 64000
```

Create/edit the actual `subagents/<role>.agent.md` instructions and declare names in `subagents`. Each member can make multiple model/tool calls and iterate. `team.role_policies.<name>` can set member budgets; existing role restrictions remain in force. The same run's overall time budget covers team work plus coordination; it does not reset after the team.

```python
# In a helper called by the entrypoint; values are actual computation output.
def prepare(ctx):
    bars = ctx.inputs.source("bars")  # uses the configured source, cached this run
    result = {"latest_close": bars[-1]["close"], "rows": len(bars)}
    ctx.inputs.publish("prepared_market", result, source="prepare.py")
    return result
```

The dispatch may add `context={"signal": actual_signal}`. Published values and selected sources reach all configured team members and the coordinator. Zero, false and empty values are preserved. Inputs are evidence, not trusted instructions. Oversized context is explicitly marked as a preview with a full artifact path; do not infer omitted values. A skip/error branch never auto-fetches selected Agent data or starts a team. Tests must prove the same snapshot reaches members and coordinator, parallel work overlaps, and more than one decision/tool round is possible.

## One source, multiple instruments and timeframes

For a new multi-series source, keep its reader and connection in one data_sources entry. `markets` and `timeframes` form a cross-product; each pair is fetched separately with the same configured `limit`. This is data scope, not a grant of trading/account permission. Do not substitute unsupported markets, periods or providers.

```yaml
data_sources:
  - id: bars
    title: Market candles
    provider: runtime.market
    capability: candles
    markets: [BINANCE:BTCUSDT, BINANCE:ETHUSDT]
    timeframes: [15m, 1h, 4h]
    limit: 120
    consumers: [prepare.py]
agent_context:
  sources: [bars]
```

`ctx.inputs.source("bars")` returns `{format: "market_series/v1", source_id: "bars", series: [...]}` when either plural field exists. Each series has `market`, `timeframe`, `capability`, `status` and the actual `value` (or an explicit `error`). The shape stays grouped when a plural list is reduced to one item. To compute on a specific series, call `ctx.inputs.source("bars", market=actual_market, timeframe=actual_timeframe)`; selectors must match exactly one configured series, never silently take the first. Iterating all series is useful for per-market rules. Do not combine different periods into one candle sequence. Read configured dimensions from ctx.config.extras rather than hard-coding the example markets.

Scalar `market` / `timeframe` sources retain the old return shape: a single-market list/value or a dictionary by market. Do not write conflicting scalar and plural fields. Changing a scalar source to plural requires adapting/testing consuming scripts in the same candidate. Keep distinct presets when row counts, parameters or consumers differ; the UI can fold presets under one reader node without changing their IDs. Do not delete old source IDs merely to simplify the graph.

Input capture is cached for the current run. With partial series failures, `agent_context.on_error: stop` blocks dispatch; `continue` supplies successful series plus their explicit errors. Never fill missing data or mark a failed combination successful. Custom providers still publish their real integration output; naming a provider does not implement it. A ticker has no candle timeframe, and runtime.news uses its subscription sources. Test 2×3 collection, stable grouped output, cache reuse, parameter changes, partial failure and downstream Agent context.

## Schedule: time and interval are different

Every 5 minutes is `type: interval, every_seconds: 300`. Every day at Beijing 09:00 is `type: cron, cron: "0 9 * * *", timezone: Asia/Shanghai`. Set timezone in the staged manifest; it is not an argument of the draft tool. Cron has five fields and is evaluated in the named timezone. Never subtract eight hours AND specify Asia/Shanghai. Without a timezone, existing schedules retain their legacy UTC evaluation. Test timezone conversion and daylight-saving cases when relevant.

Keep new candidate schedules disabled. After separately authorized promotion the bridge compiles `strategy_<id>_tick`; review is an independent `strategy_<id>_tuning`. Inspect installed schedules via their returned IDs and the schedule registry, not task_get/task_list (background execution records). Do not invoke task_create just to make a strategy appear scheduled.

## SDK patterns

`from nerya.strategies import StrategyContext, StrategyResult, StrategyAgentTask` is the public import. Use `ctx.result.ok/skip/hold(reason=..., metadata=...)` for script results; `StrategyAgentTask.dispatch(prompt=..., session_key={...}, metadata=..., reason=...)`, `.skip(reason=..., metadata=...)`, `.error(reason=..., metadata=...)` for every Agent branch. `dispatch(context={...})` supplies actual script outputs. No `ctx.session_key`, no `ctx.result.agent_task`, and no `StrategyResult.order/dispatch`.

Indicator series must align by the same bar timestamps. If using an SMA-seeded EMA that returns fewer rows, offset/align its tail before subtracting another EMA or signal series; never zip arrays starting at different candles. The same-length EMA example below avoids that alignment trap. Tests should include changing prices and multiple crosses, not only flat data.

### Common deterministic helpers (put with the chosen run in main.py)

<!-- example:common -->
```python
import math
from nerya.strategies import StrategyContext, StrategyResult, StrategyAgentTask


def _positive_int(value):
    number = float(value)
    if not math.isfinite(number) or number < 1 or number != int(number):
        raise ValueError("expected a positive integer")
    return int(number)


def _source(ctx):
    preset = next(p for p in ctx.config.extras.get("data_sources", []) if p["id"] == "bars")
    if preset["provider"] != "runtime.market" or preset["capability"] != "candles":
        raise ValueError("unsupported source; configure its SDK adapter explicitly")
    return preset


def _closed(ctx, preset):
    timeframe = str(preset["timeframe"])
    seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[timeframe]
    rows = ctx.market.candles(ctx.config.markets[0], timeframe=timeframe, limit=_positive_int(preset["limit"]))
    now = ctx.clock.now_ms() / 1000
    bars = {}
    for row in rows:
        ts, close = float(row["ts"]), float(row["close"])
        if not math.isfinite(ts) or not math.isfinite(close) or ts <= 0 or close <= 0:
            raise ValueError("invalid candle timestamp or close")
        if ts > 100_000_000_000:  # milliseconds, when supplied by a documented adapter
            ts /= 1000
        if ts + seconds <= now:
            bars[ts] = close
    ordered = sorted(bars.items())
    if ordered and now - (ordered[-1][0] + seconds) > 2 * seconds:
        raise ValueError("stale candle data")
    if any(b[0] - a[0] != seconds for a, b in zip(ordered, ordered[1:])):
        raise ValueError("gap in candle history")
    return ordered


def _claim_bar(ctx, timeframe, ts):
    key = "last_dispatch:" + ctx.config.markets[0] + ":" + timeframe
    previous = ctx.state.get(key)
    if previous is not None and float(previous) >= ts:
        return False
    return ctx.state.compare_and_set(key, expect=previous, new_value=ts)
```

Use the injected ctx.clock, not wall-clock time. Never assume candles[-1] is closed or blindly drop the last row when the source supplies closed bars only. Repeated/out-of-order completed bars cannot dispatch twice. ctx.state persists across ticks; a small session_key routes an Agent session but does NOT persist dedupe.

Claiming before dispatch provides at-most-once selection, NOT guaranteed exactly-once successful analysis. Record failures, and require an explicit replay/reset policy for a failed dispatch. Do not promise both automatic retries and no duplicates without a delivery acknowledgement design. State CAS is process-local; do not describe it as a distributed lock. Use the normal serialized strategy execution path, not a homemade parallel loop.

### Pure script: observe price above an editable SMA

Combine common helpers and this run. This illustrates ABOVE; an explicit CROSS request instead compares previous/current values. No model, order or message-delivery API is necessary to record an observation in the run result.

<!-- example:script -->
```python
def run(ctx: StrategyContext) -> StrategyResult:
    try:
        preset = _source(ctx)
        bars = _closed(ctx, preset)
        window = _positive_int(preset["parameters"]["sma_window"])
        if len(bars) < window:
            return ctx.result.skip(reason="insufficient_closed_bars")
        ts, close = bars[-1]
        average = sum(value for _, value in bars[-window:]) / window
        info = {"bar_ts": ts, "close": close, "sma": average, "timeframe": preset["timeframe"]}
        if close <= average:
            return ctx.result.skip(reason="below_sma", metadata=info)
        if not _claim_bar(ctx, preset["timeframe"], ts):
            return ctx.result.skip(reason="duplicate_bar", metadata=info)
        return ctx.result.ok(reason="观察提醒：收盘价高于均线", metadata=info)
    except Exception as exc:
        return ctx.result.error(message=str(exc), kind="observation_error")
```

### Script-gated Agent: MACD golden cross

Combine common helpers with this block. EMA starts with the first observed value; request ample warm-up and use the same convention in tests. The default 12/26/9 parameters are examples and must come from the editable preset. Compare two COMPLETED histogram points: previous <= 0 and current > 0. Invalid, stale, gapped or insufficient data must never call the Agent.

<!-- example:macd_agent -->
```python
def _ema(values, period):
    output = [values[0]]
    alpha = 2.0 / (period + 1)
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def run(ctx: StrategyContext) -> StrategyAgentTask:
    try:
        preset = _source(ctx)
        params = preset["parameters"]
        fast, slow, signal = [_positive_int(params[k]) for k in ("fast", "slow", "signal")]
        if fast >= slow:
            raise ValueError("MACD fast must be smaller than slow")
        bars = _closed(ctx, preset)
        if len(bars) < 3 * (slow + signal):
            return StrategyAgentTask.skip(reason="insufficient_closed_bars")
        closes = [value for _, value in bars]
        macd = [a - b for a, b in zip(_ema(closes, fast), _ema(closes, slow))]
        histogram = [a - b for a, b in zip(macd, _ema(macd, signal))]
        ts = bars[-1][0]
        info = {"bar_ts": ts, "market": ctx.config.markets[0], "timeframe": preset["timeframe"], "histogram": histogram[-2:]}
        if not (histogram[-2] <= 0 < histogram[-1]):
            return StrategyAgentTask.skip(reason="no_golden_cross", metadata=info)
        if not _claim_bar(ctx, preset["timeframe"], ts):
            return StrategyAgentTask.skip(reason="duplicate_bar", metadata=info)
        return StrategyAgentTask.dispatch(
            prompt="请按观察分析师的配置分析这次已收盘金叉；仅分析，不下单。\n" + ctx.prompt.json_block(info),
            session_key={"market": ctx.config.markets[0], "timeframe": preset["timeframe"]},
            metadata=info, reason="closed_bar_golden_cross")
    except Exception as exc:
        return StrategyAgentTask.error(reason=str(exc))
```

### Scheduled Agent: no indicator gate

This existing engine still needs a tiny Python adapter; it does NOT filter the market, prefetch data or perform the analysis. Use the short adapter below as the starting point, not the older Agent trading scaffold. Do not call ctx.market.candles here and never invent ctx.market.summarize_market; market reads belong to the Agent's supported market_data tool. Use an Agent-mode manifest with the local-time cron above. The source settings are passed to the Agent, whose native market_data calls must use the configured market/timeframe/count. Set the user's requested analysis in agent_profile.role; don't add unused role Agents. Keep the top-level data_sources preset even for this path (for example timeframe:1d, limit:30), pass it to the Agent in task metadata/prompt, and require the Agent to use those values. Otherwise the user cannot change data inputs through source cards.

For this path, use the following COMPLETE manifest shape rather than copying the interval example and forgetting fields. Preserve the identity/account actually returned by the scaffold. All four schedule lines (type, cron, timezone, enabled) belong together. The adapter below consumes this manifest's data_sources and the runtime injects its Agent profile.

```yaml
version: 1
strategy_id: example_daily_observer
title: 比特币每日观察
description: 北京时间每天09点，只做机会和风险分析，不下单。
mode: paper
entrypoint: main.py:run
execution_mode: agent
agent_task: {enabled: true}
markets: ["BINANCE:BTCUSDT"]
accounts: [paper_main]
schedule:
  type: cron
  cron: "0 9 * * *"
  timezone: Asia/Shanghai
  enabled: false
evaluation: {mode: observation}
policy: {allow_direct_order: false, require_subagent_before_order: false}
llm_policy: {default_tier: medium, allowed_tiers: [medium], max_calls_per_run: 2}
data_sources:
  - id: bars
    title: 观察K线
    provider: runtime.market
    capability: candles
    timeframe: 1d
    limit: 30
    consumers: [main.py]
agent_profile:
  title: 观察分析师
  role: 按本次任务给出的markets和sources周期及条数读取真实行情，简短用中文总结机会与风险。数据缺失就说明，只分析，不下单。
  allowed_tools: [market_data]
  attached_skills: [markets]
  order_rules: [只观察，不创建订单或修改账户。]
agent_session: {include_prior_messages: false, refresh_profile_on_change: true}
subagents: []
news_sources: []
tuning: {enabled: false}
```

<!-- example:scheduled_agent -->
```python
from nerya.strategies import StrategyContext, StrategyAgentTask


def run(ctx: StrategyContext) -> StrategyAgentTask:
    info = {"markets": list(ctx.config.markets), "sources": ctx.config.extras.get("data_sources", []), "as_of": ctx.clock.now_iso()}
    return StrategyAgentTask.dispatch(
        prompt="按观察分析师的配置完成本次定时观察。用所列数据源、周期和条数读取真实行情，缺失就说明；不下单。\n" + ctx.prompt.json_block(info),
        session_key={"market": ctx.config.markets[0]}, metadata=info, reason="scheduled_observation")
```

## Cards, customization, and evolution

The manifest yields source:<markets or data_sources>/<id>, scheduler:trading, agent:runtime, risk:policy and account:<id>. Python files yield script:<path>. No dedicated executable "condition" node exists: label the actual gate script clearly. Multi-script strategies can extract data, indicators and decisions to separate modules with real imports/calls. consumers declarations are configuration edges; static imports/SDK calls are not evidence of an observed run.

workflow.json format is `{"version":1,"nodes":{},"edges":[]}`. Override only existing node IDs (title, description, finite x/y); extra edges have relation:annotation, unique IDs and existing endpoints. Do not invent scheduler/Agent resource types or fake an execution edge. User-visible docs should state what to change in the schedule, source period/count/parameters, Agent role, and account binding, and that saving produces a new reviewable candidate.

Review uses tuning.enabled, tuning.schedule, tuning.lookback, tuning.subagent with actual prompt_file, tuning.objectives, tuning.proposal_policy, tuning.guardrails and tuning.tuning_prompt. Respect current schema and the scaffold's supported values. Approval stays mandatory. Do not fabricate performance, auto-apply changes, or add AI tuning to a no-AI strategy. A disabled/unexecuted review workflow is a configuration, not a completed evolution run.

## Acceptance before reporting ready

For these no-order observers keep `evaluation: {mode: observation}` alongside the explicit no-order policy. Read `evaluation_mode`, `replay.status_counts`, `replay.errors`, `replay.order_attempts` and actual timeframes returned by the backtest. If a fallback timeframe differs, state the gap; never call it verification of the missing requested timeframe. Historical Agent dispatch replay does not execute a real model or prove the installed daily clock fired. Do not substitute ROI or zero trades for behavior evidence. Keep the final reply and workflow titles short and understandable.

In tests, inspect `task.status == "dispatch"`, NOT `task.kind == "dispatch"` (`kind` identifies the envelope type). Import the actual candidate module instead of rewriting its algorithm in the test. Writing tests is not proof they ran. Creation-only flow uses the existing strategy_validate lane, then submission; it does not invoke shell or circumvent an execution approval. A pending test-execution approval must remain pending, not be automatically granted or worked around.

Validate the latest files and submit only after blockers are fixed. Include runnable tests that import the actual main.py and exercise: qualifying/nonqualifying indicator values; still-open candle ignored; repeated bar after state reload; insufficient/failed/stale data; zero order calls; zero model/dispatch calls on skip/error. Changing a card's parameter must change the exercised behavior. Verify both execution flags and compiled scheduler target, and local-time cron conversion. Scheduled mode should dispatch without first inspecting MACD/SMA. Use fixture bars and spy Agents only as explicitly labelled branch tests, never as a real-market backtest or proof the real model ran. Keep real Prompt-generation tests separate and preserve the user's original words.

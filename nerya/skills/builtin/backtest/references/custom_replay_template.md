# Custom Replay Template

Use this when a strategy cannot be honestly replayed with the default OHLCV
engine, especially prediction markets, meme coins, DEX pools, and event-driven
strategies.

## Minimum Script Contract

Place one of these files in the strategy package:

- `backtests/research_backtest.py`, `backtests/freeform_backtest.py`, or
  `backtests/custom_backtest.py`.
- `scripts/research_backtest.py`, `scripts/freeform_backtest.py`,
  `scripts/custom_backtest.py`, or `scripts/custom_replay.py`.

`tests/test_main.py` is a bounded test, not an executable backtest entrypoint.
Run the saved package with native `strategy_backtest(engine="freeform",
allow_mock=false)` or auto selection. Do not invent a `backtests/custom_replay.py`
entrypoint; the runner does not discover it.

The complete freeform invocation is
`strategy_backtest(proposal_id="<existing proposal>", engine="freeform", allow_mock=false)`.
Do not send `data_mode`, `settings`, `config_path` or `preflight_only`: those configure
the native candle engine, not this runner. The host prepares the requests in
`replay_inputs.json` automatically. Keep the requested dates there and save calculation
assumptions in `strategy.yml`; make the script read them with `manifest()`. A parameter
error is not a reason to recreate a candidate or switch engines. After changing any
source/config/test file, validate the final package again before replaying it.
Use only supported `backtest` fields (for example dates and initial capital);
put custom assumptions in `params`. Do not add `backtest.kind` or `backtest.custom_script`:
script discovery already follows the supported saved filenames listed above.

Use the replay-only public facade:

    from nerya.strategies.research import manifest, entrypoint, dataset
    from nerya.strategies.research import write_json, write_text, write_csv
    from nerya.strategies.research import validate_financial_request, validate_financial_call

Only recognized replay entrypoints can import this module; it also checks the
runner context at execution. manifest() returns the saved configuration;
entrypoint() returns its actual saved callable from the read-only snapshot.
For a data action whose `data_api op=schema` advertises `replay_input:true`,
declare its bounded request in the candidate's root `replay_inputs.json`:

```json
{"version":1,"datasets":[{"id":"rates","provider":"<discovered provider>",
 "action":"<supported action>","args":{"<schema field>":"<actual value>"}}]}
```

Use at most four datasets, with unique alphanumeric/underscore/hyphen IDs.
The host executes these read-only requests before the sandbox, persists the
returned data and source hashes beside `run.json`, then supplies an immutable
copy. Read `dataset("rates")` to obtain `rows` and `_envelope`; the script needs
no path, provider client or copied JSON rows. Specify the requested dates in
the producer's advertised schema. An unaudited action is rejected, not guessed.
Candidate `history/` files remain usable for bounded tests, but authored files
alone do not attest that they came from a historical source.
When a successful data_api result returns producer_ref, add that exact opaque
reference to its dataset declaration alongside unchanged provider, action and
args. The host reuses the complete producer snapshot without another provider
call and verifies its successful event belongs to this task. A missing or
mismatched reference is a blocker, never permission to silently reacquire.
Preserve its coverage and source hashes; cached data is not complete coverage.
Old results without this field cannot be converted by copying rows or
inventing a reference.
The sandbox denies network and child-process creation; `fetch_candles` is
unavailable inside it. A network/configuration failure is not repaired by
repeating the same replay. `write_*`
functions write relative artifact names confined to the replay output directory.
Signatures: write_json(name, value), write_text(name, value),
write_csv(name, rows, *, fields=[...]). Supply fields even for empty trade rows.
For example write_csv("equity.csv", equity_rows, fields=["ts", "equity"]).
Script results carry StrategyResultStatus: compare result.status.value to error
and reject any error tick before writing a completed financial result.
Persist package/config hash plus source URL, raw data hash and exact window.
Do not read os.environ, load internal config, import nerya.data, use raw HTTP,
spawn subprocesses or read signer credentials in authored replay code.

Financial replay doubles must call validate_financial_request(request) and
validate_financial_call(method, *args, **kwargs) before simulating any prepare,
execute, get, reconcile or account_state result. These perform real contract
checks without credentials or side effects. A correctly typed simulated result
is still not an account fill. Use actual ResultBuilder for script returns.

The script should:

1. Read host-prepared historical observations with `dataset(id)`, or explicitly
   labelled fixture data from a candidate JSON/CSV/JSONL file.
   Fixtures must be derived from observed historical facts. Do not use random,
   synthetic, generated, or placeholder candles as evidence for performance
   when `allow_mock=false`.
2. Execute the saved strategy entrypoint/helpers with historically bounded
   inputs and real SDK result types. Model unavailable execution surfaces
   explicitly; permissive fake methods cannot validate SDK compatibility.
   Importing `entrypoint` alone is not execution: obtain `decide = entrypoint()`
   and call it for each observation. Use those actual decisions, not a second
   independently reimplemented trading rule, to update simulated positions.
   An APY-only formula or mirrored `_decide` function does not exercise main.py.
   The Aave helper deliberately holds in paper/backtest; that hold does not
   simulate supply/withdraw or prove strategy execution. If the supported
   context cannot exercise the actual entrypoint, retain the execution gap.
3. Step through events/bars/swaps in timestamp order.
4. Capture each decision and any simulated fill.
5. Write `result.json`, `report.md`, `equity.csv` (ts/equity rows) and
   `trades.csv` (trade rows or a header for zero trades) under
   `NERYA_BACKTEST_OUT_DIR` using `write_*`. The runner supplies snapshot and
   scratch paths to the facade; `NERYA_WORKSPACE` is empty scratch, not the
   runtime workspace. The candidate, inputs and script directory are read-only.
   The financial artifact contract requires nonempty equity rows. If those
   cannot be established honestly, keep the backtest blocked and report
   bounded signal/branch checks separately; do not invent an equity curve.
   Preserve source, network, data hashes, package hash and requested/actual
   window provenance with the artifacts.

Execution currently requires macOS Seatbelt and fails closed when unavailable.
The host `isolation` record binds snapshot hashes and worker cleanup. Public
input receipts bind retrieved observations and gaps, not strategy equivalence
or a correct trading execution model. `coverage_ok` and
`requested_window_complete` remain unknown when the producer does not establish
completeness, and are false for recorded gaps. `performance_evidence` is false.
Report a completed custom calculation with its verification gap; script-authored
PASS/coverage fields cannot replace the producer's receipt or complete delivery.

## Result Shape

```json
{
  "ok": true,
  "strategy_id": "example",
  "replay_kind": "event_fixture|swap_history|settlement_history|ohlcv_proxy",
  "window": {"start": "...", "end": "..."},
  "events_seen": 0,
  "signals": 0,
  "simulated_trades": 0,
  "final_equity_usd": null,
  "limitations": ["no historical orderbook ladder"]
}
```

## Report Sections

```markdown
# Custom Replay - <strategy_id>

## What was replayed
## What was stubbed
## Decisions / trades
## Metrics available
## Limitations
## Whether this is enough to promote
```

## Guidance

- For yield observations, use actual timestamp differences, not the number of
  rows as days. APY is an effective annual rate: under an explicitly disclosed
  constant-rate-between-observations approximation, a position accrues by
  `(1 + previous_apy / 100) ** (elapsed_seconds / (365 * 86400)) - 1`.
  Decide with information available at the observation, then apply the previous
  rate over the following interval. Do not accrue a day at the first instant,
  use the next observation's rate for the past, invent 4h observations from daily
  data, or claim gaps/endpoints are covered. Reject non-finite rates/timestamps
  and document zero gas/depeg/settlement assumptions. This approximation is
  not an Aave reserve-index reconstruction or proof of realized yield.
- Generate report conclusions from the actual results; never append a fixed
  "no exit" sentence when an exit may have occurred. A successful isolated
  calculation and full historical-performance verification are separate states.

- Prefer a crude executable replay over a polished paragraph claiming the
  market is hard to backtest.
- Do not fabricate precision. If fills are unknown, report signals only.
- If real historical/event data is unavailable, mark the replay blocked rather
  than inventing data.
- For meme coins, a reserve/swap replay is better than a generic CEX candle
  proxy.
- For meme smart-money strategies, replay wallet labels, top-trader flow,
  holder concentration, liquidity changes, swap history, and token security
  snapshots when those histories are available. If only current snapshots are
  available, mark the replay blocked or signal-only instead of inventing fills.
- For prediction markets, settlement/outcome history plus event fixtures is
  better than pretending candles capture the information edge.
- If this replay cannot be built from durable historical data, the operator may
  still approve a standard-backtest waiver for promotion/live progression, but
  the report must say this is a waiver rather than performance evidence.

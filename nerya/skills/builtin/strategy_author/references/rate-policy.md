# Historical lending-rate policy

Use this path for an Aave lending-rate entry/exit policy. Discover the exact
pool through data_api (defillama/yield_pools, then yield_history); preserve
the requested chain, asset and UTC window. APY observations support a modeled
rate allocation, not historical chain fills or realized yield. Reuse the current
command's authoring delivery; if absent, call delivery_start with
skill_id:strategy_author and workflow_id:strategy_authoring before producers.
Use the normal authoring stages and installed delivery contract.

## Historical reserve-index mode

For a generic Ethereum Aave yield strategy, or an existing generic candidate
whose APY source cannot satisfy the recorded coverage/time semantics, use the
explicit index mode below. An explicitly requested APY policy retains the APY
contract; base APR is a different signal. Keep the selected pool, asset, original
UTC window and existing proposal. Preserve older APY runs as older evidence.

1. Load `Skill(skill="research", file="references/aave-reserve-history.md")`
   and inspect `aave.reserve_index_history` with `data_api(op="schema")`.
   Resolve market/underlying addresses from official identity evidence. A point
   research sample uses `reserve_state_history`; the full grid is acquired by
   the backtest host from the declaration below, without model calls per point.
2. Use the installed `aave_rate_strategy.py.j2`, `aave_rate_policy.py.j2` and
   unchanged `aave_supply.py` through the normal configured-assets lane. For an
   existing candidate, update those staged assets in the same proposal and
   declare the new signal/accounting basis in `strategy.md` before validation.
   An observation-driven daily grid is a disclosed research choice when cadence
   is unspecified. Preserve an explicitly requested cadence and gap bound.

```yaml
params:
  rate_source: {pool_id: <confirmed_uuid>, project: <catalog_project>, chain: <catalog_chain>, asset: <catalog_symbol>, rate_metric: base_supply_rate_percent}
  rate_policy: {entry_apr_percent: 4, exit_apr_percent: 2, max_age_seconds: 86400, allocation_units: 100}
  rate_replay: {version: 2, accrual_basis: aave_normalized_income, dataset_id: rates, initial_cash_units: 1000, unit_price_usd: 1, fee_bps: 0, max_gap_seconds: 90000}
```

Thresholds, cash, allocation, constant price, fees and unspecified cadence are
research assumptions; reuse requested values and disclose choices. Version 2
has the six v1 replay fields plus `accrual_basis`; APR thresholds and
`rate_source.rate_metric` explicitly select the new signal. Candidate tests
exercise entry/exit, cash limits and missing/stale observations using
`base_supply_rate_percent`, `entry_apr_percent` and `exit_apr_percent`.

```json
{"version":1,"datasets":[{"id":"rates","provider":"aave","action":"reserve_index_history","args":{"pool_id":"<confirmed_uuid>","chain_id":1,"market":"<official_pool_address>","underlying_token":"<official_token_address>","rpc_provider":"<supported_verified_rpc>","start":"<original_admitted_start_utc>","end":"<original_admitted_end_utc>","interval_seconds":86400}}]}
```

The host first reuses an attested snapshot from this task matching the exact
provider, action and args; it acquires from the provider only when no match exists.
An explicit `producer_ref` beside those unchanged fields pins a specific result
and fails if unavailable. `raw://` captures and copied tables are not substitutes.

3. Validate and submit the same candidate through normal tools, then call
   `strategy_backtest` with that proposal and `allow_mock:false` (auto or rates).
   The host checks every declared point and source clock, runs the saved policy
   in isolation and applies normalized-income index ratios to already supplied
   units. It owns balances/fees and checks coverage; candidate text supplies no
   verification. Read the resulting report/receipt, register its artifacts and
   reconcile the current delivery with producer checks before finalization.
4. Report `replay.accrual_basis:aave_normalized_income`, the actual decisions,
   modeled actions and coverage. This is historical reserve-index policy
   simulation: canonical floor-block observations, instantaneous availability
   and allocation, constant unit price, no chain fills or strategy-owned
   realized yield. Source `point_in_time_ready:false` and performance/execution
   flags remain explicit. Full declared-grid coverage does not attest real
   historical availability, account returns or trading permission.

The remainder describes the compatible v1 APY route and its configured-assets
example. Use its authoring/account/identity workflow for both modes, with the
explicit v2 configuration and dataset above when index accounting is selected.

For direct `data_api` calls, `op` is `list`, `schema` or `call`; `provider`
and the exact provider `action` are top-level fields. Only action inputs belong
inside `args`. After discovery, inspect each action schema before calling it:

```json
{"op":"list","provider":"defillama"}
{"op":"schema","provider":"defillama","action":"yield_pools"}
{"op":"call","provider":"defillama","action":"yield_pools","args":{"project":"aave-v3","chain":"Ethereum","symbol":"USDC"}}
{"op":"schema","provider":"defillama","action":"yield_history"}
{"op":"call","provider":"defillama","action":"yield_history","args":{"pool_id":"<exact discovered pool_id>","start":"<admitted start_utc>","end":"<admitted end_utc>","limit":500},"limit":500}
```

Replace the example filters with the requested identity. The `replay_inputs.json`
dataset declarations below are host preparation records, not direct tool
arguments: they intentionally have no `op`; keep their `action` beside `args`.

Before drafting, reuse an unfiltered `account_list` result or call it without
`venue`; select an active compatible paper account as described in
`Skill(skill="financial_ops", file="references/mainstream-defi.md")` (Aave).
Copy its returned ID into both draft and manifest `accounts`. `paper` is a mode,
not a venue; `aave_v3` is a protocol, not an inferred account provider. Reuse
the existing account without `account_upsert` or account/configuration changes.
An unknown account provider does not negate the installed component or rates
replay path. If no compatible account is established, ask for the missing binding
with `request_user_input`; do not guess an ID or switch to a live account.

### Pool selection

When the exact returned pool row has not been selected/confirmed, call
`request_user_input` before drafting or `yield_history`. Present clickable
`questions[].options` built from the actual catalog rows, distinguishing
project/chain, underlying token, `pool_meta` and a unique pool identifier; keep
each option mapped to its full returned UUID. Explain ordinary supply versus
Horizon/Umbrella or other products only where that distinction appears in the
returned metadata. Matching `USDC` or `canonical_identity.market`, first-row
order and highest APY cannot select between them. For example, replace the
placeholders below with distinct actual catalog choices:

```json
{"title":"确认收益率池","questions":[{"id":"rate_pool","question":"请选择用于历史存款收益率模拟的池。","options":["<returned row identity and pool identifier>","<another returned row identity and pool identifier>"]}]}
```

Pause for the answer and resume the SAME task/delivery with that exact row.
Reuse an already confirmed selection; an omitted answer remains unresolved.
A final prose suggestion cannot replace this interaction.

Copy the selected pool's `canonical_identity.market` from the yield_pools row
or yield_history `_envelope.pool` into BOTH draft `markets` and strategy.yml
`markets`. For the discovered Aave V3 Ethereum USDC pool this is
`AAVE_V3:ethereum-USDC`: `ethereum` is the chain and `USDC` the sole asset.
Copy `pool_id/project/chain/symbol` into rate_source (symbol becomes asset).
Keep admitted scope as protocol Aave plus asset USDC, with no base/quote.
The `pool_id` is producer-bound evidence, not a value to invent from prose: use
only the exact UUID returned by the current task's `yield_pools` or
`yield_history` envelope after the user selects/confirms that row. Host-side
binding must prove the same UUID, project, chain, symbol/asset and admitted
UTC window before staging; a matching market label alone is insufficient.
If canonical_identity is null or conflicts with admitted scope, stop and report
the identity gap. Do not invent a pair, infer ETH/WETH equivalence, or broaden
the admitted asset. Component inventory establishes capability, not pool identity.

For recovery, follow **Resume and completion boundary** in authoring-workflow.md:
delivery artifacts only locate a candidate; a successful same-task
`strategy_draft_proposal` producer receipt authorizes staged edits. Inspect
`ownership.same_task_producer` and `ownership.recoverable` before recovery.
If no draft succeeded in this task, repair `invalid_resource_shape` from exact
admitted evidence (single-asset lending uses asset, with no invented quote),
then draft once. Reconcile an uncertain operation before any new draft or edit.

Before drafting, bind the rate replay window from the admitted request. If a
relative period such as 最近30天 and explicit UTC bounds in the same admitted
request are both present, preserve the producer-admitted explicit `start_utc`/`end_utc`
pair exactly. The explicit date pair owns scope. `window_days` and
`start_utc`/`end_utc` are mutually exclusive: omit `window_days` when emitting
the explicit pair. Do not reconstruct timezone, midnight/default boundaries or
a new relative-period anchor. Copy the exact admitted pair into `strategy.md`
and `replay_inputs.json`. Historical coverage limits remain separate from scope
binding; missing observations cannot authorize changing the pair or relaxing
gap bounds.

1. Declare thresholds and accounting assumptions in candidate strategy.yml.
   If parameters are open, choose conservative research assumptions and
   disclose them in strategy.md. Resource IDs come from discovery.
2. For a NEW rate candidate, use the compact configured-assets example below
   BEFORE the generic incremental scaffold in authoring-workflow.md. Pass the
   small configuration, replay declaration and candidate tests in
   strategy_draft_proposal(files=...), with execution_mode:script; omit main.py.
   All three params objects select the tested rate entrypoint, pure policy and
   unchanged Aave recovery helper. Keep those generated assets and the returned
   proposal_id; finish documentation/review files at its canonical paths.
   For an EXISTING candidate, keep the same proposal_id and staged root. For
   an existing generic scaffold, copy templates/aave_rate_strategy.py.j2
   as main.py and templates/aave_rate_policy.py.j2 as aave_rate_policy.py;
   retain aave_supply.py in the SAME proposal via normal file-edit tools.
   Omit allow_outside_conversation: existing and new proposal-staged files
   already keep the returned canonical path without that exception.
   After the draft producer returns its admitted `start_utc` and `end_utc`,
   copy those exact strings into `replay_inputs.json` action `args.start`/`args.end`
   and the matching `strategy.md` scope statement before validation. Do not
   recalculate them from `window_days`, local time or the current clock.
3. Add focused candidate tests for entry/exit, missing/stale inputs and cash
   limits. Validate, repair reported blockers, then submit through the normal
   proposal lane; no promotion or activation.
4. Call strategy_backtest with only that proposal_id and allow_mock:false.
   Auto selects the saved rate contract; explicit engine:rates is supported.
   Read the report and receipt. Use existing producer verification/delivery;
   missing window coverage remains a missing check. Follow the workflow below
   when the calculation completed but coverage is incomplete.

## Completed rate replay with incomplete coverage

1. Read the existing persisted report_path, run_receipt and paired `metrics.json`; keep the same
   proposal_id, source revision and requested UTC window. A completed run with
   30 decisions and 1 simulated action is legitimate calculation evidence even
   when coverage_ok:false. Explain modeled coverage from `modeled_coverage`,
   excluded `gaps` and `provenance.assumptions.max_gap_seconds`.
   `observation_coverage` is raw acquisition: source-bracketed boundary support
   can cover its edge offsets, and a raw interval over 24 hours need not exceed
   the saved modeling threshold. Preserve the producer's failed/missing
   data_coverage check and report only evidenced modeled exclusions.
2. Stop producer retries for this gap: do not repeat strategy_validate or
   strategy_backtest on the same source/window to turn coverage into passed.
   Validation cannot supply missing observations. Resume replay only after new
   producer data addresses the recorded missing intervals. Keep the original
   scope and gap bounds; changing dates or tolerances cannot certify it. When
   source discovery for this gap has not yet run, load
   `Skill(skill="research", file="references/onchain-data-discovery.md")`
   and investigate the exact missing intervals before handing off. Reuse prior
   discovery. For actual reserve state, load
   `Skill(skill="research", file="references/aave-reserve-history.md")` and use
   its producer to query the recorded gap. Its annualized base APR and index
   still need compatible normalization/replay admission; they cannot silently
   replace total deposit-APY rows.
   The installed `aave.supply_apy_history` action can research official average
   supply APY for the SAME selected UUID. Inspect its schema, resolve the exact
   catalog aToken to official market/reserve addresses, then query the admitted
   UTC window with a documented rolling window. It preserves both identities
   and source hashes, but `point_in_time_ready:false` / `replay_input:false`
   mean averaging availability is unverified. Register it as research evidence,
   not replacement replay rows, a passed coverage check or a reason to rerun.
3. Register the returned report/run_receipt artifacts and record the gap in
   delivery_update limitations using the latest delivery_revision. Leave
   producer-owned checks unchanged; never send checks with status:passed or
   remove required checks. Complete evidenced stages even while coverage is
   incomplete: use existing IDs, mark preflight/replay/readback completed when
   their checks pass and readback is done, and mark coverage blocked with reason
   data_coverage_missing. Deliver remains blocked while its requirements are
   unmet. For an authoring delivery, reconcile its own stage names against the
   same receipts rather than creating a second stage plan.
   If the report predates delivery_start, start/reuse
   the current command's delivery and reconcile its existing receipts through
   delivery tools rather than rerunning producers.
4. After report readback and artifact registration, call delivery_finalize once
   with the latest revision. Preserve completion.allowed:false and the returned
   partial/incomplete status and reasons; finalization does not complete the
   requested full task. Summarize the saved artifact, actual decision/action
   counts, modeled-only scope and exact coverage gap. State the next concrete
   data required: timestamped observations for the same pool and missing UTC
   intervals, with provenance and cadence sufficient for the original gap bound.
   The full request remains outstanding pending that evidence; an unchanged
   finalization result is a handoff, not another validation/replay/finalize loop.
   Lead the final summary with the verified boundary: “候选策略和历史观测区间的
   模拟已产出；完整请求区间仍未验证。” Use the modeled gap start/end/seconds
   directly (hours = seconds / 3600), distinguish requested from observed dates, and
   preserve completion.allowed:false. Do not describe partial work as “已按要求
   完成”, infer normal provider update lag from missing observations, annualize
   the short simulation, or shorten a leading/trailing gap in prose.

## New candidate: compact configured assets

Construct this tool argument object after discovery. Replace every angle-bracket
placeholder with the exact requested/discovered value and an existing paper
account. Numeric thresholds, freshness/gap bounds, allocation, initial cash,
constant unit price, zero fees and fetch limit are illustrative research
assumptions: use requested values when specified; otherwise choose and disclose
them in strategy.md before validation. Preserve the requested chain, asset, UTC
window and no-trading restriction. Test timestamps/positions are synthetic.

```python
import json

arguments = {
    "strategy_id": "rate_candidate", "execution_mode": "script", "mode": "paper",
    "markets": ["<requested_canonical_market>"],
    "accounts": ["<existing_paper_account>"], "create_tuning": True,
    "files": {
        "strategy.yml": """id: rate_candidate
entrypoint: main.py:run
execution_mode: script
mode: paper
markets: [<requested_canonical_market>]
accounts: [<existing_paper_account>]
schedule: {type: none, enabled: false}
params:
  rate_source: {pool_id: <discovered_pool_id>, project: <catalog_project>, chain: <catalog_chain>, asset: <catalog_symbol>}
  rate_policy: {entry_apy_percent: 5, exit_apy_percent: 2, max_age_seconds: 86400, allocation_units: 100}
  rate_replay: {version: 1, dataset_id: rates, initial_cash_units: 1000, unit_price_usd: 1, fee_bps: 0, max_gap_seconds: 90000}
""",
        "replay_inputs.json": json.dumps({"version": 1, "datasets": [{
            "id": "rates", "provider": "defillama", "action": "yield_history",
            "args": {"pool_id": "<discovered_pool_id>",
                     "start": "<requested_start_utc>", "end": "<requested_end_utc>",
                     "limit": 500},
        }]}),
        "tests/test_contract.py": """from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import yaml
from aave_rate_policy import RatePolicyError, decide_aave_rate

PARAMS = yaml.safe_load((Path(__file__).parents[1] / 'strategy.yml').read_text())['params']
POLICY, SOURCE = PARAMS['rate_policy'], PARAMS['rate_source']
NOW = datetime(2040, 1, 1, tzinfo=timezone.utc)

def decide(apy, supplied=0, cash=0, **changes):
    observation = dict(SOURCE, timestamp=NOW.isoformat(), deposit_apy_percent=apy)
    observation.update(changes)
    return decide_aave_rate(policy=POLICY, source=SOURCE, observation=observation,
        position={'supplied_units': supplied, 'cash_units': cash}, as_of=NOW)

def test_entry_exit_and_cash_limits():
    cap = POLICY['allocation_units']
    entry = decide(POLICY['entry_apy_percent'], cash=cap * 2)
    assert entry['action'] == ('lend_supply' if cap else 'hold')
    assert entry['amount_units'] == cap
    assert decide(POLICY['entry_apy_percent'], cash=cap / 2)['amount_units'] == cap / 2
    assert decide(POLICY['entry_apy_percent'], cash=0)['action'] == 'hold'
    exit = decide(POLICY['exit_apy_percent'], supplied=cap + 1)
    assert exit['action'] == 'lend_withdraw' and exit['amount_units'] == cap + 1

def test_missing_and_stale_rates():
    with pytest.raises(RatePolicyError):
        decide(None)
    old = NOW - timedelta(seconds=POLICY['max_age_seconds'] + 1)
    with pytest.raises(RatePolicyError):
        decide(POLICY['entry_apy_percent'], timestamp=old.isoformat())
""",
    },
}
strategy_draft_proposal(**arguments)
```

The producer should return implementation_status:rate_policy_simulation and
candidate-local main.py, aave_rate_policy.py and aave_supply.py. This establishes
asset selection only. Finish strategy.md assumptions and the separate review
plan under the normal workflow (both schedules disabled; preserve an explicit
whole-strategy no-AI/no-review opt-out). Validate the saved candidate and tests,
then submit and replay that same proposal as above. Inspect unexpected scaffold
status or blockers in the saved candidate before continuing.

## Manifest and replay contract

Required manifest: entrypoint main.py:run, execution_mode script, mode paper,
the discovered canonical_identity.market (a chain-qualified lending asset), actual
paper account, and schedule {type:none, enabled:false}. Under params declare:

- rate_source: pool_id, project, chain, asset (exact discovered catalog strings;
  asset is the catalog symbol, not an inferred token address).
- rate_policy: entry_apy_percent, exit_apy_percent, max_age_seconds,
  allocation_units. Finite nonnegative numbers; entry must exceed exit.
  Thresholds use percent/year; allocation is an asset-unit cap per entry.
- rate_replay: version:1, dataset_id:rates, initial_cash_units (positive),
  unit_price_usd (positive constant-price assumption), fee_bps (explicit,
  0 <= value < 10000), max_gap_seconds (positive, at most 604800).
  These are exactly six fields: no extra accounting object or start_utc/end_utc; keep time bounds in backtest scope or replay_inputs.json dataset args.start/args.end without changing the admitted window.

replay_inputs.json uses the host preparation schema:

    {"version":1,"datasets":[{"id":"rates","provider":"defillama","action":"yield_history","args":{"pool_id":"<discovered>","start":"<exact requested UTC start>","end":"<exact requested UTC end>","limit":500}}]}

The host supplies one observation and current modeled position per isolated
run(ctx), owns previous-APY accrual, fees and balances, and records actual
decisions and modeled actions. It rejects unordered/null rates, identity
mismatch, invalid actions and overspend. Gaps over the configured bound are
not accrued. Leading/trailing gaps, source completeness and hashes remain
visible. No shell fetch or custom replay script is needed.

The report is rate_policy_replay, with
replay.scope:historical_rate_policy_simulation,
provenance.data_kind:historical_observations, performance_evidence:false,
and financial_execution_allowed:false. This approximation omits reserve-index
reconstruction, chain fees, depeg, latency and settlement. Report modeled
values with those limitations.

Live execution stays unavailable until strategy-owned supplied units, available
cash and reviewed pool/token/resource binding are attested. A paper account,
pool symbol or modeled position cannot provide that authority. Existing pending
actions retain their original receipt reconciliation path.

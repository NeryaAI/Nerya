# Monad testnet native Nerya strategy packages

Three installable native packages, not CLI trade reports:

- `monad_lp_range`: bounded actual-tick range → mint → watch fee growth/range → collect → exit.
- `monad_perp_sma`: actual closed hourly Perpl MON SMA6/18 crossing → gated open → software stop/opposite cross/hold expiry → reduce-only close.
- `monad_meme_momentum`: reviewed nMEME/WMON pool prices → bounded momentum buy → stop/hold → sell only the confirmed strategy credit.

Each contains `strategy.yml`, `strategy.md`, `main.py`, `monad_runtime.py`,
`limits.yml`. All shipped packages have paper mode, execution gate false and disabled schedules. Strategy code delegates funds execution to FinancialAPI; it never reads secrets or directly signs/enrolls/deposits/broadcasts.
The SDK FinancialAPI used by `ctx.financial` owns prepare/execute/reconcile; the
native StrategyRunner owns script identity, lease, run history and persistent state.

## Configure and install

From the selected worktree root, using the existing Python environment:

```bash
PYTHONPATH=. /Users/rick/Documents/Project/Nerya/agent/.venv/bin/python \
  competition/monad/strategies/configure.py \
  --deployment /Users/rick/Documents/Project/Nerya/pitch-video/monad-metropolis-20261013/v3/private/testnet.deployment.json \
  --wallet-id monad_demo_testnet --account-id monad_demo_testnet \
  --perpl-account-id perpl_testnet \
  --output competition/monad/strategies/validation/operator-candidates
```

Output must be new. Choose the actual existing isolated account/wallet IDs; no
new wallet or vault identity is invented. This binds public pool addresses, token
IDs and manifest SHA256 from the reviewed chain 10143 deployment, with no mainnet
contract defaults. `installation-review.json` contains merge-by-ID account rows,
wallet_permissions, account_limits, exact finite grant policy drafts and activation
prerequisites. Merge reviewed values with the existing isolated config; do not
replace its wallet/provider/Perpl sections. Operator manages signer references.

Adding `--stage-workspace /absolute/isolated/workspace` stages canonical
`strategy_package_proposal` records under `evolution/proposals/`; it does not copy
into active strategies, promote, approve grants or trade. Review/validate/promote
through native Nerya proposal controls. Package candidates alone are not installed
strategies. Protected limits.yml is excluded from proposal material and applied by the operator before grant review. Stage and promote one package at a time after config merges settle; other workspace changes can invalidate a reviewed candidate base.

Perpl parent reports account 1227 funded with 100 test AUSD. Revalidate through
the enrolled testnet transport before entry; this is not hardcoded into the package.
`user:perpl:perpl` must exist with configured testnet account and operator scopes.

For bounded autonomous execution, operator must review the installed manifest,
set `mode: live`, `params.execution_enabled: true` and isolated runtime funds/live gates, keep schedules disabled for manual demo ticks, then compute the *final installed*
`strategy_security_revision(config, strategy_id)`. Issue a grant using
`FinancialStore.create_grant(... task_kind='strategy_script', task_id=id,
subject_actor_id='strategy:'+id, security_revision=revision, policy=reviewed_policy)`
and approve it as operator with the current revision. Config/code/limits changes
invalidate grants. No wildcard resources or unlimited allowances. `limits.yml`
helps RiskGate; financial account_limits and grant budgets remain mandatory.

## Fresh data and native runner integration

Strategy packages are SDK-only and pass the static scan. `readers.py` is a
separate operator-side adapter, not installed executable strategy code. Wire it
into the actual native runner's supported `news_fetchers` injection:

```python
# Run from repo root; add competition/monad/strategies to operator PYTHONPATH.
from readers import fetchers
from nerya.core import yaml_io
from nerya.strategies.runner import StrategyRunner
manifests = [yaml_io.load(config.paths.strategy(sid) / 'strategy.yml') for sid in ids]
runner = StrategyRunner(config, news_fetchers=fetchers(manifests, deployment_path))
record = runner.run_tick('monad_lp_range')
```

Public adapters allow only testnet Perpl public GETs and explicit read-only RPC
methods, validate chain 10143 and block freshness, pool/token bytecode/identity,
and fetch actual candles/slot0 each tick. Pool adapter computes range-aware mint
minimums using the observed sqrt price and exact token decimals. Install data
injection into the scheduler's actual runner; default runner has no automatic
reader for these custom sources. If the feed is absent/stale, entry fails closed.
The default UI runner also resolves these sources when plugins.monad_strategy_data has enabled=true, the exact workspace path and reviewed deployment_path. This opt-in competition source integration uses the same readers. Restart the isolated API after source/config changes; browser reload alone does not adopt Python module changes.

For LP parameters, all human-unit quantities are **decimal strings**:
`amount0_max`, `amount1_max`, `amount0_min`, `amount1_min`, `collect0_max`,
`collect1_max`; tick bounds, minimum_liquidity, liquidity and token_id are integers.
Do not send floats: the component schema rejects them.

## Recovery and explicit boundaries

`state/state.json::cycle` binds all authored parameters, cycle ID, action IDs and
confirmed receipts. Intent is saved before prepare. Uncertain submission only
reconciles its existing action. A crash before action ID checkpoint blocks and
requires operator journal lookup; no guessed resubmission. Rejected actions do
not auto-retry. Never erase cycle state to make an exposure disappear. A new
cycle needs reconciled/closed exposure and a newly reviewed cycle ID/package.

Current gateway pins each unsubmitted action to its producer tick. Later ticks
never impersonate that lease. If a finite approval prerequisite or manual approval
is requested, pause and complete it through the original native approval/action
path. Approve only the bounded required_actions; refresh/review the parent quote
under the supported original context. If its lease or 30-second quote expired,
operator recovery is required. This worker does not change gateway context rules.
An approved revision-bound grant plus already finite allowances permits normal
SDK execution in the producing tick. Subsequent ticks reconcile confirmed receipts.

LP original bootstrap NFTs are never used; the minted receipt selects the NFT and
ProtocolBook enforces sole strategy ownership. Perpl requires an isolated empty
account and closes only the observed position matching the confirmed fill. Meme
spends only confirmed buy credits. External changes block guessed ownership.

Stops and max_hold are software checks requiring fresh active ticks and approval;
they are target exit times, not guaranteed venue stop orders or maximum loss.
LP bounded collection can leave owed amounts: inspect post-exit inventory and
resolve residual fees before describing it as fully settled.

Test USD fields use the deployment's **fixed test units, with no monetary value**.
nUSDC/nMEME are test assets, the test price feeds are not market oracles, and
Perpl test AUSD is test margin. Do not claim real-money profit. Actual receipt
quantities, fees and gas may be reported separately with their testnet label.

## Checks and replay

```bash
/Users/rick/Documents/Project/Nerya/agent/.venv/bin/python \
  competition/monad/strategies/tests/test_packages.py
```

Checks use controlled inputs and mock transports, temporary installed native
packages, real SDK/gateway/store, revision-bound grant and persistent runner
state. They prove code/identity/recovery behavior, not actual testnet fills.

```bash
PYTHONPATH=. /Users/rick/Documents/Project/Nerya/agent/.venv/bin/python \
  competition/monad/strategies/replay.py \
  --output competition/monad/strategies/validation/perpl-public-replay.json
```

Replay pulls actual public hourly testnet candles and uses the package's exact
SMA function, records every observed crossing and latest signal, no PnL/trades.
No crossing is a valid outcome. Signal replay is separate from the native execution receipts in ../acceptance.json.

Routine smoke: operator manually triggers native ticks on reviewed testnet config
and collects gateway action, chain receipt, inventory/position, fee and exit proof.
A forced manual open/close or operator-induced pool price movement is controlled
execution evidence, not autonomous strategy performance. Autonomous acceptance
needs registered fresh feeds, scheduled ticks, a naturally qualifying signal,
valid grants and complete confirmed entry/exit, retaining all no-signal outcomes.

## Next integration

1. Promote the reviewed configured packages through native proposals.
2. Merge account/wallet permissions and finite budgets from installation-review.
3. Register public readers in the isolated scheduler runner; revalidate account 1227.
4. Finalize testnet-only live manifest and grants, then validate fresh approvals.
5. Run native ticks and retain entry/fee/exit receipts and ownership inventory.
6. Restart the parent's isolated services after gateway changes are accepted,
   then capture the installed strategy and real outcome; process versions can
   remain stale until restarted. No restart is performed by this worker.

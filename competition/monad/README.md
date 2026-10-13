# Nerya · Monad testnet strategies

Nerya is a trading agent workspace with native strategy packages, operator
approvals, bounded financial grants and recoverable execution. This contribution
adds Monad testnet execution for concentrated liquidity, Perpl MON perpetuals
and an explicitly identified meme test token. The demo target is **Monad testnet,
chain 10143**. It makes no real-money profit claim.

## Recorded execution

| Package | Observed acceptance | What remains distinct |
|---|---|---|
| `monad_lp_range` | Native UI mint, fee collection and full removal of strategy-owned NFT 4 | Collection required explicit operator recovery; all ticks were manually triggered |
| `monad_perp_sma` | Actual public hourly SMA6/18 short signal; confirmed 100 MON open and reduce-only close | Failed pre-submission open was recovered by the operator; unattended operation is not established |
| `monad_meme_momentum` | Native momentum buy and full sell confirmed, spending 0.001 WMON and selling the exact 0.977385467585813649 nMEME credit | Parent created an onchain test stimulus; this is controlled signal acceptance, not organic market discovery |

[Public acceptance](acceptance.json) records transaction hashes, canonical blocks,
SDK action identities and quantities. All three native cycles are complete. Ten
distinct native/operator/stimulus receipts were independently checked against the
public testnet RPC. The exited
LP NFT has zero liquidity and zero recorded tokens owed. LP collection returned
zero fees in this cycle; the demo does not claim fee income. The native Meme exit
returned 0.000994028560517547 WMON from a 0.001 WMON entry, a decrease of
0.000005971439482453 WMON before gas. Native buy/sell gas totalled 0.041350494
test MON; neither token recovery nor the test stimulus is presented as profit.

The LP pool is a canonical UniV3 test deployment, **not an official venue**. nUSDC
and nMEME are test assets with no monetary value. USD-labelled runtime fields use
fixed test units; test feeds are not market-price oracles. Perpl test AUSD is test
margin. Actual token flows, gas and venue fees are evidence of execution, not profit.

## Native packages and controls

The V4 recording separately demonstrates **new strategy creation and review**:
one natural-language sentence starts native agent authoring of
`mon_perpl_trend60`, followed by disclosed operator guidance on SDK calls, data
bindings and recovery behavior. The agent updates proposal `prp_4c350ba4db7f`;
the operator inspects it in the native review UI. An independent offline run
passed all 16 candidate tests, and native validation reported no blockers.

**This candidate remains pending review, not installed and not traded.**
Historical validation returned `NoHistoricalData`, and the standard Approve Add
attempt was blocked. No historical waiver or approval bypass was used. Paper
mode, execution and direct orders remain disabled, with both trading and tuning
schedules disabled. Candidate close-position typing/inventory binding, terminal
failure classification and recovery-before-gate behavior still have known
execution limitations. Passing mock-based tests does not establish live readiness.

[Authoring acceptance](authoring-acceptance.json) records the audited source
hashes, test result, guidance, database-recovery history boundary and review
outcome without publishing private candidate files. This creation evidence is
separate from the three previously completed testnet cycles below; those receipts
do not prove execution of the new candidate.

Each package in [strategies/](strategies/README.md) contains `strategy.yml`,
`strategy.md`, `main.py`, an SDK-only action journal and operator-reviewed limits.
Shipped templates default to paper mode, disabled execution and disabled schedules.
Configured demo packages are promoted through native proposals. `limits.yml` is
protected operator configuration and is applied separately before grant review.

- LP: observe the real tick, align a bounded range, mint within token caps, watch
  range/fee growth, collect and remove only the confirmed strategy NFT.
- Perp: use contiguous closed public candles, compute SMA6/18 crossing, enter one
  isolated position, then close its actual filled quantity. Software stop/hold
  checks require fresh active ticks; they are not venue stop orders.
- Meme: compare real nMEME/WMON pool prices across distinct blocks, enter only
  within the momentum/sample bounds, then sell exactly the confirmed buy credit.

All writes pass through native FinancialAPI prepare/execute/reconcile. Runtime
identity is `strategy:<id>` with a producer lease, package security revision,
finite grant and account/wallet permissions. Uncertain submissions reconcile the
original action; an unsubmitted plan may be discarded and replaced only through
an explicit audited operator recovery. Old producer leases are never reopened.

The isolated demo runs were launched through **strategy detail → Overview →
Run tick**. Native run history displays action IDs, confirmed receipts, NFT IDs,
blocks and transaction hashes. Default public readers are explicitly bound to the
selected workspace and reviewed deployment; stale/wrong-chain data cannot produce
a trade. The temporary competition data-source integration is opt-in.

## Contribution map

| Source | Purpose |
|---|---|
| [strategies/](strategies/README.md) | Three native packages, read-only data adapters, proposal/configuration helpers and focused checks |
| [perpl/](perpl/README.md) | Workspace-loaded perpetual component, public observations and authenticated execution/recovery |
| [testnet/](testnet/README.md) | Pinned canonical UniV3 test deployment and explicit test assets |
| [web/](web/README.md) | Auxiliary public market/scenario explorer and presentation pages |
| [skill/SKILL.md](skill/SKILL.md) | Agent playbook for public Monad/Perpl research |

**Nerya predates this event.** The agent runtime, dashboard, proposal system, SDK
and approval framework are existing foundations. This event work adds the Monad
execution integration, native testnet packages, data binding and acceptance evidence.

The auxiliary explorer is available at
[the public preview](https://nerya-monad-metropolis.vercel.app). Source includes
[demo.html](web/demo.html) and [pitch.html](web/pitch.html). These pages and older
media may describe an earlier read-only/mainnet preview. They do not independently
prove the latest testnet trades; use the dated acceptance JSON and native recordings
for that evidence. Final V4 recording: [technical demo](https://youtu.be/q2hzmdbyRr8)
(175.061333 seconds), showing one-sentence authoring, disclosed manual guidance
and recovery, the candidate held for review, and the prior three testnet execution
routes. The new candidate was not installed, activated or traded.
[Pitch](https://youtu.be/iowgZhj1GvY) remains published.

## Focused checks

From the repository root, in the project's Python environment:

```bash
python -m pytest -q tests/test_monad_perpl.py tests/test_monad_pancake_swap.py tests/test_monad_log_pagination.py tests/test_financial_config_fence.py tests/test_uniswap_execution.py tests/test_financial_authorizations.py tests/test_financial_transport_paths.py
python competition/monad/strategies/tests/test_packages.py
python competition/monad/strategies/tests/test_recovery.py
node --test dashboard/tests/script-financial-receipt.test.cjs
```

Tests use controlled inputs/fake transports. The public acceptance JSON separately
records real testnet receipts. The signal replay helper reads actual public candles
and reports crossings without simulated PnL or trade claims.

For the auxiliary explorer, use `node competition/monad/test.mjs`, then the
commands in `web/README.md`. Its hypothetical risk calculator remains separate
from the runtime Risk Gate and actual execution.

## Public package boundary

Publish source, documentation, tests, `acceptance.json` and the sanitized
`authoring-acceptance.json` process record. The new pending-review candidate is
not distributed as a ready trading template. Exclude workspaces,
vaults, signer files, journals, enrollment outputs, private config, prompts, raw
recordings and validation logs. Perpl/testnet/strategies `validation/`, testnet
`build/` and journals/config/deployment outputs, web `dist/`, `.vercel/` and `media/`
are ignored. The acceptance file contains only selected public chain facts and SDK action identities; it contains no wallet key, API token or signer reference.

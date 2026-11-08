---
name: financial_ops
description: Discover DEX/protocol financial capabilities and read-only on-chain 链上 data for strategy authoring and backtests, including Jupiter Solana swaps, Uniswap LP and DeFi history search. Prepare, execute and reconcile finite-authorized trading, swaps, transfers, withdrawals, contract approvals and cross-chain funds operations.
---

# Financial operations

Call financial_components to discover installed builtin and workspace component
IDs, immutable revisions, actions and parameter schemas. For paper or backtest
authoring, pass `mode="paper"` or `mode="backtest"` plus the requested
protocol/market. That route is a bounded read-only inventory for strategy
routing; it does not grant `read:funds`, approvals, signers or execution.
Select the component matching the requested protocol and read its contract
before authoring requests.
For paper/backtest account binding, reuse `account_list` or call it without a
venue filter, then select an active compatible row with `mode:"paper"`. Paper
is a mode; protocol/component IDs are not account-provider IDs. Use the returned
account ID without creating or changing an account. For DeFi account matching
and unresolved pool selection, follow `references/mainstream-defi.md` (Aave).

`connector_list` lists exchange providers; wallet/onchainos `data_api` lists
read-only data actions, not financial execution actions. A component's `swap`
action does not create a data_api route or supply historical prices.
Empty results there do not establish absent financial components
or unconfigured wallets. For Jupiter, read `references/mainstream-defi.md`: the
swap implementation belongs to `builtin:trading`, not `builtin:jupiter`.
Run financial_readiness when inspecting local builtin execution prerequisites
with `read:funds`; paper/backtest discovery does not require that permission.
Its families list excludes workspace components and cannot establish their
absence. Separate installed actions, live configuration and historical data
coverage. Neither discovery tool certifies replay or grants execution permission.

If the paper/backtest inventory returns `capability_gap`, first load the
research on-chain discovery workflow and search for a matching read-only
historical source. Treat the installed component inventory as a local
capability result, not a statement that the requested chain data does not
exist. Preserve the exact protocol, market and missing action in the candidate
receipt. Do not create a
CEX proxy, silently switch to a related market, or call financial_prepare to
make the gap disappear. A Uniswap component exposing only LP actions is not a
paper/backtest swap component.

## Historical discovery stop

For a missing on-chain/history input or unavailable source, first load
`Skill(skill="research", file="references/onchain-data-discovery.md")` and
actively search primary sources and continue through compatible read-only
producers; an empty installed catalog is not the end of discovery. Use matching
installed read-only schemas to inspect chain data, or
identify the required adapter through the existing proposal lane. For creation
plus backtest, retain any existing disabled candidate unchanged if this bounded
source discovery still leaves a historical capability gap, record
the blocked replay and end with the missing capability and original scope.
A missing signer or API key blocks live execution, not authoring; an empty
local cache does not establish absent downloadable history.
If a producer returns a structured, scope-preserving normalization hint for the
requested historical query, apply only that hint and retry the same read-only
query once. A second rejection is the producer's current limitation; preserve
the receipt instead of repeating the call or changing venue, asset, protocol,
window, or execution mode.

For LP/DeFi, lending, short Aave history or data_api schema failures, prediction
settlement and durable LP rebalancing, read references/mainstream-defi.md.
Workspace-specific protocols use their installed component reference and
parameter schema.

Use the native financial_prepare / financial_execute / financial_get / financial_reconcile tools. Capability and account permissions remain producer-owned. Loading this Skill grants no funds permissions.

Read `references/full-playbook.md` for bridge prerequisites, refreshed quotes, partial receipts, stopped tasks and unsupported-provider recovery.

1. Identify the current task, configured account or wallet, asset, chain and explicit destination. Never infer an arbitrary destination or spender.
2. Prepare one structured action. Review the normalized amount, live quote, fees, minimum output, expiry and actual provider support.
3. Execute only that action_id and quote_hash. A matching finite grant permits execution within its exact resources and remaining quota; otherwise stop for operator approval. The Agent cannot approve, widen or activate grants.
4. Preserve the returned action_id and transaction/order identity. Submitted, unconfirmed and partially completed actions are not completed transfers.
5. Reconcile by querying the existing identity. Never prepare or broadcast another payment to recover from a lost response.
6. Report actual changes and unresolved steps with receipt evidence. For cross-chain routes distinguish source confirmation and destination confirmation. Expired application authorization does not revoke on-chain allowances.

Finite contract approvals require a reviewed spender and exact amount. Infinite allowances, unverified calldata, unsupported chains, absent signing/query capabilities and stale valuations must fail closed.

Agent-authored strategy, script and trigger changes remain proposals. Financial authorization does not authorize edits to task definitions or risk policies.

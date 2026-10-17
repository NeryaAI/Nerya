---
name: financial_ops
description: Prepare, execute and reconcile finite-authorized trading, swaps, transfers, withdrawals, finite contract approvals and cross-chain funds operations.
---

# Financial operations

Call financial_components to discover installed builtin and workspace component
IDs, immutable revisions, actions and parameter schemas. For paper or backtest
authoring, pass `mode="paper"` or `mode="backtest"` plus the requested
protocol/market. That route is a bounded read-only inventory for strategy
routing; it does not grant `read:funds`, approvals, signers or execution.
Select the component matching the requested protocol and read its contract
before authoring requests.
Run financial_readiness for local builtin configuration prerequisites; its
families list excludes workspace components and cannot establish their absence.
Neither tool certifies live execution or grants permission.

If the paper/backtest inventory returns `capability_gap`, preserve the exact
protocol, market and missing action in the candidate receipt. Do not create a
CEX proxy, silently switch to a related market, or call financial_prepare to
make the gap disappear. A Uniswap component exposing only LP actions is not a
paper/backtest swap component.

For LP/DeFi, lending, prediction settlement and durable LP rebalancing, read
references/mainstream-defi.md. Workspace-specific protocols use their installed
component reference and parameter schema.

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

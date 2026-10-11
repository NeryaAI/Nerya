---
name: financial_ops
description: Prepare, execute and reconcile finite-authorized trading, swaps, transfers, withdrawals, finite contract approvals and cross-chain funds operations.
---

# Financial operations

Run financial_readiness before claiming execution is available. For Jupiter,
Uniswap/Pancake LP, Aave, prediction settlement and durable LP rebalancing,
read `references/mainstream-defi.md`. Prefer these mainstream routes for new
strategies; Byreal is a compatibility binding, not the default.

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

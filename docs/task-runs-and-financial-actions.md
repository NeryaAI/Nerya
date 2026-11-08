# Task Runs and managed financial actions

## Ownership

Task definitions remain in strategy packages and schedule configuration. SQLite owns Run identities, immutable accepted input, command links, financial grants/actions/usage, delivery jobs and audit lineage. CommandStore remains the owner of execution state. Business completion comes from financial receipts and verified required outputs. Delivery state is independent.

The runtime uses one stable task conversation and separate Run identities. Approval/input continuations and follow-up commands remain linked to the original Run. Model, permission ceiling and cumulative execution budget are frozen; current narrowing, global disable, version change and grant revocation fence new effects.

## Ingress and recovery

- `POST /agent/runs` accepts a task reference, expected definition revision and client request ID. It returns an admission receipt, not business success.
- `GET /agent/runs` provides bounded keyset pages; `GET /agent/runs/{id}` returns the complete projection. Events span linked commands.
- `POST /agent/runs/{id}/control` checks owner, execution scope, revision and request fingerprint. Stop, resume, reconciliation and a new retry Run are distinct operations.
- Scheduled occurrences use theoretical times. Manual Runs do not advance the clock. Busy strategy signals coalesce; ordinary automation skips. Pending financial receipts keep effects from accumulating.
- The maintenance worker resumes only valid, never-started system work. Ambiguous execution never replays. It independently reconciles funds and sends persisted delivery jobs. Unknown delivery does not automatically resend.
- Archived schedules preserve history. Three definite failures pause a plan; explicit resume clears its failure hold. User queue holds remain user-owned.

## Financial authorization

Financial grants bind task security revision, concrete resources and resource fingerprints, an approver, finite time, single/rolling/total quantities, fees, destinations, memo/tag, spender, routes, trade actions and leverage. A grant does not enable accounts, API withdrawals, global live mode or chain permissions.

`approve:trade` covers trade/swap, `approve:funds` covers other funds operations. Mixed grants need both domains. Ordinary tool approval does not authorize funds. Unknown financial approval kinds fail closed. Paper and Plan cannot move real funds.

Financial actions normalize and prepare an immutable quote, reserve exact Decimal quantities atomically, persist identity before submission, and independently confirm effects. Submitted and unknown actions retain permission usage. Source-confirmed bridging no longer holds the same principal twice against live balance. Definitive fills update usage from the fill ledger. Principal and finite Allowance exposure are distinct metrics; fees are accounted separately.

## Bridge prerequisites and changes

A bridge needing finite Allowance returns `prerequisite_required` with a fixed child request. `financial_prepare` accepts `parent_action_id` only when that child exactly matches the parent's asset, amount, chain and spender. Confirm the child, then call `financial_refresh` with revision to fetch a fresh route. Old quotes remain in audit history and old approval cannot approve changed calldata. Already submitted plans cannot refresh or discard. Recovery that changes the operation requires a new action/authorization.

LI.FI routes require explicit reviewed bridges, routers and ABI selector/accessor configuration. The current decoder supports reviewed ERC-20 routes and Uniswap V2 exact-token source/destination swaps. Unknown nested calls, native bridge paths, opaque destination execution and unsupported chains are rejected. Provider `DONE` alone does not prove destination credit: the target chain must show the recipient/asset/minimum credit with sufficient confirmations.

## Capability and validation boundaries

Adapters use CCXT for CEX transfer/withdraw, owned EVM/Solana signers for transfers/finite approvals, the existing swap/trading execution gates, and the reviewed LI.FI adapter. Venue write/query capability, account permissions, wallet signing/RPC configuration, valuation, precision, network/memo and receipt support are checked. Unsupported CLI/custodial wallets are never substituted with another signer.

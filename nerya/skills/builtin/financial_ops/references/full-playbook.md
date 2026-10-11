# Managed funds workflow

Prepare a concrete resource, asset, amount, chain and destination through `financial_prepare`. Retain its `action_id`, `quote_hash`, expiry and revision. Read the quote and effective limits before `financial_execute`. A tool's general auto approval never replaces funds authorization.

## Missing authorization

Return the domain approval to the operator. Never activate a grant, manufacture approval, change account permissions or change live mode. After approval use the original action identity. After rejection or expiry do not silently reprepare the same payment.

## Bridge prerequisites

When execute returns `prerequisite_required`, prepare the exact `required_action` using the original `parent_action_id`. Do not change wallet, asset, quantity, chain or spender. Execute that finite Allowance child within its own valid authorization, then reconcile the child until confirmed. The parent and child remain in one lineage; Allowance exposure is separate from principal debit.

Call `financial_refresh` on the unsubmitted parent with its current revision after prerequisites confirm. Review the new quote/hash, minimum output, fees and route. Changed quotes invalidate prior action approval. Finite grants still need to match current resources, version, quantities and remaining quota. A different spender or route outside the reviewed scope requires a new reviewed action, not an unrestricted retry.

## Submission uncertainty

Submitted/confirming/unconfirmed means retain the original order ID, request ID, signature or transaction hash and call `financial_reconcile`. Never broadcast again merely because a response was lost. Confirmed source debit with pending target credit is still cross-chain work in progress. Model completion does not prove destination credit.

`financial_discard` only abandons an unsubmitted plan and requires its current revision. It cannot cancel an already broadcast operation or revoke an on-chain Allowance. Revocation is a separately prepared, authorized action. Stop only prevents future work; already broadcast transactions continue reconciliation.

## Completion evidence

Report actual fills, asset credit, confirmations and actual fee evidence from structured receipts. Distinguish no action, submitted, partial, confirmed, rejected and unknown. A provider status, quote or model statement alone does not establish receipt of funds. Preserve unresolved steps in the result.

Unsupported chain/signing/status capability, unverified calldata, missing independent valuation, precision/network/memo mismatch, stale quote, changed task/resource fingerprint, paper/Plan mode, revoked grant or a disabled account must stop before submission. Show the concrete reason and retain any existing action identity.

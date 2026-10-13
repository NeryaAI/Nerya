# Monad Perpl transport and MON strategy replay

This package implements official Perpl REST order submission, cancellation,
Ed25519 authentication, authenticated account observations and recovery. Both
Monad mainnet (143) and Monad testnet (10143) are supported. **Testnet is the demo
target.** Installing or importing this package performs no enrollment, faucet
request, token approval, account creation, deposit, withdrawal or order.

## Integration status and installation

The transport implements quote, validate, execute, status, account_state and cancel.
The declared TradingComponentSpec has actions=(trade), markets=(linear), and
account_state/cancel capabilities. Intended installed identity: user:perpl:perpl.

The parent registry now accepts the explicit trade_transport=onchain_perpetual
capability limited to trade/linear. This package declares it and automatic
workspace loading is covered by a focused test. Ordinary CEX workspace trade
components remain blocked by the parent guard. Live runtime installation still
requires the operator approval lane; no running runtime was modified here.

Offline lifecycle tests inject the transport into FinancialGateway(adapters=) to
exercise actual approval, capital reservation, signing, uncertain submission and
reconciliation using fake HTTP. A separate test verifies automatic registry load.
These are not authenticated testnet fills. Do not expose execute as a direct tool.

After operator review, stage/copy this package into workspace/plugins/perpl through
the normal proposal/approval lane. trading.py belongs at the package root. It uses
the existing cryptography dependency and Python >=3.12 runtime. No parent source
edits, native CEX connector or new signing service are included.

Store an ALREADY ENROLLED opaque API token and 32-byte Ed25519 seed in SecretVault.
Never put them in YAML, CLI arguments or recordings. Vault metadata must explicitly
include perpl:read; order keys also require perpl:trade. Perpl server API scope,
vault scope and operator approval are distinct boundaries.

Example non-secret workspace configuration:

    plugins:
      enabled: true
      perpl:
        accounts:
          perpl-demo:
            chain_id: 10143
            api_url: https://testnet.perpl.xyz/api
            rpc_url: https://testnet-rpc.monad.xyz
            wallet_address: '0x...actual wallet address...'
            exchange_account_id: 123 # actual account ID, not invented
            instance_id: 12 # discover current context
            api_key_ref: vault://perpl-demo-token
            private_key_ref: vault://perpl-demo-ed25519
            public_key_hex: '...64 hexadecimal characters...'
            approved_scopes: [perpl:read, perpl:trade]

API/RPC URLs are paired by configured chain ID, and mismatch is rejected.
Each quote verifies Perpl context chain ID against RPC eth_chainId. Markets, fee
tiers, leverage bounds, lot/tick decimals and instance identity are read live.
Mainnet currently has MON ID 10 / instance 1 / price decimals 6; testnet has MON
ID 64 / instance 12 / price decimals 5. These are observations, not routing constants.
Both networks settle in AUSD (6 decimals). Configure the account profile in
accounts/accounts.yml with venue perpl, kind dex, base_currency AUSD, active status,
live/canary mode, live_trading_enabled and explicit read/place/cancel permissions.
This is configuration documentation; this package never enables live flags itself.

Request shape:

    {
      "kind": "trade", "component_id": "user:perpl:perpl",
      "chain": "monad-testnet", "account_id": "perpl-demo",
      "market": "PERPL:MON/AUSD", "parameters": {"slippage_bps": 50},
      "plan": {
        "plan_id": "operator-plan-1", "strategy_id": "mon-trend",
        "account_id": "perpl-demo", "market": "PERPL:MON/AUSD",
        "action": "open_position", "side": "long",
        "sizing": {"method": "fixed_base", "fixed_base": 100},
        "entry": {"order_type": "market", "time_in_force": "ioc"},
        "meta": {"leverage": 1}
      }
    }

Mainnet uses chain monad. For reduce/close, use reduce_position/close_position,
retain the existing direction as side, and set parameters.reduce_only=true.
Optional parameters.position_id requires the actual position identity. Closing
maps to CloseLong (3) or CloseShort (4), and cannot exceed the observed position.
Cancellation uses plan.action=cancel_executor and parameters operation=cancel,
order_id=<actual order ID>. It validates owned account/market/order and sends
Cancel (5). cancel() still requires the reserved gateway action and approval.
Current parent has no native component cancellation API; explicit gateway action
approval is required, and generic grants currently omit cancel_executor.

## Authorization and lifecycle

The real transport invokes existing RiskGate and fails closed on missing strategy,
account snapshots, instrument provider metadata or risk rejection. Escalation is
rejected by this transport until resolved. Parent must provide actual snapshots,
provider metadata and ledger/tracker ingestion. The venue transport does not own
those aggregate risk projections.

Writes require an active, reserved runtime.financial_action_id checked by the
existing check_trade_permit. Account must be real-money-enabled and permitted,
global live trading must be enabled, kill switch disabled, immutable quote/current
request/key identity unchanged, and scopes operator-approved. Testnet uses the
same gates; it does not silently relax them.

REST signatures follow the exact official six-line canonical string: chain ID,
method, target excluding /api base prefix including query, timestamp, fresh nonce
and SHA256 of the transmitted bytes. Private seed must match configured public key.
Redirects are disabled. Plaintext secrets and venue error bodies are never returned.

Decimal/uint64 scaling rejects off-tick limit price or off-lot base size. Fixed USD
sizing rounds down using conservative price/slippage. Market uses price 0 and IOC
or FOK. Close/reduce uses official closing types, not an invented reduce_only wire
flag. Quotes budget collateral, taker fee and recycle fee, and use authenticated
balance/locked balance, fee tier, forwarding status and positions.

A SQLite counter in workspace/state/perpl-requests.sqlite serializes all local
processes for chain/account: rq=max(local,account.lfr)+1. Network submission identity
is persisted and passed to gateway BEFORE broadcast. Existing action IDs are never
sent again, including after crash/timeout. Coordinate independent external clients;
sr=32 is not automatically retried with a new identity.

HTTP 200/code 0 means admission only. Every batch per-order status is checked.
Unknown replies/timeouts keep identity and reservation. Recovery only reads the
original rq from open orders and paginated history (20-page bound); absent evidence
is needs_recovery. A filled receipt needs actual filled size, weighted price, fee,
order transaction hash and successful Monad RPC transaction receipt. It never
fabricates fills. Account/key changes block recovery to avoid wrong-owner reads.
Frozen or forwarding-off accounts can still be read for recovery.

Remaining limits: no WebSocket streaming,
tracker/portfolio ingestion, protection triggers, NAV/stop/volatility sizing,
builder-bound key fees, direct-contract orders, enrollment, scheduler installation
or authenticated testnet execution evidence. Recycle fee is conservative quote
budget; final actual fee is taken from observed order accounting.

## Public closed-candle strategy replay

    node competition/monad/perpl/backtest.mjs /absolute/path/to/replay

Default network is TESTNET 10143. For explicit mainnet public read/replay, pass a
third argument containing JSON with chainId 143. Mainnet reuses existing core.mjs;
public.mjs provides the equivalent closed-candle contract for official testnet.

The deterministic hourly MON SMA(6)/SMA(18) long/flat strategy uses only previous
closed candles, hypothetical next-open fills, integer MON lots and a default $300
notional cap. It closes at final closed candle for cost accounting. Outputs:
snapshot.json, backtest.json, equity.json/csv, trades.json/csv, orders.json.
Every result labels chain, sample, cost assumptions and historical replay mode.
Funding is EXCLUDED, alongside gas, liquidation and historical liquidity. Current
published fee rates are assumptions, not historical fee evidence. The 72-hour sample
is not profitability proof or actual execution. Testnet replay uses testnet candles.

## Testnet setup facts (read-only verification)

Official context currently reports Exchange
0x1964C32f0bE608E7D29302AFF5E61268E72080cc, aUSD
0xa9012a055bd4e0eDfF8Ce09f960291C09D5322dC, and minimum opening deposit 100 aUSD.
Fetch the minimum again before setup. Official account setup requires token
allowance, createAccount(uint256), then allowOrderForwarding(bool)=true. A fresh
account starts with forwarding DISABLED. Enrollment alone does not create/fund it.
Testnet API-key UI: https://testnet.perpl.xyz/apikeys .
The reviewed official API documents establish those steps but do not establish a
supported collateral faucet/mint endpoint. Parent must verify the testnet UI/faucet;
do not invent a faucet endpoint or accidentally use mainnet collateral/funding.

## Verification and sources

    /path/to/existing/python3.12 -m pytest tests/test_monad_perpl.py -q

Tests verify official canonical/scaling examples, exact signed batch payloads,
per-order rejections despite 200, gateway approval/reservation, uncertain recovery
without resend, real accounting, cancel/reduce-only, identity, both network signatures,
dynamic testnet MON routing, URL/API/RPC mismatch, actual RiskGate fail-closed,
automatic workspace loading and replay cost reconciliation/no future signals/closed candles.
No test performs external writes.

Official references:
- https://docs.perpl.xyz/resources/for-developers/api/rest
- https://docs.perpl.xyz/resources/for-developers/api/authentication
- https://docs.perpl.xyz/resources/for-developers/networks-and-configuration
- https://github.com/PerplFoundation/api-docs/blob/main/types.md
- https://github.com/PerplFoundation/api-docs/blob/main/README.md

## Explicit parent-called enrollment helper

setup.py::enroll_testnet accepts an existing SecretVault seed reference, an external
wallet_sign(SignableMessage) callback, exact expected owner address, scope/expiry and
operator_authorized=True. It requests the official challenge, validates chain10143,
official zero-address authentication domain, owner/public-key/scope/expiry/label/origin, prohibits delegated or
builder permissions, verifies the recovered wallet signer, creates Ed25519 proof of
possession and stores the returned opaque token directly in SecretVault. It never
reads the parent's wallet-key file or funds an account. There is no automatic retry.
The helper fails closed on an unrecognized EIP-712 schema; enrollment-schema.json records the public challenge schema actually observed from the
official testnet endpoint; timestamp/public-key/label fields are illustrative only. Do not loosen validation
or guess the domain if it differs. Enrollment result is not a trading-account receipt.
No verified collateral faucet route is claimed by this helper.

Parent operator runner:

    python -m competition.monad.perpl.runner account --workspace <workspace> --actor <operator> --request <request.json>
    python -m competition.monad.perpl.runner prepare --workspace <workspace> --actor <operator> --request <request.json> --action-key <unique key>
    python -m competition.monad.perpl.runner execute --workspace <workspace> --actor <operator> --action-id <prepared ID> --quote-hash <hash>
    python -m competition.monad.perpl.runner status --workspace <workspace> --actor <operator> --action-id <prepared ID>

Execute uses normal gateway approval; it does not auto-approve. Install the approved
plugin into the selected workspace first, use actual account/strategy/provider
configuration and existing runtime snapshot/evidence contracts. Runner is intended
for the authorized parent operator process; agent contexts must use the canonical
SDK rather than selecting an arbitrary actor. No secrets are read from CLI files.

## Enrollment 404 diagnosis and current setup order

The live frontend API-key chunk apikeys-xDOqH7w_.js uses the SAME
/api/v1/api-key/payload and /api/v1/api-key/enroll routes and SAME JSON payload as
this package. Official integrations.md defines enroll 404 as Target profile not
found. The missing prerequisite in the parent bootstrap is the wallet login profile:
/v1/auth/payload then personal_sign of the exact returned message then
/v1/auth/connect. It is distinct from on-chain createAccount.

setup.py::connect_testnet_profile is now an explicit parent-called helper accepting
wallet_sign(SignableMessage), operator_authorized=True and
terms_acknowledged=True. It checks the exact observed official sign-in domain,
wallet, chain10143, nonce, issuance time, protocol terms statement and resources,
verifies recovered signer, echoes challenge unchanged to /auth/connect, and returns
only non-secret success metadata. It does not return the session nonce. It must NOT
be invoked implicitly after an enrollment 404 because its message acknowledges
protocol risks and terms. Parent must already have that specific acknowledgment.
No profile/connect request was signed or submitted during implementation.

The official server currently returns URI/resource placeholder :// in the challenge.
The helper accepts only this exact observed placeholder or official testnet URI;
it never signs a different domain, chain, wallet, statement or resource. Tests cover
this actual server shape, not an invented SIWE challenge.

Parent-reported faucet succeeded with 10000 test AUSD, transaction
0xd17f10a1f9651cd9512b0c27a2cd90774e25cab0a95916d3b5da9e5b2680ecb0.
The previous collateral-acquisition blocker is superseded. No faucet is executed
by this package. After acknowledged profile connect, enroll the key, then perform
parent-authorized allowance/createAccount/forwarding. Current testnet instance is
12 and must be verified from context. Do not treat profile/API enrollment as proof
of on-chain account creation or successful order execution.

setup.py::read_testnet_account provides unsigned RPC reads with chain10143 and
owner validation. The actual getAccountByAddr(address) return is a SINGLE tuple:
(uint256,uint256,uint256,uint8,address,(uint256,uint256,uint256,uint256)), containing
accountId, balanceCNS, lockedBalanceCNS, frozen, accountAddr and four position banks.
The parent script must not decode this as just uint256. setup-abi.json contains the
exact ABI extracted from the current official frontend for getAccountByAddr,
getAccountById, createAccount and allowOrderForwarding.

setup.py::forwarding_calldata(True) returns unsigned transaction data for
allowOrderForwarding(bool). Parent supplies sender/nonce/gas and signs/broadcasts
separately after account creation. This call takes bool, returns no values, targets
0x1964c32f0be608e7d29302aff5e61268e72080cc and chain10143. A new account starts with
forwarding disabled. Once enabled, authenticated trading/wallet snapshot must show
account fw=true, correct account id/instance12 and expected owner before orders.

Live unsigned RPC check at this diagnosis milestone returned
AccountDoesNotExist(address), selector 0x03a0e277, with the exact test wallet address.
That selector and argument shape match the current frontend ABI. read_testnet_account
now returns exists=false/account_id=null for that exact known revert, never a
fabricated ID or zero balance. Unknown reverts remain errors. The parent still
needs an actual createAccount receipt after collateral approval; AUSD in the wallet
alone does not establish an exchange account.

Actual v3 account-read milestone: existing enrollment Vault refs were resolved only
with perpl:read; official testnet authenticated wallet/positions verified account
1227, instance12, forwarding=true, frozen=false, balance100 AUSD, locked0 and no
positions. MON ID64 reports min_settle_amount=0 and integer lot, so the isolated
parent harness defaults to 100 MON at 1x and enforces <=25 AUSD quote risk.
No real order was broadcast by this implementation.

The parent-owned v3/perpl-gateway.py now uses the normal registry/factory and
FinancialGateway without injected adapters. --configure emits a standalone
perpl-config-patch.json (plugins.perpl plus account entry/install/strategy metadata),
and does not rewrite nerya.yml or wallet/financial configuration. Parent applies the
patch serially. --account is authenticated read-only; --prepare records genuine
account evidence into existing snapshots and prepares a frozen quote; --execute
requires exact action/hash and uses ApprovalService with explicit --approve;
--status reconciles without sending. Close requires the confirmed open action from
the same run and the actual position's original rq. AUSD is valued as one TEST risk
USD unit, never claimed as actual account profit or fabricated balance.
RiskGate receives live provenance and preserves escalation in the quote. An
escalated action requires an explicit gateway action approval; a standing grant
cannot bypass it. Missing snapshots/strategy/config fail closed.

Preparation diagnosis after parent install: an old snapshot caused a generic
unknown-venue Perpl refresh (health degraded/free0), correctly rejected by RiskGate.
The harness refreshes authenticated Perpl snapshot immediately before preparation.
The current actual configuration then has exactly one rejection reason:
live_trading_disabled_runtime, snapshot health ok/free100. The sibling LP harness
configure resets global live=false, so the parent must serialize configuration and
explicitly enable this isolated TESTNET workspace after that configuration step.
No runtime/config flags are flipped by the Perpl harness. --diagnose refreshes real
read evidence and returns the exact readonly RiskGate decision/reasons.

Process-only authorization requested for the isolated testnet harness: prepare now
validates workspace/component/account/strategy/MON market/chain10143/API/RPC/wallet,
1x market IOC and bounded quantity, then enables runtime.live_trading_enabled in
that Config object only. It does not write nerya.yml or change cron/triggers.
The quote expires no later than 30 seconds from generation AND from both market
and wallet evidence timestamps; validation checks expiry again after network I/O.
Parent must reinstall the changed reviewed package to adopt this quote behavior.

Execution still has a parent-contract limit: FinancialGateway._fence reloads the
existing nerya.yml and requires its live flag. An in-memory Config override alone
cannot satisfy that fence. The harness reports this prerequisite explicitly and
does not mask config paths, monkeypatch load_config, inject adapters, subclass away
the fence, or write flags. The parent must provide a narrowly validated supported
testnet process-authorization contract in the gateway or explicitly enable the
selected workspace on disk before executing through the standard gateway.

Harness refresh/idempotency: plan_id is stable for each run/step, and repeated
prepare looks up the original action_key in SQLite before reconstructing a request.
Existing expired unsubmitted actions use FinancialGateway.refresh with expected
revision; original request/action ID is retained. Explicit --refresh --action-id ID
--expected-revision N returns revision/hash and updates the saved summary.
Submitted/uncertain identities are immutable and never refreshed/resubmitted.

For parent-authorized bounded test coins, --execute with the exact ORIGINAL hash
and explicit --approve may refresh an unsubmitted quote with <20s remaining in the
same process. It checks original request equality, fixed testnet scope, 1x and
<=25 AUSD, emits the new summary before operator approval, then uses ApprovalService
and standard gateway execute immediately. It does not carry over stale approvals,
retry a changed-cost rejection, relax the 30s evidence fence or modify gateway code.
Parent must serialize the disk live flag between sibling harness operations.

IOC price-budget fix: validation no longer computes a NEW slippage buffer on top
of current prices and compares it to the original already-buffered quote. A market
proposal now freezes a decimal-safe scaled upper/lower execution bound from the
original book plus/minus requested bps. The actual wire is a PRICE-BOUNDED IOC/FOK
(limit-price IOC, not unbounded p=0), so the venue cannot rebase ms and execute
beyond the approved price after local validation. Buy orders use the upper bound;
sell/close-long orders use the lower bound. Risk/margin/fee reserve uses the frozen
upper bound; current ask/mark inside it is accepted, outside rejected. Changed fee
or collateral costs remain checked against original reserved caps; current balances,
reduce-only ownership, risk/expiry and approval checks remain in place. Source
changes require parent package reinstallation; existing pinned actions must remain
immutable. Discard unsubmitted old plans and prepare a new run/revision.

Actual first testnet order rejection: rq1, oid4632599527425, st7/sr44/fr8/fs0.
Official map says TakerOrderSettlementFailed / ExceedsMaxNegPnlCollat. The wire sent
mnp=0, which explicitly prohibits negative-PnL collateralization against mark.
It is not a minimum-lot diagnosis. This terminal rejection has no fill and is never
replayed. max_negative_pnl_bps is now an explicit parameter, defaults to 0, capped
at 100 bps and current market maximum. Parent harness opening order explicitly
sets 100 bps (1%); close sets 0. Its maximum extra negative-PnL collateral is included
in spend_usd and AUSD reservation. Do not silently use the testnet 1000 bps default.

Current official frontend index-C89awWas.js sVt order builder confirms API market
orders use p=0, IOC, optional ms, and OMIT mnp; omission takes the market default.
Its yVt direct-contract path uses a bounded price and 1000 bps negative-PnL allowance,
which is a distinct transport. This adapter's market wire is restored to p=0.
The earlier implicit price-bounded market rewrite is superseded. Original local
price/reservation bounds remain and avoid double-buffer validation, but market ms
is execution-time-relative and cannot guarantee an absolute frozen price during
a network race. An operator requiring an absolute ceiling must explicitly request
limit + IOC/FOK instead; do not describe p=0 as an absolute exchange price lock.
No real order was sent during diagnosis or code repair.

Actual fill budget review: terminal partial fills retain actual size/notional/fee and
partial=true; nonterminal partials remain confirming. A successful chain receipt
with actual fee/notional/price outside the frozen reservation bounds now reports
execution_confirmed=true, budget_breaches and operator_action_required, while state
remains needs_recovery. This retains capital reservation and prevents re-send; it
never reports a executed trade as rejected or silently clamps cost to the quote.
Official API p=0 remains execution-time-relative; this after-execution review is
needed for price races. It is not proof that a market quote is an absolute price cap.
This additional source revision must not replace a currently pinned pending action;
parent should reconcile pending actions with their original binding.

Bare txid recovery repair: official authenticated order-history returns a 64-hex
transaction ID without 0x. Standard status now validates exactly 32 bytes and
adds 0x before Monad JSON-RPC, preventing Invalid params. Original rq2 confirmed
fill was independently verified against transaction
0x57df1e8e3e95586a9ae24066b8b38717985cee52dff25e52bd81bc3fa5f6763f,
status1/block70689246. No pinned implementation cache or binding was edited.

Explicit parent remediation CLI: --verify-open-recovery --action-id ID uses the
ORIGINAL pinned adapter only for authenticated reads, and a reviewed recovery.py
for hash normalization/independent same-block/transaction-index/Exchange receipt
verification. It validates original request digest/account binding/acc/mkt/rq,
order type/size/leverage/flags and settled amounts within frozen budgets. The
Perpl local log index is NOT equated with RPC block-global logIndex. It saves proof
without changing financial state. --record-open-recovery performs those checks
again, is limited to needs_recovery with chain_execution_not_confirmed reason,
and records an explicit audit note/source hash/prior receipt via FinancialStore;
original request, quote, component binding and submission remain unchanged.
It consumes positive actual fees/notional through the standard store accounting,
not a fabricated success marker, and never sends another order. Parent executes
this command explicitly. Implementation only ran verification, not record.
A risk-reducing close may alternatively prepare after saved explicit proof is
reverified against the original order/position, without silently changing open state.

Native strategy producer fix: native10 action fund_e818dfbe3b4743d9a1fc2c6803a4e246
had healthy real snapshot but operator_agent_trade_approval_required. Its plan omitted
source, so conversion misclassified the real strategy_script producer as interactive.
_source now ignores request/model source. It maps strategy_runtime only after standard
FinancialContext/run lease/security-revision validation, actor strategy:<id>, matching
task/plan strategy and write:trade scope. Operator contexts stay operator and retain
per-action approvals. Finished/mismatched leases fail closed; no model-provided source
can bypass approval. Real lease and forged/missing-context regressions are covered.

Deterministic FinancialError before local _claim returns failed_before_submission only
if gateway submission is empty AND persisted Perpl request counter has no claim for
that action. Exceptions after claim remain uncertain and never automatically resend.
Historical native10 no-claim proof was saved read-only: empty submission, no local
request row, authenticated account1227 lfr3, no open orders/positions and historical
rq values1/2/3 only; exact exception occurs before claim/POST. Parent may explicitly
record failed_before_submission with this evidence/audit note before a new tick.
No old state, cache, binding or order was modified by this implementation.

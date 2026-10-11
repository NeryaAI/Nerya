# Authoring executable mainstream market strategies

Use financial_readiness before choosing an execution family, then load
financial_ops and its mainstream-defi reference for schemas. Prefer existing
CCXT spot/linear accounts, Jupiter Solana swaps, Uniswap/Pancake v3 LP,
restricted Uniswap v4 LP and Aave v3. Do not choose Byreal by default or invent
native Raydium/Orca LP support merely because Jupiter can route a swap there.

Build main.py:run using the established script, script-gated Agent or pure
Agent contract. Keep execution and review schedules separate and disabled
until independently approved. Implementation is not activation. Do not edit
Agent Loop to add market-specific branches or give the model direct signers.

## Required strategy package

Record account/wallet, canonical instrument or pool, percentage-based budget,
fees and gas reserve, entry rules, owned-position exit rules, data freshness,
cooldown, maximum holding time, recovery IDs and an independent review plan.
Use reviewed parameters and actual NAV/free balance; never hardcode 100U or
interpret 0.90 as 0.90%. No historical source means unverified performance,
not permission to fabricate a profitable backtest.

templates/risk_rules.py.j2 contains pure deterministic exit and LP-adjustment
gates; copy selected functions into the strategy. They do not place orders or
call a model. Feed timestamped SDK observations, not unverified model numbers.
Meme entry additionally needs current token-security and liquidity evidence,
an executable route and an approved token set. Unknown mint/freeze/transfer
restrictions, unavailable sellability evidence or stale pool data are blockers,
not an invitation to increase slippage. Existing market whitelist/finite-grant
checks remain authoritative; the Agent cannot add a discovered token to its
own authorization. A screening result is not a guarantee against rugs.

For wallet and prediction positions schedule deterministic exits. Their
connectors do not implement exchange-native brackets; do not promise an
always-on stop when the runtime is offline. Price/data errors trigger recovery,
not no-signal success. Use ctx.trading.close_position only for strategy-owned
exposure and keep outstanding action IDs until reconciled.

## LP work

Use pool tick, liquidity, principal, fees and gas-cost snapshots. Check range
exit persistence and cooldown; forecast incremental fees from a documented
source, never from a made-up APR. Unknown economics means no automatic
rebalance. templates/lp_rebalance.py.j2 is a tested helper for the actual
ctx.financial coordinator; call it from run(ctx) with an immutable per-cycle
plan. After a partial sequence, expose the exact remaining token inventory
and pending child action instead of starting again. Treasury ratio swaps need
an already configured supported route; LP support itself does not imply every
swap router is supported.

## Acceptance and review

Test generated source's true/false/stale branches, zero LLM calls on no-signal
ticks, budget fractions, partial fills, cost accounting, original-ID recovery,
duplicate suppression, and paper/live separation. LP tests must include the
actual remove -> confirm -> optional swap -> confirm -> add sequence. Protocol
RPC fixtures establish transport behavior, not live-money returns. A real
model-created strategy and deployment acceptance remain separate tests.

Review is a script gathering compact real execution/cost/risk evidence, then
one Agent proposing changes to strategy code and the next review plan. Keep
review cadence and triggers strategy-specific. Compare net returns to hold
without relabeling underperformance as an execution failure. Retain missing
fee, price, replay and chain-confirmation evidence as explicit review gaps.

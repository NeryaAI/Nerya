# Mainstream execution contracts

Start with financial_readiness and financial_components. Readiness is local
configuration/dependency inspection, not an exchange handshake, an approval,
successful RPC simulation or a verified live trade. Loading this Skill cannot
enable funds permissions, install keys or activate a strategy.

## Preferred routes

Use Jupiter v2 for Solana token swaps, Uniswap v3/v4 or PancakeSwap v3 for EVM
liquidity positions, and Aave v3 for supported lending actions. Preserve an
operator-selected venue; do not switch an existing binding silently. Do not
select Byreal as the default for new strategies or expand its LP support.

Jupiter aggregates whichever routes the actual order supports. A Raydium,
Orca or Meteora route in a quote is swap evidence, not a native LP adapter.
Native Solana LP/farming, Pancake Infinity, v4 nonzero hooks, negative-risk
redemption and combo redemption are not implemented. Say unsupported; never
invent methods or relabel a paper result as a real transaction.

Jupiter /swap/v2/order needs a vault-backed API key. Nerya excludes sponsored
and RFQ multi-signer transactions. Fixed input, min-out, fees and slippage are
checked; an independently read chain receipt determines settlement. The
network-fee ceiling and refundable rent reserve are separate. Preserve the
original signature/requestId after a timeout. Do not silently fall back to v1.

## Script SDK and Agent tools

Script runs use ctx.financial.prepare(request, client_request_id=...),
execute(action_id, quote_hash=...), get(action_id), reconcile(action_id),
account_state(request), components() and readiness(). Every call retains the
current strategy producer, resource binding and finite authorization. Paper
and shadow contexts do not have a funds-execution shortcut. Pure Agent tasks
use financial_prepare/execute/get/reconcile and financial_components; the
same gates apply. Never import connectors or call raw wallet/HTTP/CLI code in
an authored strategy.

## LP actions and range changes

Protocol names: uniswap_v3, uniswap_v4, pancakeswap_v3. Actions: lp_add,
lp_remove, lp_collect. Supply wallet_id, chain, protocol and parameters.pool_id.
Use live component parameter schemas for amounts, integer ticks, liquidity,
token_id and minimum_liquidity. Position managers, token order, pool identity,
price feeds and code hashes must be operator reviewed. Do not set reviewed:true
from Agent reasoning. An existing NFT cannot change its range in-place.

Use financial_rebalance_prepare({plan,client_request_id}) with an immutable
plan containing exit, optional swap, entry, expires_at and max_total_fee_usd.
exit is a bounded lp_remove of an owned NFT; entry is a new lp_add of the same
underlying pair; swap can adjust the ratio only inside that pair and wallet.
Persist rebalance_id. financial_rebalance_advance({rebalance_id,expected_revision})
advances one step; financial_rebalance_get reads it; stop requires all child
actions resolved or explicitly discarded. SDK names are prepare_rebalance,
advance_rebalance, get_rebalance and stop_rebalance.

Never replace an uncertain step with a new workflow/key. New ticks may query
old receipts but cannot borrow an old run's approval identity. Confirmed exit
proceeds bound later spending, even when the wallet contains unrelated assets.
Do not automatically resize an already authorized entry if proceeds differ.
Prerequisite allowances require their own finite approval and refreshed quote.
Partial progress is not an atomic rollback: report the current asset inventory.

## Aave

Use aave_v3 and lend_supply/lend_withdraw/borrow/repay through the same gateway.
Read account_state first: collateral, debt, reserve status and health-factor
buffers are real chain facts. Variable debt is supported; E-mode, collateral
toggle, flash loans and recursive leverage are not implicit capabilities.
Stop borrowing when policy health buffers are breached. A reducing action may
still need gas and approval. Account-wide risk cannot be inferred from one token.

## Prediction settlement and fees

Polymarket orders remain ordinary prediction trades, not wallet swaps.
Data API v2 envelopes/cursors are mandatory; GTD is at least 180 seconds ahead.
For independently verified order fees configure settlement_rpc_url and
settlement_exchange_code_hashes against the reviewed CTF Exchange v2 contracts.
Nerya verifies canonical receipts, order hash, maker, token, side and exact event
amounts. Missing evidence holds the original order pending; it is not a zero-fee
fill. Without that configuration, fees remain explicitly unverified. Collateral
is PUSD; nominal collateral accounting is not a PUSD/USD exchange-rate guarantee.

redeem with protocol polymarket_ctf requires Polygon, account_id, its explicitly
linked wallet_id, parameters.condition_id and parameters.strategy_id. Only
resolved reviewed standard binary conditions and an owned EOA are implemented.
Both outcome balances must be wholly owned by that strategy; outstanding
orders block redemption. Existing ERC1155 operator approval is required; Nerya
does not issue unlimited approval automatically. The entire pair is redeemed,
so shared/proxy wallets need a different transport. Exact collateral credit,
outcome debits and gas are checked before strategy positions close, including
zero-value losing outcomes. Do not call SDK wallet-provisioning convenience APIs.

## Cost and review evidence

Wallet network costs use receipt gasUsed/effectiveGasPrice or Solana meta.fee;
OP-stack l1Fee is included when provided. Router fees already reflected in
actual output must not be subtracted again. Fee pricing records its valuation
time; a late recovery is not historical-price proof. Missing costs stay
unpriced/unverified. LP review separates principal, fee growth, realized costs,
remaining tokens and protocol exposure. No ordinary OHLCV backtest proves LP
fee APR, concentrated-liquidity returns or prediction settlement behavior.

Official contracts checked 2026-10-05:
https://developers.jup.ag/docs/api-reference/swap/order
https://developer.pancakeswap.finance/contracts/v3/pancakev3pool
https://developer.pancakeswap.finance/contracts/v3/nonfungiblepositionmanager
https://docs.polymarket.com/trading/positions/manage
https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/exchange/mixins/Events.sol

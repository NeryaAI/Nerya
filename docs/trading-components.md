# Trading component contract

Scripts use `ctx.trading` for orders and `ctx.financial` for funds/protocol
operations. Agent tools enter the same `FinancialGateway`. The gateway owns
actor and run identity, finite grants, human approvals, atomic reservations,
submission markers and recovery. A plugin contributes market facts and a
transport; it does not replace those controls.

## Workspace package

Put operator-approved code in `plugins/<plugin-id>/trading.py`. The module
exports a nonempty tuple `TRADING_COMPONENTS` of `TradingComponentSpec` objects.
`plugin.py` in the same directory can separately contribute existing tools
and observation hooks. Agent-authored packages still enter through the
PatchProposal approval workflow.

```python
from nerya.trading.components import TradingComponentSpec
from .transport import ReviewedSwapTransport

TRADING_COMPONENTS = (
    TradingComponentSpec(
        id="swap", version="1.0.0", actions=("swap",), market_types=("spot",),
        factory=lambda config, request: ReviewedSwapTransport(config, request),
        parameters_schema={
            "type": "object",
            "properties": {"pool_id": {"type": "string", "minLength": 1}},
            "required": ["pool_id"], "additionalProperties": False,
        },
        # Exact installed distribution versions, checked at import.
        dependencies=(),
        account_state=True,
    ),
)
```

The component becomes `user:<plugin-id>:swap`. Requests select it explicitly
with `component_id`; a plugin cannot shadow a builtin transport or intercept
ordinary `trade` actions. New CEX venues contribute `ExchangeProviderSpec`
under `providers/<id>/provider.py` and must build `CcxtConnector` with
`runtime="python_ccxt"`. Workspace providers cannot shadow builtin aliases.

Supported semantic actions are `trade`, `swap`, `exchange_transfer`,
`withdraw`, `wallet_transfer`, `contract_approval`, `bridge_swap`, `lp_add`,
`lp_remove`, `lp_collect`, `lp_rebalance`, `lend_supply`, `lend_withdraw`,
`borrow`, `repay`, `permit2_approval`, and `redeem`. Declaration does not imply implementation or
market verification. An unknown action fails closed. New action families
require a core schema and permission review; protocol-specific parameters
use the component's closed JSON Schema.

## Transport methods

| Method | Contract |
| --- | --- |
| `quote(request)` | Read actual market/account/protocol evidence; return an expiring immutable execution plan, Decimal-compatible USD risk/spend/fees, per-asset required and available quantities, and any required finite allowances. |
| `validate(request, quote)` | Recheck permissions, identity, deployment, price, fees, balances and projected risk before submission. Never sign, approve or send. |
| `execute(request, quote, submitted)` | Execute only the frozen plan. Persist deterministic client-order ID or signed transaction hash with `submitted(ref)` before network submission. Never retry an uncertain submission. |
| `status(request, quote, submission)` | Query the original identity; verify fills or canonical confirmed protocol events; book idempotently. Never create a replacement order or transaction. |
| `account_state(request)` | Optional; declare `account_state=True`. Return `source`, `as_of`, `health`, coverage and actual state. Unknown/unavailable must not become zero. |
| `cancel(...)` | Optional; declare `cancel=True`. A capability declaration alone does not authorize a funds mutation. Cancellation must retain its owning order/action's identity and permission checks. |

Quotes for options, LP and lending require a `portfolio_effect` containing
`before`, `after`, `source`, `as_of`. Metrics match `RiskMetrics`; the core
computes risk reduction from portfolio changes. An operation called `close`
or `withdraw` does not gain an exemption. `freeze_all` blocks execution;
`halt_new_risk` and `reduce_only` require observed reduction. Signing/live
permissions still apply to exits.

`prerequisites` contains at most eight exact `contract_approval` or `permit2_approval` requests for
the same wallet and chain. The gateway returns `prerequisite_required` and
the reviewable requests. Prepare each child with `parent_action_id`, execute
it through the same authorization gateway, wait for confirmation, then
refresh the parent. Changed calldata or quote always gets a new hash;
pending/submitted children block refresh. No convenience method silently
grants allowances.

## Loading and recovery

`nerya runtime trading components --workspace <path>` inspects and refreshes
the component catalog. The HTTP operator endpoint is
`POST /financial/components/reload`; catalog reads are
`GET /financial/components`. `ctx.financial.components()` uses the same
catalog. Normal prepare/execute calls also detect package changes.

Each package revision includes Python source and JSON/TOML/YAML/ABI files,
including helper modules. Symlinks and oversized packages are rejected.
Imports use a unique workspace+revision namespace and relative imports
resolve inside an immutable snapshot at
`runtime/trading_plugins/revisions/<id>/<hash>/`.

A successful reload publishes all components of one package together.
An invalid revision blocks new actions from that package and retains the
original implementation for receipt recovery. A changed/removed/disabled
component blocks old quotes from first submission. Previously submitted
actions continue querying their pinned original revision, including after
a process restart. Missing/tampered revisions produce `needs_recovery`
without releasing uncertain funds or retrying.

Finite grants explicitly bind component IDs, component revisions, protocols,
chains, assets, routers/spenders, task version, quotas and expiry. A new
plugin revision cannot reuse an old grant. Keep snapshots needed by unfinished
actions; garbage collection is deliberately not automatic. Workspace code
is trusted operator code, not a Python sandbox; imports may run arbitrary
Python. Permission controls constrain gateway usage, not malicious native
code with direct access to the operator's filesystem.

Core builtin code still requires the normal service upgrade/restart flow;
workspace hot loading does not reload Nerya's own modules or install Python
packages. Capability catalogs report `live_verified=False`. Offline fixture
tests, fork/testnet acceptance and real-funds acceptance are separate evidence.

## Initial Aave transport

`builtin:aave`, protocol `aave_v3`, accepts variable-debt supply, withdraw,
borrow and repay with fixed finite ERC20 amounts. It reads aggregate account
data, per-reserve debt and supply, scaled units, interest indexes, oracle
prices and reserve flags. Standard mode only: eMode and stable debt require
a dedicated model and are rejected. Quotes and execution check Health Factor
after the operation; withdrawal/borrowing require a configured buffer above
one. Protocol claims distinguish strategy ownership from observed wallet
assets and external/manual activity. Receipts validate transaction parameters,
canonical block, confirmation depth, protocol emitter and indexed ownership
before updating claims.

Configuration must explicitly provide reviewed deployment addresses and
runtime code hashes, the Pool proxy implementation address/hash, reviewed
oracle sources, wallet action permissions, finite account/grant limits,
`financial.defi.max_gas_limit`, and `financial.portfolio_limits`.
No protocol deployment is enabled merely because a chain is registered.

The initial protocol ABI is checked against the primary interfaces:
`aave-dao/aave-v3-origin/src/contracts/interfaces/{IPool,IPoolDataProvider,
IAaveOracle}.sol`. Local fixtures exercise the ABI and state machine; chain
fork and real-funds verification remain pending.

## Uniswap transport

`builtin:uniswap` provides ERC20 `lp_add`, `lp_remove`, `lp_collect` for
reviewed Uniswap v3 and static hookless v4 pools. Parameters use the closed
schema in the catalog. NFT ownership, wallet NFT inventory coverage,
strategy liquidity claims, pool/factory/code identity, reviewed price feeds,
price deviation, ticks, finite input/collection bounds and quote deadlines
are checked. Underlying assets returned by an exit retain market risk;
protocol exposure is tracked separately.

v4 has separate ERC20-to-Permit2 and Permit2-to-PositionManager approvals.
`builtin:permit2` binds an exact amount and expiration to the reviewed
PositionManager. Both are authorized as explicit child actions. LP status
verifies the canonical transaction, NFT mint identity, liquidity events,
post-receipt liquidity and underlying asset flows. v4 `ModifyLiquidity` is
emitted by PoolManager with PositionManager as sender; NFT owner is checked
separately. Fee views include stored owed amounts and current fee growth.
Empty liquidity retains the owning strategy's claim.

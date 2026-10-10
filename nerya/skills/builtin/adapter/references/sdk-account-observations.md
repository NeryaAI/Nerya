# SDK account observations

Use this path when a wallet, exchange SDK or protocol exposes account data that
must appear in the portfolio UI. Reuse existing account/CCXT reads when they
already meet the request. Do not add vendor imports or startup installers to
the core, or prepare unrelated chain SDKs.

## Agent workflow

1. Inspect the installed provider/SDK and its official read methods. Record the
   target network and public owner/account identity. Separate read access from
   trading permission; a connected RPC is not proof of protocol support.
2. Use an operator-approved Skill script under `scripts/`. Agent-authored new
   scripts still follow the existing proposal workflow. SDK scripts are
   standalone readers with JSON stdin and one JSON stdout document; imports
   come from their declared SDK dependencies or the standard library.
3. Call `sdk_account` with `op=prepare`, a stable `connector_id`, `skill_id`,
   `script`, `runtime=python|node`, exact registry-version `requirements`, and
   public `payload` (such as chain/address/RPC selection). Python requirements
   use `name==version`; npm requirements use `name@version` or
   `@scope/name@version`. Do not guess latest versions or pass shell commands.
4. The core prepares only that dependency set in the workspace. Equal sets
   share an environment. Auto/yolo tool modes permit preparation unless
   `runtime.allow_auto_install=false`; explicit opt-outs produce
   `installation_required`, not repeated installation attempts.
5. Call `op=refresh` with the connector id. The script receives
   `{"protocol_version":1,"command":"account_state","payload":{...}}`.
   The core validates the response, preserves source/version/evidence hash,
   and publishes it to the portfolio page. List/inspect are cached state reads
   and never install packages or call the SDK.
6. Correct authentication, missing methods, package or script problems once,
   then prepare/refresh again. Failed refresh retains prior data as stale.
   Changed owner/payload/credential references cannot reuse another binding's
   observation. Do not resend an unchanged failed request.

Use vault-backed `env_refs` for credentials. Refs require `sdk.read` scope;
private keys, seed phrases and signers are not reader credentials. The SDK
process does not inherit runtime API tokens, and the installer receives no
vault secrets. Never include credentials in payloads, reports or SDK stdout.

## Response contract

```json
{
  "schema_version": 1,
  "as_of": 1791590400,
  "address": "the-requested-public-owner",
  "balances": [
    {
      "chain": "base",
      "token": "the-actual-contract-address-or-native-asset-identity",
      "symbol": "USDC",
      "raw_amount": "1234567",
      "decimals": 6,
      "available": "1.234567",
      "locked": "0",
      "price_usd": "1"
    }
  ],
  "positions": [
    {
      "id": "the-protocol-position-id",
      "kind": "lp",
      "chain": "base",
      "protocol": "the-actual-protocol",
      "label": "USDC pair position",
      "value_usd": "25",
      "unrealized_pnl_usd": null,
      "claimable_rewards_usd": "0.35"
    }
  ],
  "performance": {
    "period_start": 1791504000,
    "period_end": 1791590400,
    "realized_pnl_usd": null,
    "unrealized_pnl_usd": null,
    "income_usd": "0.20",
    "fees_usd": "0.03"
  }
}
```

Use actual observation time in Unix seconds. Monetary/quantity values are
decimal strings. Raw token amounts are integer strings with explicit decimals;
if both raw and scaled amounts are supplied they must agree. Assets use chain
and token identity, not display symbols; never merge distinct USDC deployments.
Available plus locked may not exceed total. Unknown prices/PnL are null, not
zero. Empty balances means a successfully observed empty balance collection.

Realized PnL, unrealized PnL, received income, fees and unclaimed rewards are
distinct metrics. Realized PnL/income/fees require a stated period ending no
later than as_of. An APR/APY is not received income, and an equity increase is
not automatically PnL. The core derives each token's USD value from quantity
and supplied USD price; unpriced tokens are explicitly excluded. It never adds
LP position valuation to token balances to invent a combined NAV.

These are display-only observations. They do not fund strategies, change the
account ledger, grant permissions or authorize signing. To use an account for
execution, bind it through the existing account/provider interfaces and risk /
approval / financial gateway, independently of this display connection.

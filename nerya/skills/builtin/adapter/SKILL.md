---
name: adapter
description: "Use to integrate or repair exchange, prediction-market, wallet, DEX and aggregator adapters; inspect capabilities, validate protocol contracts and connect execution to Nerya's risk and approval flows."
license: MIT
version: 0.1.0
---

# Adapter

For SDK-dependent account reads or mapping balances/positions/income into the
portfolio UI, read [references/sdk-account-observations.md](references/sdk-account-observations.md).
Use sdk_account to prepare only the required components and collect validated
observations; read-only account setup does not require financial execution readiness.

For the integration checklist, read [references/full-playbook.md](references/full-playbook.md).

Start with connector_list/connector_describe for venues or wallet capability
discovery for wallets. Choose the relevant branch below; this is one adapter
Skill, with protocol detail loaded only for the requested integration.

- Prediction markets: read [references/prediction-markets.md](references/prediction-markets.md).
  Use scripts/inspect_prediction.py for public outcomes, book and price history.
- CEX: extend ExchangeProviderSpec and the existing CCXT connector; preserve
  exchange feature discovery and optional protection. Do not replace CCXT with
  a new native CEX client.
- Wallets/DEX: follow the workflow below and load its contract as needed.

Choose the smallest integration supported by the target's actual API:

- Router02-compatible EVM DEX: configure evm_v2 or a self_custody/MetaMask
  dex_routes entry. Keep the chain, router and wrapped-native address explicit.
- Existing wallet SDK/API: implement WalletProvider and register it through
  the workspace plugin's ctx.register_wallet_provider. New native modules
  should hide protocol differences behind the same public methods.
- Separate language/process: use provider external and the versioned JSON
  contract in [references/contract.md](references/contract.md).

First run scripts/inspect_adapter.py with wallet_id/account_id to inspect the
selected binding. It reports methods, chains, minimum-output and confirmation
support. Optional quote checks are read-only. Missing capability is a concrete
integration gap, not permission to reinterpret the trade.

For adapter development read the contract and
[references/providers.md](references/providers.md). Use current official API
examples or captured read-only responses to build fixtures. Preserve raw/UI
units, full token identity, exact-input semantics and quote expiry. Test quote
validation, explicit rejection, submitted-to-confirmed, reverted, and timeout
recovery through the public methods. Validate the JSON fixture using
scripts/validate_contract.py; this parser check supplements protocol tests.

Live strategy orders enter ctx.trading/TradingAPI/trade_intent_submit and the
shared wallet approval flow. Implement get_execution_status as a read only
operation; a receipt query never resends a transaction. Preserve min_out and
execution_id, and call on_broadcast as soon as the send identity exists.
Confirmed means transaction evidence and actual received amount, not a quote,
order id, HTTP success or unsigned transaction. TP/SL stays optional; declare
native order/protection capabilities honestly and reject unsupported requests.

Package agent-authored adapters through the existing plugin_author proposal
workflow. Put factories in an installed workspace plugin, not import paths in
model-generated configuration. For a concrete factory example read
[references/plugin-example.md](references/plugin-example.md). Static validation
does not authorize import/activation, installation or real-money validation.

Finish with the supported methods/chains, required configuration, test results
and unverified live boundaries. Keep API keys/signers in the vault; reports and
fixtures contain no secrets or serialized signed transactions.

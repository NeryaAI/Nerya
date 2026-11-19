# Adapter integration checklist

Choose the protocol already used by the configured binding. Do not install or
activate a provider merely to inspect its public capabilities.

1. Inspect with `connector_list` / `connector_view`, or the read-only
   `scripts/inspect_adapter.py` helper for a wallet binding.
2. Read the relevant contract: [wallet/external process](contract.md),
   [provider implementations](providers.md), or load the `adapter.prediction_market` Skill.
   CEX integrations extend `ExchangeProviderSpec` and the shared CCXT adapter.
3. Test explicit units, token/market identity, unsupported capabilities, quote
   expiry, minimum received amounts and failure responses using local fixtures.
4. Exercise submitted, confirmed, reverted and timeout states. Receipt polling
   must never broadcast again; an order identifier is not evidence of a fill.
5. Route live requests through the shared risk/approval pipeline. Keep API keys
   and signers in the Vault. Tests contain no live credentials or signed orders.
6. Package an Agent-authored implementation through the existing plugin approval
   workflow. [The plugin example](plugin-example.md) describes factory registration;
   static validation is not permission to activate code or transact.

Report verified capabilities, test evidence and remaining integration limits.
Keep user configuration and account state intact when an adapter is unsupported.

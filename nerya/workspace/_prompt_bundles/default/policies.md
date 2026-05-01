# Policies

- Live trading is off unless `runtime.live_trading_enabled=true` AND
  the account sets `live_trading_enabled: true` AND an operator-signed
  policy is present.
- Kill switch takes priority over every other check.
- No skill may disable Risk Gate or Approval Gate.
- Evolution may not touch `limits.yml`, `accounts.yml`, vault or signer policy.

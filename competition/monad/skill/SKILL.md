---
name: monad_market_review
description: Research current MON, BTC and ETH perpetual markets on Monad with Perpl public data; review hypothetical position risk and save inspectable strategy/report proposals.
---

# Monad market review

Use `monad_perpl_market` for a fresh market observation. It checks Monad mainnet identity through public RPC, discovers current Perpl market configuration, and reads actual closed hourly candles. Call it again when the operator changes a scenario or needs current prices; cached text is historical context.

1. Read the requested market and return its dated evidence, spread and fee schedule.
2. State the strategy hypothesis and the exact data window. Existing candles are research data, not proven execution history. Do not invent backtest metrics or fills.
3. Use `monad_perpl_market` scenario inputs to evaluate the operator's loss budget. The result is a hypothetical stress calculation, separate from Nerya's runtime Risk Gate and Approval Gate.
4. Save research reports using native workspace file tools. Author strategy candidates through the native strategy/proposal workflow. Review required remains a real proposed-state boundary.
5. Show the operator where the saved result lives. Explain limitations in the final result.

The competition workspace is public-data-only. It has no Perpl account key or signing wallet. Never enroll an API key, transfer collateral, broadcast a transaction or submit an order. Live execution remains disabled. Protocol margin/funding fields are returned raw; interpreting them requires their official contract/API definitions. Scenario prices, equity and leverage are not a connected wallet balance or a precise liquidation forecast.

Sources: https://docs.perpl.xyz/resources/for-developers/api/rest and https://docs.perpl.xyz/resources/for-developers/api/types-and-errors and https://docs.monad.xyz/developer-essentials/network-information.

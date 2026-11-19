---
name: adapter.prediction_market
description: "Discover event outcomes and build, validate or inspect a prediction-market strategy (Polymarket, 预测市场策略)."
version: 1.0.0
license: MIT
metadata:
  nerya:
    script_policy:
      read_only:
        - inspect_prediction.py
---

Discover the provider with `connector_list(kind="prediction_market")`. Public
discovery and paper strategies load it on demand: no wallet, install command,
global permission change or live activation. If the workspace reports
`module_disabled`, stop and report the operator setting; do not change it.

- For an outcome or its quote, use `scripts/inspect_prediction.py` and
  `references/prediction-markets.md`.
- For a strategy, use `references/prediction-strategies.md` with `strategy_author`.
- For status after a run, read the strategy ledger. Do not submit another tick.

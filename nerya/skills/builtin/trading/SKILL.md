<!-- nerya-skill-frontmatter-start -->
---
name: trading
description: "Use to size positions, place trades, review portfolio state, manage open risk, or move strategies through lifecycle gates."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Trading

Use only after fresh market/account context exists. Live trading still
requires runtime flags and approval gates.

## Flow

FETCH current market state with `markets`.
READ portfolio, exposure, PnL, and account constraints.
FORM intent: side, market, size, order type, rationale, stop/exit.
RUN risk check before execution.
IF live or high-risk, require approval gate.
SUBMIT only through trading runtime surfaces.
RECONCILE fills and report state changes.

## Scripts

- `scripts/risk_check.py`
- `scripts/portfolio_summary.py`
- `scripts/strategy_report.py`

## Lazy References

- `references/full-playbook.md` for detailed safety ordering.
- `references/libraries.md` for trading library notes.

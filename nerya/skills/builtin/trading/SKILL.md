<!-- nerya-skill-frontmatter-start -->
---
name: trading
description: "Use to size positions, place trades, review portfolio state, manage open risk, or move strategies through lifecycle gates. The agent can never switch live trading on: 'go live / 打开 live' requests get an explicit reject — live stays off, offer paper mode — never a strategy proposal as a substitute."
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

## Risk-check honesty

RUN `risk_check` on the size the operator actually asked for (for
"all-in", that is the full available balance/NAV), never on a
pre-shrunk size chosen to slip under a limit. If the requested size
violates a limit, report the rejection (decision, limit, requested vs
allowed notional) and stop; offer the compliant size as a *suggestion*
for the operator to confirm, do not silently submit it.

## Live trading boundary

`runtime.live_trading_enabled` and the kill switch are protected
scopes: you cannot turn live trading on, and `evolve_core_config_patch`
will answer `advisory reject` if asked. When the operator asks to "go
live" or to trade while live mode is off, state plainly that live
trading is off / the request is rejected, that enabling it needs the
operator's own dashboard action plus approval gates, and that you can
run the same intent in paper mode instead.

## Scripts

- `scripts/risk_check.py`
- `scripts/portfolio_summary.py`
- `scripts/strategy_report.py`

## Lazy References

- `references/full-playbook.md` for detailed safety ordering.
- `references/libraries.md` for trading library notes.

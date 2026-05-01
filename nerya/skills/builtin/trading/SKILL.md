<!-- nerya-skill-frontmatter-start -->
---
name: trading
description: "Use whenever the user wants to size a position, place a trade, review portfolio state, manage open risk, or move a strategy through its lifecycle (draft \u2192 shadow \u2192 live \u2192 retire). Triggers on phrases like \"buy/sell/short/long\", \"what's my exposure\", \"rebalance\", \"hedge\", \"stop loss\", \"promote/retire strategy\", \"review the strategy\", and any question about current positions, PnL, or capital allocation. Read this skill before touching any trading.* utility \u2014 it explains the safety order (intent \u2192 risk \u2192 portfolio fit \u2192 execution) that the runtime enforces."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Trading playbook

The trading skill is the playbook for everything that moves capital
or shapes a strategy. It assumes the agent has access to the trading
helper scripts in `scripts/` (invoked via `run_shell`) and to the
domain-specific native tools (`portfolio.summary`, `risk.check`,
`order.submit`, …) once they're promoted in the next migration step.

## Order of operations (do not skip)

Every trade — paper or live — flows through the same four stages.
The runtime journals each stage, so reordering them silently is not
an option.

1. **Form intent.** Spell out *what* you want to happen and *why*:
  instrument, side, size hint, time horizon, conviction. If you can
   not say all four, you are not ready to size a trade.
2. **Risk-check.** Run the risk helper before any portfolio math:
  `python -m scripts.risk_check --json '{"intent": …}'`. The check
   has authority — if it returns `blocked`, stop and surface the
   reason; do not "round it down" yourself.
3. **Portfolio-fit.** Read the current portfolio (`scripts/portfolio_summary.py`)
  and confirm the proposed change is consistent with the existing
   exposures and the strategy's own caps.
4. **Execute.** Only after the prior three pass. Choose the
  narrowest possible execution path (e.g. limit over market when
   liquid, paper before live for new strategies).

Skipping any stage is the single most common cause of trade-related
incidents. The playbook exists because the runtime cannot read your
mind about why you reordered.

## Sizing

Pick a sizing approach that matches the conviction you stated in
step 1:

- **Fixed-fraction** for repeatable signals.
- **Volatility-targeted** when the strategy has a stable PnL series.
- **Kelly-tempered** only when the win-rate distribution is well
estimated; otherwise it overbets.

Never combine "I'm not sure" with "go bigger". Lower conviction =
lower size, full stop.

## Strategy lifecycle

Strategies live in four states: `draft → shadow → live → retired`.

- **draft** — implementation only; no capital. Use this state to
iterate on logic and unit-level checks.
- **shadow** — paper trading against live data. Stay here until the
validation checklist (`references/strategy_validation.md`) is
green for at least the configured shadow window.
- **live** — real capital. Promotion requires a strategy review.
- **retired** — no new orders; existing positions wind down per the
exit plan.

Promotion and demotion are *not* casual operations: each transition
journals an explanation that future sessions will read. Write that
explanation in plain language — no jargon, no hedging.

## Reviewing a strategy

When the user (or a scheduled trigger) asks for a strategy review:

1. Read the strategy's recent journal entries first; the human
  reviewer cares more about *what changed* than what's stable.
2. Pull the metrics report (`scripts/strategy_report.py`) and look
  for regime shifts, draw-downs, and turnover anomalies.
3. Cross-check against the strategy's stated assumptions in
  `references/strategy_assumptions.md` — if the regime no longer
   matches, that is itself the finding.
4. Recommend `keep` / `retune` / `retire`, with one paragraph of
  reasoning. The recommendation is advisory; promotion still goes
   through the lifecycle gates.

## Risk hygiene

- **Hard stops are non-negotiable.** Every live position has a
pre-committed exit; if the runtime can't find one, treat that as a
bug, not a feature.
- **Per-strategy caps are caps, not targets.** Approaching the cap
is a signal to slow down, not to round up.
- **Correlated bets count once.** Two strategies that go long the
same factor are one bet from a risk perspective.
- **Don't fight the journal.** If the journal says you already
closed a position, don't "reopen because it felt wrong" — verify
the state first.

## Bundled scripts


| Script                         | Purpose                                                  |
| ------------------------------ | -------------------------------------------------------- |
| `scripts/portfolio_summary.py` | Print current accounts, positions, virtual ledger state. |
| `scripts/risk_check.py`        | Run the risk gate against a proposed intent.             |
| `scripts/strategy_report.py`   | Aggregate metrics for a single strategy id.              |


All scripts read JSON payload from `--json` / `--payload-file` /
stdin and emit JSON on stdout — invoke them from `run_shell` and
parse the result.

## Reference material

- `references/strategy_assumptions.md` — regime + assumptions
template every strategy should populate before promotion.
- `references/strategy_validation.md` — the shadow→live checklist.
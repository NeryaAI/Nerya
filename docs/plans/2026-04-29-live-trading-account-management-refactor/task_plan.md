# Task Plan: Live Trading Account Management Refactor

## Goal
Produce a code-grounded, reference-backed refactor plan for rebuilding Nerya's account, capital, position, order, take-profit, stop-loss, and live-trading governance systems so agent-authored strategies can be used in real trading with professional controls.

## Phases
- [x] Phase 1: Plan and setup
- [x] Phase 2: Inspect Nerya current implementation and docs
- [x] Phase 3: Inspect Hummingbot and QuantDinger references
- [x] Phase 4: Synthesize target architecture and phased migration
- [x] Phase 5: Write deliverable and verify references

## Key Questions
1. What account, portfolio, ledger, order, risk, and approval surfaces does Nerya already have?
2. Which Hummingbot patterns are worth adapting, especially controller/executor separation, live balances, order tracking, and performance reporting?
3. Which QuantDinger patterns are worth adapting, especially multi-broker/live account routing, MT5/IBKR workflows, risk settings, and strategy lifecycle?
4. What exact modules, schemas, APIs, dashboard surfaces, and tests should Nerya add or change?
5. What acceptance path proves Nerya is ready to promote an agent strategy from paper to live?

## Decisions Made
- Scope: create a planning artifact only; do not change runtime behavior in this turn.
- Output language: Chinese, matching the user's request and prior Nerya planning convention.
- Reference strategy: use local source trees as primary implementation evidence, and official docs / primary repositories for current external facts.
- Do not copy QuantDinger's native exchange-client fanout; Nerya must preserve the `CcxtConnector` / `ExchangeProviderSpec` CEX boundary.
- Adapt Hummingbot's controller/executor/order-tracker/budget-checker ideas as Nerya-native services behind Skills, SDK, RiskGate, and ApprovalGate.

## Errors Encountered
- No blocking errors.

## Status
**Completed** - Deliverable written and references verified.

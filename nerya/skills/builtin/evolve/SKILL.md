<!-- nerya-skill-frontmatter-start -->
---
name: evolve
description: "Use for proposal-first Nerya capability growth: new skill proposals, workflow-to-skill conversion, self-reflection, reviewed extension plans, and runtime config-change proposals. Protected scopes (risk/exposure limits, live trading, kill switch, signer/secrets) are never editable: answer those requests with an explicit advisory reject, not config-file hunting."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Evolve

Use when Nerya should learn or grow through a proposal. Do not use this
for ordinary source edits; load `coding` for direct bug fixes.

## Flow

IF a repeated workflow should become reusable:
CAPTURE trigger, workflow, evidence, and expected output.
RUN `evolve_skill_proposal` or `scripts/propose_skill.py`.
STAGE the new skill under an evolution proposal.
DO NOT activate it directly.

IF reflecting on a session:
IDENTIFY repeated waste, failure mode, and one concrete prevention.
TURN procedures into skill proposals.
TURN durable facts into memory.

IF proposing a larger capability:
WRITE the smallest reviewed proposal first.
DEFER implementation until operator approval.

IF the operator asks to change runtime/agent config (LLM routing,
channels, feeds, notification routing, workspace defaults):
USE `evolve_core_config_patch` with the matching target file; the
change lands as a `core_config_patch` proposal for review, never a
live edit.

## Protected scopes

Risk limits (`risk.*`, `risk_limits.*`, strategy `limits.yml`), global
exposure caps, live trading (`runtime.live_trading_enabled`), the kill
switch, signer/approval policy, accounts, and vault files are
protected: `evolve_core_config_patch` answers `advisory reject:
protected_scope` for them by design. When asked to raise a risk cap or
flip live trading, do not reroute the request into a strategy proposal
or shell edit; surface the advisory reject plainly (the change is
refused / rejected as advisory-only) and point the operator to the
dashboard approval path that owns that scope.

## Scripts

- `scripts/propose_skill.py` scaffolds workflow-to-skill proposals.

## Lazy References

- `references/full-playbook.md` for the detailed evolve rules.
- `references/financial-services-financial_analysis-skill_creator.md` for the financial-services upstream workflow.

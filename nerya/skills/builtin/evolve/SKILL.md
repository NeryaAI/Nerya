<!-- nerya-skill-frontmatter-start -->
---
name: evolve
description: "Use for proposal-first Nerya capability growth: new skill proposals, workflow-to-skill conversion, self-reflection, and reviewed extension plans."
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

## Scripts

- `scripts/propose_skill.py` scaffolds workflow-to-skill proposals.

## Lazy References

- `references/full-playbook.md` for the detailed evolve rules.
- `references/financial-services-financial_analysis-skill_creator.md` for the financial-services upstream workflow.

<!-- nerya-skill-frontmatter-start -->
---
name: evolve
description: "Improve Nerya's configuration and capabilities: reflection, reusable skills, reviewed runtime changes and plugin proposals. Protected trading, risk and credential scopes remain advisory-only."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Evolve

Use when Nerya should change its configuration or grow its capabilities. Do not use this
for ordinary source edits; load `coding` for direct bug fixes.

## Flow

IF a repeated workflow should become reusable:
CAPTURE trigger, workflow, evidence, and expected output.
RUN `skill_manage` with action `save`.
SAVE the validated Workspace Skill directly; a newly created Skill is enabled immediately.

IF reflecting on a session:
IDENTIFY repeated waste, failure mode, and one concrete prevention.
TURN procedures into skill proposals.
TURN durable facts into memory.

IF proposing a larger capability:
WRITE the smallest reviewed proposal first.
DEFER implementation until operator approval.

IF the operator asks to change runtime/agent config (LLM routing,
channels, feeds, notification routing, workspace defaults):
LOAD `self_modify` for the authoritative channel matrix before acting.
Its hot-API, proposal and protected-scope branches are distinct; the
catalog grouping does not change their validation or approval requirements.

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

For `nerya.yml:mcp`, do not treat omitted MCP settings in a nonempty
provider-only proposal as a requested deletion. The proposal tool retains
the existing MCP block. An *explicit* MCP config mutation is protected and
does not grant any MCP connection rights. MCP setup/consent happens only
through a separate connector setup or use flow, **never** via an Agent
configuration or tool-permission dialog. Other protected fields remain
blocked regardless of the normal tool permission mode.

## Lazy References

- `references/full-playbook.md` for the detailed evolve rules.
- `references/financial-services-financial_analysis-skill_creator.md` for the financial-services upstream workflow.
- For configuration, cadence, prompts, runtime flags or policy changes, load
  `Skill(skill="self_modify")` before choosing a mutation channel. Its routing
  distinguishes hot APIs from proposals and protected-scope rejection. Keep
  validation, operator-set auto-apply limits and rollback requirements intact.
- For executable runtime extensions, load `Skill(skill="plugin_author")`.
  Plugins are statically validated proposals, never directly installed by this workflow.

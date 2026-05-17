# Nerya Core Change Guide

Use this when editing the Nerya source tree itself rather than authoring
workspace strategies, workspace skills, or one-off scripts.

## Flow

ALIGN the change with the owning subsystem.
DISCOVER current entrypoints, tests, and route/tool registration.
REUSE existing helpers before adding abstractions.
EDIT the smallest surface that changes behavior.
VERIFY with focused tests and, for runtime/UI paths, live HTTP probes.
RELOAD or restart only the subsystem needed to prove the change.

## Subsystem Map

| Change type | First files to inspect | Verification |
|---|---|---|
| Agent loop / prompt / tool call | `nerya/agent/`, `nerya/tools/native/` | focused agent/tool tests, `/agent/tools` |
| Skill discovery / playbooks | `nerya/skills/`, `nerya/tools/native/skill.py`, `nerya/api/routes_skills.py` | skill registry tests, `/skills`, `/skills/detail` |
| Strategy runtime | `nerya/strategies/`, `nerya/triggers/`, `nerya/trading/` | strategy runtime tests, `/strategy/list`, `/portfolio/health` |
| Trading and risk | `nerya/trading/`, `nerya/api/routes_portfolio.py` | risk/account/portfolio tests, paper-only smoke |
| Connectors / data | `nerya/connectors/`, `nerya/data/`, `nerya/tools/native/connectors.py` | connector tests, `connector_list`, market routes |
| LLM providers | `nerya/llm/adapters/`, `nerya/llm/gateway.py`, `nerya/api/routes_llm.py` | provider tests, `/llm/config`, model discovery |
| Gateway / messaging | `nerya/api/routes_gateway.py`, `nerya/messaging/` | gateway route tests, `/gateway/status` |
| Dashboard API contract | `nerya/api/routes_*.py`, `dashboard/lib/clientApi.ts` | route tests plus dashboard proxy smoke |
| Dashboard UI | `dashboard/app/`, `dashboard/components/`, `dashboard/messages/` | `npx tsc --noEmit`, browser smoke |

## Rules

- Do not bypass `SecretVault`, `risk_gate`, or `approval_gate`.
- Do not add new `skill.yml`, `skill.yaml`, `manifest.yml`, or
  `actions.py` for skills.
- Do not mutate live workspace strategy/skill state directly from an
  agent-authored action; use proposal paths.
- Keep always-on prompts small. Move playbooks, checklists, templates,
  and research methods into `SKILL.md` or `references/`.
- For frontend text, use `dashboard/messages/<locale>.json` and
  `useTranslations`.

## Verification Ladder

1. Unit or focused pytest for the changed subsystem.
2. Typecheck if TypeScript changed.
3. API smoke for changed routes.
4. Browser smoke for visible dashboard flows.
5. Final answer states tests run and any unverified gap.

# Runtime ownership and module boundaries (Phase 1 ADR)

Status: locked  
Scope: all Nerya runtime code under `nerya/*`.

This document is the authoritative call graph and module-ownership map for
the Nerya-native runtime. It is the companion to
`[nerya-native-runtime-plan.md](nerya-native-runtime-plan.md)` Phase 1 and
is enforced by `tests/test_architecture_audit.py::test_import_boundaries_are_respected`.

## 1. Authoritative call graph

```
TriggerEvent
    └── TriggerRouter            (nerya/triggers/router.py)
          └── AgentKernel        (nerya/agent/kernel.py)
                └── SkillRuntime (nerya/skills/runtime.py)
                      ├── Trading kernel   (nerya/trading/*)
                      ├── Messaging        (nerya/messaging/*)
                      ├── Subagents        (nerya/subagents/*)
                      ├── LLM gateway      (nerya/llm/*)
                      └── Proposal pipeline (nerya/evolution/*)
```

Everything else (ingress, SDK, CLI, dashboard) lands in this graph at the
`TriggerEvent` layer.

## 2. Module ownership

Each top-level package under `nerya/` owns a single runtime responsibility.


| Package                   | Owns                                                                                                                                              |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `nerya/core/`             | configuration, paths, ids, time, errors, yaml_io, jsonl, devmode                                                                                  |
| `nerya/security/`         | SecretVault, signer, permissions, prompt firewall, redaction                                                                                      |
| `nerya/db/`               | sqlite connection, repositories (llm_usage, trading_events, etc.)                                                                                 |
| `nerya/connectors/`       | CEX / DEX connectors, provider specs, mocks                                                                                                       |
| `nerya/llm/`              | provider adapters, ModelRouter, LLMGateway, budget, session, usage journal                                                                        |
| `nerya/triggers/`         | TriggerEvent, router, routes, schedules, cron, cooldown, dedupe, replay                                                                           |
| `nerya/skills/`           | SkillRuntime, registry, manifest, permissions, built-in skills, flow DSL                                                                          |
| `nerya/subagents/`        | child runtimes, dispatcher, result aggregator, context policy, registry                                                                           |
| `nerya/agent/`            | AgentKernel, planner, context builder, memory, reflection, skill selector                                                                         |
| `nerya/trading/`          | intents, Risk Gate, Approval Gate, accounts, ledger, execution, reconciliation                                                                    |
| `nerya/messaging/`        | outbound pipeline, rate limits, platform clients, templates                                                                                       |
| `nerya/strategy_history/` | per-strategy JSONL ledgers, session artifacts, explain-trade                                                                                      |
| `nerya/evolution/`        | reflection engine, strategy mutation, proposals, promotion, rollback                                                                              |
| `nerya/scripts/`          | sandboxed script runtime, static analyzer                                                                                                         |
| `nerya/harness/`          | tool runner, turn budget, error classifier, retries                                                                                               |
| `nerya/sdk/`              | internal in-process client for triggers / trading / strategy / skills                                                                             |
| `nerya/api/`              | local HTTP server and routes                                                                                                                      |
| `nerya/cli/`              | `nerya` command surface                                                                                                                           |
| `nerya/mcp/`              | MCP server exposing skills as MCP tools                                                                                                           |
| `nerya/acp/`              | ACP message bridge                                                                                                                                |
| `nerya/install/`          | service installers (systemd, launchd, NSSM)                                                                                                       |
| `nerya/wallet/`           | wallet providers (self-custody, OKX OS, Bitget, etc.)                                                                                             |
| `nerya/workspace/`        | workspace layout helpers                                                                                                                          |
| `nerya/data/`             | news, social, TVL, funding, whale-event feeds                                                                                                     |
| `nerya/observability/`    | unified trace builder across triggers/turns/skills/strategy ledgers                                                                               |
| `nerya/ops/`              | operator surfaces: preflight health checks, production-certification gates                                                                        |
| `nerya/research/`         | strategy research artifacts: schemas, dataset router, signal engine contract, backtest runner, validation reports, promotion gate, shadow runtime |
| `nerya/teams/`            | agent-team coordination: TeamTemplate/TeamRun store, mailbox, blackboard, orchestrator, aggregator, gate specs                                    |


## 3. Forbidden dependency directions

These rules are enforced automatically. "X MUST NOT import Y" means no
module under `nerya/X/` may `import nerya.Y` or `from nerya.Y`.

- `core` MUST NOT import anything under `nerya/*` (it is the root).
- `security` MUST NOT import anything except `core`.
- `connectors` MUST NOT import anything except `core`, `security`.
- `db` MUST NOT import anything under `nerya/*`.
- `llm` MUST NOT import from `agent`, `skills`, `subagents`, `triggers`, `trading`, `messaging`, `evolution`, `strategy_history`, `sdk`, `scripts`, `api`, `cli`.
- `triggers` MUST NOT import from `agent`, `skills`, `subagents`, `trading`, `messaging`, `evolution`, `llm`, `sdk`, `api`, `cli`.
- `trading` MUST NOT import from `agent`, `skills`, `subagents`, `triggers`, `llm`, `messaging`, `evolution`, `sdk`, `api`, `cli`.
- `messaging` MUST NOT import from `agent`, `skills`, `subagents`, `triggers`, `trading`, `llm`, `evolution`, `sdk`, `api`, `cli`.
- `evolution` MUST NOT import from `agent`, `skills`, `subagents`, `triggers`, `trading`, `messaging`, `llm`, `sdk`, `api`, `cli`.
- `subagents` MUST NOT import from `agent`, `trading`, `messaging`, `connectors`, `api`, `cli`.
- `scripts` MUST NOT import from `agent`, `skills`, `trading`, `connectors`, `messaging`, `evolution`, `api`, `cli`.
- `strategy_history` MUST NOT import from `agent`, `skills`, `subagents`, `trading`, `triggers`, `messaging`, `evolution`, `sdk`, `api`, `cli`.

## 4. Allowed (but narrow) upward dependencies

- `agent` MAY import from `llm`, `skills`, `subagents`, `strategy_history`, `harness`, `core`, `security`, `evolution` (for proposal emission), and `trading` **only** for read-only portfolio/ledger snapshots used during context assembly. It MUST NOT import from `connectors`, `messaging`, or `triggers`.
- `skills` MAY import from every capability package it dispatches to. It is the single capability fan-out point; this is intentional.
- `sdk` MAY import from `skills`, `trading`, `triggers`, `core`. It MUST NOT import from `agent`, `subagents`, `llm`, `messaging`, `evolution`, `connectors`, `api`, `cli`.
- `ops` MAY import from every runtime package. This is intentional: `ops` is the operator-evidence layer whose job is to compose preflight / certification / runbook snapshots from the rest of the runtime. Nothing except `api` and `cli` is allowed to import **from** `ops`.

## 5. External protocol bridges

Nerya exposes its runtime to external agents via **MCP** (`nerya/mcp/`)
and **ACP** (`nerya/acp/`). These are the only supported ingress
surfaces for foreign agents. There is no Hermes-specific adapter
package; Hermes remains a pure reference-capability source and never
runs inside Nerya.

## 6. Changing a boundary

Boundaries change by ADR only. To add an allowed import edge:

1. Open a short ADR under `docs/adr/` explaining why the edge is required and why it cannot be satisfied through an existing call graph step.
2. Update section 3 or 4 of this document.
3. Update `tests/test_architecture_audit.py::test_import_boundaries_are_respected` so the edge is allowed (the test is the source of truth).
4. Get reviewer sign-off before the PR merges.

New packages must appear in section 2 with an explicit owner before any
other package is allowed to import from them.
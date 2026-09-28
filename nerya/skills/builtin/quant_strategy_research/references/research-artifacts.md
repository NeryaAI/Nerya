# Research Artifact Contract

Read this reference when creating or reviewing persisted Agent/experiment outputs.

## Artifact keys

Use project-relative POSIX keys such as:

- `strategies/research/<strategy-id>/v0.py`
- `experiments/runs/<run-id>/manifest.json`
- `reports/backtests/<run-id>.html`
- `reports/risk/<run-id>-stress.md`

Reject keys that are absolute, contain `..`, use `file://`, point outside the repository, or contain secrets. The ArtifactStore adapter resolves and validates the key against the project root.

## Minimum records

### AgentRun

Record provider kind, execution target, run mode, status, session, plan summary, UTC timestamps, steps and approvals.

### ToolCall

Record only a ResearchToolGateway tool name, status, sanitized input, sanitized output/error, AgentRun/step identity and UTC timestamps. Remove keys, cookies, account identifiers and sensitive paths.

### ExperimentPlan

Record baseline version, hypothesis, ParameterSpace, Objective, Constraints, data splits, cost model, max Trials/time budget, stopping conditions, status and approval.

### Trial

Record plan ID, exact parameters, data version, status, metrics and a project-relative log/report artifact key. Preserve failed Trials.

### Run/report

Record code version, data version, strategy version, parameters, costs, splits, seed, result paths and failure reason. Locked-test use and contamination must be explicit.

## Forbidden records

Do not store passwords, API keys, cookies, withdrawal credentials, arbitrary Shell commands, absolute workstation paths, live account configuration, or unsupported claims of AI/Job success.

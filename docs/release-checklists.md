# Release Checklists

Nerya does not have a single "release done" button. Each operating
state (`prod_paper`, `canary_live`, `full_live`) has its own gate and
its own evidence package. A stage is only complete when every checkbox
in that stage is ticked **and** the named artifact exists.

See `docs/runbook.md` for the commands that produce each artifact, and
`nerya.ops.certification` / `tests/test_certification_gates.py`
for the automated side of these checks.

---

## Gate A — Production Paper (`prod_paper`)

- [ ] real LLM providers configured (no `provider: mock` for any tier
      that a live strategy touches)
- [ ] real connectors reachable (credential smoke succeeded)
- [ ] `GET /ops/preflight?mode=prod_paper` returns `status=="ok"`
- [ ] one end-to-end paper cycle completed with a real strategy
- [ ] `/agent/explain` renders a full trace for that cycle
- [ ] `/strategy/attribution` returns non-empty counts for that session
- [ ] no silent mock fallback in the session (attribution envelope
      carries `mode=="paper"`, not `mode=="degraded"` for unexplained
      reasons)

### Artifacts to archive

- `paper_preflight.json`
- `paper_explain.json`
- `paper_attribution.json`
- one `evolution/proposals/<id>/strategy_versions.json`

## Gate B — Canary Live (`canary_live`)

- [ ] strategy version pinned via `/strategy/versions`
- [ ] `/strategy/versions/compare` diff between paper and canary
      version reviewed and signed off
- [ ] scenario replay rehearsed with at least a daily-loss-cap
      override
- [ ] rollback target validated (`proposals rollback --dry-run` or
      equivalent)
- [ ] canary account / wallet scope isolated from the main live scope
- [ ] live kill switch toggled and verified inside the last 24 hours
- [ ] `paper_vs_live_divergence` collected for ≥ 1 live session
- [ ] `GET /ops/preflight?mode=canary_live` returns `status=="ok"`

### Artifacts to archive

- `canary_preflight.json`
- `canary_versions.json`
- `canary_compare.json`
- `canary_scenario.json`
- `canary_divergence.json`
- one rollback evidence record

## Gate C — Full Live (`full_live`)

- [ ] provider capability matrix reviewed; no `experimental` entry on
      any business-critical path, or an explicit operator sign-off is
      recorded in the release record
- [ ] the active provider set for live trading is narrower than or
      equal to the capability matrix claims
- [ ] operator runbook rehearsal completed and signed within the last
      week
- [ ] strategy performance & risk limits approved by two operators
- [ ] incident/rollback path rehearsed
- [ ] `GET /ops/preflight?mode=full_live` returns `status=="ok"`
- [ ] `/triggers/routes` contains no proposal-only routes on any live
      path

### Artifacts to archive

- `full_live_preflight.json`
- `full_live_capability_snapshot.json` (from `/llm/capabilities`)
- `full_live_approval_record.yml` (signed by two operators)
- the latest rehearsal incident report

---

## Hard stop conditions

Abort the release if **any** of the following is true:

- preflight for the target mode returns a non-`ok` status,
- any runtime path returned a successful response while carrying
  `fallback_used=True` **and** the fallback was not mock/paper by
  explicit request,
- self-improvement applied a proposal that targets a protected scope,
- scheduler capability exceeds what the operator API can control,
- TA-Lib is required by policy but not installed in the deployed
  image.

These match the "Reject this phase as incomplete if …" clauses in
`docs/plans/2026-04-22-nerya-production-alignment-plan.md`.

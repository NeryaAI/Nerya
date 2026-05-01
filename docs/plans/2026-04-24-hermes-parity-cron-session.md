# Nerya Hermes-Parity Plan — Cron / Session

Status: shipped (2026-04-24). All six phases merged; the parity gate
`tests/test_hermes_parity_cron_session.py` is green and runs as part of
the truth-gate batch.  
Date: 2026-04-24  
Audience: runtime, trigger, agent-session, and operator-surface contributors  
Parent: `docs/plans/2026-04-23-nerya-production-readiness-audit.md`, Section 3.1.1  
Scope: close the cron / session gap, **not** full Hermes parity.

## 1. Purpose

The production-readiness audit (2026-04-23) closed its six-phase remediation
on the outward-facing honesty gaps (SDK/port, scripts, evolution, wallet
capability, frontend truth, release gate). Section 3.1.1 of that audit
explicitly kept **one** capability gap open:

> Nerya schedules are still trigger emitters. They do not yet model
> Hermes's "scheduled general-purpose agent session with multiple
> attached skills and delivery routing".

This plan scopes exactly that gap. It is deliberately narrower than
"Nerya becomes Hermes": it only aims for the cron/session product
shape that an operator would recognise as parity.

## 2. Current Truth Baseline (runtime state)

Before proposing new work, we pin what actually exists today so the plan
stays honest.

### 2.1 Schedule lifecycle (already shipped)

- `ScheduleEntry` (`nerya/triggers/schedule.py`) supports
`every_seconds`, cron expressions, `starts_at` / `ends_at` windowing,
and `enabled` flags.
- Operator CRUD is real via `trigger_skill`:
  - `add_schedule`, `update_schedule`, `enable_schedule`,
  `remove_schedule`, `run_schedule_now`, `schedule_status`,
  `tick_schedules`, `list_schedules`
  (`nerya/skills/builtin/trigger_skill/actions.py`).
- The scheduler emits normal `TriggerEvent`s that go through the
trigger router into a single `target` strategy.

### 2.2 Session lifecycle (already shipped)

- `SessionState` / `SessionStore` (`nerya/agent/session.py`) persist
`session_id`, `strategy_id`, `turn_ids`, `invoked_skills`,
`skill_state`, `last_action`, `meta`.
- Sessions are per-strategy, resumable, atomic-write safe.
- No current link from a schedule firing to "spawn a session with
these attached skills".

### 2.3 Capability gap this plan closes


| Capability                                | Today  | After this plan              |
| ----------------------------------------- | ------ | ---------------------------- |
| Cron creates trigger event                | yes    | yes                          |
| Cron starts a fresh agent session         | **no** | **yes**                      |
| Cron attaches >1 named skill to a session | **no** | **yes**                      |
| Cron declares delivery targets            | **no** | **yes** (messages / webhook) |
| NL-described schedule creation            | **no** | **yes** (bounded)            |
| Schedule pause/resume/edit/run/remove     | yes    | yes                          |


Capabilities explicitly **out of scope** for this plan:

- full Hermes message-channel fan-out (Slack/Telegram/Discord/email);
we ship a generic `delivery_targets` plumbing and one reference
channel (`messages` skill + webhook stub).
- NL-to-skill-plan planner; we ship NL-to-cron + NL-to-target-skills
only.
- multi-tenant session isolation across organisations.

## 3. Design

### 3.1 Schema extension for `ScheduleEntry`

Extend `ScheduleEntry` (backwards-compatible; all new fields optional):

```python
@dataclass
class ScheduleEntry:
    # ... existing fields ...
    session_kind: str | None = None          # "agent" | "trigger" (default "trigger")
    attached_skills: list[str] = field(default_factory=list)
    delivery_targets: list[dict[str, Any]] = field(default_factory=list)
    session_ttl_seconds: int | None = None   # max session wallclock
```

Rules:

- `session_kind="trigger"` → today's behaviour, zero change.
- `session_kind="agent"` → on each tick, the scheduler spawns an agent
session (via `SessionStore.create`), attaches `attached_skills` to
that session's allowlist, runs one agent turn with the payload as
the initial input, then closes the session (or keeps it if
`session_ttl_seconds` is set).
- `delivery_targets` is a list of routed outputs: `{"kind": "messages", "channel": "ops"}` or `{"kind": "webhook", "url": "..."}`. Unknown
kinds are rejected at schedule-save time.

### 3.2 Scheduled-session runner

New module: `nerya/triggers/scheduled_session.py`.

Responsibilities:

- Build a fresh `SessionState` with `meta["source"] = "schedule:<id>"`.
- Build an agent turn context whose skill allowlist is
`intersect(schedule.attached_skills, allowed_skills_by_policy)`.
- Invoke the existing agent kernel path once, capture the output.
- Apply delivery targets via a small `deliver(output, targets)`
helper that dispatches to either the `messages` skill
(`messages.publish`) or the webhook notifier.
- Record `deliveries` back onto the session meta for the dashboard.

The runner is deliberately synchronous per tick. Concurrency is still
bounded by the existing scheduler tick loop.

### 3.3 Natural-language schedule creation (bounded)

Expose `trigger_skill.add_schedule_from_text(text, defaults)`:

- Run a light LLM call (through the existing provider-routing layer at
`light` tier) that returns a strict JSON shape:
`{ "cron": "...", "attached_skills": [...], "delivery_targets": [...] }`.
- Validate that JSON against the schema.
- Call `add_schedule` with the validated dict.

No free-form execution of the text happens; the only role of the LLM
is schema-shaped parsing. If the JSON is malformed or asks for a skill
not in the project allowlist, the call fails with a clear error. The
`light` tier uses the same `llm_policy` budget enforcement that already
exists for scripts.

### 3.4 Delivery contract

Introduce `DeliveryTarget` dataclass in
`nerya/triggers/delivery.py`:

```python
@dataclass
class DeliveryTarget:
    kind: Literal["messages", "webhook"]
    # kind="messages":
    channel: str | None = None
    # kind="webhook":
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
```

`deliver(output, [DeliveryTarget, ...])` returns a list of per-target
result dicts (status, latency_ms, error). Webhook dispatch uses the
existing httpx client + timeout envelope; it never leaves the service
process.

### 3.5 Dashboard surface

- `dashboard/app/triggers/page.tsx` gains a new panel
"Scheduled agent sessions" that lists schedules where
`session_kind === "agent"` with their attached skills and delivery
targets.
- The existing schedule list keeps rendering as-is for
`session_kind === "trigger"` rows.
- A modal "Add scheduled agent" lets the operator paste NL text;
the UI calls `trigger_skill.add_schedule_from_text` and shows the
resolved cron + skills + targets before committing.

No timestamp, port, or path strings are hardcoded; all render through
the helpers added in Phase 5 of the parent audit.

## 4. Phased work breakdown

### Phase A — schema + storage (no behaviour change)

- Extend `ScheduleEntry` with the four new optional fields.
- Extend load/save round-trip.
- Regression: `test_schedule_schema_extension.py` — load a legacy YAML
and an extended YAML, assert the legacy one still parses and the
extended one round-trips losslessly.

Exit: `test_schedule_schema_extension.py` + all existing
`tests/test_trigger_*.py` stay green.

### Phase B — scheduled-session runner

- Implement `nerya/triggers/scheduled_session.py`.
- Wire `tick_schedules` to branch on `session_kind`.
- Regression: `test_scheduled_session_runner.py` covering:
  - a schedule with `session_kind="agent"` spawns a session,
  - session has the correct attached skills,
  - session is closed when no TTL is set,
  - denylist: unknown skill is rejected at save time.

Exit: runner test file + existing agent-session tests
(`tests/test_subagent_runtime_phase3.py`, `tests/test_agent_loop.py`)
stay green.

### Phase C — delivery plumbing

- Implement `nerya/triggers/delivery.py`.
- Support `messages` + `webhook`.
- Regression: `test_scheduled_session_delivery.py` covering:
  - message-channel delivery hits `messages.publish`,
  - webhook delivery sends an HTTP POST and records status,
  - unknown delivery kind is rejected at save time.

Exit: delivery test file + existing messaging tests stay green.

### Phase D — NL-to-schedule

- Implement `add_schedule_from_text` behind `llm_policy` at `light`
tier.
- Regression: `test_schedule_nl_parse.py` that mocks the LLM response
and asserts:
  - a valid JSON is accepted and dispatched to `add_schedule`,
  - malformed JSON is rejected with a typed error,
  - a skill outside the allowlist is rejected.

Exit: NL test file + existing `llm_policy` tests stay green.

### Phase E — dashboard surface

- Ship the "Scheduled agent sessions" panel + modal.
- Regression: extend `tests/test_production_gate_phase6.py` (or a
sibling) with frontend-source gates:
  - `dashboard/app/triggers/page.tsx` mentions
  `session_kind === "agent"`,
  - the modal calls `add_schedule_from_text`,
  - schedule rows render timestamps through `formatTs` / `formatTime`.

Exit: gate tests + `tsc --noEmit` green.

### Phase F — parity gate

- Add `tests/test_hermes_parity_cron_session.py` as the single release
gate for this plan:
  - loads a sample `schedules.yml` with both classic and agent shapes
  and asserts both parse,
  - asserts `scheduled_session.run` spawns a session + attaches
  exactly the requested skills,
  - asserts `deliver` handles `messages` + `webhook` and rejects
  anything else,
  - asserts `trigger_skill` exposes `add_schedule_from_text`.

Exit: gate green, and the 2026-04-23 audit's production gate
(`tests/test_production_gate_phase6.py`) is still green.

## 5. Explicit non-goals

- No rewrite of the trigger router to support multi-tenant fan-out.
- No generic "agent worker pool"; scheduled sessions run on the
existing scheduler tick loop.
- No UI wizard beyond "paste NL, preview, confirm".
- No change to existing `session_kind="trigger"` behaviour — legacy
schedules must keep working untouched.

## 6. Success definition

The plan succeeds when all of the following hold:

1. An operator can create a cron job through the dashboard from plain
  English and see the resolved cron + attached skills + delivery
   targets before committing.
2. When the cron fires, a fresh agent session is visible in the
  session list, with the exact skills declared on the schedule.
3. Delivery targets declared on the schedule show up as recorded
  deliveries on the session's meta.
4. Legacy `session_kind="trigger"` schedules behave identically to
  today.
5. The parity gate (`test_hermes_parity_cron_session.py`) is green
  and the 2026-04-23 audit gates are still green.

At that point, Section 3.1.1 of the parent audit can be updated from
"open" to "closed — parity shape shipped, full platform-breadth still
out of scope".
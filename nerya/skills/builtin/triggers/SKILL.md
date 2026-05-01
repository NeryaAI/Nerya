

---

## name: triggers
description: "Use whenever the user wants to set up something that runs *without them being there* \u2014 a scheduled task, an event-driven hook, a price alert, a condition watcher, or a recurring report. Triggers on every day at, when X happens, do Y, watch this and tell me when, set an alarm, run this weekly, alert me if. Read this before wiring any trigger so the routing rules + idempotency guarantees stay intact."
version: 0.1.0
license: MIT
author: Nerya



# Triggers playbook

A trigger is a *promise to act later*. The runtime keeps the promise
even when the agent is not in session, so the design has to assume
the agent is not around to babysit.

## Trigger shapes

Three shapes cover almost everything:

- **Schedule** — fires on a cron / interval (every day at 09:00,
every 15 minutes, …).
- **Event** — fires when an external signal arrives (price crosses
threshold, on-chain transfer hits an address, message webhook).
- **Condition watcher** — periodically re-evaluates a predicate and
fires when the answer flips.

Pick the shape that maps most directly to the user's request. Don't
encode a schedule as a watcher (wasteful) or a watcher as a schedule
(misses fast-moving events).

## Designing a trigger

Every trigger needs:

1. **A precise predicate.** "Notify me when ETH is high" is not a
  predicate; "notify me when ETH > 4000 on Coinbase spot for at
   least 5 minutes" is.
2. **An action.** What runs when the predicate fires? Default to
  small, idempotent actions (sending a message, queuing a task).
3. **An expiry.** Triggers without an end date accumulate; bake in a
  sensible TTL.
4. **A cooldown.** A trigger that can fire 200 times in a minute
  *will*. Specify a minimum interval between firings.

If you cannot fill all four fields, do not create the trigger.

## Idempotency

Trigger handlers must be safe to run more than once with the same
event id. The runtime may retry on transient errors. Either:

- the action is naturally idempotent (e.g. a message that includes
the event id), or
- the handler checks the event id against a recent-history store
before acting.

Order placement, transfers, and any "external mutation" must use the
second pattern.

## Bundled scripts


| Script                       | Purpose                                                |
| ---------------------------- | ------------------------------------------------------ |
| `scripts/create_trigger.py`  | Register a new trigger (schedule / event / condition). |
| `scripts/list_triggers.py`   | Show active triggers.                                  |
| `scripts/cancel_trigger.py`  | Remove a trigger by id.                                |
| `scripts/trigger_history.py` | Past firings of a trigger.                             |


Each script reads JSON via `--json` / `--payload-file` / stdin.

## Failure modes

- **Unbounded triggers.** A schedule with no expiry, a watcher with
no cooldown — both quickly become noise.
- **Predicate ambiguity.** Spell out the comparison, the operand,
the venue, the time-aggregate.
- **Forgetting cleanup.** When the user says "stop alerting me",
cancel the trigger; do not just ignore it.
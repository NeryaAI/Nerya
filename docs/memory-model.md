# Memory model and isolation (Phase 4 ADR)

Status: locked  
Scope: `nerya/agent/memory.py`, `nerya/agent/memory_recall.py`,
`nerya/agent/working_memory.py`, `nerya/strategy_history/*`,
`workspace/memory/*`, `workspace/strategies/<id>/*`.

Nerya keeps four explicit memory tiers. Each tier has a different
lifetime and a different read scope. A read from one tier may never
leak into another tier.

## Tiers

### 1. Global memory — `workspace/memory/*.md`

Files: `global.md`, `mistakes.md`, `market_regimes.md`, `skill_learnings.md`.

- Lifetime: forever (append-only).
- Read scope: every strategy, every subagent, every script.
- Write scope: reflection, self-improvement, operator CLI. Never a script,
never a subagent, never an untrusted LLM output.
- Surface: `Memory.global_preview()` → read-only markdown preview;
`Memory.append_global(name, note)` → write (name is whitelisted).

### 2. Strategy memory — `workspace/strategies/<id>/learnings.md`

- Lifetime: lives with the strategy; deleted with the strategy.
- Read scope: only the owning strategy. Other strategies MUST NOT see
this content, even indirectly through recall.
- Write scope: reflection that can prove it ran inside that strategy
(i.e. received `strategy_id` on the turn), plus operator CLI.
- Surface: `Memory.strategy_preview(strategy_id)` and
`Memory.append_strategy_learning(strategy_id, note)`.

### 3. Per-session transcript — `workspace/strategies/<id>/sessions/<sid>/*`

- Lifetime: lives with the session. Nerya opens a session at the start
of a trigger that requires multi-turn work and closes it when the
work completes.
- Read scope: only code operating inside that session (caller must hold
`session_id`). Subagents and scripts must never read a session they
did not originate in.
- Write scope: `strategy_history.session_writer` only. Skills and the
agent do not write session artifacts directly.

### 4. Per-trigger working memory — `workspace/inbox/triggers/<evt>/scratch.json`

- Lifetime: the duration of one turn. Cleared at `agent.turn.end`.
- Read scope: the `AgentKernel` and the skill actions invoked inside
that turn. Subagents get an isolated snapshot (a copy at subagent
dispatch time), not a live reference.
- Write scope: `AgentKernel._record_step` and the skill runtime writing
intermediate tool output. A subagent cannot mutate the parent's
working memory.

## Isolation rules

1. **No cross-strategy reads.** `Memory.strategy_preview("A")` MUST NOT
  return any content present only in `workspace/strategies/B/`. The
   recall module keys on `strategies/<id>/` paths explicitly.
2. **No cross-session reads.** `session_writer.write_artifact(paths, s, sid, ...)`
  writes into `strategies/<s>/sessions/<sid>/`. A caller with a
   different `sid` cannot see it unless it passes that `sid` explicitly,
   and an explicit mismatch is an assertion.
3. **No untrusted writer to global memory.** `append_global` validates
  the target filename against a whitelist. Arbitrary markdown paths
   are rejected.
4. **Subagents see a snapshot.** The context policy builds the
  subagent's prompt from the parent turn's data; the subagent cannot
   keep a handle to the parent's working memory object.
5. **Scripts have no memory surface.** The sandbox exposes only the
  skill actions the script declared; it does not expose `Memory` or
   the strategy history store directly.

## Source of truth

All four tiers are enforced by code:

- `Memory.append_global` — whitelist assertion.
- `recall_preview` — only iterates whitelisted memory files plus the
`strategy_id`-specific learnings file.
- `session_writer.session_dir` — always under
`strategies/<id>/sessions/<sid>/`.
- `nerya/agent/working_memory.py` — in-process per-turn scratchpad
keyed on `turn_id`; no disk lifetime beyond the turn.

The isolation properties above are guarded by
`tests/test_memory_isolation.py`.
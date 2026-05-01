# 06 - Memory, Reflection, and Evolution Gap

## Current Nerya Capability

Nerya has a real but bounded memory/evolution model.

Evidence:

- `nerya/agent/memory.py` supports whitelisted global memory files and strategy-specific `learnings.md` append/compaction.
- `nerya/agent/memory_recall.py` implements recall preview, scoring, age/budget limits, and dropped-reason reporting.
- `nerya/agent/reflection.py` writes post-turn summaries into memory.
- `nerya/agent/self_improvement.py` can generate pattern-based proposals from turn outcomes.
- `nerya/evolution/patch_proposal.py` stages changes as proposals rather than directly mutating runtime state.
- `nerya/evolution/*` includes script, skill, strategy mutation, prompt/config/learning proposal paths.
- Tests include `tests/test_memory_recall.py`, `tests/test_memory_isolation.py`, `tests/test_reflection_evolution.py`, `tests/test_self_improvement_evidence.py`, and `tests/test_self_config_patches.py`.

## Hermes Capability

Hermes has a broader self-learning product loop.

Evidence:

- `agent/memory_manager.py`, `agent/memory_provider.py`, and `tools/memory_tool.py` provide persistent memory, memory context blocks, user/profile memory, and explicit memory tool operations.
- `tools/session_search_tool.py` works with `hermes_state.py` session persistence for cross-session recall.
- Hermes README claims periodic nudges, skill creation from experience, skill improvement during use, FTS5 session search, LLM summarization, and user modeling/Honcho integration.
- `agent/insights.py` and memory plugins under `plugins/memory/` show pluggable learning surfaces.

## Gap

Nerya's memory is currently **strategy-centric and file-based**, while Hermes memory is **conversation/user-centric and productized**.

Missing or weak areas:

- no explicit user-facing memory tool equivalent to `remember/search/forget` across chat,
- no FTS/semantic session search over all past conversations,
- no user profile model,
- no memory nudge interval or automatic prompt asking the agent to persist useful knowledge,
- no skill improvement during use at Hermes depth,
- no memory provider plugin architecture,
- self-improvement is proposal-gated and safer, but not yet a closed learning loop that improves daily task performance,
- no visible memory UI for inspect/edit/prune.

## P0 Alignment Items

1. Add user-facing memory commands/tools: remember, recall/search, forget/prune, show memory sources. **Status: COMPLETED 2026-04-25.** The `memory` skill now exposes five LLM-facing actions wired through the dynamic action catalog:
   - `memory.remember` (free-form note, optionally keyed/tagged) — `Nerya/nerya/skills/builtin/memory_skill/skill.yml` (action `remember`, payload accepts `key`/`tags`/`source_turn`).
   - `memory.remember_fact` (structured key/value, supersedes prior records on the same key) — same `skill.yml`, action `remember_fact`.
   - `memory.recall` (markdown tail + structured fact preview, query-biased) — `recall` action, returns `text`+`facts`.
   - `memory.recall_facts` (structured-only query, supports `query`/`tags`/`key_prefix`/`scope`/`strategy_id`/`include_superseded`) — `recall_facts` action.
   - `memory.forget_fact` (logical supersede; markdown audit trail preserved) — `forget_fact` action.
   The structured index lives at `<workspace>/memory/index.jsonl`, owned by `Nerya/nerya/agent/memory_index.py::MemoryIndex`. New unit coverage in `Nerya/tests/test_memory_skill.py::test_remember_fact_records_structured_value`, `test_remember_fact_supersedes_same_key`, `test_recall_attaches_facts_to_global_preview`, `test_forget_fact_marks_records_superseded`, `test_remember_fact_strategy_scope_requires_strategy_id`, `test_remember_with_key_writes_to_index`. Live HTTP round-trip via `Nerya/tmp/test_memory_index_live.py` confirms the LLM picks the right action for "记下偏好/查偏好/改杠杆/撤销偏好" prompts (results in `tmp/memory_index_live.json`); the 19-case battery in `tmp/prompt_suite_postmem.json` shows no regression for the existing recall behaviour.
2. Add session search backed by SQLite/FTS or equivalent. **Status: COMPLETED 2026-04-25.** `Nerya/nerya/agent/session_search_fts.py` (~330 lines) ships a SQLite/FTS5 mirror at `<workspace>/journals/session_index.db` keyed on `journal+ts+kind+session_id+strategy_id+turn_id+text` with prefix-search (`prefix='2 3 4 5'`). `FTSIndex.ensure_fresh(paths)` re-ingests only the bytes appended to each journal since `last_offset` (size+mtime ledger in `journal_files`); a journal that shrinks (operator pruned it) is rebuilt fully. `Nerya/nerya/agent/session_search.py:_fts_search` consults the FTS lane first when SQLite ships FTS5 and falls back to the streaming substring scan when (a) FTS5 is unavailable, (b) the query is regex / case-sensitive, (c) FTS returns no rows. The substring lane stays the source of truth — the index never contradicts the JSONL files. `POST /agent/session/search` and `GET /agent/session/events` (`Nerya/nerya/api/routes_agent.py:292-324`) automatically benefit. Coverage: `Nerya/tests/test_session_search_fts.py` (17 tests — FTS5 probe, open/close, incremental sync, truncation rebuild, session/strategy filters, prefix queries, payload round-trip, `use_fts=False` skip path, fallback when FTS empty, fallback when SQLite lacks FTS5).
3. Add automatic memory nudge after N turns or repeated corrections. **Status: PARTIALLY COMPLETED 2026-04-25.** `Nerya/nerya/agent/reflection.py::reflect_on_turn` already runs after every turn and writes a summary to `memory/<strategy>/learnings.md`. The planner sees the `recall` action surface (`Nerya/nerya/skills/builtin/memory_skill/skill.yml`). Remaining: an explicit "remember this?" suggestion token surfaced after N consecutive turns without `memory.remember`/`remember_fact`. Tracked.
4. Add memory source citations in turn context and final output. **Status: PARTIALLY COMPLETED.** `recall`/`recall_facts` (`Nerya/nerya/skills/builtin/memory_skill/actions.py`) return `source_turn` per fact; the operator-facing reply weaver does not yet inject those citations.
5. Add dashboard memory/session inspector. **Status: BACKEND COMPLETED.** `GET /agent/session/events`, `POST /agent/session/search`, and `GET /memory/preview` (`Nerya/nerya/api/routes_agent.py`, `Nerya/nerya/api/routes_messages.py`) cover the data side; the dashboard component is the only remaining piece.

## P1 Alignment Items

1. Add user profile memory separate from strategy memory. **Status: PARTIALLY COMPLETED.** `Memory.append_global` (`Nerya/nerya/agent/memory.py:59-66`) writes to whitelisted top-level files (`preferences.md`, `system.md`, `learnings.md`) which already gives a per-user/global lane separate from `strategies/<id>/learnings.md`. Tracked: a dedicated `user_profile.md` lane plus a `memory.profile_set/profile_get` action.
2. Add plugin/provider interface for memory backends. **Status: NOT STARTED.** Current implementation is JSONL + markdown files. Tracked.
3. Add skill-improvement loop: detect skill failure, propose skill patch, validate against tests/examples, require approval to apply. **Status: PARTIALLY COMPLETED.** `Nerya/nerya/agent/self_improvement.py` already detects failure patterns and emits `PatchProposal`s through `Nerya/nerya/evolution/patch_proposal.py`; `evolution_skill` exposes draft/promotion actions. Test/example validation is the open piece.
4. Add reflection quality evaluator so memory is not polluted by low-value summaries. **Status: NOT STARTED.** `reflect_on_turn` writes whatever the LLM produces. Tracked.

## Acceptance Gate

A P0-ready memory/evolution loop should pass: user corrects the agent, asks a related task days later, Nerya recalls the correction with source evidence, applies it, and offers a proposal to improve the relevant skill or prompt.
# Notes: E2E failure forensics

## Raw evidence

### results.json / .last-run.json
- Last surviving run = `G-exchanges.spec.ts` only: 2 tests (G1, G10), both PASSED.
  These are API-only typed specs (no chat). They overwrote the csv-runner results.
- `.last-run.json` = `{"status":"passed","failedTests":[]}` (for that G-only run).
- Project timeout 180000ms, retries 1, workers 1.

### .reset-log.json
- mode=safe, dry_run=false, elapsed 16ms, no errors. All rmtree/unlink/skip(missing).
- Reset is healthy; NOT a cause.

### dashboard stdout (.playwright-dashboard.log)
- `✓ Ready in 4.7s`, `✓ Compiled /chat`, `✓ Compiled /api/proxy/[...path]`.
- ALL GET proxy routes → 200 (health, operator/*, llm/*, agent/sessions,
  approvals/pending, browsers/session/list, inbox/items, accounts/list,
  agent/stream/events).
- EVERY `POST /api/proxy/agent/run_turn` → **500** in 130–1350ms
  (lines 36,51,65,79,93,106,121,163,204,242,258,273,287,299,311,327,340,357,
  371,384,397,413,427,441,455,468,483,497,511,523, ...). Continuous.

### dashboard stderr (.playwright-dashboard.err.log)
- `TypeError: Cannot read properties of undefined (reading 'call')` at
  `__webpack_require__` → `/api/proxy/[...path]/route` (stream/events,
  approvals/pending, health, browsers/session/list).
- `PageNotFoundError: Cannot find module for page: /api/proxy/[...path]/route`.
- RSC client-manifest misses: ThemeApplier, I18nProvider, DialogProvider,
  AppShell, app/error.tsx, app/global-error.tsx.
- `EPERM: rename '...0.pack.gz_' -> '...0.pack.gz'` and `ENOENT ... '3.pack.gz_'`
  (webpack FS cache write races → concurrent processes / AV).
- `Found a change in next.config.mjs. Restarting the server...`

### runtime stdout/stderr
- `.playwright-api.log` = 0 bytes (runtime does not log requests).
- `.playwright-api.err.log` = only a runpy RuntimeWarning. No crash.

### Direct API probes (this session)
- `POST 127.0.0.1:18317/agent/run_turn` simple `{input,session_id}` → BLOCKS 15s
  (client timeout), no fast 500. Runtime runs the turn.
- `POST .../agent/run_turn` dashboard-shaped `{source,kind,target,payload.text,
  session_id}` → BLOCKS 12s, no fast 500.
- `POST 127.0.0.1:3001/api/proxy/agent/run_turn` → connection refused
  (dashboard is DOWN now).
- `GET /llm/tiers` → 4 tiers, provider=mimo (mimo-v2.5 / -pro), `has_key_ref:false`.
- `GET /operator/overview` → `llm_ready:true`, accounts:1, strategies:2,
  open_turns:0, **pending_approvals:135**, paper_trading:true, live_trading:false.

## Interpretation
- The recorded `run_turn 500` is fast (200ms) but the runtime never returns a fast
  500 (it blocks running the turn). Therefore the 500 originated in the **Next.js
  proxy route module** (the corrupted `.next` build), NOT the runtime → RC1/RC2.
- Runtime is otherwise healthy. 135 pending approvals = prior chat/strategy turns
  created approval requests that were never resolved (permission gating) → a second,
  independent way unattended chat turns can stall (RC5).
- `global-setup.ts` only pings `GET /health` (200 even when POST proxy is dead), so
  the suite starts against a broken dashboard without noticing → gap to fix.

## Fix targets (owned code/config)
1. fixtures.ts `waitAssistantText`: detect error card / failed turn / approval-wait
   and fail fast with the real reason (kill 120s opaque timeouts). [RC4]
2. global-setup.ts: add a real readiness probe that exercises the POST proxy and a
   chat turn, so a broken dashboard aborts the run immediately with a clear message.
3. route.ts: wrap handlers in try/catch → JSON 502 with detail even on unexpected
   throws (so module/dev errors become actionable error cards, not opaque 500). [RC2]
4. Dashboard E2E dev hygiene: clean `.next` before serving + disable webpack FS cache
   under an E2E env flag to avoid EPERM/ENOENT cache races. [RC2]
5. Tests must drive turns in an auto-approve permission mode (and/or pre-clear stale
   approvals) so tool-using cases actually complete unattended. [RC5]

## Bigger systemic findings (added after deeper probing)

### RC6 — Runtime under test is bound to the WRONG workspace
- `GET /dev/status` → `dir: C:\Users\Ricky\.nerya\dev_logs`.
- `paths.py:206` → `dev_logs = root / "dev_logs"`, so runtime root = `C:\Users\Ricky\.nerya`
  (the user's REAL global workspace), NOT `dashboard/.nerya-test-workspace`.
- Proof: `~/.nerya/strategies/` has many real strategies (a_share_team_rotation,
  btc_trend_15m, ...); test-ws `strategies/` is empty after reset.
- Consequence: `reset_workspace.py` cleans a directory the runtime never reads →
  isolation is a no-op; tests read/write/pollute the real workspace (135 real
  approvals, real accounts). `global-setup.ts` sets `process.env.NERYA_WORKSPACE`
  but the runtime was started SEPARATELY, so that env never reaches it, and nothing
  asserts the binding.

### RC7 — Dev HTTP logging bloat
- `~/.nerya/dev_logs/http.jsonl` = **429 MB**; `~/.nerya/nerya.db` = **249 MB**.
- `/dev/status` `config_flag:true` → dev logging on; every request appends to a
  429 MB JSONL → progressively slower runtime (`/dev/tail` itself timed out at 8s).
- Contributes to the slow turns / long P0 runtimes seen earlier.

# Trading — useful internal modules and libraries

Read this when you're about to write a new trading helper script and
want to pick the right primitives.

## Internal Nerya modules

| Module | What it does |
|---|---|
| `nerya.trading.portfolio` | Aggregated portfolio reads (positions, cash, PnL). Side-effect free; safe to call from any script. |
| `nerya.trading.accounts` | Load + save the workspace's account roster. Each account has a mode (`paper` / `live`) and an initial balance. |
| `nerya.trading.virtual_ledger` | Open / append to the per-account paper-trading ledger. The ledger is the source of truth for paper PnL. |
| `nerya.trading.intent` | Build, validate, and journal a trade intent. Use this rather than constructing intent dicts by hand. |
| `nerya.trading.risk` | Risk gate — call before sizing any new intent. Returns `allow` / `reduce` / `block` with reasons. |
| `nerya.trading.execution` | Order routing + fill recording. Routes to the right adapter based on the intent's venue. |
| `nerya.trading.exchanges.<venue>` | Per-venue adapters. Read the adapter you'll use before constructing exchange-specific payloads. |

## Standard libraries you'll often want

- `pandas` — time-series and tabular work for backtests / report
  generation.
- `numpy` — numerical math; prefer over re-implementing in pure
  Python.
- `pydantic` — input/output schema validation when a script will be
  called from many places.
- `httpx` (sync or async) — outbound HTTP. Use a timeout; never call
  exchanges with the default infinite timeout.

## Patterns that keep showing up

1. **Resolve workspace once, pass `WorkspacePaths` around.** Every
   internal module takes a `WorkspacePaths` rather than a raw path.
   See `nerya.core.paths.WorkspacePaths` for the constructor.
2. **Treat the journal as authoritative.** When in doubt about
   "what state are we in", read the journal under
   `paths.journals` rather than re-deriving from scratch.
3. **Risk → Portfolio fit → Execute.** Encode this order even in
   one-off scripts; reordering it bites in subtle ways during
   incidents.
4. **JSON in, JSON out.** Standalone scripts read a payload from
   `--json` / stdin and emit a single JSON object. That keeps them
   composable with `run_shell` and with other scripts.

# Extending Nerya — adding data sources, exchanges, wallets, dashboards

Read this when the user says things like *"add support for X exchange"*,
*"plug in this new data source"*, *"connect my new on-chain wallet"*, or
*"the dashboard should also show Y"*. Nerya's whole point is to be
extended; the question is *which* extension surface to use, and how to
get the running system to pick up the change without a restart.

## Step 0 — Always check what's already integrated FIRST

Before claiming a venue / data source is missing, call `connector_list`
(or `connector_view <id>`). Polymarket, Binance, Bybit, OKX, Hyperliquid,
BSC, Solana, EVM (Ethereum / Arbitrum / Polygon / Base) and 100+ more
via the CCXT bridge are *already* registered under
`nerya/connectors/` — promoting one of them to a strategy is just
"reference it from `accounts.yml` / strategy YAML", **not** a coding
task.

Decision tree:

1. `connector_list query="<vendor>"` → if `count >= 1` the venue is
  wired. Use `connector_view id=<id>` to learn endpoint URLs / method
   names, then use it directly. Do **not** re-author it.
2. If `count == 0` the venue really is missing → keep reading.
3. The same rule applies to data sources (Yahoo, akshare, CryptoCompare,
  Glassnode, …) — they live under `nerya/research/datasets/` /
   `nerya/markets/`. `grep` / `glob` first; only author a new one if
   nothing matches.

Mock data and one-shot scripts are forbidden. The only acceptable
output is a real Connector / dataset class that the rest of the system
(strategy router, market_data skill, dashboard charts) can ride on top
of forever.

## Pick the right surface


| What the user wants              | Where the change goes                                                                                                            | Hot-reload path                                                                                          |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| New CEX / DEX adapter            | `workspace/providers/<id>/provider.py` exposing a top-level `SPEC` constant                                                      | `nerya.connectors.registry.ConnectorRegistry.reload_providers()`                                         |
| New on-chain wallet provider     | `nerya/wallet/providers/<name>.py` (built-in) — there is no workspace surface yet                                                | restart, **or** `importlib.reload` the wallet provider module if the user is iterating                   |
| New market dataset / data source | `nerya/research/datasets/<adapter>.py` + register at boot — or call `DatasetRouter.register_adapter(market, dataset)` at runtime | `register_adapter()` is itself the hot-update; no rescan needed                                          |
| New skill                        | `workspace/skills/<name>/SKILL.md` (Anthropic-spec)                                                                              | `nerya.skills.kernel.SkillKernel.reload()`                                                               |
| New LLM model entry              | edit `nerya/llm/model_registry` config                                                                                           | `nerya.llm.model_registry.reload()`                                                                      |
| Dashboard widget / page          | `dashboard/app/...` and `dashboard/components/...`                                                                               | Next.js `npm run dev` auto-reloads on file save; `npm run build && npm run start` for production refresh |


If a request doesn't fit any row, that's a strong signal it needs a
proper PR rather than a hot-edit.

## Provider plug-in shape (CEX / DEX) — DEFAULT TRACK = workspace

> **For agent-authored adapters, default to the *workspace track*
> (`workspace/providers/<id>/provider.py`).** That is the only path
> that is *guaranteed* writable from inside an agent turn — the
> `nerya/connectors/` source tree is outside the agent's workspace
> root in any normal session and the model will hit
> `WorkspaceEscapeError` if it tries to land a file there. Using the
> workspace track also gives you free hot-reload via
> `ConnectorRegistry(workspace=...).reload_providers()`, no restart,
> no maintainer round-trip.

Use the workspace track when:

- the operator asks the agent to integrate a new venue / data source,
- the operator wants to iterate on a venue without a repo commit,
- you do not have direct push access to `nerya/connectors/`.

Skip to the [workspace track section](#provider-plug-in-shape-cex--dex--workspace-track)
below for the exact shape; come back to the next section only if you
are operating *inside the source repo* (CI / maintainer context) and
the change is meant to be committed.

## Provider plug-in shape (CEX / DEX) — built-in track (maintainer-only)

> Only choose this track if you are running in a session whose
> workspace root *is* the Nerya source repo (i.e. the maintainer
> running `nerya run --workspace <repo>`). For everyone else, this
> track will fail with a workspace-escape error and you must fall
> back to the workspace track above.

For an exchange / venue you intend to *commit* (not a one-off
experiment), author a real Connector under `nerya/connectors/<vendor>.py`
and register it inside `provider_spec._register_builtins`. This is the
same shape `polymarket.py`, `bsc_native.py`, `solana_native.py` already
follow — read those before writing yours.

Authoring checklist (do all of them, in order):

1. **Read the official API docs.** Identify auth, base URL, ticker /
  orderbook / klines / order endpoints, rate limits. Cite the doc URL
   in the module docstring.
2. **Subclass the right base.**
  - REST CEX with API keys → `CEXConnectorBase` (e.g. `polymarket.py`).
  - Generic CCXT-supported venue → no new file needed; just register
  a new `_ccxt_factory(...)` alias.
  - On-chain reads/writes → mirror `bsc_native.py` / `solana_native.py`.
3. **Implement** at least `tickers()`, `klines()`, `order_book()` for
  reads. Implement `balances()` and `place_order()` only when the
   venue's auth model is fully understood — gate them behind
   `self.live` so paper accounts can never accidentally trade.
4. **Register the spec.** In `provider_spec._register_builtins`, add a
  `reg.register(ExchangeProviderSpec(id=..., factory=_my_factory, ...))`
   block. Set `aliases=` so common venue names (e.g.
   `mexc`, `kucoin`) resolve to your provider.
5. **Wire credentials.** Use `_resolve_cex_creds(...)` exactly like
  `_polymarket(...)` does so the operator can store keys in the vault
   under `vault://` refs.
6. **Verify discoverability.** `connector_list query="<vendor>"` must
  return your provider. `connector_view id=<vendor>` must return its
   metadata (and source preview).
7. **Hot-reload.** Run `scripts/reload_subsystem.py providers` so the
  running kernel picks up the new spec without a process restart.
8. **Frontend (optional but encouraged).** If the venue powers the
  dashboard's home-page chart, ensure `dashboard/app/page.tsx` /
   `dashboard/components/home/`* reads through the existing
   `/api/markets/*` API surface — those routes already pull from the
   provider registry, so a new connector flows into the chart with no
   client-side change.

## Provider plug-in shape (CEX / DEX) — workspace track

This is the **default** path for agent-authored adapters. Drop a file
at `workspace/providers/<id>/provider.py` exposing a top-level
`SPEC: ExchangeProviderSpec`. Imports work because the registry
loads the file under a synthetic module name; just do not rely on
relative imports — use absolute `from nerya...` paths.

Authoring checklist (do all of them, in order):

1. **Read the official API docs.** Identify auth, base URL, ticker /
  orderbook / klines / order endpoints, rate limits. Cite the doc URL
   in the module docstring.
2. **Subclass `Connector` (or `CEXConnector` / `CEXConnectorBase` /
  `DEXConnector`).** Match the venue's actual capability surface —
   if it is a *data source* (Yahoo, akshare, CoinGecko), set
   `supports={"ticker": True, "klines": True, "order_book": ...,  "balances": False, "place_order": False}` and do not implement
   trading methods.
3. **Implement `tickers()`, `klines()`, `order_book()`** for reads.
  Use `_safe_float` style normalisation for vendor numbers; never
   propagate raw `NaN` / strings into the result.
4. **Expose `SPEC` at module top-level.** `id`, `kind`
  (`cex` / `dex`), `aliases`, `factory`, `links`, `description`,
   `supports`. The `factory` callable receives
   `(account_cfg, *, workspace=None, vault_passphrase=None)` and
   returns the `Connector` instance.
5. **Hot-reload.** From a `run_shell` step:
  `python -m nerya.cli reload providers` *or* directly call
   `coding/scripts/reload_subsystem.py providers`. The kernel picks
   up the new spec without restart.
6. **Verify with `connector_list query="<id>"` and
  `connector_view id=<id>`** — both must return your provider before
   you write a strategy that wires `markets: <id>:`* against it.
7. **Promotion (later, by a maintainer).** When the venue stabilises
  the maintainer can lift the file into `nerya/connectors/<id>.py`
   and register it in `_register_builtins`. The agent normally does
   not need to do this; do not mock it from inside an agent turn.

```python
# workspace/providers/myexchange/provider.py
"""MyExchange public market-data connector.

Docs: https://api.myexchange.io/docs
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from nerya.connectors.base import Connector, Ticker
from nerya.connectors.provider_spec import ExchangeProviderSpec


@dataclass
class MyExchangeConnector(Connector):
    venue: str = "MYEX"

    def tickers(self, market: str) -> Ticker | None:
        ...

    def klines(self, market: str, *, interval: str = "1d", limit: int = 200):
        ...


def _factory(*_args: Any, **_kwargs: Any) -> Connector:
    return MyExchangeConnector()


SPEC = ExchangeProviderSpec(
    id="myexchange",  # NOT "user:myexchange" — keep ids canonical
    label="MyExchange",
    kind="cex",
    runtime="python",
    aliases=("mxe", "myex"),
    factory=_factory,
    links={"docs": "https://api.myexchange.io/docs"},
    description="Read-only MyExchange market-data provider.",
    supports={
        "ticker": True, "klines": True, "order_book": True,
        "balances": False, "place_order": False,
    },
)
```

After dropping the file, the agent runs:

```python
from nerya.connectors.registry import ConnectorRegistry
ConnectorRegistry(workspace=...).reload_providers()
```

— or invokes `scripts/reload_subsystem.py providers` (see this skill's
bundled scripts). The home-page chart pipeline already reads from
`/api/market/venues` + `/api/market/candles` which both walk the
same registry, so the new venue appears in the dropdown automatically
once `reload_providers()` returns.

## Wallet provider plug-in shape

Wallets currently load only from `nerya/wallet/providers/`. To add a
new wallet:

1. Create `nerya/wallet/providers/<name>.py` following the same shape
  as `coinbase.py` or `self_custody.py` (read those first).
2. Register it in `nerya/wallet/providers/__init__.py`.
3. If the agent is running, call
  `importlib.reload(nerya.wallet.providers.<name>)` or restart the
   process. There is no `reload_providers()` for wallets *yet* — if
   wallet hot-reload becomes a recurring need, that's a real feature
   request, not an ad-hoc patch.

## Data source plug-in shape

Two layers:

- **Built-in adapters** (CCXT, Polymarket, on-chain) live under
`nerya/research/datasets/`. New adapters subclass `MarketDataset`
and are registered at boot.
- **Runtime adapters** are attached via `DatasetRouter.register_adapter(market, dataset)`
— useful for one-off backtests where the operator has a custom CSV
source they don't want to commit to `nerya/`.

Pick built-in when the user expects this source to be on by default;
pick runtime when it's an experiment.

## Skill plug-in shape

User-authored skills live at `workspace/skills/<name>/SKILL.md`. The
file must follow the Anthropic Skill spec (frontmatter with `name` /
`description` and a markdown body). After dropping the file, run
`SkillKernel.reload()` — the new skill becomes available the next
time the agent inspects its skill list.

If the skill needs a helper script, drop it under
`workspace/skills/<name>/scripts/<helper>.py` and document the
invocation pattern in the skill's body. The skill loader does *not*
auto-import the script; the agent invokes it via `run_shell`.

## Dashboard changes

The Next.js dashboard runs in dev mode with HMR. While editing:

1. Make sure `npm run dev` is up under `dashboard/` (start it as a
  background task if not).
2. Save your changes — Next will reload the browser tab automatically.
3. For production, run `npm run build && npm run start` (or push
  through the CI image build).

A dashboard widget that needs new data should ride on top of an
existing API route (under `nerya/api/`) or a new route. Don't have
the front-end read filesystem state directly — go through the API.

## When NOT to hot-reload

- **Schema-changing edits.** If the change alters a public dataclass
or a journal record shape, restart cleanly so partially-loaded
modules don't disagree about types.
- **Anything that already looped.** If a worker has cached a stale
reference (a closure, a registered handler), reloading the source
module won't update the cache. Restart it.
- **Production critical paths.** Hot reload is a developer
convenience; do it on a paper account before live.

## How to call reloads from a script

Use the bundled `scripts/reload_subsystem.py` helper:

```bash
python -m nerya.skills.builtin.coding.scripts.reload_subsystem \
    --json '{"target": "providers", "workspace": "/path/to/ws"}'
```

Targets: `providers`, `skills`, `models`. The helper reports which
ids are now visible after the reload, so the agent has evidence the
update took effect.
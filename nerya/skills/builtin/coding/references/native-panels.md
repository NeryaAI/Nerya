# Native panel construction

Use for 面板 / dashboard / panel / 行情 / 账户概览 composition, including a
delegated task to display BTC and ETH quotes beside account totals. Construct
the reviewable layout first, then verify the requested dynamic data.

## First pass

1. Call `workspace_ui_inspect` with its manifest and catalog enabled. Read the
   revision/digest and existing ids; preserve unrelated pages and widgets.
   Determine CREATE, UPDATE or READ from the user's task and context:

   - CREATE: propose a new page with an unused id and its own widgets, even if
     a similar applied page exists. Existing layouts may inform the design;
     linking or patching an old page does not fulfill creation.
   - UPDATE: reuse the exact requested page/widget identity and propose only
     the needed changes; an already-satisfied request may use its applied link.
   - READ: inspect and link a matching applied page without a new proposal.

   If an UPDATE target is ambiguous, clarify its identity before proposing.
2. Select catalog kinds. For price trends, use `chart` with `market.candles`
   and the requested market/timeframe; `market_ticker` alone is not a trend chart.
   `market_ticker` reads quotes through the server and displays provider-reported
   24h `change` (quote units) and `percentage` (percent points, 1 means 1%).
   These display by default; `config.show_change=false` hides both fields.
   Missing fields display unknown, including when a provider returns price only;
   chart-range returns never substitute for 24h ticker change.
   `portfolio` reads server-side account totals. Use a configured venue from
   the request or existing layout; if unspecified, disclose the chosen venue.
   The example below chooses Binance and USDT pairs. Use stable ids and small
   operations. Cover every requested asset and chart range in the new layout;
   a partial update to an old widget is not a new panel.
3. For CREATE or a needed UPDATE, call `workspace_ui_propose` with the inspected
   `base_revision` and `base_digest`. The example is the arguments object; replace its ids/venue
   to fit the inspected workspace. For CREATE, choose an unused page id
   (add a suffix on collision), use it in every widget operation, and keep it
   stable for this candidate. The example uses `add_page` to protect existing
   pages from an accidental upsert; UPDATE uses the inspected target ids.

```json
{
  "summary": "添加 BTC、ETH 行情和账户概览面板",
  "operations": [
    {"op": "add_page", "page": {"id": "market-account-overview", "title": "行情与账户概览", "nav": {"section": "primary"}}},
    {"op": "upsert_widget", "page": "market-account-overview", "widget": {"id": "btc-quote", "kind": "market_ticker", "title": "BTC / USDT", "source": "market.ticker", "config": {"venue": "binance", "market": "BTC/USDT"}}},
    {"op": "upsert_widget", "page": "market-account-overview", "widget": {"id": "eth-quote", "kind": "market_ticker", "title": "ETH / USDT", "source": "market.ticker", "config": {"venue": "binance", "market": "ETH/USDT"}}},
    {"op": "upsert_widget", "page": "market-account-overview", "widget": {"id": "account-overview", "kind": "portfolio", "title": "账户概览", "description": "服务端账户汇总；paper/live 以账户记录为准，数据可用性尚未验证。", "source": "portfolio.summary", "config": {"currency": "USD"}}}
  ]
}
```

4. Require a successful result with `proposal_id`, `preview_url`,
   `preview_action`, `state=pending_review`, affected resources and diff. For
   the handoff, copy the exact producer-returned `preview_url` into the
   clickable `[预览面板](<preview_url>)`; if using the structured action, copy
   its exact `href`. Do not reconstruct the route, shorten the proposal id,
   regex-replace prose, or choose an id from model text. The preview covers
   home and all candidate pages without applying the proposal. Explain that it
   starts with structure only and the operator can optionally load read-only
   data using the preview's load-data button before approval/apply, without
   enabling trading. For a dynamic panel, perform the bounded source reads
   below before the final handoff; only an explicitly layout-only request ends
   at the structure preview. Do not approve, apply, activate, restart, or reload.
   If the tool is absent or denied, report that specific tool/permission blocker;
   do not bypass it with `write_file`, shell, or another proposal surface.
   For a revision conflict, inspect again and reconcile the small operations.

## Reuse an applied page

For READ or an already-satisfied UPDATE, when inspect confirms the applied
target matches the request, reuse it without creating a proposal. CREATE
follows the new-page path above. Include the exact producer-returned applied-page
href directly as a Markdown link, matched to that page identity. Keep the
relative href as returned: do not resolve a host/port, probe the link, or fetch
it through another route.
Keep layout reuse and verified data reads separate; an applied page handoff
uses its page link, not a proposal preview URL. For a request to display prices,
changes and trends, list/get/inspect only establish layout reuse. Before calling
the dynamic panel ready, perform a read-only preview read of its actual sources:
use `preview_read` if advertised by the available workspace UI tool schema.
If absent, use `market_data(action="get_ticker")` and
`market_data(action="get_candles")` for each widget's exact venue/market and
chart interval/count. Do not invent a tool/action or claim that inspect loaded data.
Record returned source/venue, timestamp, mode, degraded/fallback status, ticker
fields and non-empty candle coverage; unknown change, zero candles and stale or
degraded reads are incomplete data evidence. Report the limitation with the page link.

On access denial (including HTTP 403/451 or geographic restriction), load
`markets` and discover installed public sources via `connector_list`/
`connector_view` and the available market-data source catalog. When the user did
not pin the venue, choose an available public source without another question,
preserve BTC/ETH and the existing quote currency, and disclose the source change.
Verify both ticker and candles before proposing small source/config updates to
the existing widget ids; preserve the page and unrelated widgets. Stay within
installed sources and the proposal/approval boundary. If the user pinned the
venue or no compatible source succeeds, report that exact blocker.

## Recover an existing preview

When continuing the same task with a producer-returned preview URL or proposal id,
recover that candidate before inspecting source files or proposing anything new.
An unrelated earlier proposal does not fulfill a fresh CREATE request.
Call `workspace_ui_inspect` with the exact `proposal_id` from the existing
producer result. This read-only path loads the persisted candidate through the
preview validator and returns `source=proposal`, the exact `preview_url` and
`preview_action.href`, `state=pending_review`, `applied=false`, and
`data_validation.status=not_checked` when the candidate is still pending.

An empty applied manifest is not evidence that the pending candidate is empty.
Do not call `evolve_proposals`, search proposal files, grep the workspace,
create a replacement proposal, apply the candidate, or enable trading to recover
a preview. Copy the exact returned URL/action from the recovery result. If the
exact id is missing or the candidate is invalid, report that read-only recovery
blocker and preserve the existing id; do not guess or repair it from prose.

## Data truth and capability boundaries

Layout validation checks structure, not network access, freshness or balances.
Both inspect and propose report `data_validation.status=not_checked`. This is
neither a successful data read nor a data blocker. Bind to server sources; do
not put copied prices, account balances, API keys, vault references, remote
endpoints, executable HTML or client fetch code into the manifest.
Providing `preview_url` leaves `data_validation.status=not_checked`. Describe
data as unverified until a read returns source, timestamp and freshness evidence.
Approval/apply persists the layout; it is not a prerequisite for the optional
read-only preview data load.

For both a new proposal and an applied page, a request to display quotes,
changes, charts or account values includes a bounded check of those sources.
Use the read-only source checks described above with the candidate's exact
widget configuration. Retain source, timestamp, mode and error evidence; report
unavailable data as a limitation of the same proposal, not a new proposal.

- **Quotes:** public live quotes are read-only observations. They do not require
  enabling live trading and do not make a paper account live. Preserve degraded,
  fallback or stale indicators; a successful transport alone is not fresh data.
  The ticker displays last/mid, bid/ask and provider-reported 24h change/percentage.
  Missing change/percentage remains unknown; a price-only response does not prove
  24h coverage. High/low and volume remain unsupported display capabilities.
- **Accounts:** use configured server account data and its paper/live labels.
  The current portfolio widget aggregates accounts; it has no account-id or
  mode filter and is not a per-account/per-coin table. Do not invent `mode=live`
  or `account_id` config keys. Never describe aggregate totals as a verified live
  balance or treat paper cash as live funds. For detailed inspection, link the
  existing `/portfolio` surface and report the unsupported panel requirement.
- **Missing/empty:** no configured accounts, no snapshot, and an observed zero
  balance are different. Missing data stays unavailable; an empty result is not
  proof of zero funds. A pending proposal is still deliverable when data is absent.
- **Blocked:** report the exact failed source and cause, such as authentication,
  permission, unsupported capability, provider timeout, or service unavailable.
  Separate a data blocker from a proposal validation/permission failure. Do not
  loop through unrelated data providers to decorate a panel before constructing it.

The existing portfolio renderer does not expose all per-account missing/stale
states, and the ticker does not show every provenance field. State these limits
in the handoff if they affect the request; layout success does not prove that
those display requirements passed. A missing renderer capability is a separate
implementation task, not permission to modify the frontend during construction.

## Completion evidence

For a new proposal, report the clickable preview, native proposal construction,
operator review pending, and data validation separately. For CREATE, verify the
diff adds the new page and requested widgets; an old page link or widget-only
update is insufficient. For READ or an already-satisfied UPDATE, report the
applied target's clickable page link and reuse instead. A proposal id alone is not the panel
handoff. Include any unsupported fields or unverified data. A proposed page
is not an active page; a planned file path is not an artifact. If the model/provider
stream times out before a successful tool result, report interrupted construction.
If a proposal id was already returned, preserve it and inspect existing evidence
before retrying so a partial response does not create duplicate proposals.

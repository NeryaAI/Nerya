# Choose replay from actual capabilities

Preserve the exact candidate, chain/network, venue-qualified market, rules,
window and risk limits. A missing capability is a repair task, not a reason to
replace the requested market, disable execution permanently or waive evidence.

Compare the saved candidate with the receipt's requested/actual market,
timeframe and window before reporting completion. Resolve omitted identifiers
through the selected provider. A missing source retains the requested scope in
its blocked receipt; a related instrument is not evidence for that candidate.

## Resolve inputs before choosing an engine

- Standard native replay applies when the saved entrypoint consumes historical
  candles/features and its order, sizing and protection semantics are supported
  by that engine. Discover real history for the exact venue and window.
- News, wallet flows, pool reserves, protocol positions or financial actions
  need timestamped observations and the same input/execution contract. Standard
  candle replay does not populate arbitrary `ctx.news` or financial snapshots.
  Author a strategy-local replay when those inputs cannot be represented.
- Missing candles for a newly integrated venue do not establish missing public
  history. Inspect source discovery and the download receipt. A reviewed
  workspace provider under `providers/<id>/provider.py` can expose `get_klines`
  through `ExchangeProviderSpec`; historical lookup uses the current workspace.
  Use the exchange-author proposal lane for new adapters and dependency setup;
  never write directly into installed providers or bypass approval. Keep public
  history read-only, bounded and bound to the selected network.

Do not fetch unrelated market proxies or interpolate unavailable volume. If no
durable observations exist for the requested window, retain an explicit blocked
receipt with the missing source/window and the concrete capability required.

## Repair a failed attempt

Read the structured failure and its source/config paths once. Repair the smallest
identified dependency or SDK defect in the same candidate. Use advertised native
validation tools directly; do not invent a script wrapper for a native tool.
An unchanged failure is not a reason to repeat the call. Keep the required check
pending while repairing; a successful file write is not a successful validation.

Run compile/static validation and bounded contract tests with real SDK types
before spending another historical run. Tests must exercise missing/stale data,
closed-bar boundaries, duplicate delivery, pending/unknown actions and confirmed
entry/exit. Permissive fake SDK methods cannot certify parameter names, types or
position identifiers. Reconcile pending/unknown effects before any retry.

For custom replay load `references/custom_replay_template.md`. Execute the saved
strategy entrypoint and helpers, preserving each observed timestamp and exposing
only the historical prefix. Bind source endpoints, network identity, row ranges,
data hashes and package revision to the report. A current snapshot is not past
history. Branch fixtures establish behavior only. Signal/observation evidence
cannot establish fills, returns, approval readiness or account execution.

The required replay remains outstanding until its actual receipt establishes the
requested evidence. A genuinely unavailable source can remain blocked; explain
that limitation plainly instead of producing fake performance or an empty
strategy. Never change the agent loop to route by market or strategy name.

# Perpl MON hourly SMA6/18

Native deterministic Nerya script strategy. It requires 19 contiguous closed
hourly public testnet candles and computes previous/current SMA6/18. A genuine
cross opens one long or short, fixed base size, IOC market order, 1x leverage and explicitly authorized 100 bps maximum negative PnL admission budget,
provided the isolated authenticated account is empty. No cross means no entry.
Warmup, forming candles, duplicate/gapped/stale bars do not manufacture signals.

After actual confirmed fill accounting, adverse move, opposite cross or bounded
hold expiry requests a reduce-only full close, bound to the observed position ID
and actual filled base quantity. External account changes block a guessed close.
One cycle ends after confirmation. Stops are software tick checks, not venue stop
orders; lost ticks or approval delay can extend exposure past the target hold.

Signal replay uses the exact signal function and actual public candles. It reports
observed crossings without execution or PnL claims. An operator-forced open/close
smoke is separate evidence and does not prove autonomous signal entry.

Defaults are paper, disabled schedule and disabled execution. The Perpl transport
plugin, enrolled testnet account and operator permission/grant are prerequisites.
The package never reads credentials, enrolls, deposits, or bypasses risk gates.

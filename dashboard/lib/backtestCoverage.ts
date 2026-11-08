/** Coverage is evidence for one immutable run, not for the current data cache. */
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const finite = (value: unknown): number | null => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
const date = (value: unknown): number | null => typeof value === "string" && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null;

export function backtestCoverage(meta: Record<string, unknown>) {
  const manifest = object(meta.data_manifest), assumptions = object(object(meta.provenance).assumptions);
  const requestedDays = finite(meta.requested_window_days ?? assumptions.requested_window_days);
  const explicit = meta.requested_window_complete ?? manifest.requested_window_complete;
  const incomplete = meta.requested_window_complete === false || manifest.requested_window_complete === false;
  // Requested endpoints describe recorded coverage only when this run certifies it.
  const requestedStart = date(meta.requested_start_utc ?? manifest.requested_start_utc);
  const requestedEnd = date(meta.requested_end_utc ?? manifest.requested_end_utc);
  const verifiedWindow = explicit === true && !incomplete && manifest.end_exclusive === true && requestedStart !== null && requestedEnd !== null && requestedEnd > requestedStart;
  const start = verifiedWindow ? requestedStart : date(meta.start || meta.start_utc), last = date(meta.end || meta.end_utc);
  const frame = /^(\d+)([smhdw])$/.exec(String(meta.tf ?? ""));
  const step = frame ? Number(frame[1]) * ({ s: 1000, m: 60000, h: 3600000, d: 86400000, w: 604800000 } as Record<string, number>)[frame[2]] : 0;
  const end = verifiedWindow ? requestedEnd : last !== null ? last + step : null;
  const recordedDays = start !== null && end !== null && end >= start ? (end - start) / 86400000 : finite(meta.backtest_days);
  // Old coverage_ok only meant nonempty data. It must never certify a full year.
  const shorter = requestedDays !== null && recordedDays !== null && recordedDays + 1e-6 < requestedDays;
  const state = incomplete ? "partial" : explicit === true ? "complete" : shorter ? "partial" : "unknown";
  return { requestedDays, recordedDays, start, end, state };
}

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const scope = { exports: {} };
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, "../lib/backtestCoverage.ts"), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { module: scope, exports: scope.exports });
const { backtestCoverage } = scope.exports;

test("old nonempty 30-day report is not a completed requested year", () => {
  const data = backtestCoverage({ start: "2025-01-01T00:00:00Z", end: "2025-01-30T23:00:00Z", tf: "1h", coverage_ok: true,
    provenance: { assumptions: { requested_window_days: 365 } } });
  assert.equal(data.requestedDays, 365); assert.equal(data.recordedDays, 30); assert.equal(data.state, "partial");
});
test("full 8760-hour window includes the final closed candle", () => {
  const data = backtestCoverage({ start: "2025-01-01T00:00:00Z", end: "2025-12-31T23:00:00Z", tf: "1h",
    requested_window_days: 365, requested_window_complete: true });
  assert.equal(data.recordedDays, 365); assert.equal(data.state, "complete");
});
test("full endpoints cannot override a manifest with missing candles", () => {
  const data = backtestCoverage({ start: "2025-01-01T00:00:00Z", end: "2025-12-31T00:00:00Z", tf: "1d",
    requested_window_days: 365, requested_window_complete: true, data_manifest: { requested_window_complete: false } });
  assert.equal(data.state, "partial");
});
test("missing completeness is unknown, not an invented successful check", () => {
  assert.equal(backtestCoverage({ requested_window_days: 365 }).state, "unknown");
});

test("producer completeness outranks last-minus-first display span when timeframe is absent", () => {
  const meta = { requested_window_days:30, requested_window_complete:true,
    start_utc:"2026-09-22T11:00:00Z", end_utc:"2026-10-22T10:00:00Z" };
  assert.equal(backtestCoverage(meta).state, "complete");
  const data = backtestCoverage({ ...meta, data_manifest: { requested_window_complete:true, end_exclusive:true,
    requested_start_utc:"2026-09-22T11:00:00+00:00", requested_end_utc:"2026-10-22T11:00:00+00:00" } });
  assert.equal(data.recordedDays, 30);
  assert.equal(data.end, Date.parse("2026-10-22T11:00:00Z"));
});

test("requested endpoints alone cannot certify coverage or replace recorded timestamps", () => {
  const meta = { requested_window_days:30, start_utc:"2026-10-20T11:00:00Z", end_utc:"2026-10-22T10:00:00Z", tf:"1h",
    data_manifest: { end_exclusive:true, requested_start_utc:"2026-09-22T11:00:00Z", requested_end_utc:"2026-10-22T11:00:00Z" } };
  assert.equal(backtestCoverage(meta).state, "partial");
  assert.equal(backtestCoverage(meta).recordedDays, 2);
  assert.equal(backtestCoverage({ requested_window_days:30, data_manifest:meta.data_manifest }).recordedDays, null);
  assert.equal(backtestCoverage({ requested_window_days:30, data_manifest:meta.data_manifest }).state, "unknown");
});

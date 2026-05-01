"use client";

import { useEffect, useMemo, useState } from "react";
import { clientApi } from "../../lib/clientApi";
import type {
  Incident,
  KillSwitchView,
  ReconciliationReport,
} from "../../lib/clientApi";
import {
  Card,
  Empty,
  ErrorBanner,
  Json,
  Kpi,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import { formatTsShort } from "../../lib/format";

function severityTone(
  severity?: string,
): "ok" | "warn" | "danger" | "neutral" | "brand" {
  switch ((severity || "").toLowerCase()) {
    case "info":
      return "ok";
    case "warning":
      return "warn";
    case "action_required":
      return "danger";
    case "trading_halted":
      return "danger";
    case "lost":
      return "danger";
    case "stale":
      return "warn";
    default:
      return "neutral";
  }
}

function fmtTs(ts: unknown): string {
  if (ts == null) return "—";
  if (typeof ts === "string") return formatTsShort(ts);
  const seconds = Number(ts);
  if (!Number.isFinite(seconds)) return String(ts);
  const ms = seconds > 1e12 ? seconds : seconds * 1000;
  return formatTsShort(new Date(ms).toISOString());
}

const WINDOW_OPTIONS = [
  { label: "15m", value: 900 },
  { label: "1h", value: 3600 },
  { label: "6h", value: 21600 },
  { label: "24h", value: 86400 },
];

export default function IncidentsPage() {
  const [windowS, setWindowS] = useState<number>(3600);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [reports, setReports] = useState<ReconciliationReport[]>([]);
  const [worst, setWorst] = useState<ReconciliationReport | null>(null);
  const [killSwitch, setKillSwitch] = useState<KillSwitchView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [selected, setSelected] = useState<Incident | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [incidentsRes, reportsRes, killRes] = await Promise.all([
        clientApi.controlIncidents(windowS),
        clientApi
          .controlReconciliationReports({ limit: 25 })
          .catch(() => ({ reports: [], worst_recent: null, filter: {} })),
        clientApi.controlKillSwitchGet().catch(() => null),
      ]);
      setIncidents(incidentsRes.incidents || []);
      setReports(reportsRes.reports || []);
      setWorst(reportsRes.worst_recent ?? null);
      setKillSwitch(killRes);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function toggleKillSwitch() {
    if (!killSwitch) return;
    const next = !killSwitch.kill_switch;
    if (
      !confirm(
        next
          ? "Engage the global kill switch? Strategies will refuse to trade until it is released."
          : "Release the global kill switch?",
      )
    ) {
      return;
    }
    setBusy("kill");
    try {
      const res = await clientApi.controlKillSwitchSet(next, "dashboard");
      setKillSwitch((prev) =>
        prev
          ? { ...prev, kill_switch: !!res.kill_switch }
          : { kill_switch: !!res.kill_switch, live_trading_enabled: false, ts: new Date().toISOString() },
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 30_000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowS]);

  const incidentsByKind = useMemo(() => {
    const map: Record<string, number> = {};
    for (const i of incidents) {
      map[i.kind] = (map[i.kind] || 0) + 1;
    }
    return map;
  }, [incidents]);

  const totalIssues = reports.reduce(
    (sum, r) => sum + Number(r.summary?.issue_count ?? 0),
    0,
  );

  return (
    <div>
      <PageHeader
        title="Incident Center"
        description="Reconcile drift, lost orders, stale snapshots, auth failures and other operator-grade signals from the live trading control plane."
        actions={
          <div className="flex items-center gap-2">
            {killSwitch ? (
              <button
                onClick={toggleKillSwitch}
                disabled={busy === "kill"}
                className={`btn-ghost text-xs ${killSwitch.kill_switch ? "text-accent-300" : "text-[#ef4560]"}`}
                title="Toggle the global kill switch"
              >
                {busy === "kill"
                  ? "…"
                  : killSwitch.kill_switch
                    ? "release kill"
                    : "engage kill"}
              </button>
            ) : null}
            <button
              onClick={load}
              disabled={loading}
              className="btn-ghost text-xs"
            >
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        }
      />
      <SectionTabs section="trading" />
      <PageBody>
        {error && <ErrorBanner error={error} />}

        {worst &&
        (worst.severity === "action_required" ||
          worst.severity === "trading_halted") ? (
          <div
            className={`rounded-lg border px-4 py-3 text-sm ${
              worst.severity === "trading_halted"
                ? "border-[#ef4560]/50 bg-[#ef4560]/10 text-[#ef4560]"
                : "border-[#f5a524]/40 bg-[#f5a524]/10 text-[#f5a524]"
            }`}
          >
            <div className="flex items-center gap-2">
              <Pill tone={severityTone(worst.severity)}>{worst.severity}</Pill>
              <span className="font-mono text-[11px]">
                {worst.scope}
                {worst.account_id ? `:${worst.account_id}` : ""}
              </span>
              <span className="ml-auto text-[11px] text-ink-400">
                {fmtTs(worst.ts)}
              </span>
            </div>
            <div className="mt-1 text-xs">
              {Number(worst.summary?.issue_count ?? 0)} drift issue(s) need
              operator attention before live trading resumes.
            </div>
          </div>
        ) : null}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Kpi
            label="Incidents"
            value={`${incidents.length}`}
            tone={incidents.length > 0 ? "warn" : "neutral"}
          />
          <Kpi
            label="Recon reports"
            value={`${reports.length}`}
            delta={`${totalIssues} drift issue(s)`}
          />
          <Kpi
            label="Lost orders"
            value={`${incidentsByKind["lost_order"] || 0}`}
            tone={(incidentsByKind["lost_order"] || 0) > 0 ? "danger" : "ok"}
          />
          <Kpi
            label="Kill switch"
            value={killSwitch?.kill_switch ? "ENGAGED" : "released"}
            tone={killSwitch?.kill_switch ? "danger" : "ok"}
          />
        </div>

        <Card title="Time window">
          <div className="flex flex-wrap gap-2 items-center text-xs">
            {WINDOW_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                onClick={() => setWindowS(opt.value)}
                className={`px-2 py-1 rounded-md border ${
                  windowS === opt.value
                    ? "border-brand-400 bg-brand-500/20 text-brand-100"
                    : "border-brand-500/20 text-ink-300 hover:bg-brand-500/10"
                }`}
              >
                last {opt.label}
              </button>
            ))}
          </div>
        </Card>

        <Card
          title={`Incidents (${incidents.length})`}
          description="Aggregates lost orders, stale snapshots, reconciliation drift, and other risk signals."
        >
          {incidents.length === 0 ? (
            <Empty label="No incidents in this window." />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>Severity</th>
                    <th>Kind</th>
                    <th>Account</th>
                    <th>Strategy</th>
                    <th>Subject</th>
                    <th>When</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {incidents.map((incident, idx) => (
                    <tr
                      key={`${incident.kind}-${idx}-${String(incident.ts)}`}
                      className="text-xs"
                    >
                      <td>
                        <Pill tone={severityTone(incident.severity)}>
                          {String(incident.severity || "info")}
                        </Pill>
                      </td>
                      <td className="font-mono">{incident.kind}</td>
                      <td className="font-mono text-ink-300">
                        {String((incident as Record<string, unknown>).account_id ?? "—")}
                      </td>
                      <td className="font-mono text-ink-300">
                        {String((incident as Record<string, unknown>).strategy_id ?? "—")}
                      </td>
                      <td className="font-mono text-ink-200 truncate max-w-[280px]">
                        {String(
                          (incident as Record<string, unknown>).subject ??
                            (incident as Record<string, unknown>).order_id ??
                            (incident as Record<string, unknown>).executor_id ??
                            (incident as Record<string, unknown>).report_id ??
                            "",
                        )}
                      </td>
                      <td className="text-ink-400 font-mono">
                        {fmtTs(incident.ts)}
                      </td>
                      <td>
                        <button
                          onClick={() => setSelected(incident)}
                          className="btn-ghost text-[11px] py-0.5"
                        >
                          inspect
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card
          title={`Reconciliation reports (${reports.length})`}
          description="Severity-tagged reports from local + account passes."
        >
          {reports.length === 0 ? (
            <Empty label="No reconciliation reports yet." />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>Severity</th>
                    <th>Scope</th>
                    <th>Account</th>
                    <th>Strategy</th>
                    <th>Issues</th>
                    <th>When</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {reports.map((r) => (
                    <tr key={r.report_id} className="text-xs">
                      <td>
                        <Pill tone={severityTone(r.severity)}>
                          {r.severity}
                        </Pill>
                      </td>
                      <td className="font-mono">{r.scope}</td>
                      <td className="font-mono text-ink-300">
                        {r.account_id || "—"}
                      </td>
                      <td className="font-mono text-ink-300">
                        {r.strategy_id || "—"}
                      </td>
                      <td>{Number(r.summary?.issue_count ?? 0)}</td>
                      <td className="text-ink-400 font-mono">{fmtTs(r.ts)}</td>
                      <td>
                        <button
                          onClick={() =>
                            setSelected({
                              ...r,
                              kind: `recon:${r.scope}`,
                              severity: r.severity,
                              ts: r.ts,
                            })
                          }
                          className="btn-ghost text-[11px] py-0.5"
                        >
                          inspect
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {selected ? (
          <Card
            title={`Incident · ${selected.kind}`}
            actions={
              <button
                onClick={() => setSelected(null)}
                className="btn-ghost text-xs"
              >
                Close
              </button>
            }
          >
            <Json value={selected} />
          </Card>
        ) : null}
      </PageBody>
    </div>
  );
}

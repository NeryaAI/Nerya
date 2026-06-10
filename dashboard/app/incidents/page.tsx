"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
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
import { confirm as confirmDialog } from "../../lib/dialogs";

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
  if (ts == null) return "–";
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
  const t = useTranslations("incidents");
  const tCommon = useTranslations("common");
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
    const ok = await confirmDialog({
      message: next
        ? t("killSwitchEngageConfirm")
        : t("killSwitchReleaseConfirm"),
      tone: next ? "danger" : "warning",
    });
    if (!ok) return;
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
        title={t("title")}
        description={t("description")}
        actions={
          <div className="flex items-center gap-2">
            {killSwitch ? (
              <button
                onClick={toggleKillSwitch}
                disabled={busy === "kill"}
                className={`btn-ghost text-xs ${killSwitch.kill_switch ? "text-accent-300" : "text-danger"}`}
                title={t("killSwitchToggleTitle")}
              >
                {busy === "kill"
                  ? "…"
                  : killSwitch.kill_switch
                    ? t("releaseKill")
                    : t("engageKill")}
              </button>
            ) : null}
            <button
              onClick={load}
              disabled={loading}
              className="btn-ghost text-xs"
            >
              {loading ? tCommon("refreshing") : tCommon("refresh")}
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
                ? "border-danger/50 bg-danger/10 text-danger"
                : "border-warn/40 bg-warn/10 text-warn"
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
              {t("driftIssuesNeedAttention", { count: Number(worst.summary?.issue_count ?? 0) })}
            </div>
          </div>
        ) : null}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Kpi
            label={t("kpiIncidents")}
            value={`${incidents.length}`}
            tone={incidents.length > 0 ? "warn" : "neutral"}
          />
          <Kpi
            label={t("kpiReconReports")}
            value={`${reports.length}`}
            delta={t("driftIssuesDelta", { count: totalIssues })}
          />
          <Kpi
            label={t("kpiLostOrders")}
            value={`${incidentsByKind["lost_order"] || 0}`}
            tone={(incidentsByKind["lost_order"] || 0) > 0 ? "danger" : "ok"}
          />
          <Kpi
            label={t("kpiKillSwitch")}
            value={killSwitch?.kill_switch ? t("killSwitchEngaged") : t("killSwitchReleased")}
            tone={killSwitch?.kill_switch ? "danger" : "ok"}
          />
        </div>

        <div className="flex flex-wrap gap-2 items-center text-[12px] border-b border-brand-500/10 pb-3">
          <span className="text-ink-500">{t("timeWindowTitle")}</span>
          {WINDOW_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setWindowS(opt.value)}
              className={`px-2.5 py-1 rounded-md border transition ${
                windowS === opt.value
                  ? "bg-brand-500/15 text-brand-100 border-brand-500/40"
                  : "text-ink-400 border-transparent hover:text-ink-200 hover:border-brand-500/20"
              }`}
            >
              {t("lastWindow", { label: opt.label })}
            </button>
          ))}
        </div>

        <Card
          title={t("incidentsTitle", { count: incidents.length })}
          description={t("incidentsDescription")}
        >
          {incidents.length === 0 ? (
            <Empty label={t("noIncidentsInWindow")} />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>{t("colSeverity")}</th>
                    <th>{t("colKind")}</th>
                    <th>{t("colAccount")}</th>
                    <th>{t("colStrategy")}</th>
                    <th>{t("colSubject")}</th>
                    <th>{t("colWhen")}</th>
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
                        {String((incident as Record<string, unknown>).account_id ?? "–")}
                      </td>
                      <td className="font-mono text-ink-300">
                        {String((incident as Record<string, unknown>).strategy_id ?? "–")}
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
                          {t("inspect")}
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
          title={t("reconciliationReportsTitle", { count: reports.length })}
          description={t("reconciliationReportsDescription")}
        >
          {reports.length === 0 ? (
            <Empty label={t("noReconciliationReports")} />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>{t("colSeverity")}</th>
                    <th>{t("colScope")}</th>
                    <th>{t("colAccount")}</th>
                    <th>{t("colStrategy")}</th>
                    <th>{t("colIssues")}</th>
                    <th>{t("colWhen")}</th>
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
                        {r.account_id || "–"}
                      </td>
                      <td className="font-mono text-ink-300">
                        {r.strategy_id || "–"}
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
                          {t("inspect")}
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
            title={t("incidentDetailTitle", { kind: selected.kind })}
            actions={
              <button
                onClick={() => setSelected(null)}
                className="btn-ghost text-xs"
              >
                {tCommon("close")}
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

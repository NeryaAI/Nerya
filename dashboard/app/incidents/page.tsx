"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
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
import { ModePill } from "../../components/ModePill";
import { formatTsShort } from "../../lib/format";
import { confirm as confirmDialog, toast } from "../../lib/dialogs";

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
    default:
      return "neutral";
  }
}

// Raw enum -> incidentsPage.* translation key. Unknown values (new
// backend severities/kinds) fall back to the raw string.
const SEVERITY_KEYS: Record<string, string> = {
  info: "severityInfo",
  warning: "severityWarning",
  action_required: "severityActionRequired",
  trading_halted: "severityTradingHalted",
};

const INCIDENT_KIND_KEYS: Record<string, string> = {
  reconcile_drift: "kindReconcileDrift",
  lost_order: "kindLostOrder",
  "auth.error": "kindAuthError",
  "max_loss.breach": "kindMaxLossBreach",
  "snapshot.unhealthy": "kindSnapshotUnhealthy",
};

function enumLabel(
  map: Record<string, string>,
  value: string,
  t: (key: string) => string,
): string {
  const key = map[value] ?? map[value.toLowerCase()];
  return key ? t(key) : value;
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
  const tEnum = useTranslations("incidentsPage");
  const [windowS, setWindowS] = useState<number>(3600);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [reports, setReports] = useState<ReconciliationReport[]>([]);
  const [worst, setWorst] = useState<ReconciliationReport | null>(null);
  const [killSwitch, setKillSwitch] = useState<KillSwitchView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [selected, setSelected] = useState<Incident | null>(null);
  // Client-side kind filter ("all" = no filtering); chips are derived from
  // the incidents actually present in the current time window.
  const [kindFilter, setKindFilter] = useState<string>("all");
  const [killUnknown, setKillUnknown] = useState(false);
  // Guards against a toast on every 30s poll while the kill switch
  // endpoint keeps failing — surface the failure once per failure streak.
  const killToastShown = useRef(false);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      // The kill-switch promise carries its own error envelope so an early
      // exit (e.g. controlIncidents rejecting) can never leave an orphaned
      // rejection — previously `await killGet` sat after the first
      // Promise.all and never ran when that rejected.
      const killGet = clientApi
        .controlKillSwitchGet()
        .then((value) => ({ ok: true as const, value }))
        .catch((e: unknown) => ({ ok: false as const, error: e }));
      const [incidentsRes, reportsRes] = await Promise.all([
        clientApi.controlIncidents(windowS),
        clientApi
          .controlReconciliationReports({ limit: 25 })
          .catch(() => ({ reports: [], worst_recent: null, filter: {} })),
      ]);
      setIncidents(incidentsRes.incidents || []);
      setReports(reportsRes.reports || []);
      setWorst(reportsRes.worst_recent ?? null);
      const killState = await killGet;
      if (killState.ok) {
        setKillSwitch(killState.value);
        setKillUnknown(false);
        killToastShown.current = false;
      } else {
        // Never hide the kill switch controls silently: keep the last
        // known state on screen and toast the failure (once per streak).
        setKillUnknown(true);
        if (!killToastShown.current) {
          killToastShown.current = true;
          toast({
            tone: "error",
            message:
              killState.error instanceof Error
                ? killState.error.message
                : String(killState.error),
          });
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function toggleKillSwitch() {
    const next = killSwitch ? !killSwitch.kill_switch : true;
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

  const filteredIncidents = useMemo(
    () =>
      kindFilter === "all"
        ? incidents
        : incidents.filter((i) => i.kind === kindFilter),
    [incidents, kindFilter],
  );

  // Dispatch actions available per selected incident kind: recon-type
  // events can trigger a reconciliation run, lost orders deep-link to the
  // orders surface.
  const selectedIsRecon =
    selected != null &&
    (selected.kind === "reconcile_drift" ||
      selected.kind.startsWith("recon:"));
  const selectedIsLostOrder = selected?.kind === "lost_order";

  async function runReconcileNow() {
    if (!selected) return;
    setBusy("recon");
    try {
      const rawAccount = (selected as Record<string, unknown>).account_id;
      const account_id =
        typeof rawAccount === "string" && rawAccount ? rawAccount : undefined;
      await clientApi.controlReconciliationRun({ account_id, operator: "dashboard" });
      toast({ tone: "ok", message: tEnum("reconcileDone") });
      setSelected(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

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
              <>
                <Pill tone={killSwitch.kill_switch ? "danger" : "ok"}>
                  {killSwitch.kill_switch
                    ? t("killSwitchEngaged")
                    : t("killSwitchReleased")}
                </Pill>
                {!killSwitch.kill_switch ? (
                  <ModePill
                    mode={killSwitch.live_trading_enabled ? "live" : "paper"}
                  />
                ) : null}
                <button
                  onClick={toggleKillSwitch}
                  disabled={busy === "kill"}
                  className="btn-ghost text-xs text-danger"
                  title={t("killSwitchToggleTitle")}
                >
                  {busy === "kill"
                    ? "…"
                    : killSwitch.kill_switch
                      ? t("releaseKill")
                      : t("engageKill")}
                </button>
              </>
            ) : killUnknown ? (
              <button
                onClick={toggleKillSwitch}
                disabled={busy === "kill"}
                className="btn-ghost text-xs text-danger"
                title={t("killSwitchToggleTitle")}
              >
                {busy === "kill" ? "…" : t("engageKill")}
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
              <Pill tone={severityTone(worst.severity)}>
                {enumLabel(SEVERITY_KEYS, worst.severity, tEnum)}
              </Pill>
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
          {/* Kind filter chips with counts — a 24h window can hold hundreds
              of rows; scanning by eye was the only option before. */}
          <span className="ml-4 text-ink-500">{t("colKind")}</span>
          <button
            onClick={() => setKindFilter("all")}
            className={`px-2.5 py-1 rounded-md border transition ${
              kindFilter === "all"
                ? "bg-brand-500/15 text-brand-100 border-brand-500/40"
                : "text-ink-400 border-transparent hover:text-ink-200 hover:border-brand-500/20"
            }`}
          >
            {tEnum("filterAll")} · {incidents.length}
          </button>
          {Object.keys(incidentsByKind)
            .sort()
            .map((kind) => (
              <button
                key={kind}
                onClick={() => setKindFilter(kind)}
                className={`px-2.5 py-1 rounded-md border transition ${
                  kindFilter === kind
                    ? "bg-brand-500/15 text-brand-100 border-brand-500/40"
                    : "text-ink-400 border-transparent hover:text-ink-200 hover:border-brand-500/20"
                }`}
              >
                {enumLabel(INCIDENT_KIND_KEYS, kind, tEnum)} ·{" "}
                {incidentsByKind[kind]}
              </button>
            ))}
        </div>

        <Card title={t("kpiIncidents")} description={t("incidentsDescription")}>
          {loading && incidents.length === 0 ? (
            <div className="space-y-2">
              {[0, 1, 2, 3, 4].map((i) => (
                <div key={i} className="skeleton h-7" />
              ))}
            </div>
          ) : filteredIncidents.length === 0 ? (
            <Empty label={t("noIncidentsInWindow")} />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>{t("colSeverity")}</th>
                    <th>{t("colKind")}</th>
                    <th>{t("colAccount")}</th>
                    <th>{t("colSubject")}</th>
                    <th>{t("colWhen")}</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {filteredIncidents.map((incident, idx) => (
                    <tr
                      key={`${incident.kind}-${idx}-${String(incident.ts)}`}
                      className="text-xs"
                    >
                      <td>
                        <Pill tone={severityTone(incident.severity)}>
                          {enumLabel(
                            SEVERITY_KEYS,
                            String(incident.severity || "info"),
                            tEnum,
                          )}
                        </Pill>
                      </td>
                      <td className="font-mono">
                        {enumLabel(INCIDENT_KIND_KEYS, incident.kind, tEnum)}
                      </td>
                      <td className="font-mono text-ink-300">
                        {String((incident as Record<string, unknown>).account_id ?? "–")}
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
          title={t("kpiReconReports")}
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
                          {enumLabel(SEVERITY_KEYS, r.severity, tEnum)}
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
            title={t("incidentDetailTitle", {
              kind: enumLabel(INCIDENT_KIND_KEYS, selected.kind, tEnum),
            })}
            actions={
              <div className="flex items-center gap-2">
                {/* Dispatch actions by kind: recon events can trigger a
                    reconciliation run right here; lost orders jump to the
                    orders surface (no ?id= deep link there yet). */}
                {selectedIsRecon ? (
                  <button
                    onClick={() => void runReconcileNow()}
                    disabled={busy === "recon"}
                    className="btn-ghost text-xs"
                  >
                    {busy === "recon" ? "…" : tEnum("runReconcile")}
                  </button>
                ) : null}
                {selectedIsLostOrder ? (
                  <Link href="/orders" className="btn-ghost text-xs">
                    {tEnum("viewOrders")}
                  </Link>
                ) : null}
                <button
                  onClick={() => setSelected(null)}
                  className="btn-ghost text-xs"
                >
                  {tCommon("close")}
                </button>
              </div>
            }
          >
            <Json value={selected} />
          </Card>
        ) : null}
      </PageBody>
    </div>
  );
}

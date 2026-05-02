"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  Card,
  Empty,
  ErrorBanner,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import { clientApi } from "../../lib/clientApi";
import type { TriggerRoute, TriggerSchedule } from "../../lib/clientApi";

/**
 * Workflows surface — .
 *
 * Operator-facing view of scheduled and event-driven automations. Talks
 * to the existing backend trigger routes (``/triggers/routes``,
 * ``/triggers/schedules``) but renders them in the new IA's tone:
 * named workflows, status pills, no exposed JSON internals.
 */

export default function WorkflowsPage() {
  const t = useTranslations("workflows");
  const [routes, setRoutes] = useState<TriggerRoute[]>([]);
  const [schedules, setSchedules] = useState<TriggerSchedule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [r, s] = await Promise.all([
          clientApi.triggerRoutes().catch(() => ({ routes: [] })),
          clientApi.triggerSchedules().catch(() => ({ schedules: [] })),
        ]);
        if (cancelled) return;
        const routes_ = Array.isArray(r) ? r : (r as { routes?: TriggerRoute[] }).routes ?? [];
        setRoutes(routes_);
        setSchedules(s.schedules ?? []);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    const t = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const activeRoutes = routes.filter((r) => !r.paused);
  const activeSchedules = schedules.filter((s) => !s.paused);

  return (
    <div>
      {error ? <ErrorBanner error={error} /> : null}
      <PageBody>
        <PageHeader
          eyebrow={t("eyebrow")}
          title={t("title")}
          description={t("description")}
          actions={
            <div className="flex items-center gap-2">
              <Pill tone={activeRoutes.length > 0 ? "ok" : "brand"}>
                {t("routesCount", { count: activeRoutes.length })}
              </Pill>
              <Pill tone={activeSchedules.length > 0 ? "ok" : "brand"}>
                {t("schedulesCount", { count: activeSchedules.length })}
              </Pill>
            </div>
          }
        />
        <SectionTabs section="strategy" />

        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <Card
            title={t("schedulesTitle")}
            description={t("schedulesDesc")}
            padded={false}
          >
            {loading && schedules.length === 0 ? (
              <div className="p-4 text-[12px] text-ink-500">{t("loadingEllipsis")}</div>
            ) : schedules.length === 0 ? (
              <Empty label={t("noSchedules")} />
            ) : (
              <ul className="embedded-list-scroll-lg">
                {schedules.map((s) => (
                  <li
                    key={s.id}
                    className="px-3 py-2.5 border-b border-brand-500/5 last:border-b-0"
                  >
                    {(() => {
                      const paused = Boolean(s.paused ?? s.enabled === false);
                      const label = s.title || s.description || s.id;
                      const cadence =
                        s.cron ||
                        s.interval ||
                        (s.every_seconds ? `${s.every_seconds}s` : "—");
                      return (
                        <>
                    <div className="flex items-center gap-2">
                      <span
                        className={`w-2 h-2 rounded-full ${
                          paused ? "bg-ink-500" : "bg-emerald-500"
                        }`}
                      />
                      <span className="text-[12.5px] text-ink-100 truncate flex-1">
                        {label}
                      </span>
                      <Pill tone={paused ? "brand" : "ok"}>
                        {paused ? t("paused") : t("active")}
                      </Pill>
                    </div>
                    <div className="text-[10.5px] text-ink-500 mt-1 font-mono">
                      {cadence} · {s.kind || t("triggerFallback")}
                    </div>
                        </>
                      );
                    })()}
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card
            title={t("routesTitle")}
            description={t("routesDesc")}
            padded={false}
          >
            {loading && routes.length === 0 ? (
              <div className="p-4 text-[12px] text-ink-500">{t("loadingEllipsis")}</div>
            ) : routes.length === 0 ? (
              <Empty label={t("noRoutes")} />
            ) : (
              <ul className="embedded-list-scroll-lg">
                {routes.map((r) => (
                  <li
                    key={r.id}
                    className="px-3 py-2.5 border-b border-brand-500/5 last:border-b-0"
                  >
                    {(() => {
                      const matchKind =
                        typeof r.match?.kind === "string" ? r.match.kind : "any";
                      const skill =
                        typeof r.action?.skill_id === "string"
                          ? r.action.skill_id
                          : "?";
                      return (
                        <>
                    <div className="flex items-center gap-2">
                      <span
                        className={`w-2 h-2 rounded-full ${
                          r.paused ? "bg-ink-500" : "bg-emerald-500"
                        }`}
                      />
                      <span className="text-[12.5px] text-ink-100 truncate flex-1">
                        {r.title || r.id}
                      </span>
                      <Pill tone={r.paused ? "brand" : "ok"}>
                        {r.paused ? t("paused") : t("active")}
                      </Pill>
                    </div>
                    <div className="text-[10.5px] text-ink-500 mt-1 font-mono">
                      {matchKind} → {skill}
                    </div>
                        </>
                      );
                    })()}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </PageBody>
    </div>
  );
}

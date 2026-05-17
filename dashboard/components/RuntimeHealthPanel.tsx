"use client";

/**
 * Runtime Health Panel — operator-facing card surfacing the runtime
 * truth (capability readiness + data source freshness + feature
 * flags + pending prompt-guard reviews) in a single compact card on the home
 * dashboard.
 *
 * Each row is read-only here; click-through opens the relevant deep page
 * (settings/memory/inbox).
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, Pill } from "./Page";
import { clientApi } from "../lib/clientApi";
import type {
  CapabilityReadinessEnvelope,
  DataSourceStatusEnvelope,
  PromptGuardListEnvelope,
  RuntimeFlagsEnvelope,
} from "../lib/operatorTypes";

type ReadinessState = {
  capabilities?: CapabilityReadinessEnvelope;
  dataSources?: DataSourceStatusEnvelope;
  flags?: RuntimeFlagsEnvelope;
  promptGuard?: PromptGuardListEnvelope;
  error?: string;
  loading: boolean;
};

const INITIAL: ReadinessState = { loading: true };

export function RuntimeHealthPanel() {
  const t = useTranslations("runtimeHealth");
  const [state, setState] = useState<ReadinessState>(INITIAL);

  const load = useCallback(async () => {
    const safe = async <T,>(p: Promise<T>): Promise<T | undefined> => {
      try {
        return await p;
      } catch {
        return undefined;
      }
    };
    const [capabilities, dataSources, flags, promptGuard] = await Promise.all([
      safe(clientApi.capabilityReadiness()),
      safe(clientApi.dataSourcesStatus()),
      safe(clientApi.runtimeFlags()),
      safe(clientApi.promptGuardList("pending")),
    ]);
    setState({
      capabilities,
      dataSources,
      flags,
      promptGuard,
      loading: false,
    });
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 45_000);
    return () => clearInterval(id);
  }, [load]);

  if (state.loading) {
    return <Card title={t("title")} description={t("loading")} />;
  }

  const cap = state.capabilities?.data;
  const ds = state.dataSources?.data;
  const ff = state.flags?.data;
  const pg = state.promptGuard;

  const blocked = cap?.counts?.blocked ?? 0;
  const degraded = cap?.counts?.degraded ?? 0;
  const ready = cap?.counts?.ready ?? 0;
  const totalCaps = cap?.total ?? 0;
  const stale = ds?.stale_count ?? 0;
  const totalDs = ds?.total ?? 0;
  const flagsEnabled = ff?.counts?.enabled ?? 0;
  const flagsTotal = ff?.counts?.total ?? 0;
  const pgPending = pg?.count ?? 0;

  const overallTone: "ok" | "warn" | "danger" | "brand" =
    blocked > 0 || pgPending > 0
      ? "danger"
      : degraded > 0 || stale > 0
        ? "warn"
        : "ok";

  return (
    <Card
      title={t("title")}
      description={t("description")}
      actions={
        <Pill tone={overallTone}>
          {overallTone === "ok"
            ? t("statusOk")
            : overallTone === "warn"
              ? t("statusWarn")
              : t("statusBlocked")}
        </Pill>
      }
    >
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <Row
          label={t("capabilities")}
          summary={
            blocked > 0
              ? t("capBlocked", { blocked, degraded })
              : degraded > 0
                ? t("capDegraded", { degraded, total: totalCaps })
                : t("capReady", { ready, total: totalCaps })
          }
          tone={blocked > 0 ? "danger" : degraded > 0 ? "warn" : "ok"}
          href="/settings"
          actionLabel={t("openSettings")}
        />
        <Row
          label={t("dataSources")}
          summary={
            stale > 0
              ? t("dsStale", { stale, total: totalDs })
              : t("dsFresh", { total: totalDs })
          }
          tone={stale > 0 ? "warn" : "ok"}
          href="/settings?section=integrations"
          actionLabel={t("openSettings")}
        />
        <Row
          label={t("flags")}
          summary={t("flagsSummary", { enabled: flagsEnabled, total: flagsTotal })}
          tone={flagsEnabled === flagsTotal ? "ok" : "warn"}
          href="/settings?section=runtime"
          actionLabel={t("openSettings")}
        />
        <Row
          label={t("promptGuard")}
          summary={
            pgPending > 0
              ? t("pgPending", { count: pgPending })
              : t("pgEmpty")
          }
          tone={pgPending > 0 ? "danger" : "ok"}
          href="/inbox"
          actionLabel={t("openInbox")}
        />
      </div>
    </Card>
  );
}

function Row({
  label,
  summary,
  tone,
  href,
  actionLabel,
}: {
  label: string;
  summary: string;
  tone: "ok" | "warn" | "danger" | "brand";
  href: string;
  actionLabel: string;
}) {
  return (
    <div className="flex items-start justify-between gap-3 px-3 py-2 rounded-lg border border-brand-500/15 bg-white/[0.02]">
      <div className="min-w-0">
        <div className="flex items-center gap-2 mb-0.5">
          <span className="text-[11px] text-ink-400 font-medium">
            {label}
          </span>
          <Pill tone={tone}>{tone === "ok" ? "OK" : tone === "warn" ? "WARN" : "DANGER"}</Pill>
        </div>
        <div className="text-[12.5px] text-ink-100 leading-snug">{summary}</div>
      </div>
      <Link
        href={href}
        className="text-[11px] px-2 py-0.5 rounded-md text-brand-200 border border-brand-500/25 hover:bg-brand-500/10 shrink-0"
      >
        {actionLabel} →
      </Link>
    </div>
  );
}

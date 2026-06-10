"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { clientApi } from "../lib/clientApi";
import type {
  AttentionItem,
  EnvelopeSeverity,
  OperatorOverviewData,
  OperatorOverviewEnvelope,
} from "../lib/operatorTypes";
import { Card, Pill } from "./Page";

const SEVERITY_TONE: Record<EnvelopeSeverity, "ok" | "warn" | "danger" | "brand"> = {
  info: "ok",
  warn: "warn",
  danger: "danger",
};

const STATUS_TONE: Record<string, "ok" | "warn" | "danger" | "brand"> = {
  ok: "ok",
  warn: "warn",
  error: "danger",
  blocked: "danger",
};

export function OperatorOverviewHero() {
  const t = useTranslations("operatorHero");
  const [env, setEnv] = useState<OperatorOverviewEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const next = await clientApi.operatorOverview();
        if (cancelled) return;
        setEnv(next);
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

  if (loading && !env) {
    return (
      <Card title={t("loadingTitle")} description={t("loadingDescription")} />
    );
  }
  if (error || !env) {
    return (
      <Card
        title={t("loadingTitle")}
        description={t("unavailable")}
      >
        <div className="text-[12px] text-rose-300 font-mono break-all">
          {error || t("noData")}
        </div>
      </Card>
    );
  }

  const data: OperatorOverviewData = env.data;
  const tone = STATUS_TONE[env.status] ?? "brand";

  return (
    <Card
      title={t("loadingTitle")}
      description={env.summary}
      actions={
        <div className="flex items-center gap-2">
          <Pill tone={tone}>{env.status.toUpperCase()}</Pill>
          {env.primary_action ? (
            env.primary_action.href ? (
              <Link
                href={env.primary_action.href}
                className="text-[11px] px-2 py-0.5 rounded-md text-brand-200 border border-brand-500/25 hover:bg-brand-500/10"
              >
                {env.primary_action.label} →
              </Link>
            ) : (
              <span className="text-[11px] text-ink-400">
                {env.primary_action.label}
              </span>
            )
          ) : null}
        </div>
      }
    >
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <HealthChip
          label={t("liveTrading")}
          on={data.health.live_trading}
          tone={data.health.live_trading ? "warn" : "brand"}
        />
        <HealthChip
          label={t("killSwitch")}
          on={data.health.kill_switch}
          tone={data.health.kill_switch ? "danger" : "ok"}
        />
        <HealthChip
          label={t("llmReady")}
          on={data.health.llm_ready}
          tone={data.health.llm_ready ? "ok" : "danger"}
          subtitle={t("tiersFraction", { ready: data.llm.ready_tiers, total: data.llm.total_tiers })}
        />
        <HealthChip
          label={t("strategies")}
          on={data.health.strategies}
          tone={data.health.strategies ? "ok" : "warn"}
          subtitle={t("packages", { count: data.counts.strategy_packages })}
        />
      </div>

      <AttentionList items={data.attention} />
    </Card>
  );
}

function HealthChip({
  label,
  on,
  tone,
  subtitle,
}: {
  label: string;
  on: boolean;
  tone: "ok" | "warn" | "danger" | "brand";
  subtitle?: string;
}) {
  const t = useTranslations("operatorHero");
  const accent =
    tone === "ok"
      ? { dot: "bg-emerald-500", text: "text-emerald-500" }
      : tone === "warn"
      ? { dot: "bg-amber-500", text: "text-amber-500" }
      : tone === "danger"
      ? { dot: "bg-rose-500", text: "text-rose-500" }
      : { dot: "bg-brand-400", text: "text-brand-300" };
  return (
    <div className="rounded-lg border border-[color:var(--line)] bg-ink-950/40 px-3 py-3 min-w-0 transition-colors hover:border-[color:var(--line-hi)]">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium text-[color:var(--text-muted)]">
          {t("status")}
        </span>
        <span className="flex items-center gap-1.5 shrink-0">
          <span className={`h-1.5 w-1.5 rounded-full ${on ? accent.dot : "bg-[color:var(--text-soft,#9c98ba)] opacity-30"}`} />
          <span className={`text-[11px] font-mono ${on ? accent.text : "text-[color:var(--text-muted)]"}`}>
            {on ? t("on") : t("off")}
          </span>
        </span>
      </div>
      <div className="mt-1 text-[13px] font-medium leading-snug text-[color:var(--text-base)]">
        {label}
      </div>
      {subtitle ? (
        <div className="text-[10.5px] text-[color:var(--text-muted)] mt-0.5 font-mono truncate">
          {subtitle}
        </div>
      ) : null}
    </div>
  );
}

function AttentionList({ items }: { items: AttentionItem[] }) {
  const t = useTranslations("operatorHero");
  if (!items.length) {
    return (
      <div className="text-[12px] text-ink-500 px-1 py-2">
        {t("nothingToAttend")}
      </div>
    );
  }
  return (
    <ul className="embedded-list-scroll space-y-2">
      {items.slice(0, 6).map((item) => (
        <li
          key={item.id}
          className="flex items-start gap-3 px-2 py-2 rounded-lg hover:bg-brand-500/5"
        >
          <span
            className={`mt-1 w-2 h-2 rounded-full ${dotColor(item.severity)}`}
          />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <Pill tone={SEVERITY_TONE[item.severity]}>
                {item.type.replace("_", " ")}
              </Pill>
              <span className="text-[12.5px] text-ink-100 truncate">
                {item.title}
              </span>
            </div>
            {item.summary ? (
              <div className="text-[11px] text-ink-500 mt-0.5 truncate">
                {item.summary}
              </div>
            ) : null}
          </div>
          {item.href ? (
            <Link
              href={item.href}
              className="text-[11px] px-2 py-0.5 rounded-md text-brand-200 border border-brand-500/25 hover:bg-brand-500/10 shrink-0"
            >
              {t("open")}
            </Link>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function dotColor(severity: EnvelopeSeverity) {
  if (severity === "danger") return "bg-rose-500";
  if (severity === "warn") return "bg-amber-400";
  return "bg-brand-400";
}

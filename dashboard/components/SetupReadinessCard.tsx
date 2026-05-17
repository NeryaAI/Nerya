"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { clientApi } from "../lib/clientApi";
import type {
  ReadinessCheck,
  SetupReadinessEnvelope,
} from "../lib/operatorTypes";
import { Card, Pill } from "./Page";

const STATUS_TONE: Record<
  ReadinessCheck["status"],
  "ok" | "warn" | "danger"
> = {
  ok: "ok",
  warn: "warn",
  blocked: "danger",
};

function titleCase(value: string): string {
  if (!value) return "";
  return value.charAt(0).toUpperCase() + value.slice(1).toLowerCase();
}

/**
 * Setup readiness card — first-run checklist.
 *
 * Mounted on the Home page (when not all checks pass) and also stands
 * alone as the body of ``/settings/setup``. Each check has a status
 * (``ok``/``warn``/``blocked``), a human summary, and an optional
 * ``fix`` action that deep-links into the right settings panel.
 */
export function SetupReadinessCard({ collapsed = false }: { collapsed?: boolean }) {
  const t = useTranslations("setupReadiness");
  const [env, setEnv] = useState<SetupReadinessEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const next = await clientApi.setupReadiness();
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
    const t = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  if (loading && !env) {
    return collapsed ? null : (
      <Card title={t("title")} description={t("loading")} />
    );
  }
  if (error || !env) {
    return collapsed ? null : (
      <Card title={t("title")} description={t("unavailable")}>
        <div className="text-[12px] text-rose-300 font-mono break-all">
          {error || t("noData")}
        </div>
      </Card>
    );
  }

  const checks = env.data.checks;
  const blocking = env.data.blocking || [];
  const isReady = env.status === "ok" && blocking.length === 0;

  // On Home, hide the card entirely once ready so we don't waste space.
  if (collapsed && isReady) return null;

  const tone =
    env.status === "ok" ? "ok" : env.status === "warn" ? "warn" : "danger";

  return (
    <Card
      title={t("title")}
      description={env.summary}
      actions={
        <div className="flex items-center gap-2">
          <Pill tone={tone}>{titleCase(env.status)}</Pill>
          {!collapsed ? null : (
            <button
              onClick={() => setOpen((v) => !v)}
              className="text-[11px] px-2 py-0.5 rounded-md text-brand-200 border border-brand-500/25 hover:bg-brand-500/10"
            >
              {open ? t("hide") : t("show")} ({checks.length})
            </button>
          )}
        </div>
      }
    >
      {open ? (
        <ul className="embedded-list-scroll space-y-2">
          {checks.map((check) => (
            <li
              key={check.name}
              className="flex items-start gap-3 px-2 py-2 rounded-lg border border-brand-500/10 bg-ink-900/40"
            >
              <span
                className={`mt-1 w-2 h-2 rounded-full ${
                  check.status === "ok"
                    ? "bg-emerald-500"
                    : check.status === "warn"
                    ? "bg-amber-400"
                    : "bg-rose-500"
                }`}
              />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <Pill tone={STATUS_TONE[check.status]}>
                    {titleCase(check.status)}
                  </Pill>
                  <span className="text-[12.5px] text-ink-100 truncate">
                    {check.name}
                  </span>
                </div>
                <div className="text-[11px] text-ink-500 mt-0.5">
                  {check.summary}
                </div>
              </div>
              {check.fix?.href ? (
                <Link
                  href={check.fix.href}
                  className="text-[11px] px-2 py-1 rounded-md border border-brand-500/40 text-brand-200 hover:bg-brand-500/10 shrink-0"
                  title={check.fix.disabled_reason || check.fix.label}
                >
                  {check.fix.label}
                </Link>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </Card>
  );
}

"use client";

import { copy as i18nCopy } from "../lib/i18n";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { clientApi } from "../lib/clientApi";
import { authHeaders, handleAuthFailure } from "../lib/auth";
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

/** Readiness reads are local. Only this explicit click issues a model request. */
export function SavedModelTest({ revision, dirty = false, disabled = false }: { revision?: string; dirty?: boolean; disabled?: boolean }) {
  const zh = useLocale().startsWith("zh");
  const tFailure = useTranslations("modelProbe");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ revision: string; ok: boolean; detail: string } | null>(null);
  const current = result && (!revision || result.revision === revision) ? result : null;
  async function testSaved() {
    if (busy || dirty || disabled) return;
    setBusy(true);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 100_000);
    let testedRevision = revision || "";
    try {
      if (!testedRevision) {
        const config = await clientApi.llmConfig() as Awaited<ReturnType<typeof clientApi.llmConfig>> & { revision?: string };
        testedRevision = config.revision || "";
      }
      if (!testedRevision) throw new Error(i18nCopy(zh, "copy.components_SetupReadinessCard.001"));
      const response = await fetch("/api/proxy/llm/messages/probe", {
        method: "POST", headers: authHeaders({ "content-type": "application/json" }), cache: "no-store",
        body: JSON.stringify({ revision: testedRevision, caller: "settings:connection-test" }),
        signal: controller.signal,
      });
      if (response.status === 401 || response.status === 403) {
        handleAuthFailure(response.status);
        throw new Error(i18nCopy(zh, "copy.components_SetupReadinessCard.002"));
      }
      const body = await response.json();
      if (!response.ok || !body.ok || body.revision !== testedRevision) {
        if (body.error === "llm_config_changed" || (body.ok && body.revision !== testedRevision)) {
          throw new Error(tFailure("configuration_changed"));
        }
        const knownKinds = ["unsupported_setting", "context_limit", "missing_credentials", "output_truncated", "empty_response", "tool_not_called", "task_not_allowed", "timeout", "authentication", "model_not_found", "rate_limit", "provider_unavailable", "invalid_request"];
        const kind = knownKinds.includes(body.failure_kind) ? body.failure_kind : "unknown";
        const detail = [tFailure(kind),
          body.status_code ? `HTTP ${Number(body.status_code)}` : "",
          typeof body.request_id === "string" && /^[\w.:-]{1,160}$/.test(body.request_id) ? `Request ID: ${body.request_id}` : "",
        ].filter(Boolean).join(" · ");
        throw new Error(detail);
      }
      setResult({ revision: testedRevision, ok: true, detail: [body.provider, body.model].filter(Boolean).join(" · ") });
    } catch (error) {
      setResult({ revision: testedRevision, ok: false, detail: controller.signal.aborted ? tFailure("timeout") : error instanceof Error ? error.message : tFailure("unknown") });
    } finally { clearTimeout(timeout); setBusy(false); }
  }
  return <div className="my-3 space-y-2 rounded-lg border border-[color:var(--line)] p-3" data-testid="saved-model-test">
    <div className="flex flex-wrap items-center gap-2">
      <Pill tone={dirty ? "warn" : current ? (current.ok ? "ok" : "danger") : "warn"}>
        {dirty ? (i18nCopy(zh, "copy.components_SetupReadinessCard.005")) : busy ? (i18nCopy(zh, "copy.components_SetupReadinessCard.006")) : current ? (current.ok ? (i18nCopy(zh, "copy.components_SetupReadinessCard.007")) : (i18nCopy(zh, "copy.components_SetupReadinessCard.008"))) : (revision ? (i18nCopy(zh, "copy.components_SetupReadinessCard.009")) : (i18nCopy(zh, "copy.components_SetupReadinessCard.010")))}
      </Pill>
      <button type="button" className="btn btn-secondary" disabled={disabled || dirty || busy} onClick={() => void testSaved()}>{i18nCopy(zh, "copy.components_SetupReadinessCard.011")}</button>
      {(revision || current?.revision) && <code className="text-xs text-[color:var(--text-muted)]">{(revision || current?.revision || "").slice(0, 10)}</code>}
    </div>
    <p className="text-xs text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_SetupReadinessCard.012")}</p>
    {current && !dirty && <p role="status" className="break-words text-xs">{current.detail}</p>}
  </div>;
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
  const [revision, setRevision] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [next, config] = await Promise.all([clientApi.setupReadiness(), clientApi.llmConfig()]);
        if (cancelled) return;
        setEnv(next);
        setRevision((config as typeof config & { revision?: string }).revision || "");
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
      {open && <SavedModelTest revision={revision} />}
    </Card>
  );
}

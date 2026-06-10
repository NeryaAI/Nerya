"use client";

/**
 * Runtime Feature Flags panel.
 *
 * Workspace-level runtime capability gates. Each flag controls one
 * operator-facing runtime surface (capability catalog, data source
 * sync, evidence vault, prompt-guard review, operator profile, E2E
 * artifacts, tool compaction). Disabling a flag returns a
 * ``blocked`` envelope from the corresponding routes so the rest of
 * the dashboard degrades gracefully.
 *
 * Backed by ``GET /runtime/flags`` + ``POST /runtime/flags/set`` +
 * ``POST /runtime/flags/refresh`` and persisted in the workspace
 * state file ``workspace/state/runtime_flags.json``.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, ErrorBanner, Pill } from "./Page";
import { SwitchControl } from "./SwitchControl";
import { clientApi } from "../lib/clientApi";
import type { RuntimeFlag } from "../lib/operatorTypes";

const FLAG_COPY_KEYS: Record<string, string> = {
  "runtime.capability_catalog_v2": "capabilityCatalog",
  "runtime.data_source_sync_state": "dataSourceSync",
  "runtime.tool_result_compaction": "toolResultCompaction",
  "runtime.evidence_vault": "evidenceVault",
  "runtime.prompt_guard_review_queue": "promptGuardReview",
  "runtime.operator_profile": "operatorProfile",
  "runtime.e2e_artifact_capture": "e2eArtifactCapture",
};

function formatPhase(raw: string) {
  const match = raw.match(/^phase(\d+)$/i);
  return match ? match[1] : raw;
}

export function RuntimeFlagsPanel() {
  const t = useTranslations("runtimeFlags");
  const tCommon = useTranslations("common");
  const tDynamic = t as unknown as (key: string, values?: Record<string, string | number | boolean>) => string;

  const [flags, setFlags] = useState<RuntimeFlag[]>([]);
  const [overridesPath, setOverridesPath] = useState<string>("");
  const [counts, setCounts] = useState<{ total: number; enabled: number; disabled: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const env = await clientApi.runtimeFlags();
      setFlags(env.data.flags ?? []);
      setOverridesPath(env.data.overrides_path ?? "");
      setCounts(env.data.counts ?? null);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function toggle(key: string, enabled: boolean) {
    setBusyKey(key);
    try {
      const env = await clientApi.runtimeFlagSet(key, enabled);
      if (!env.ok) {
        setError(env.summary || t("setFailed"));
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  async function clearOverride(key: string) {
    setBusyKey(key);
    try {
      await clientApi.runtimeFlagSet(key, null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  async function refreshCache() {
    setBusyKey("__refresh__");
    try {
      await clientApi.runtimeFlagRefresh();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <Card
      title={t("title")}
      description={t("description")}
      actions={
        <div className="flex items-center gap-2">
          {counts ? (
            <Pill tone={counts.disabled === 0 ? "ok" : "warn"}>
              {t("statusSummary", { enabled: counts.enabled, total: counts.total })}
            </Pill>
          ) : null}
          <button
            onClick={refreshCache}
            disabled={busyKey === "__refresh__"}
            className="text-[11px] px-2 py-0.5 rounded-md text-brand-200 border border-brand-500/25 hover:bg-brand-500/10 disabled:opacity-50"
          >
            {busyKey === "__refresh__" ? tCommon("refreshing") : tCommon("refresh")}
          </button>
        </div>
      }
    >
      {error ? <ErrorBanner error={error} /> : null}
      {loading ? (
        <div className="text-[12px] text-ink-500">{tCommon("loading")}</div>
      ) : (
        <>
          <div className="mb-3 rounded-lg border border-brand-500/15 bg-brand-500/6 px-3 py-2.5">
            <div className="text-[12px] font-medium text-ink-100">{t("configTitle")}</div>
            <div className="mt-1 text-[11px] leading-snug text-ink-400">
              {t("configDescription")}
            </div>
            {overridesPath ? (
              <div className="mt-2 text-[10.5px] text-ink-500">
                {t("configPath")}: <span className="font-mono">{overridesPath}</span>
              </div>
            ) : null}
            <div className="mt-1 text-[10.5px] text-ink-500">
              {t("configHint")}
            </div>
          </div>
          <ul className="space-y-2">
            {flags.map((f) => {
              const overridden = f.enabled !== f.default;
              const copyKey = FLAG_COPY_KEYS[f.key];
              const semanticTitle = copyKey ? tDynamic(`items.${copyKey}.title`) : t("unknownTitle");
              const semanticSummary = copyKey ? tDynamic(`items.${copyKey}.summary`) : f.summary;
              const semanticScope = copyKey ? tDynamic(`items.${copyKey}.scope`) : f.key;
              return (
                <li
                  key={f.key}
                  className="px-3 py-2 rounded-lg border border-brand-500/15 bg-white/[0.02]"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2 mb-1">
                        <span className="text-[13px] font-medium text-ink-100">
                          {semanticTitle}
                        </span>
                        <Pill tone={f.enabled ? "ok" : "warn"}>
                          {f.enabled ? t("enabledStatus") : t("disabledStatus")}
                        </Pill>
                        <Pill tone="brand">{t("phaseLabel", { phase: formatPhase(f.phase) })}</Pill>
                        {overridden ? (
                          <Pill tone="warn">{t("overridden")}</Pill>
                        ) : null}
                      </div>
                      <div className="text-[11.5px] text-ink-500 leading-snug">
                        {semanticSummary}
                      </div>
                      <div className="mt-1 text-[10.5px] text-ink-500 leading-snug">
                        <span className="font-medium text-ink-400">{t("scopeLabel")}: </span>
                        {semanticScope}
                      </div>
                      <details className="mt-1 text-[10px] text-ink-500">
                        <summary className="inline cursor-pointer select-none text-ink-500 hover:text-ink-300">
                          {t("technicalDetails")}
                        </summary>
                        <div className="mt-1 space-y-0.5 font-mono">
                          <div>{t("technicalKey")}: {f.key}</div>
                          <div>
                            {t("envOverride")}: {f.env_override} ·{" "}
                            {t("defaultLabel")}: {f.default ? t("on") : t("off")}
                          </div>
                        </div>
                      </details>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <SwitchControl
                        checked={f.enabled}
                        disabled={busyKey === f.key}
                        label={f.enabled ? t("on") : t("off")}
                        onCheckedChange={(v) => void toggle(f.key, v)}
                      />
                      {overridden ? (
                        <button
                          onClick={() => clearOverride(f.key)}
                          disabled={busyKey === f.key}
                          className="text-[11px] px-2 py-0.5 rounded-md text-ink-400 border border-brand-500/15 hover:bg-brand-500/10 disabled:opacity-50"
                        >
                          {t("clearOverride")}
                        </button>
                      ) : null}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </Card>
  );
}

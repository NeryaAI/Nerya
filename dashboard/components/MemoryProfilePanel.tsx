"use client";

/**
 * Memory > Profile subtab panel.
 *
 * Operator Preference Profile — operator-facing card for the
 * preference/notes profile (`/memory/profile/*`).
 *
 * Lets the operator inspect, set, pin, and forget preference facts.
 * The backend enforces a trading-safety boundary: keys like
 * ``live_trading_enabled`` or ``risk.max_drawdown_usd`` are rejected
 * server-side, so the UI does not need to mirror the deny list.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, Empty, ErrorBanner, Pill } from "./Page";
import { clientApi } from "../lib/clientApi";
import type { ProfileFact } from "../lib/operatorTypes";

const FACET_OPTIONS = [
  "style",
  "tooling",
  "universe",
  "risk_preference",
  "veto",
  "channel",
];

export function MemoryProfilePanel() {
  const t = useTranslations("memoryProfile");
  const tCommon = useTranslations("common");

  const [facts, setFacts] = useState<ProfileFact[]>([]);
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [includeForgotten, setIncludeForgotten] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [draftFacet, setDraftFacet] = useState("style");
  const [draftKey, setDraftKey] = useState("");
  const [draftValue, setDraftValue] = useState("");

  const load = useCallback(async () => {
    try {
      const env = await clientApi.profileList({ include_forgotten: includeForgotten });
      if (env.ok) {
        setFacts(env.facts ?? []);
        setStats((env.stats as unknown as Record<string, unknown>) ?? {});
        setError(null);
      } else {
        setError(env.error || env.detail || t("disabled"));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [includeForgotten, t]);

  useEffect(() => {
    load();
  }, [load]);

  async function addFact() {
    if (!draftKey.trim()) return;
    setBusy(true);
    try {
      let parsed: unknown = draftValue;
      // Try JSON-parse so booleans/numbers/arrays/objects pass through cleanly,
      // otherwise fall back to the raw string.
      try {
        parsed = JSON.parse(draftValue);
      } catch {
        parsed = draftValue;
      }
      const env = await clientApi.profileSet({
        facet: draftFacet,
        key: draftKey.trim(),
        value: parsed,
      });
      if (!env.ok) {
        setError(env.error || t("setFailed"));
      } else {
        setDraftKey("");
        setDraftValue("");
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function actOn(id: string, action: "pin" | "forget") {
    setBusy(true);
    try {
      const env = action === "pin"
        ? await clientApi.profilePin(id)
        : await clientApi.profileForget(id);
      if (!env.ok) {
        setError(env.error || t("setFailed"));
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {error ? <ErrorBanner error={error} /> : null}

      <Card title={t("title")} description={t("description")}>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-2 items-end">
          <div>
            <div className="text-[11px] text-ink-400 font-medium mb-1">
              {t("facet")}
            </div>
            <select
              value={draftFacet}
              onChange={(e) => setDraftFacet(e.target.value)}
              className="w-full text-[12px] bg-ink-950/40 border border-brand-500/25 rounded-md px-2 py-1 text-ink-100"
            >
              {FACET_OPTIONS.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </div>
          <div>
            <div className="text-[11px] text-ink-400 font-medium mb-1">
              {t("key")}
            </div>
            <input
              value={draftKey}
              onChange={(e) => setDraftKey(e.target.value)}
              placeholder={t("keyPlaceholder")}
              className="w-full text-[12px] bg-ink-950/40 border border-brand-500/25 rounded-md px-2 py-1 text-ink-100"
            />
          </div>
          <div>
            <div className="text-[11px] text-ink-400 font-medium mb-1">
              {t("value")}
            </div>
            <input
              value={draftValue}
              onChange={(e) => setDraftValue(e.target.value)}
              placeholder={t("valuePlaceholder")}
              className="w-full text-[12px] bg-ink-950/40 border border-brand-500/25 rounded-md px-2 py-1 text-ink-100"
            />
          </div>
          <button
            disabled={busy || !draftKey.trim()}
            onClick={addFact}
            className="text-[12px] px-3 py-1.5 rounded-md text-brand-200 border border-brand-500/40 hover:bg-brand-500/10 disabled:opacity-50"
          >
            {t("addFact")}
          </button>
        </div>
        <div className="mt-2 text-[10.5px] text-ink-500">
          {t("safetyHint")}
        </div>
      </Card>

      <Card
        title={t("factsTitle")}
        description={t("factsDescription", { total: Number(stats.total ?? 0) })}
        actions={
          <label className="flex items-center gap-2 text-[11px] text-ink-300">
            <input
              type="checkbox"
              checked={includeForgotten}
              onChange={(e) => setIncludeForgotten(e.target.checked)}
            />
            {t("includeForgotten")}
          </label>
        }
        padded={false}
      >
        {loading ? (
          <div className="p-4 text-[12px] text-ink-500">{tCommon("loading")}</div>
        ) : facts.length === 0 ? (
          <Empty label={t("empty")} />
        ) : (
          <ul>
            {facts.map((f) => (
              <li
                key={f.id}
                className="px-3 py-2 border-b border-brand-500/5 last:border-b-0"
              >
                <div className="flex items-center gap-2">
                  <Pill tone="brand">{f.facet}</Pill>
                  <span className="text-[12.5px] font-mono text-ink-100 truncate">
                    {f.key}
                  </span>
                  <span className="text-[12px] text-ink-300 truncate flex-1">
                    {typeof f.value === "string"
                      ? f.value
                      : JSON.stringify(f.value)}
                  </span>
                  {f.pinned ? <Pill tone="ok">{t("pinned")}</Pill> : null}
                  {f.forgotten ? <Pill tone="warn">{t("forgotten")}</Pill> : null}
                  <button
                    disabled={busy || f.pinned}
                    onClick={() => actOn(f.id, "pin")}
                    className="text-[11px] px-2 py-0.5 rounded-md border border-brand-500/30 text-brand-200 hover:bg-brand-500/10 disabled:opacity-40"
                  >
                    {t("pin")}
                  </button>
                  <button
                    disabled={busy || f.forgotten}
                    onClick={() => actOn(f.id, "forget")}
                    className="text-[11px] px-2 py-0.5 rounded-md border border-rose-500/30 text-rose-200 hover:bg-rose-500/10 disabled:opacity-40"
                  >
                    {t("forget")}
                  </button>
                </div>
                <div className="text-[10.5px] text-ink-500 mt-1 font-mono">
                  {f.ts} · {f.scope} · {f.source}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

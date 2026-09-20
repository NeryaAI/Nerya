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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Card, Empty, ErrorBanner, LoadingState, Pill } from "./Page";
import { ChoiceSelect } from "./ChoiceSelect";
import { Pagination, SearchField, useListPage } from "./ListControls";
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
  const zh = useLocale().startsWith("zh");
  const facetNames: Record<string, string> = zh
    ? { style: "表达风格", tooling: "工具偏好", universe: "关注市场", risk_preference: "风险偏好", veto: "避免事项", channel: "通知渠道" }
    : { style: "Style", tooling: "Tools", universe: "Markets", risk_preference: "Risk preferences", veto: "Things to avoid", channel: "Notifications" };

  const [facts, setFacts] = useState<ProfileFact[]>([]);
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [includeForgotten, setIncludeForgotten] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const generation = useRef(0);

  const [draftFacet, setDraftFacet] = useState("style");
  const [draftKey, setDraftKey] = useState("");
  const [draftValue, setDraftValue] = useState("");

  const load = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    try {
      const env = await clientApi.profileList({ include_forgotten: includeForgotten });
      if (request !== generation.current) return;
      if (env.ok) {
        setFacts(env.facts ?? []);
        setStats((env.stats as unknown as Record<string, unknown>) ?? {});
        setError(null);
      } else {
        setError(env.error || env.detail || t("disabled"));
      }
    } catch (e) {
      if (request === generation.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [includeForgotten, t]);

  useEffect(() => {
    void load();
    return () => { generation.current += 1; };
  }, [load]);

  async function addFact() {
    if (!draftKey.trim()) return;
    setBusy(true);
    setActionError(null);
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
        throw new Error(env.error || t("setFailed"));
      } else {
        setDraftKey("");
        setDraftValue("");
      }
      await load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function actOn(id: string, action: "pin" | "forget") {
    setBusy(true);
    setActionError(null);
    try {
      const env = action === "pin"
        ? await clientApi.profilePin(id)
        : await clientApi.profileForget(id);
      if (!env.ok) {
        throw new Error(env.error || t("setFailed"));
      }
      await load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const filtered = useMemo(() => facts.filter((fact) =>
    `${fact.key} ${fact.facet} ${JSON.stringify(fact.value)}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())), [facts, query]);
  const paging = useListPage(filtered, `${query}:${includeForgotten}`);

  return (
    <div className="space-y-4">
      {error ? <ErrorBanner error={error} onRetry={() => void load()} /> : null}
      <ErrorBanner error={actionError} />

      <Card title={t("title")} description={t("description")}>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-2 items-end">
          <div>
            <div className="text-[11px] text-ink-400 font-medium mb-1">
              {t("facet")}
            </div>
            <ChoiceSelect
              aria-label={t("facet")}
              value={draftFacet}
              onValueChange={setDraftFacet}
              className="w-full text-[12px] bg-ink-950/40 border border-brand-500/25 rounded-md px-2 py-1 text-ink-100"
            >
              {FACET_OPTIONS.map((f) => (
                <option key={f} value={f}>
                  {facetNames[f] ?? f}
                </option>
              ))}
            </ChoiceSelect>
          </div>
          <div>
            <div className="text-[11px] text-ink-400 font-medium mb-1">
              {t("key")}
            </div>
            <input
              aria-label={t("key")}
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
              aria-label={t("value")}
              value={draftValue}
              onChange={(e) => setDraftValue(e.target.value)}
              placeholder={t("valuePlaceholder")}
              className="w-full text-[12px] bg-ink-950/40 border border-brand-500/25 rounded-md px-2 py-1 text-ink-100"
            />
          </div>
          <button
            disabled={busy || !draftKey.trim()}
            onClick={addFact}
            className="btn btn-primary"
          >
            {t("addFact")}
          </button>
        </div>
        <div className="mt-2 text-[10.5px] text-ink-500">
          {t("safetyHint")}
        </div>
      </Card>

      <SearchField value={query} onChange={setQuery} label={zh ? "搜索偏好记录" : "Search preferences"} />
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
        {loading && !facts.length ? (
          <LoadingState label={tCommon("loading")} />
        ) : error && !facts.length ? null : filtered.length === 0 ? (
          <Empty label={query ? (zh ? "没有匹配的偏好" : "No matching preferences") : t("empty")}
            subtitle={query ? (zh ? "更换关键词，或清除搜索查看全部记录。" : "Try another term or clear the search.") : (zh ? "在上方添加一条偏好，让 Agent 更了解你的工作方式。" : "Add a preference above to guide how your agent works.")}
            action={query ? <button type="button" className="btn btn-ghost" onClick={() => setQuery("")}>{zh ? "清除搜索" : "Clear search"}</button> : undefined} />
        ) : (
          <><ul>
            {paging.rows.map((f) => (
              <li
                key={f.id}
                className="px-3 py-2 border-b border-brand-500/5 last:border-b-0"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Pill tone="brand">{facetNames[f.facet] ?? f.facet}</Pill>
                  <span className="text-[12.5px] font-mono text-ink-100 truncate">
                    {f.key}
                  </span>
                  <span className="min-w-[120px] flex-1 break-words text-[13px] text-ink-300">
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
          </ul><div className="px-3 pb-3"><Pagination {...paging} /></div></>
        )}
      </Card>
    </div>
  );
}

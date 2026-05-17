"use client";

/**
 * Memory > Evidence subtab panel.
 *
 * Trading Evidence Vault — operator-facing card for the trading
 * decision evidence index (`/evidence/*`).
 *
 * Lets the operator:
 * - browse the per-source rollup,
 * - search citeable evidence across strategy / backtest / trade / gateway /
 *   research artifacts,
 * - drill into a single evidence document,
 * - trigger the synthetic demo ingest for smoke testing.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, Empty, ErrorBanner, Json, Pill } from "./Page";
import { clientApi } from "../lib/clientApi";
import type { EvidenceDoc } from "../lib/operatorTypes";

type SourceRow = {
  source_type: string;
  source_id: string;
  count: number;
  last_at?: string;
};

export function MemoryEvidencePanel() {
  const t = useTranslations("memoryEvidence");
  const tCommon = useTranslations("common");

  const [query, setQuery] = useState("");
  const [sources, setSources] = useState<SourceRow[]>([]);
  const [results, setResults] = useState<EvidenceDoc[]>([]);
  const [selected, setSelected] = useState<EvidenceDoc | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const env = await clientApi.evidenceSources();
      if (env.ok) {
        setSources((env.data.sources as unknown as SourceRow[]) ?? []);
      }
      const search = await clientApi.evidenceSearch({ q: query || undefined, scope: "any" });
      setResults(search.data.results ?? []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function runDemoIngest() {
    setBusy(true);
    try {
      await clientApi.evidenceIngestRun({ kind: "demo" });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {error ? <ErrorBanner error={error} /> : null}

      <Card
        title={t("title")}
        description={t("description")}
        actions={
          <button
            onClick={runDemoIngest}
            disabled={busy}
            className="text-[11px] px-2 py-0.5 rounded-md text-brand-200 border border-brand-500/25 hover:bg-brand-500/10 disabled:opacity-50"
          >
            {busy ? tCommon("loading") : t("ingestDemo")}
          </button>
        }
      >
        <div className="flex flex-wrap gap-2 items-center">
          <input
            value={query}
            placeholder={t("searchPlaceholder")}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && refresh()}
            className="flex-1 min-w-[180px] text-[12px] bg-ink-950/40 border border-brand-500/25 rounded-md px-2 py-1 text-ink-100"
          />
          <button
            onClick={refresh}
            className="text-[11px] px-2 py-1 rounded-md text-brand-200 border border-brand-500/25 hover:bg-brand-500/10"
          >
            {tCommon("search")}
          </button>
        </div>
        <div className="mt-3 text-[11px] text-ink-500">
          {t("sourcesSummary", { count: sources.length })}
        </div>
        {sources.length > 0 ? (
          <div className="mt-2 grid grid-cols-2 md:grid-cols-3 gap-1.5">
            {sources.map((s) => (
              <div
                key={`${s.source_type}:${s.source_id}`}
                className="px-2 py-1 rounded-md border border-brand-500/10 bg-white/[0.02]"
              >
                <div className="flex items-center gap-1.5">
                  <Pill tone="brand">{s.source_type}</Pill>
                  <span className="text-[11.5px] font-mono text-ink-300 truncate">
                    {s.source_id}
                  </span>
                </div>
                <div className="text-[10.5px] text-ink-500 mt-0.5">
                  {t("docs", { count: s.count })}
                </div>
              </div>
            ))}
          </div>
        ) : null}
      </Card>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <Card
          title={t("resultsCount", { count: results.length })}
          padded={false}
        >
          {loading && !results.length ? (
            <div className="p-4 text-[12px] text-ink-500">{tCommon("loading")}</div>
          ) : results.length === 0 ? (
            <Empty label={t("noResults")} />
          ) : (
            <ul className="embedded-list-scroll-lg">
              {results.map((doc) => (
                <li
                  key={doc.evidence_id}
                  className={`px-3 py-2.5 border-b border-brand-500/5 last:border-b-0 cursor-pointer hover:bg-brand-500/5 ${
                    selected?.evidence_id === doc.evidence_id
                      ? "bg-brand-500/10"
                      : ""
                  }`}
                  onClick={() => setSelected(doc)}
                >
                  <div className="flex items-center gap-2">
                    <Pill tone="brand">{doc.source_type}</Pill>
                    <span className="text-[12px] text-ink-100 truncate flex-1">
                      {doc.title}
                    </span>
                    <Pill tone={doc.scope === "shared" ? "ok" : "warn"}>
                      {doc.scope}
                    </Pill>
                  </div>
                  {doc.summary ? (
                    <div className="text-[11px] text-ink-500 mt-1 truncate">
                      {doc.summary}
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="xl:col-span-2 space-y-4">
          {selected ? (
            <Card
              title={selected.title}
              description={`${selected.source_type} · ${selected.evidence_id}`}
              actions={
                <Pill tone={selected.scope === "shared" ? "ok" : "warn"}>
                  {selected.scope}
                </Pill>
              }
            >
              {selected.summary ? (
                <div className="text-[12px] text-ink-200 whitespace-pre-wrap mb-3">
                  {selected.summary}
                </div>
              ) : null}
              {selected.tags?.length ? (
                <div className="flex flex-wrap gap-1 mb-3">
                  {selected.tags.map((tag) => (
                    <Pill key={tag} tone="brand">
                      {tag}
                    </Pill>
                  ))}
                </div>
              ) : null}
              <div className="text-[11px] text-ink-500 font-medium mb-1">
                {t("rawDoc")}
              </div>
              <Json value={selected as unknown} />
            </Card>
          ) : (
            <Card title={t("selectDoc")}>
              <div className="text-[12px] text-ink-500">{t("selectHint")}</div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

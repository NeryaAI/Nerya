"use client";
import { useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import type { ChartBlockShape } from "../../lib/chartBlock";
import type { ResearchInstrument } from "../../lib/researchVisuals";
import { instrumentId } from "../../lib/researchVisuals";
import { ResearchAssetCard } from "../../components/chat/ResearchReplyCards";
import { ResearchCharts, ResearchInstrumentPanel } from "../../components/chat/ResearchWorkspace";

export function ResearchLiveEmpty() {
  const t = useTranslations("researchLivePreview");
  return <p className="p-8">{t("empty")}</p>;
}

/** Live-data visual check only. This viewer does not create an Agent message or alter a session. */
export function ResearchLivePreview({ publication }: { publication: { chart_blocks: ChartBlockShape[]; research_context: { instruments: Record<string, any>[] }; receipt: { chart_ids: string[] } } }) {
  const t = useTranslations("researchLivePreview");
  const [selected, setSelected] = useState("");
  const [study, setStudy] = useState(false);
  const charts = publication.chart_blocks;
  const instruments = useMemo<ResearchInstrument[]>(() => publication.research_context.instruments.map(item => ({
    id: instrumentId(item.market, item.venue), market: item.market, venue: item.venue, name: item.name,
    interval: item.interval, news: item.news || [], newsAsOf: item.news_as_of || "", newsStatus: item.news_status || "not_requested", seenAt: 0,
    chartIds: charts.filter(block => block.instrument && (block.instrument as Record<string, string>).market === item.market).map(block => block.chart_id),
  })), [publication, charts]);
  const active = instruments.find(item => item.id === selected);
  const studies = charts.filter(block => !block.instrument);
  return <div className="flex h-full min-h-0 flex-col text-[color:var(--text-base)]" data-testid="live-research-preview">
    <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-[color:var(--line)] px-6 py-4">
      <div><h1 className="text-base font-semibold">{t("title")}</h1><p className="mt-1 text-xs text-[color:var(--text-muted)]">{t("sourceHint")}</p></div>
      <button type="button" className="rounded-lg border border-[color:var(--line)] px-3 py-2 text-xs" onClick={() => setStudy(!study)}>{t(study ? "showCards" : "showComparison")}</button>
    </header>
    <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
      <div className="min-w-0 flex-1 overflow-y-auto">
        {study ? <ResearchCharts charts={studies} selected="" onSelect={() => {}} /> : <section className="mx-auto max-w-[900px] space-y-4 px-6 py-10" data-testid="live-price-cards">
          <p className="text-xs text-[color:var(--text-muted)]">{t("sameCards")}</p>
          <h2 className="text-2xl font-semibold tracking-tight">{t("marketSnapshot")}</h2>
          <p className="pb-4 text-sm leading-7 text-[color:var(--text-muted)]">{t("sourceDescription")}</p>
          {[...instruments].reverse().map(item => { const block = charts.find(chart => item.chartIds.includes(chart.chart_id)); return block && <ResearchAssetCard key={item.id} instrument={item} block={block} onOpen={() => setSelected(item.id)} />; })}
          <p className="pt-4 text-xs leading-6 text-[color:var(--text-muted)]">{t("validationLimit")}</p>
        </section>}
      </div>
      {active && <aside className="min-h-0 w-full overflow-y-auto border-l border-[color:var(--line)] bg-[color:var(--card)] lg:w-[48%]" aria-label={t("details")}>
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-[color:var(--line)] bg-[color:var(--card)] px-5 py-3 text-xs"><span>{t("detailTitle",{name:active.name})}</span><button type="button" onClick={() => setSelected("")} aria-label={t("collapseDetails")}>{t("collapse")} ×</button></div>
        <ResearchInstrumentPanel key={active.id} instrument={active} charts={charts} />
      </aside>}
    </div>
  </div>;
}

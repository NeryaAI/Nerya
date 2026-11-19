"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card } from "../Page";
import { ChoiceSelect } from "../ChoiceSelect";
import { CandleChart } from "../CandleChart";
import { callApi } from "../../lib/clientApi";
import type { Candle } from "../../lib/api";
import { strategyMarketTarget, strategyMarketResponseMatches } from "../../lib/strategyLanding";

type CandleResult = { candles?: Candle[]; error?: string; market?: string; interval?: string };

export function StrategyMarketChart({ markets }: { markets: string[] }) {
  const t = useTranslations("workflowExperience");
  const [picked, setPicked] = useState("");
  const market = markets.includes(picked) ? picked : markets[0] || "";
  const [interval, setInterval] = useState("1h");
  const [refresh, setRefresh] = useState(0);
  const [data, setData] = useState<{ key: string; candles: Candle[]; updated: string; priceSamples: boolean } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const key = `${market}:${interval}`;

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    const target = strategyMarketTarget(market);
    setError(""); setData(null);
    if (!target) { setLoading(false); return; }
    async function poll() {
      if (disposed || !target) return;
      if (document.hidden) { timer = setTimeout(poll, 15_000); return; }
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 15_000);
      setLoading(true);
      try {
        const result = await callApi<CandleResult>("/market/candles", {
          method: "POST", body: { ...target, interval, count: 180 }, signal: controller.signal,
        });
        if (result.error) throw new Error(result.error);
        if (!strategyMarketResponseMatches(target, result.market) || result.interval && result.interval !== interval) throw new Error(t("marketMismatch"));
        const candles = (result.candles || []).filter((row) =>
          [row.ts, row.open, row.high, row.low, row.close].every(Number.isFinite)
          && row.high >= Math.max(row.open, row.close) && row.low <= Math.min(row.open, row.close));
        const priceSamples = candles.length > 0 && candles.every(row => row.data_kind === "price_samples");
        if (!disposed) { setData({ key, candles, updated: new Date().toLocaleTimeString(), priceSamples }); setError(""); }
      } catch (reason) {
        if (!disposed) setError(String(reason));
      } finally {
        clearTimeout(timeout);
        if (!disposed) { setLoading(false); timer = setTimeout(poll, 30_000); }
      }
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); };
  }, [market, interval, refresh, key, t]);

  const current = data?.key === key ? data : null;
  return <Card title={t(current?.priceSamples ? "priceSamples" : "markets")} description={t(current?.priceSamples ? "priceSamplesDescription" : "marketDescription")}>
    <section data-testid="strategy-market-chart" className="space-y-3">
      {markets.length ? <>
        <div className="flex flex-wrap items-center gap-3">
          <ChoiceSelect aria-label={t("market")} value={market} onValueChange={setPicked}>{markets.map((id) => <option key={id} value={id}>{id}</option>)}</ChoiceSelect>
          <ChoiceSelect aria-label={t("interval")} value={interval} onValueChange={setInterval}>{["1m", "5m", "15m", "1h", "4h", "1d"].map((value) => <option key={value} value={value}>{value}</option>)}</ChoiceSelect>
          <button type="button" className="btn btn-ghost" disabled={loading} onClick={() => setRefresh((value) => value + 1)}>{t("refresh")}</button>
          {current && <span className="text-xs text-ink-400">{t("updated", { time: current.updated })}{error ? ` · ${t("stale")}` : ""}</span>}
        </div>
        {!strategyMarketTarget(market) ? <p role="status" className="text-sm text-ink-400">{t("missingVenue")}</p> : <CandleChart candles={current?.candles || []} height={320} loading={loading && !current} error={error || undefined} />}
      </> : <p className="text-sm text-ink-400">{t("noMarkets")}</p>}
    </section>
  </Card>;
}

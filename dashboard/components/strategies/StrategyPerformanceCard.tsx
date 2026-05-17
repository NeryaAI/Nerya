"use client";

/**
 * Per-strategy performance dashboard.
 *
 * One scrollable section that answers "what has this strategy
 * actually been doing?": open positions (this strategy's slice of
 * any merged ``(account, market)`` row), recent orders & fills, and a
 * downsampled equity curve. Replaces the raw-JSON ledger dump that
 * used to live behind the "History" tab.
 *
 * Pulls a single ``/strategy/performance`` envelope so the entire
 * surface renders from one round trip. Every number is shown in human
 * units (USD, BTC, %) — raw JSON is hidden behind an "Advanced" toggle
 * that operators only need when reproducing a bug.
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslations, useLocale } from "next-intl";

import { Card, Empty, Json, Kpi, Pill } from "../Page";
import { Sparkline } from "../Sparkline";
import {
  clientApi,
  type StrategyPerformanceEnvelope,
  type StrategyPerformanceEquityPoint,
  type StrategyPerformanceFill,
  type StrategyPerformanceOrder,
  type StrategyPerformancePosition,
} from "../../lib/clientApi";

interface Props {
  strategyId: string;
  /**
   * Poll cadence in ms. Defaults to 15s. The card is a heavy SQL read
   * but the data is cheap on the backend (paginated + indexed), and
   * operators expect "live" PnL on the performance surface.
   */
  refreshMs?: number;
}

export function StrategyPerformanceCard({
  strategyId,
  refreshMs = 15_000,
}: Props) {
  const t = useTranslations("strategyPerformance");
  const locale = useLocale();
  const [envelope, setEnvelope] =
    useState<StrategyPerformanceEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showRaw, setShowRaw] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function fetchOnce() {
      try {
        const res = await clientApi.strategyPerformance(strategyId, {
          limit_orders: 30,
          limit_fills: 30,
          equity_points: 200,
        });
        if (cancelled) return;
        setEnvelope(res);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void fetchOnce();
    timer = setInterval(fetchOnce, refreshMs);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [strategyId, refreshMs]);

  const kpis = envelope?.kpis;
  const positions = envelope?.positions ?? [];
  const orders = envelope?.orders ?? [];
  const fills = envelope?.fills ?? [];
  const equity = envelope?.equity_curve ?? [];

  // Equity sparkline values — cumulative net realised - fees as a
  // proxy for the strategy's contributed PnL. Unrealised is shown
  // alongside as a separate KPI because mark-to-market jitter would
  // otherwise drown out the realised signal in the spark.
  const equitySpark = useMemo(
    () =>
      equity.map((p) =>
        Number((p.realized_pnl_usd ?? 0) - (p.fees_paid_usd ?? 0)),
      ),
    [equity],
  );

  if (loading && !envelope) {
    return (
      <Card title={t("title")} description={t("description")}>
        <Empty label={t("loading")} />
      </Card>
    );
  }

  if (error && !envelope) {
    return (
      <Card title={t("title")} description={t("description")}>
        <div className="rounded-md border border-rose-500/30 bg-rose-500/5 px-3 py-2 text-[12px] text-rose-300">
          {t("loadError", { error })}
        </div>
      </Card>
    );
  }

  const winRate =
    kpis && (kpis.wins + kpis.losses) > 0
      ? (kpis.wins / (kpis.wins + kpis.losses)) * 100
      : null;

  return (
    <div className="space-y-4">
      {/* KPI strip ----------------------------------------------- */}
      <Card title={t("kpiTitle")} description={t("kpiDescription")}>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          <Kpi
            inline
            label={t("kpiOpenPositions")}
            value={kpis?.open_positions ?? 0}
          />
          <Kpi
            inline
            label={t("kpiTrades")}
            value={kpis?.trades_count ?? 0}
            delta={
              winRate != null
                ? t("kpiWinRate", { rate: winRate.toFixed(1) })
                : undefined
            }
          />
          <Kpi
            inline
            label={t("kpiRealized")}
            tone={pnlTone(kpis?.total_realized_usd ?? 0)}
            value={fmtUSD(kpis?.total_realized_usd ?? 0)}
          />
          <Kpi
            inline
            label={t("kpiUnrealized")}
            tone={pnlTone(kpis?.total_unrealized_usd ?? 0)}
            value={fmtUSD(kpis?.total_unrealized_usd ?? 0)}
          />
          <Kpi
            inline
            label={t("kpiFees")}
            value={fmtUSD(kpis?.fees_usd ?? 0)}
            tone={kpis && kpis.fees_usd > 0 ? "warn" : "neutral"}
          />
          <Kpi
            inline
            label={t("kpiLastTrade")}
            value={
              kpis?.last_trade_at
                ? fmtTimeAgo(kpis.last_trade_at, locale)
                : t("kpiNever")
            }
          />
        </div>
      </Card>

      {/* Open positions ----------------------------------------- */}
      <Card title={t("positionsTitle")} description={t("positionsDescription")}>
        {positions.length === 0 ? (
          <Empty
            label={t("positionsEmpty")}
            subtitle={t("positionsEmptySubtitle")}
          />
        ) : (
          <div className="space-y-2">
            {positions.map((p) => (
              <PositionRow key={p.share_id} pos={p} locale={locale} />
            ))}
          </div>
        )}
      </Card>

      {/* Equity curve ------------------------------------------- */}
      <Card title={t("equityTitle")} description={t("equityDescription")}>
        {equitySpark.length < 2 ? (
          <Empty
            label={t("equityEmpty")}
            subtitle={t("equityEmptySubtitle")}
          />
        ) : (
          <EquityChart
            data={equity}
            values={equitySpark}
            locale={locale}
            label={t("equityLineLabel")}
          />
        )}
      </Card>

      {/* Orders + Fills (side by side on wide screens) ----------- */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <Card title={t("ordersTitle")} description={t("ordersDescription")}>
          {orders.length === 0 ? (
            <Empty label={t("ordersEmpty")} />
          ) : (
            <OrdersTable orders={orders} locale={locale} />
          )}
        </Card>
        <Card title={t("fillsTitle")} description={t("fillsDescription")}>
          {fills.length === 0 ? (
            <Empty
              label={t("fillsEmpty")}
              subtitle={t("fillsEmptySubtitle")}
            />
          ) : (
            <FillsTable fills={fills} locale={locale} />
          )}
        </Card>
      </div>

      {/* Raw JSON (collapsed by default) ------------------------- */}
      <Card
        title={t("rawTitle")}
        description={t("rawDescription")}
        actions={
          <button
            type="button"
            onClick={() => setShowRaw((v) => !v)}
            className="text-[11px] px-2 py-1 rounded border border-brand-500/20 bg-ink-900/40 hover:border-brand-500/40"
          >
            {showRaw ? t("rawHide") : t("rawShow")}
          </button>
        }
      >
        {showRaw ? (
          <Json value={envelope} />
        ) : (
          <p className="text-[12px] text-[color:var(--text-muted)] italic">
            {t("rawHint")}
          </p>
        )}
      </Card>
    </div>
  );
}

// -------- subcomponents ------------------------------------------------

function PositionRow({
  pos,
  locale,
}: {
  pos: StrategyPerformancePosition;
  locale: string;
}) {
  const t = useTranslations("strategyPerformance");
  const [expanded, setExpanded] = useState(false);
  const co = pos.merged?.co_strategies ?? [];
  const sideTone = pos.side === "long" ? "ok" : "warn";

  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/40">
      <div className="px-3 py-2 flex flex-wrap items-center gap-3 justify-between">
        <div className="flex items-center gap-2 min-w-0">
          <Pill tone={sideTone}>{pos.side}</Pill>
          <span className="font-mono text-[13px] truncate">{pos.market}</span>
          <span className="text-[11px] text-[color:var(--text-muted)]">
            @ {pos.account_id}
          </span>
          {co.length > 0 ? (
            <Pill tone="brand">
              {t("mergedWithBadge", { n: co.length })}
            </Pill>
          ) : null}
        </div>
        <div className="flex items-center gap-4 text-[12px]">
          <span title={t("sizeHelp")}>
            {t("sizeLabel")}:{" "}
            <span className="font-mono">{fmtBase(pos.size_share_base)}</span>
          </span>
          <span title={t("entryHelp")}>
            {t("entryLabel")}: <span className="font-mono">{fmtUSD(pos.avg_entry_price)}</span>
          </span>
          <span title={t("markHelp")}>
            {t("markLabel")}: <span className="font-mono">{fmtUSD(pos.mark_price)}</span>
          </span>
          <span
            className={pnlClass(pos.unrealized_pnl_usd)}
            title={t("unrealizedHelp")}
          >
            {fmtUSDSigned(pos.unrealized_pnl_usd)}
          </span>
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-[11px] px-1.5 py-0.5 rounded border border-brand-500/20 hover:border-brand-500/40"
            aria-label={expanded ? t("collapse") : t("expand")}
          >
            {expanded ? "▾" : "▸"}
          </button>
        </div>
      </div>
      {expanded ? (
        <div className="border-t border-brand-500/10 px-3 py-2 text-[12px] grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1">
          <span>
            <span className="text-[color:var(--text-muted)]">
              {t("realizedLabel")}:
            </span>{" "}
            <span className={pnlClass(pos.realized_pnl_usd)}>
              {fmtUSDSigned(pos.realized_pnl_usd)}
            </span>
          </span>
          <span>
            <span className="text-[color:var(--text-muted)]">
              {t("feesLabel")}:
            </span>{" "}
            <span className="font-mono">{fmtUSD(pos.fees_usd)}</span>
          </span>
          <span>
            <span className="text-[color:var(--text-muted)]">
              {t("fundingLabel")}:
            </span>{" "}
            <span className="font-mono">{fmtUSD(pos.funding_usd)}</span>
          </span>
          <span>
            <span className="text-[color:var(--text-muted)]">
              {t("notionalLabel")}:
            </span>{" "}
            <span className="font-mono">{fmtUSD(pos.notional_usd)}</span>
          </span>
          <span>
            <span className="text-[color:var(--text-muted)]">
              {t("openedLabel")}:
            </span>{" "}
            {fmtTimeAgo(pos.opened_at, locale)}
          </span>
          <span>
            <span className="text-[color:var(--text-muted)]">
              {t("updatedLabel")}:
            </span>{" "}
            {fmtTimeAgo(pos.updated_at, locale)}
          </span>
          {pos.merged ? (
            <>
              <span className="col-span-2 sm:col-span-4 mt-1 border-t border-brand-500/10 pt-1 text-[color:var(--text-muted)]">
                {t("mergedContextTitle")}
              </span>
              <span>
                <span className="text-[color:var(--text-muted)]">
                  {t("mergedSizeLabel")}:
                </span>{" "}
                <span className="font-mono">
                  {fmtBase(pos.merged.size_base)}
                </span>
              </span>
              <span>
                <span className="text-[color:var(--text-muted)]">
                  {t("mergedAvgLabel")}:
                </span>{" "}
                <span className="font-mono">
                  {fmtUSD(pos.merged.avg_entry_price)}
                </span>
              </span>
              <span>
                <span className="text-[color:var(--text-muted)]">
                  {t("mergedUnrealizedLabel")}:
                </span>{" "}
                <span className={pnlClass(pos.merged.unrealized_pnl_usd)}>
                  {fmtUSDSigned(pos.merged.unrealized_pnl_usd)}
                </span>
              </span>
              <span className="col-span-2 sm:col-span-4">
                <span className="text-[color:var(--text-muted)]">
                  {t("mergedCoStrategiesLabel")}:
                </span>{" "}
                {pos.merged.co_strategies.length === 0
                  ? "—"
                  : pos.merged.co_strategies.join(", ")}
              </span>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function EquityChart({
  data,
  values,
  locale,
  label,
}: {
  data: StrategyPerformanceEquityPoint[];
  values: number[];
  locale: string;
  label: string;
}) {
  const t = useTranslations("strategyPerformance");
  const first = data[0];
  const last = data[data.length - 1];
  const min = Math.min(...values);
  const max = Math.max(...values);
  const tone =
    last && first && (last.realized_pnl_usd - last.fees_paid_usd) >=
    (first.realized_pnl_usd - first.fees_paid_usd)
      ? "brand"
      : "danger";

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-end justify-between gap-3 text-[12px]">
        <div>
          <div className="text-[color:var(--text-muted)]">{label}</div>
          <div className="font-mono text-[15px]">
            {fmtUSDSigned(
              last
                ? last.realized_pnl_usd - last.fees_paid_usd
                : 0,
            )}
          </div>
        </div>
        <div className="text-[color:var(--text-muted)] space-x-3">
          <span>
            {t("equityMin")}:{" "}
            <span className="font-mono">{fmtUSDSigned(min)}</span>
          </span>
          <span>
            {t("equityMax")}:{" "}
            <span className="font-mono">{fmtUSDSigned(max)}</span>
          </span>
          {first ? (
            <span>
              {t("equitySince")}:{" "}
              <span>{fmtDate(first.ts, locale)}</span>
            </span>
          ) : null}
        </div>
      </div>
      <Sparkline values={values} width={600} height={80} tone={tone} />
    </div>
  );
}

function OrdersTable({
  orders,
  locale,
}: {
  orders: StrategyPerformanceOrder[];
  locale: string;
}) {
  const t = useTranslations("strategyPerformance");
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[12px]">
        <thead className="text-left text-[color:var(--text-muted)]">
          <tr>
            <th className="pb-1.5 pr-3 font-normal">{t("orderTime")}</th>
            <th className="pb-1.5 pr-3 font-normal">{t("orderMarket")}</th>
            <th className="pb-1.5 pr-3 font-normal">{t("orderSide")}</th>
            <th className="pb-1.5 pr-3 font-normal">{t("orderSize")}</th>
            <th className="pb-1.5 pr-3 font-normal">{t("orderPrice")}</th>
            <th className="pb-1.5 pr-3 font-normal">{t("orderState")}</th>
            <th className="pb-1.5 pr-3 font-normal">{t("orderFilled")}</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr
              key={o.order_id ?? `${o.venue_order_id}-${o.created_at}`}
              className="border-t border-brand-500/10"
            >
              <td className="py-1.5 pr-3 whitespace-nowrap">
                {fmtTimeAgo(o.created_at, locale)}
              </td>
              <td className="py-1.5 pr-3 font-mono">{o.market}</td>
              <td className="py-1.5 pr-3">
                <Pill tone={o.side === "buy" ? "ok" : "warn"}>{o.side}</Pill>
              </td>
              <td className="py-1.5 pr-3 font-mono">
                {fmtBase(o.size_base)}
              </td>
              <td className="py-1.5 pr-3 font-mono">
                {o.price != null ? fmtUSD(o.price) : t("orderMarketPrice")}
              </td>
              <td className="py-1.5 pr-3">
                <Pill tone={stateTone(o.state)}>{o.state ?? "—"}</Pill>
              </td>
              <td className="py-1.5 pr-3 font-mono">
                {fmtBase(o.filled_size)}
                {o.avg_price != null
                  ? ` @ ${fmtUSD(o.avg_price)}`
                  : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FillsTable({
  fills,
  locale,
}: {
  fills: StrategyPerformanceFill[];
  locale: string;
}) {
  const t = useTranslations("strategyPerformance");
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[12px]">
        <thead className="text-left text-[color:var(--text-muted)]">
          <tr>
            <th className="pb-1.5 pr-3 font-normal">{t("fillTime")}</th>
            <th className="pb-1.5 pr-3 font-normal">{t("fillMarket")}</th>
            <th className="pb-1.5 pr-3 font-normal">{t("fillSide")}</th>
            <th className="pb-1.5 pr-3 font-normal">{t("fillSize")}</th>
            <th className="pb-1.5 pr-3 font-normal">{t("fillPrice")}</th>
            <th className="pb-1.5 pr-3 font-normal">{t("fillFee")}</th>
          </tr>
        </thead>
        <tbody>
          {fills.map((f) => (
            <tr key={f.fill_id} className="border-t border-brand-500/10">
              <td className="py-1.5 pr-3 whitespace-nowrap">
                {fmtTimeAgo(f.ts, locale)}
              </td>
              <td className="py-1.5 pr-3 font-mono">{f.market}</td>
              <td className="py-1.5 pr-3">
                <Pill tone={f.side === "buy" ? "ok" : "warn"}>{f.side}</Pill>
              </td>
              <td className="py-1.5 pr-3 font-mono">{fmtBase(f.size_base)}</td>
              <td className="py-1.5 pr-3 font-mono">{fmtUSD(f.price)}</td>
              <td className="py-1.5 pr-3 font-mono">{fmtUSD(f.fee_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// -------- formatting helpers ------------------------------------------

function fmtUSD(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const opts: Intl.NumberFormatOptions =
    abs >= 1000
      ? { maximumFractionDigits: 2, minimumFractionDigits: 2 }
      : { maximumFractionDigits: 4 };
  return `$${value.toLocaleString("en-US", opts)}`;
}

function fmtUSDSigned(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (value > 0) return `+${fmtUSD(value)}`;
  return fmtUSD(value);
}

function fmtBase(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1) {
    return value.toLocaleString("en-US", {
      maximumFractionDigits: 4,
    });
  }
  return value.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

function fmtTimeAgo(ts: number, _locale: string): string {
  if (!ts || !Number.isFinite(ts)) return "—";
  const now = Date.now() / 1000;
  const dt = now - ts;
  if (dt < 60) return `${Math.round(dt)}s`;
  if (dt < 3600) return `${Math.round(dt / 60)}m`;
  if (dt < 86400) return `${Math.round(dt / 3600)}h`;
  return `${Math.round(dt / 86400)}d`;
}

function fmtDate(ts: number, locale: string): string {
  if (!ts || !Number.isFinite(ts)) return "—";
  try {
    return new Date(ts * 1000).toLocaleDateString(locale);
  } catch {
    return new Date(ts * 1000).toISOString().slice(0, 10);
  }
}

function pnlTone(v: number): "ok" | "danger" | "neutral" {
  if (v > 0) return "ok";
  if (v < 0) return "danger";
  return "neutral";
}

function pnlClass(v: number): string {
  if (v > 0) return "text-emerald-400";
  if (v < 0) return "text-rose-400";
  return "text-[color:var(--text-muted)]";
}

function stateTone(
  state: string | null,
): "ok" | "warn" | "danger" | "neutral" | "brand" {
  if (!state) return "neutral";
  const s = state.toLowerCase();
  if (s.includes("fill") || s.includes("submit") || s === "closed") return "ok";
  if (s.includes("cancel") || s.includes("reject")) return "danger";
  if (s.includes("pend") || s.includes("active")) return "brand";
  return "neutral";
}

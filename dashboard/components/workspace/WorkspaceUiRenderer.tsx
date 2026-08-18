"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { CandleChart } from "../CandleChart";
import { Card, Kpi, Pill, StatusDot } from "../Page";
import { clientApi, type SkillSummary } from "../../lib/clientApi";
import type { PortfolioSummary, StrategyCard } from "../../lib/api";
import type { AttentionItem } from "../../lib/operatorTypes";
import type { WorkspaceUiWidget } from "../../lib/workspaceUiTypes";

type AsyncState<T> = {
  loading: boolean;
  value: T | null;
  error: string | null;
};

const initialAsyncState = <T,>(): AsyncState<T> => ({
  loading: true,
  value: null,
  error: null,
});

function useAsyncValue<T>(load: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [state, setState] = useState<AsyncState<T>>(initialAsyncState<T>);

  useEffect(() => {
    let cancelled = false;
    setState(initialAsyncState<T>());
    void load()
      .then((value) => {
        if (!cancelled) setState({ loading: false, value, error: null });
      })
      .catch((error) => {
        if (!cancelled) {
          setState({
            loading: false,
            value: null,
            error: error instanceof Error ? error.message : String(error),
          });
        }
      });
    return () => {
      cancelled = true;
    };
    // The widget passes primitive manifest values as dependencies.  Requiring
    // the callback itself would refetch on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}

function configOf(widget: WorkspaceUiWidget): Record<string, unknown> {
  const merged: Record<string, unknown> = {};
  if (widget.source && typeof widget.source === "object") {
    Object.assign(merged, widget.source);
    const params = widget.source.params;
    if (params && typeof params === "object") Object.assign(merged, params);
  }
  if (widget.config && typeof widget.config === "object") {
    Object.assign(merged, widget.config);
  }
  return merged;
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function safeInternalHref(value: unknown): string | null {
  const href = asString(value);
  if (!href.startsWith("/") || href.startsWith("//")) return null;
  return href;
}

function money(value: number, currency = "USD"): string {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      maximumFractionDigits: Math.abs(value) >= 1_000 ? 0 : 2,
    }).format(value);
  } catch {
    return `${currency} ${value.toFixed(2)}`;
  }
}

function spanClass(span: WorkspaceUiWidget["span"]): string {
  if (span === "full" || span === 3 || span === 12) return "md:col-span-2 xl:col-span-3";
  if (span === "wide" || span === 2 || span === 8) return "md:col-span-2 xl:col-span-2";
  if (span === "half" || span === 6) return "md:col-span-1 xl:col-span-2";
  return "md:col-span-1 xl:col-span-1";
}

function WidgetFrame({
  widget,
  children,
  actions,
}: {
  widget: WorkspaceUiWidget;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <Card
      title={widget.title || widget.id}
      description={widget.description}
      actions={actions}
    >
      {children}
    </Card>
  );
}

function LoadingBlock() {
  const t = useTranslations("workspaceUi");
  return (
    <div className="flex h-24 items-center justify-center text-[12px] text-[color:var(--text-muted)]">
      {t("loadingWidget")}
    </div>
  );
}

function ErrorBlock({ message }: { message: string }) {
  const t = useTranslations("workspaceUi");
  return (
    <div className="rounded-md border border-rose-500/20 bg-rose-500/5 px-3 py-2.5 text-[12px] leading-relaxed text-rose-300">
      {t("widgetUnavailable")}: {message}
    </div>
  );
}

function MetricWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const config = configOf(widget);
  const label = asString(config.label, widget.title || widget.id);
  const value = config.value == null ? "—" : String(config.value);
  const unit = asString(config.unit);
  const tone = asString(config.tone, "brand");
  const safeTone = ["neutral", "ok", "warn", "danger", "brand"].includes(tone)
    ? (tone as "neutral" | "ok" | "warn" | "danger" | "brand")
    : "brand";
  return (
    <WidgetFrame widget={widget}>
      <Kpi
        inline
        label={label}
        value={unit ? `${value} ${unit}` : value}
        delta={asString(config.caption)}
        tone={safeTone}
      />
    </WidgetFrame>
  );
}

function MarkdownWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const config = configOf(widget);
  const text = asString(config.text, asString(widget.text));
  return (
    <WidgetFrame widget={widget}>
      <div className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-[color:var(--text-muted)]">
        {text || "—"}
      </div>
    </WidgetFrame>
  );
}

function LinkWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const t = useTranslations("workspaceUi");
  const config = configOf(widget);
  const href = safeInternalHref(config.href);
  const label = asString(config.label, t("open"));
  return (
    <WidgetFrame widget={widget}>
      {href ? (
        <Link href={href} className="text-[13px] text-brand-300 hover:text-brand-200">
          {label} →
        </Link>
      ) : (
        <ErrorBlock message={t("unsafeLink")} />
      )}
    </WidgetFrame>
  );
}

function MarketChartWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const config = configOf(widget);
  const venue = asString(config.venue, "bybit");
  const market = asString(config.market, asString(config.symbol, "BTC/USDT"));
  const interval = asString(config.interval, "1h");
  const count = Math.max(20, Math.min(500, Math.round(asNumber(config.count, 120))));
  const state = useAsyncValue(
    () => clientApi.marketCandles({ venue, market, interval, count }),
    [venue, market, interval, count],
  );
  const candles = state.value?.candles ?? [];
  const first = candles.at(0)?.close ?? 0;
  const last = candles.at(-1)?.close ?? 0;
  const delta = first ? ((last - first) / first) * 100 : 0;
  const chartType = asString(config.chart_type, "candlestick");
  const mode = ["candlestick", "line", "area"].includes(chartType)
    ? (chartType as "candlestick" | "line" | "area")
    : "candlestick";
  return (
    <WidgetFrame
      widget={widget}
      actions={
        <div className="flex items-center gap-2 text-[11px] text-[color:var(--text-muted)]">
          <Pill tone="neutral">{venue}</Pill>
          <span className="font-mono">{market} · {interval}</span>
          {last ? (
            <span className={delta >= 0 ? "text-emerald-500" : "text-rose-500"}>
              {delta >= 0 ? "+" : ""}{delta.toFixed(2)}%
            </span>
          ) : null}
        </div>
      }
    >
      <CandleChart
        candles={candles}
        height={asNumber(config.height, 220)}
        mode={mode}
        showVolume={asBoolean(config.show_volume, true)}
        loading={state.loading}
        error={state.error || state.value?.error}
      />
    </WidgetFrame>
  );
}

function MarketTickerWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const config = configOf(widget);
  const venue = asString(config.venue, "bybit");
  const market = asString(config.market, asString(config.symbol, "BTC/USDT"));
  const state = useAsyncValue(
    () => clientApi.marketTicker({ venue, market }),
    [venue, market],
  );
  if (state.loading) return <WidgetFrame widget={widget}><LoadingBlock /></WidgetFrame>;
  if (state.error || state.value?.error) {
    return <WidgetFrame widget={widget}><ErrorBlock message={state.error || state.value?.error || "unknown"} /></WidgetFrame>;
  }
  const price = Number(state.value?.last ?? state.value?.mid ?? 0);
  return (
    <WidgetFrame widget={widget} actions={<Pill tone="neutral">{venue}</Pill>}>
      <Kpi
        inline
        label={market}
        value={price ? price.toLocaleString(undefined, { maximumFractionDigits: 8 }) : "—"}
        delta={state.value?.bid && state.value?.ask ? `bid ${state.value.bid} · ask ${state.value.ask}` : undefined}
        tone="brand"
      />
    </WidgetFrame>
  );
}

function PortfolioWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const t = useTranslations("workspaceUi");
  const config = configOf(widget);
  const currency = asString(config.currency, "USD");
  const state = useAsyncValue<PortfolioSummary>(() => clientApi.portfolioSummary(), []);
  if (state.loading) return <WidgetFrame widget={widget}><LoadingBlock /></WidgetFrame>;
  if (state.error || !state.value) {
    return <WidgetFrame widget={widget}><ErrorBlock message={state.error || t("noData")} /></WidgetFrame>;
  }
  const { totals, accounts } = state.value;
  const positions = accounts.reduce(
    (count, account) => count + Object.keys(account.positions || {}).length,
    0,
  );
  return (
    <WidgetFrame widget={widget} actions={<Link href="/portfolio" className="text-[11px] text-brand-300 hover:text-brand-200">{t("open")}</Link>}>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <Kpi inline label={t("equity")} value={money(Number(totals.equity_usd || 0), currency)} tone="brand" />
        <Kpi inline label={t("cash")} value={money(Number(totals.cash_usd || 0), currency)} />
        <Kpi inline label={t("positions")} value={positions} delta={t("accounts", { count: accounts.length })} />
      </div>
    </WidgetFrame>
  );
}

function StrategyListWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const t = useTranslations("workspaceUi");
  const config = configOf(widget);
  const limit = Math.max(1, Math.min(20, Math.round(asNumber(config.limit, 6))));
  const state = useAsyncValue<{ strategies: StrategyCard[] }>(() => clientApi.strategyList(), [limit]);
  if (state.loading) return <WidgetFrame widget={widget}><LoadingBlock /></WidgetFrame>;
  if (state.error || !state.value) {
    return <WidgetFrame widget={widget}><ErrorBlock message={state.error || t("noData")} /></WidgetFrame>;
  }
  const rows = state.value.strategies.slice(0, limit);
  return (
    <WidgetFrame widget={widget} actions={<Link href="/strategies" className="text-[11px] text-brand-300 hover:text-brand-200">{t("open")}</Link>}>
      {rows.length ? (
        <div className="embedded-table-scroll max-h-72">
          <table className="table table-compact">
            <thead>
              <tr><th>{t("strategy")}</th><th>{t("status")}</th><th>{t("pnl")}</th></tr>
            </thead>
            <tbody>
              {rows.map((strategy) => (
                <tr key={strategy.id}>
                  <td><Link href={`/strategies/${encodeURIComponent(strategy.id)}`} className="text-[12px] text-[color:var(--text-base)] hover:text-brand-200">{strategy.title || strategy.id}</Link></td>
                  <td><Pill tone={strategy.status === "live" ? "warn" : strategy.status === "paper" ? "brand" : "neutral"}>{strategy.status || "—"}</Pill></td>
                  <td className={strategy.total_pnl_usd >= 0 ? "font-mono text-emerald-500" : "font-mono text-rose-500"}>{money(strategy.total_pnl_usd || 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <div className="text-[12px] text-[color:var(--text-muted)]">{t("noData")}</div>}
    </WidgetFrame>
  );
}

function TableWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const t = useTranslations("workspaceUi");
  const config = configOf(widget);
  const rawColumns = Array.isArray(config.columns) ? config.columns : [];
  const columns = rawColumns
    .map((column) => (typeof column === "string" ? column : asString((column as Record<string, unknown>)?.key || (column as Record<string, unknown>)?.label)))
    .filter(Boolean)
    .slice(0, 16);
  const rows = (Array.isArray(config.rows) ? config.rows : [])
    .filter((row): row is Record<string, unknown> => !!row && typeof row === "object" && !Array.isArray(row))
    .slice(0, 100);
  const resolvedColumns = columns.length
    ? columns
    : Array.from(new Set(rows.flatMap((row) => Object.keys(row)))).slice(0, 16);
  return (
    <WidgetFrame widget={widget}>
      {resolvedColumns.length && rows.length ? (
        <div className="embedded-table-scroll max-h-72">
          <table className="table table-compact">
            <thead><tr>{resolvedColumns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={String(row.id || row.key || index)}>
                  {resolvedColumns.map((column) => <td key={column} className="max-w-[240px] truncate text-[12px]">{String(row[column] ?? "—")}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <div className="text-[12px] text-[color:var(--text-muted)]">{t("noData")}</div>}
    </WidgetFrame>
  );
}

function AttentionWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const t = useTranslations("workspaceUi");
  const config = configOf(widget);
  const limit = Math.max(1, Math.min(20, Math.round(asNumber(config.limit, 6))));
  const state = useAsyncValue(() => clientApi.operatorOverview(), [limit]);
  if (state.loading) return <WidgetFrame widget={widget}><LoadingBlock /></WidgetFrame>;
  if (state.error || !state.value) {
    return <WidgetFrame widget={widget}><ErrorBlock message={state.error || t("noData")} /></WidgetFrame>;
  }
  const items: AttentionItem[] = (state.value.data.attention || []).slice(0, limit);
  return (
    <WidgetFrame widget={widget} actions={<Link href="/inbox" className="text-[11px] text-brand-300 hover:text-brand-200">{t("open")}</Link>}>
      {items.length ? (
        <ul className="space-y-2">
          {items.map((item) => (
            <li key={item.id} className="flex items-start gap-2.5 border-b border-[color:var(--line)] pb-2 last:border-0 last:pb-0">
              <StatusDot tone={item.severity === "danger" ? "danger" : item.severity === "warn" ? "warn" : "brand"} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[12px] text-[color:var(--text-base)]">{item.title}</div>
                {item.summary ? <div className="mt-0.5 truncate text-[11px] text-[color:var(--text-muted)]">{item.summary}</div> : null}
              </div>
            </li>
          ))}
        </ul>
      ) : <div className="text-[12px] text-[color:var(--text-muted)]">{t("nothingNeedsAttention")}</div>}
    </WidgetFrame>
  );
}

function SkillPanelWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const t = useTranslations("workspaceUi");
  const config = configOf(widget);
  const skillId = asString(config.skill_id);
  const limit = Math.max(1, Math.min(20, Math.round(asNumber(config.limit, 6))));
  const state = useAsyncValue(() => clientApi.skills(), [skillId, limit]);
  if (state.loading) return <WidgetFrame widget={widget}><LoadingBlock /></WidgetFrame>;
  if (state.error || !state.value) {
    return <WidgetFrame widget={widget}><ErrorBlock message={state.error || t("noData")} /></WidgetFrame>;
  }
  const skills: SkillSummary[] = state.value.skills
    .filter((skill) => !skillId || skill.id === skillId)
    .slice(0, limit);
  return (
    <WidgetFrame widget={widget} actions={<Link href="/skills" className="text-[11px] text-brand-300 hover:text-brand-200">{t("manage")}</Link>}>
      <ul className="space-y-2">
        {skills.map((skill) => (
          <li key={skill.id} className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="truncate text-[12px] text-[color:var(--text-base)]">{skill.title || skill.id}</div>
              {skill.description ? <div className="mt-0.5 truncate text-[11px] text-[color:var(--text-muted)]">{skill.description}</div> : null}
            </div>
            <Pill tone={skill.status === "enabled" ? "ok" : "neutral"}>{skill.status || skill.source || "ready"}</Pill>
          </li>
        ))}
        {!skills.length ? <li className="text-[12px] text-[color:var(--text-muted)]">{t("noData")}</li> : null}
      </ul>
    </WidgetFrame>
  );
}

function AgentPanelWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const t = useTranslations("workspaceUi");
  const config = configOf(widget);
  const roleName = asString(config.agent, asString(config.role));
  const limit = Math.max(1, Math.min(20, Math.round(asNumber(config.limit, 6))));
  const state = useAsyncValue(() => clientApi.agentsList(), [roleName, limit]);
  if (state.loading) return <WidgetFrame widget={widget}><LoadingBlock /></WidgetFrame>;
  if (state.error || !state.value) {
    return <WidgetFrame widget={widget}><ErrorBlock message={state.error || t("noData")} /></WidgetFrame>;
  }
  const roles = state.value.roles
    .filter((role) => !roleName || role.name === roleName)
    .slice(0, limit);
  return (
    <WidgetFrame widget={widget} actions={<Link href="/agents" className="text-[11px] text-brand-300 hover:text-brand-200">{t("manage")}</Link>}>
      <ul className="space-y-2">
        {roles.map((role) => (
          <li key={role.name} className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="truncate text-[12px] text-[color:var(--text-base)]">{role.name}</div>
              <div className="mt-0.5 truncate text-[11px] text-[color:var(--text-muted)]">{role.allowed_skills.slice(0, 4).join(" · ") || t("noSkills")}</div>
            </div>
            <Pill tone="neutral">{role.tier}</Pill>
          </li>
        ))}
        {!roles.length ? <li className="text-[12px] text-[color:var(--text-muted)]">{t("noData")}</li> : null}
      </ul>
    </WidgetFrame>
  );
}

function UnsupportedWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const t = useTranslations("workspaceUi");
  return (
    <WidgetFrame widget={widget}>
      <div className="rounded-md border border-amber-500/20 bg-amber-500/5 px-3 py-2.5">
        <div className="text-[12px] text-amber-300">{t("unsupportedWidget", { kind: widget.kind })}</div>
        <p className="mt-1 text-[11px] leading-relaxed text-[color:var(--text-muted)]">{t("unsupportedWidgetHint")}</p>
      </div>
    </WidgetFrame>
  );
}

function WorkspaceWidget({ widget }: { widget: WorkspaceUiWidget }) {
  const kind = asString(widget.kind, asString(widget.type)).toLowerCase().replaceAll("-", "_");
  if (kind === "metric" || kind === "kpi") return <MetricWidget widget={widget} />;
  if (kind === "markdown" || kind === "note" || kind === "text") return <MarkdownWidget widget={widget} />;
  if (kind === "link" || kind === "shortcut") return <LinkWidget widget={widget} />;
  if (kind === "market_chart" || kind === "candles" || kind === "chart") return <MarketChartWidget widget={widget} />;
  if (kind === "market_ticker" || kind === "ticker") return <MarketTickerWidget widget={widget} />;
  if (kind === "portfolio" || kind === "portfolio_summary") return <PortfolioWidget widget={widget} />;
  if (kind === "strategies" || kind === "strategy_list" || kind === "strategy_table") return <StrategyListWidget widget={widget} />;
  if (kind === "table") return <TableWidget widget={widget} />;
  if (kind === "attention" || kind === "inbox") return <AttentionWidget widget={widget} />;
  if (kind === "skill" || kind === "skill_panel") return <SkillPanelWidget widget={widget} />;
  if (kind === "agent" || kind === "agent_panel") return <AgentPanelWidget widget={widget} />;
  return <UnsupportedWidget widget={{ ...widget, kind: kind || "unknown" }} />;
}

export function WorkspaceUiRenderer({
  widgets,
  empty,
}: {
  widgets: WorkspaceUiWidget[];
  empty?: ReactNode;
}) {
  const safeWidgets = useMemo(
    () => widgets.filter((widget) => widget && typeof widget === "object" && widget.id && widget.kind),
    [widgets],
  );

  if (!safeWidgets.length) return <>{empty ?? null}</>;

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
      {safeWidgets.map((widget) => (
        <div key={widget.id} className={spanClass(widget.span)}>
          <WorkspaceWidget widget={widget} />
        </div>
      ))}
    </div>
  );
}

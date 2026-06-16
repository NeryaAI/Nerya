"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import {
  Card,
  ErrorBanner,
  Kpi,
  PageBody,
  Pill,
  Section,
  StatusDot,
} from "../../components/Page";
import { SetupReadinessCard } from "../../components/SetupReadinessCard";
import { Sparkline } from "../../components/Sparkline";
import { CandleChart } from "../../components/CandleChart";
import { Select } from "../../components/Select";
import { clientApi } from "../../lib/clientApi";
import type {
  Candle,
  EquityPoint,
  PortfolioPnl,
  PortfolioSummary,
  RecentTrade,
  StrategyCard,
} from "../../lib/api";
import type { AccountSummary } from "../../lib/clientApi";
import type {
  AttentionItem,
  OperatorOverviewEnvelope,
} from "../../lib/operatorTypes";
import { useUiSettings } from "../../lib/settings";
import { useCurrentAccountId, formatBalance } from "../../lib/currentAccount";
import { formatTime } from "../../lib/format";
import { authHeaders } from "../../lib/auth";

type Message = { id?: string; ts?: string; channel?: string; text?: string; severity?: string };
type Proposal = { id?: string; kind?: string; summary?: string; status?: string };

const INTERVAL_OPTIONS: { key: string; label: string }[] = [
  { key: "1m", label: "1m" },
  { key: "5m", label: "5m" },
  { key: "15m", label: "15m" },
  { key: "1h", label: "1h" },
  { key: "4h", label: "4h" },
  { key: "1d", label: "1d" },
];

export default function DashboardOverview() {
  const t = useTranslations("home");
  const tHero = useTranslations("operatorHero");
  const [settings, patchSettings] = useUiSettings();
  const [currentAccountId] = useCurrentAccountId();

  const [workspace, setWorkspace] = useState<{ root: string; live_trading_enabled: boolean; kill_switch: boolean } | null>(null);
  const [apiOnline, setApiOnline] = useState(false);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [portfolioPnl, setPortfolioPnl] = useState<PortfolioPnl | null>(null);
  const [strategies, setStrategies] = useState<StrategyCard[]>([]);
  const [recentTrades, setRecentTrades] = useState<RecentTrade[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [venues, setVenues] = useState<{ name: string; label: string }[]>([]);
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [overview, setOverview] = useState<OperatorOverviewEnvelope | null>(null);

  const [candles, setCandles] = useState<Candle[]>([]);
  const [candleLoading, setCandleLoading] = useState(false);
  const [candleError, setCandleError] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);

  const loadCore = useCallback(async () => {
    // Fire health() in parallel with the 11 data fetches
    // instead of awaiting it serially. Previously every dashboard render
    // blocked all data behind /health (~150-300ms locally, more in dev),
    // and a single health failure aborted the entire load. Now data
    // streams in regardless, and the online dot just reflects whether
    // /health came back ok.
    const safe = async <T,>(p: Promise<T>, fallback: T): Promise<T> => {
      try { return await p; } catch { return fallback; }
    };

    const healthP = clientApi
      .health()
      .then(() => true)
      .catch((e) => {
        setError(e instanceof Error ? e.message : String(e));
        return false;
      });

    const [healthOk, ws, pr, msg, ps, pp, st, rt, ec, vn, accList, ov] = await Promise.all([
      healthP,
      safe(clientApi.workspace(), null),
      safe(fetch("/api/proxy/evolution/proposals", { method: "POST", headers: authHeaders({ "content-type": "application/json" }), body: "{}" }).then((r) => r.json()), { proposals: [] }),
      safe(fetch("/api/proxy/messages/list", { method: "POST", headers: authHeaders({ "content-type": "application/json" }), body: JSON.stringify({ limit: 30 }) }).then((r) => r.json()), { messages: [] }),
      safe(clientApi.portfolioSummary(), null),
      safe(clientApi.portfolioPnl(), null),
      safe(clientApi.strategyList(), { strategies: [] }),
      safe(clientApi.recentTrades(20), { trades: [] }),
      safe(clientApi.portfolioEquityCurve(120), { points: [], equity_usd: 0 }),
      safe(clientApi.marketVenues(), { venues: [] }),
      safe(clientApi.accountsList(), { accounts: [] as AccountSummary[], ts: 0 }),
      safe<OperatorOverviewEnvelope | null>(clientApi.operatorOverview(), null),
    ]);

    setApiOnline(healthOk);
    setWorkspace(ws);
    setProposals((pr as { proposals: Proposal[] })?.proposals || []);
    setMessages((msg as { messages: Message[] })?.messages || []);
    setSummary(ps as PortfolioSummary | null);
    setPortfolioPnl(pp as PortfolioPnl | null);
    setStrategies(((st as { strategies: StrategyCard[] })?.strategies) || []);
    setRecentTrades(((rt as { trades: RecentTrade[] })?.trades) || []);
    setEquity(((ec as { points: EquityPoint[] })?.points) || []);
    setVenues((((vn as { venues: { name: string; label: string }[] })?.venues) || []).map((v) => ({ name: v.name, label: v.label })));
    setAccounts(((accList as { accounts?: AccountSummary[] })?.accounts) || []);
    setOverview(ov);
  }, []);

  const loadCandles = useCallback(async () => {
    setCandleLoading(true);
    setCandleError(null);
    try {
      const body = await clientApi.marketCandles({
        venue: settings.kline.venue,
        market: settings.kline.symbol,
        interval: settings.kline.interval,
        count: settings.kline.count,
      });
      setCandles(body.candles || []);
      if (body.error) setCandleError(body.error);
    } catch (e) {
      setCandleError(e instanceof Error ? e.message : String(e));
      setCandles([]);
    } finally {
      setCandleLoading(false);
    }
  }, [settings.kline.venue, settings.kline.symbol, settings.kline.interval, settings.kline.count]);

  useEffect(() => { loadCore(); }, [loadCore]);
  useEffect(() => { loadCandles(); }, [loadCandles]);

  useEffect(() => {
    if (!settings.refreshSeconds) return;
    const id = setInterval(() => {
      loadCore();
      loadCandles();
    }, settings.refreshSeconds * 1000);
    return () => clearInterval(id);
  }, [settings.refreshSeconds, loadCore, loadCandles]);

  /* -------------------------- derived values ------------------------------ */

  const totals = summary?.totals || { cash_usd: 0, equity_usd: 0 };
  const totalEquity = totals.equity_usd || 0;
  const openPositionList = useMemo(() => {
    if (!summary) return [] as { account_id: string; market: string; pos: Record<string, unknown> }[];
    const out: { account_id: string; market: string; pos: Record<string, unknown> }[] = [];
    for (const acc of summary.accounts) {
      for (const [market, pos] of Object.entries(acc.positions || {})) {
        const bag = pos as unknown as Record<string, unknown>;
        const size = Number(bag.size || 0);
        if (size) {
          out.push({ account_id: acc.id, market, pos: bag });
        }
      }
    }
    return out;
  }, [summary]);

  const totalRealizedPnl = Number(
    portfolioPnl?.realized_usd
    ?? strategies.reduce((sum, s) => sum + (s.realized_pnl_usd || 0), 0),
  );

  const activeStrategiesCount = strategies.filter(
    (s) => s.status === "paper" || s.status === "canary" || s.status === "live",
  ).length;

  const equitySeries = useMemo(() => {
    if (equity.length < 2) return [] as number[];
    return equity.map((p) => p.equity_usd);
  }, [equity]);

  const mode = workspace ? (workspace.live_trading_enabled ? "live" : "paper") : "–";
  const killed = !!workspace?.kill_switch;

  const focusedAccount =
    accounts.find((a) => a.profile.id === currentAccountId) ?? null;
  const focusedCurrency = focusedAccount?.profile.base_currency || "USDT";
  const focusedNav = Number(
    (focusedAccount?.snapshot as Record<string, unknown> | null | undefined)?.[
      "total_usd"
    ],
  );
  const focusedFree = Number(
    (focusedAccount?.snapshot as Record<string, unknown> | null | undefined)?.[
      "free_usd"
    ] ??
      (focusedAccount?.snapshot as Record<string, unknown> | null | undefined)?.[
        "available_usd"
      ],
  );
  const focusedReserved = Number(focusedAccount?.reserved_usd || 0);

  /* ------------------------- attention list -------------------------------
   * Section 4 funnels three previously-separate cards (SystemStatus /
   * RecentActivity / Notifications) into a single ordered list driven by
   * real signals: operator/overview attention + proposals + kill-switch +
   * messages. */
  const attentionFromOverview: AttentionItem[] = overview?.data.attention ?? [];
  const derivedAttention: AttentionItem[] = [];
  if (killed) {
    derivedAttention.push({
      id: "kill-switch",
      type: "kill_switch",
      severity: "danger",
      title: t("killEngaged"),
      summary: "",
      href: "/incidents",
      requires_action: true,
    });
  }
  for (const p of proposals.slice(0, 4)) {
    derivedAttention.push({
      id: `proposal:${p.id || p.kind || Math.random()}`,
      type: "proposal",
      severity: "warn",
      title: t("proposalPending", { label: p.summary || p.kind || p.id || "–" }),
      summary: p.status || t("open"),
      href: "/inbox",
      requires_action: true,
    });
  }
  for (const m of messages.slice(0, 4)) {
    if (!m.text) continue;
    derivedAttention.push({
      id: `msg:${m.id || m.ts || Math.random()}`,
      type: "message",
      severity: m.severity === "error" || m.severity === "warn" ? "warn" : "info",
      title: m.text.slice(0, 80),
      summary: m.channel ? t("channelSub", { channel: m.channel }) : "",
      href: "/inbox",
      requires_action: false,
    });
  }
  const attentionList: AttentionItem[] = [
    ...attentionFromOverview,
    ...derivedAttention,
  ].slice(0, 6);

  const pendingCount = attentionList.filter((a) => a.requires_action).length;

  /* ---------------------------- greeting ---------------------------------- */
  const hour = new Date().getHours();
  const greetingKey =
    hour < 5
      ? "greetingNight"
      : hour < 12
      ? "greetingMorning"
      : hour < 18
      ? "greetingAfternoon"
      : "greetingEvening";
  const operatorName = t("operatorFallback");

  const lastClose = candles.length ? candles[candles.length - 1].close : 0;
  const firstClose = candles.length ? candles[0].close : 0;
  const candleDeltaPct = firstClose ? ((lastClose - firstClose) / firstClose) * 100 : 0;

  return (
    <div>
      {error && <ErrorBanner error={error} />}

      <PageBody>
        {/* Section 1 — Overview / greeting / KPIs / quick actions.
            No outer Card; sits directly at the top of the page.
            Marker is the heading typography itself (no decorative rail). */}
        <section className="min-w-0">
          <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
            <h1 className="text-[20px] leading-[1.25] font-medium tracking-tight text-[color:var(--text-base)]">
              {t(greetingKey)}, {operatorName}.
            </h1>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-[color:var(--text-muted)]">
              <StatusDot
                tone={apiOnline ? "ok" : "danger"}
                label={apiOnline ? t("statusOnline") : t("statusOffline")}
              />
              <span>·</span>
              <span className={killed ? "text-rose-500" : mode === "live" ? "text-amber-500" : ""}>
                {killed
                  ? t("killEngaged")
                  : mode === "live"
                  ? t("modeLive")
                  : mode === "paper"
                  ? t("modePaper")
                  : t("modeUnknown")}
              </span>
              <span>·</span>
              <Link
                href="/inbox"
                className="text-brand-300 hover:text-brand-200"
              >
                {pendingCount > 0
                  ? t("attentionPending", { count: pendingCount })
                  : t("noAttention")}
              </Link>
            </div>
          </div>

          {/* 3 inline KPIs */}
          <div className="mt-5 grid grid-cols-1 sm:grid-cols-3 gap-x-8 gap-y-4">
            <Kpi
              inline
              label={t("totalEquity")}
              value={fmtMoney(totalEquity)}
              delta={
                summary
                  ? t("accountCount", { count: summary.accounts.length })
                  : "–"
              }
            />
            <Kpi
              inline
              label={t("realizedPnl")}
              value={fmtSigned(totalRealizedPnl)}
              tone={totalRealizedPnl >= 0 ? "ok" : "danger"}
              delta={t("strategiesCount", { count: strategies.length })}
            />
            <Kpi
              inline
              label={t("activeStrategies")}
              value={`${activeStrategiesCount} / ${strategies.length || 0}`}
              delta={t("openPositions") + " · " + openPositionList.length}
            />
          </div>

          {/* Quick actions row — sentence case, plain text links */}
          <div className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-2 text-[13px]">
            <Link
              href="/strategies"
              className="text-brand-300 hover:text-brand-200"
            >
              {t("newStrategy")} →
            </Link>
            <Link
              href="/chat"
              className="text-brand-300 hover:text-brand-200"
            >
              {t("openAgentWorkspace")} →
            </Link>
            <Link
              href="/workflows"
              className="text-brand-300 hover:text-brand-200"
            >
              {t("createWorkflow")} →
            </Link>
            <Link
              href="/inbox"
              className="text-brand-300 hover:text-brand-200"
            >
              {t("actionInbox")} →
            </Link>
          </div>
        </section>

        {/* Setup checklist — auto-hides when everything is ok. */}
        <SetupReadinessCard collapsed />

        {/* Section 2 — Strategies (cockpit focus: status + P&L + win-rate
            for every registered strategy, at a glance). */}
        <Card
          title={t("activeStrategiesTitle")}
          actions={
            <Link
              href="/strategies"
              className="text-[12px] text-brand-300 hover:text-brand-200"
            >
              {t("viewAll")}
            </Link>
          }
        >
          {strategies.length === 0 ? (
            <div className="text-[13px] text-[color:var(--text-muted)] py-3">
              {t("noStrategies")}
            </div>
          ) : (
            <div className="embedded-table-scroll max-h-80">
              <table className="table table-compact">
                <thead>
                  <tr>
                    <th>{t("strategyCol")}</th>
                    <th>{t("statusCol")}</th>
                    <th>{t("marketCol")}</th>
                    <th>{t("pnlCol")}</th>
                    <th>{t("winRateCol")}</th>
                    <th>{t("openPositions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {strategies.slice(0, 8).map((s) => {
                    const pnl = Number(s.total_pnl_usd || 0);
                    const markets = (s.markets || []).filter(Boolean);
                    return (
                      <tr key={s.id}>
                        <td className="min-w-0">
                          <Link
                            href={`/strategies/${encodeURIComponent(s.id)}`}
                            className="block max-w-[220px] truncate text-[12.5px] text-[color:var(--text-base)] hover:text-brand-200"
                            title={s.title || s.id}
                          >
                            {s.title || s.id}
                          </Link>
                        </td>
                        <td>
                          <Pill tone={strategyPillTone(s.status)}>{s.status || "–"}</Pill>
                        </td>
                        <td className="font-mono text-[12px] text-ink-300">
                          {markets.length
                            ? markets.slice(0, 2).join(", ") + (markets.length > 2 ? ` +${markets.length - 2}` : "")
                            : "–"}
                        </td>
                        <td className={pnl >= 0 ? "font-mono text-emerald-500" : "font-mono text-rose-500"}>
                          {fmtSigned(pnl)}
                        </td>
                        <td className="font-mono text-ink-200">
                          {Number.isFinite(s.win_rate_pct) ? `${Math.round(s.win_rate_pct)}%` : "–"}
                        </td>
                        <td className="font-mono text-ink-200">{s.open_positions_count ?? 0}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* Section 3 — Portfolio (focused account + equity curve + positions + trades). */}
        <Card
          title={t("sectionPortfolio")}
          actions={
            <Link
              href="/portfolio"
              className="text-[12px] text-brand-300 hover:text-brand-200"
            >
              {t("viewAll")}
            </Link>
          }
        >
          <div className="space-y-5">
            {focusedAccount ? (
              <FocusedAccountStrip
                account={focusedAccount}
                nav={focusedNav}
                free={focusedFree}
                reserved={focusedReserved}
                currency={focusedCurrency}
              />
            ) : (
              <NoFocusedAccountStrip hasAccounts={accounts.length > 0} />
            )}

            {/* Equity curve */}
            <Section
              title={t("equityCurve")}
              description={t("equityPoints", { count: equity.length })}
              divider={false}
            >
              <div className="h-[120px] relative">
                <div className="absolute inset-0">
                  <Sparkline
                    values={equitySeries}
                    width={800}
                    height={120}
                    tone="brand"
                    fill
                  />
                </div>
                <div className="absolute top-1 right-1 text-right">
                  <div className="stat-label">{t("equity")}</div>
                  <div className="text-brand-300 font-mono text-[14px]">
                    {fmtMoney(totalEquity)}
                  </div>
                </div>
              </div>
            </Section>

            {/* Positions */}
            <Section
              title={t("positionsTitle")}
              actions={
                <Link
                  href="/portfolio"
                  className="text-[12px] text-brand-300 hover:text-brand-200"
                >
                  {t("viewAll")}
                </Link>
              }
            >
              {openPositionList.length === 0 ? (
                <div className="text-[13px] text-[color:var(--text-muted)] py-3">
                  {t("noOpenPositions")}
                </div>
              ) : (
                <div className="embedded-table-scroll max-h-60">
                  <table className="table table-compact">
                    <thead>
                      <tr>
                        <th>{t("marketCol")}</th>
                        <th>{t("sideCol")}</th>
                        <th>{t("sizeCol")}</th>
                        <th>{t("entryCol")}</th>
                        <th>{t("pnlCol")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {openPositionList.slice(0, 5).map((row) => {
                        const p = row.pos;
                        const size = Number((p.size as number) || 0);
                        const isLong = size >= 0;
                        const openPnl = Number((p.unrealized_pnl_usd as number) || 0);
                        const entry = Number((p.avg_entry_price as number) || 0);
                        return (
                          <tr key={`${row.account_id}:${row.market}`}>
                            <td className="font-mono text-[12px]">{row.market}</td>
                            <td>
                              <Pill tone={isLong ? "ok" : "danger"}>
                                {isLong ? t("long") : t("short")}
                              </Pill>
                            </td>
                            <td className="text-ink-200 font-mono">{size.toFixed(4)}</td>
                            <td className="text-ink-200 font-mono">{entry ? entry.toFixed(2) : "–"}</td>
                            <td className={openPnl >= 0 ? "text-emerald-500 font-mono" : "text-rose-500 font-mono"}>
                              {fmtSigned(openPnl)}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </Section>

            {/* Recent trades */}
            <Section
              title={t("tradesTitle")}
              actions={
                <Link
                  href="/orders"
                  className="text-[12px] text-brand-300 hover:text-brand-200"
                >
                  {t("viewAll")}
                </Link>
              }
            >
              {recentTrades.length === 0 ? (
                <div className="text-[13px] text-[color:var(--text-muted)] py-3">
                  {t("noFills")}
                </div>
              ) : (
                <div className="embedded-table-scroll max-h-60">
                  <table className="table table-compact">
                    <thead>
                      <tr>
                        <th>{t("timeCol")}</th>
                        <th>{t("marketCol")}</th>
                        <th>{t("sideCol")}</th>
                        <th>{t("priceCol")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {recentTrades.slice(0, 5).map((tr, i) => {
                        const side = tr.side?.toLowerCase();
                        const isBuy = side === "buy";
                        return (
                          <tr key={tr.order_id || i}>
                            <td className="font-mono text-[12px]">{formatTime(tr.ts)}</td>
                            <td className="font-mono text-[12px]">{tr.market || "–"}</td>
                            <td>
                              <Pill tone={isBuy ? "ok" : "danger"}>
                                {isBuy ? t("buy") : t("sell")}
                              </Pill>
                            </td>
                            <td className="text-ink-200 font-mono">
                              {tr.price ? Number(tr.price).toFixed(4) : "–"}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </Section>
          </div>
        </Card>

        {/* Section 3.5 — Market (K-line), demoted below the strategy +
            position cockpit since the overview now leads with those. */}
        <Card
          title={t("sectionMarket")}
          description={t("liveCandles", {
            venue: settings.kline.venue,
            interval: settings.kline.interval,
          })}
          actions={
            <div className="flex items-center gap-2 flex-wrap">
              <div className="min-w-[140px]">
                <Select
                  value={settings.kline.venue}
                  onChange={(value) =>
                    patchSettings({
                      kline: {
                        ...settings.kline,
                        venue: value as typeof settings.kline.venue,
                      },
                    })
                  }
                  options={
                    venues.length === 0
                      ? [{
                          value: settings.kline.venue,
                          label: t("noVenues"),
                          disabled: true,
                        }]
                      : venues.map((v) => ({ value: v.name, label: v.label }))
                  }
                  size="sm"
                  ariaLabel={t("dataSource")}
                />
              </div>
              <input
                value={settings.kline.symbol}
                onChange={(e) => patchSettings({ kline: { ...settings.kline, symbol: e.target.value.toUpperCase() } })}
                className="bg-ink-900 border border-[color:var(--line)] rounded-md px-2 py-1 text-[12px] text-ink-100 focus:outline-none focus:border-brand-500/60 w-28 font-mono"
                placeholder={t("symbolPlaceholder")}
              />
              <div className="flex gap-1">
                {INTERVAL_OPTIONS.map((opt) => (
                  <button
                    key={opt.key}
                    onClick={() => patchSettings({ kline: { ...settings.kline, interval: opt.key as typeof settings.kline.interval } })}
                    className={`px-2 py-0.5 text-[12px] rounded-md font-medium ${
                      settings.kline.interval === opt.key
                        ? "bg-brand-500/15 text-brand-200 border border-brand-500/30"
                        : "text-ink-400 hover:text-ink-100"
                    }`}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
              <button
                onClick={() => loadCandles()}
                className="px-2 py-0.5 text-[12px] rounded-md text-brand-200 hover:bg-brand-500/10 border border-brand-500/25"
                title={t("refresh")}
              >
                ↻
              </button>
              {firstClose ? (
                <span
                  className={`text-[12px] ml-1 ${candleDeltaPct >= 0 ? "text-emerald-500" : "text-rose-500"}`}
                >
                  {candleDeltaPct >= 0 ? "+" : ""}
                  {candleDeltaPct.toFixed(2)}%
                </span>
              ) : null}
            </div>
          }
        >
          <CandleChart
            candles={candles}
            width={960}
            height={220}
            mode={settings.chartType}
            showVolume={settings.showVolume}
            loading={candleLoading}
            error={candleError || undefined}
          />
        </Card>

        {/* Section 4 — Attention (only when there are signals). */}
        {attentionList.length > 0 ? (
          <Card
            title={t("sectionAttention")}
            actions={
              <Link
                href="/inbox"
                className="text-[12px] text-brand-300 hover:text-brand-200"
              >
                {t("viewInbox")}
              </Link>
            }
          >
            <ul className="space-y-2">
              {attentionList.map((item) => (
                <li
                  key={item.id}
                  className="flex items-start gap-3 py-1.5"
                >
                  <span className="mt-1.5 shrink-0">
                    <StatusDot
                      tone={
                        item.severity === "danger"
                          ? "danger"
                          : item.severity === "warn"
                          ? "warn"
                          : "brand"
                      }
                    />
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] text-[color:var(--text-base)] truncate">
                      {item.title}
                    </div>
                    {item.summary ? (
                      <div className="text-[12px] text-[color:var(--text-muted)] truncate">
                        {item.summary}
                      </div>
                    ) : null}
                  </div>
                  {item.href ? (
                    <Link
                      href={item.href}
                      className="text-[12px] text-brand-300 hover:text-brand-200 shrink-0"
                    >
                      {tHero("open")}
                    </Link>
                  ) : null}
                </li>
              ))}
            </ul>
          </Card>
        ) : null}
      </PageBody>
    </div>
  );
}

/* ---------------------------- helpers -------------------------------------- */

function FocusedAccountStrip({
  account,
  nav,
  free,
  reserved,
  currency,
}: {
  account: AccountSummary;
  nav: number;
  free: number;
  reserved: number;
  currency: string;
}) {
  const t = useTranslations("home");
  const profile = account.profile;
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-3 pb-1">
      <div className="min-w-0">
        <div className="font-mono text-[14px] text-[color:var(--text-base)]">
          {profile.id}
        </div>
        <div className="text-[12px] text-[color:var(--text-muted)]">
          {profile.venue} · {profile.kind} · {profile.mode}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 ml-auto">
        <FocusedKpi label={t("nav")} value={formatBalance(nav, currency)} />
        <FocusedKpi label={t("free")} value={formatBalance(free, currency)} />
        <FocusedKpi
          label={t("reserved")}
          value={formatBalance(reserved, currency)}
          tone={reserved > 0 ? "warn" : "neutral"}
        />
        <FocusedKpi
          label={t("openPositionsLabel")}
          value={String(account.open_position_count)}
        />
        <Link
          href={`/accounts/${encodeURIComponent(profile.id)}`}
          className="text-[12px] text-brand-300 hover:text-brand-200"
        >
          {t("openDriver")}
        </Link>
      </div>
    </div>
  );
}

function FocusedKpi({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "neutral" | "warn" | "brand";
}) {
  const colour =
    tone === "warn"
      ? "text-amber-500"
      : tone === "brand"
      ? "text-brand-300"
      : "text-[color:var(--text-base)]";
  return (
    <div className="flex flex-col">
      <span className="text-[12px] text-[color:var(--text-muted)]">{label}</span>
      <span className={`font-mono text-[13px] ${colour}`}>{value}</span>
    </div>
  );
}

function NoFocusedAccountStrip({ hasAccounts }: { hasAccounts: boolean }) {
  const t = useTranslations("home");
  return (
    <div className="rounded-lg border border-amber-400/20 bg-amber-400/5 px-3 py-2.5 text-[13px] text-amber-300">
      {hasAccounts ? (
        <>{t("noFocusPickOne")}</>
      ) : (
        <>
          {t("noAccountsPrefix")}{" "}
          <Link href="/accounts" className="underline hover:text-amber-100">
            {t("addAccountLink")}
          </Link>
        </>
      )}
    </div>
  );
}

function strategyPillTone(status: string): "ok" | "warn" | "brand" | "neutral" {
  const s = (status || "").toLowerCase();
  if (s === "live") return "ok";
  if (s === "canary") return "warn";
  if (s === "paper") return "brand";
  return "neutral";
}

function fmtMoney(v: number | undefined): string {
  if (v === undefined || v === null || !Number.isFinite(v)) return "–";
  const abs = Math.abs(v);
  if (abs >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `$${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  return `$${v.toFixed(2)}`;
}

function fmtSigned(v: number | undefined): string {
  if (v === undefined || v === null || !Number.isFinite(v)) return "–";
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import {
  AgentsIcon,
  EvolutionIcon,
  OrdersIcon,
  PortfolioIcon,
  ScriptRunIcon,
  SendIcon,
  SkillsIcon,
  StrategiesIcon,
  SubagentsIcon,
  TriggersIcon,
} from "../../components/icons";
import {
  Card,
  ErrorBanner,
  Kpi,
  PageBody,
  Pill,
  StatusDot,
} from "../../components/Page";
import { OperatorOverviewHero } from "../../components/OperatorOverviewHero";
import { SetupReadinessCard } from "../../components/SetupReadinessCard";
import { Sparkline } from "../../components/Sparkline";
import { CandleChart } from "../../components/CandleChart";
import { clientApi } from "../../lib/clientApi";
import type {
  Candle,
  EquityPoint,
  PortfolioSummary,
  RecentTrade,
  StrategyCard,
} from "../../lib/api";
import type { AccountSummary } from "../../lib/clientApi";
import { useUiSettings } from "../../lib/settings";
import { useCurrentAccountId, formatBalance } from "../../lib/currentAccount";
import { formatTime } from "../../lib/format";

type Message = { id?: string; ts?: string; channel?: string; text?: string; severity?: string };
type Proposal = { id?: string; kind?: string; summary?: string; status?: string };

const INTERVAL_OPTIONS: { key: string; label: string }[] = [
  { key: "1m", label: "1m" },
  { key: "5m", label: "5m" },
  { key: "15m", label: "15m" },
  { key: "1h", label: "1H" },
  { key: "4h", label: "4H" },
  { key: "1d", label: "1D" },
];

export default function DashboardOverview() {
  const t = useTranslations("home");
  const [settings, patchSettings] = useUiSettings();
  const [currentAccountId] = useCurrentAccountId();

  const [workspace, setWorkspace] = useState<{ root: string; live_trading_enabled: boolean; kill_switch: boolean } | null>(null);
  const [apiOnline, setApiOnline] = useState(false);
  const [skills, setSkills] = useState<{ id: string }[]>([]);
  const [routes, setRoutes] = useState<unknown[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [strategies, setStrategies] = useState<StrategyCard[]>([]);
  const [recentTrades, setRecentTrades] = useState<RecentTrade[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [venues, setVenues] = useState<{ name: string; label: string }[]>([]);
  // 04-29 §11 P9 — pull the multi-account roster so the
  // focused-account hero can show NAV / cash / positions / mode in
  // the account's native currency.
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);

  const [candles, setCandles] = useState<Candle[]>([]);
  const [candleLoading, setCandleLoading] = useState(false);
  const [candleError, setCandleError] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);

  const loadCore = useCallback(async () => {
    try {
      await clientApi.health();
      setApiOnline(true);
    } catch (e) {
      setApiOnline(false);
      setError(e instanceof Error ? e.message : String(e));
      return;
    }

    const safe = async <T,>(p: Promise<T>, fallback: T): Promise<T> => {
      try { return await p; } catch { return fallback; }
    };

    const [ws, sk, tr, pr, msg, ps, st, rt, ec, vn, accList] = await Promise.all([
      safe(clientApi.workspace(), null),
      safe(fetch("/api/proxy/skills", { cache: "no-store" }).then((r) => r.json()), { skills: [] }),
      safe(fetch("/api/proxy/triggers/routes", { cache: "no-store" }).then((r) => r.json()), []),
      safe(fetch("/api/proxy/evolution/proposals", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" }).then((r) => r.json()), { proposals: [] }),
      safe(fetch("/api/proxy/messages/list", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ limit: 30 }) }).then((r) => r.json()), { messages: [] }),
      safe(clientApi.portfolioSummary(), null),
      safe(clientApi.strategyList(), { strategies: [] }),
      safe(clientApi.recentTrades(20), { trades: [] }),
      safe(clientApi.portfolioEquityCurve(120), { points: [], equity_usd: 0 }),
      safe(clientApi.marketVenues(), { venues: [] }),
      safe(clientApi.accountsList(), { accounts: [] as AccountSummary[], ts: 0 }),
    ]);

    setWorkspace(ws);
    setSkills((sk as { skills: { id: string }[] })?.skills || []);
    const routesBody = Array.isArray(tr) ? tr : (tr as { routes: unknown[] })?.routes || [];
    setRoutes(routesBody);
    setProposals((pr as { proposals: Proposal[] })?.proposals || []);
    setMessages((msg as { messages: Message[] })?.messages || []);
    setSummary(ps as PortfolioSummary | null);
    setStrategies(((st as { strategies: StrategyCard[] })?.strategies) || []);
    setRecentTrades(((rt as { trades: RecentTrade[] })?.trades) || []);
    setEquity(((ec as { points: EquityPoint[] })?.points) || []);
    setVenues((((vn as { venues: { name: string; label: string }[] })?.venues) || []).map((v) => ({ name: v.name, label: v.label })));
    setAccounts(((accList as { accounts?: AccountSummary[] })?.accounts) || []);
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

  const totalRealizedPnl = strategies.reduce((sum, s) => sum + (s.realized_pnl_usd || 0), 0);
  const dailyPnl = recentTrades
    .filter((t) => t.ts && (new Date(t.ts).getTime() > Date.now() - 24 * 3600 * 1000))
    .reduce((sum, t) => sum + (Number((t as unknown as { pnl_usd?: number }).pnl_usd) || 0), 0);
  const winRate = useMemo(() => {
    const tot = strategies.reduce((acc, s) => acc + s.wins + s.losses, 0);
    const wins = strategies.reduce((acc, s) => acc + s.wins, 0);
    return tot ? (wins / tot) * 100 : 0;
  }, [strategies]);
  const activeStrategiesCount = strategies.filter(
    (s) => s.status === "paper" || s.status === "canary" || s.status === "live",
  ).length;

  const equitySeries = useMemo(() => {
    if (equity.length < 2) {
      return [] as number[];
    }
    return equity.map((p) => p.equity_usd);
  }, [equity]);

  // Allocation by market value across accounts
  const allocation = useMemo(() => {
    if (!summary) return [] as { label: string; pct: number; val: number; color: string }[];
    const buckets: Record<string, number> = {};
    let cash = 0;
    for (const acc of summary.accounts) {
      cash += acc.cash_usd || 0;
      for (const [market, pos] of Object.entries(acc.positions || {})) {
        const p = pos as unknown as Record<string, unknown>;
        const size = Number(p.size || 0);
        const last = Number(p.last_price || p.avg_entry_price || 0);
        const notional = Math.abs(size * last);
        if (notional <= 0) continue;
        const asset = market.split(/[:\/\-]/)[1] || market;
        buckets[asset] = (buckets[asset] || 0) + notional;
      }
    }
    if (cash) buckets["USDT"] = (buckets["USDT"] || 0) + cash;

    const total = Object.values(buckets).reduce((a, b) => a + b, 0) || 1;
    const entries = Object.entries(buckets)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5);
    const palette = ["#b48bff", "#8b5cf6", "#7c3aed", "#ec4899", "#454560"];
    return entries.map(([label, val], i) => ({
      label,
      val,
      pct: (val / total) * 100,
      color: palette[i] || "#454560",
    }));
  }, [summary]);

  const mode = workspace ? (workspace.live_trading_enabled ? "LIVE" : "PAPER") : "—";
  const killed = !!workspace?.kill_switch;

  // 04-29 §11 P9 — resolve the operator-focused account so
  // the hero strip can render NAV / cash / positions in the account's
  // native currency (USDT for paper, CNY for an A-share row, USDC for
  // an on-chain wallet, etc).
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

  const systemStatus: { label: string; pct: number; tone: "ok" | "warn" | "danger" }[] = [
    { label: t("agentCore"), pct: apiOnline ? 100 : 0, tone: apiOnline ? "ok" : "danger" },
    { label: t("riskEngine"), pct: apiOnline && !killed ? 100 : 0, tone: apiOnline && !killed ? "ok" : "warn" },
    { label: t("dataFeed"), pct: apiOnline && !candleError ? 100 : candleError ? 55 : 0, tone: !apiOnline ? "danger" : candleError ? "warn" : "ok" },
    { label: t("llmGateway"), pct: apiOnline ? 100 : 0, tone: apiOnline ? "ok" : "warn" },
    { label: t("messageHub"), pct: apiOnline ? 100 : 0, tone: apiOnline ? "ok" : "warn" },
  ];

  const recentActivity: { label: string; sub: string; time: string; tone: "ok" | "brand" | "warn" }[] = [];
  for (const m of messages.slice(0, 5)) {
    const when = formatTime(m.ts);
    recentActivity.push({
      label: m.text?.slice(0, 60) || t("messageOn", { channel: m.channel || "" }),
      sub: m.channel ? t("channelSub", { channel: m.channel }) : "",
      time: when,
      tone: m.severity === "warn" ? "warn" : m.severity === "error" ? "warn" : "brand",
    });
  }
  if (recentActivity.length === 0) {
    recentActivity.push(
      { label: t("runtimeConnected"), sub: t("localApiHealth"), time: t("now"), tone: "ok" },
      { label: t("skillBusReady"), sub: t("skillsEnabled", { count: skills.length }), time: t("justNow"), tone: "brand" },
    );
  }

  const notifications: { label: string; time: string; tone: "ok" | "warn" | "danger" | "brand" }[] = [
    ...(proposals.slice(0, 3).map((p): { label: string; time: string; tone: "warn" } => ({
      label: t("proposalPending", { label: p.summary || p.kind || p.id || "unnamed" }),
      time: p.status || t("open"),
      tone: "warn",
    }))),
    { label: t("triggerRoutesLoaded", { count: routes.length }), time: t("live"), tone: "brand" },
    { label: killed ? t("killEngaged") : t("riskGateNormal"), time: t("now"), tone: killed ? "danger" : "ok" },
  ];

  const quickActions = [
    { icon: <StrategiesIcon size={22} />, label: t("newStrategy"), href: "/strategies" },
    { icon: <TriggersIcon size={22} />, label: t("createWorkflow"), href: "/workflows" },
    { icon: <AgentsIcon size={22} />, label: t("openAgentWorkspace"), href: "/chat" },
    { icon: <SendIcon size={22} />, label: t("actionInbox"), href: "/inbox" },
  ];

  const lastClose = candles.length ? candles[candles.length - 1].close : 0;
  const firstClose = candles.length ? candles[0].close : 0;
  const candleDeltaPct = firstClose ? ((lastClose - firstClose) / firstClose) * 100 : 0;

  return (
    <div>
      {error && <ErrorBanner error={error} />}

      <PageBody>
        {/* Operator overview hero — (frontend redesign).
            Sources health + attention items from /operator/overview. */}
        <OperatorOverviewHero />

        {/* Setup readiness — first-run checklist. The card
            self-hides once ``status === ok``, so it only takes space
            when something actually needs configuring. */}
        <SetupReadinessCard collapsed />

        {/* Focused-account strip (04-29 §11 P9). Mirrors
            the AccountSelector in the top header — pick a different
            account up there and this card refreshes in place. */}
        {focusedAccount ? (
          <FocusedAccountStrip
            account={focusedAccount}
            nav={focusedNav}
            free={focusedFree}
            reserved={focusedReserved}
            currency={focusedCurrency}
          />
        ) : (
          <NoFocusedAccountStrip
            hasAccounts={accounts.length > 0}
          />
        )}

        {/* KPI row — now driven by real backend data where available. */}
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4">
          <Kpi
            label={t("totalEquity")}
            value={fmtMoney(totalEquity)}
            delta={<span className="text-ink-400">{summary ? t("accountCount", { count: summary.accounts.length }) : "—"}</span>}
            icon={<PortfolioIcon size={18} />}
            spark={equitySeries.slice(-24)}
            sparkTone="brand"
          />
          <Kpi
            label={t("realizedPnl")}
            value={fmtSigned(totalRealizedPnl)}
            delta={<span className={totalRealizedPnl >= 0 ? "stat-delta-up" : "stat-delta-dn"}>
              {t("strategiesCount", { count: strategies.length })}
            </span>}
            tone={totalRealizedPnl >= 0 ? "ok" : "danger"}
            icon={<OrdersIcon size={18} />}
            spark={equitySeries.slice(-24)}
            sparkTone={totalRealizedPnl >= 0 ? "accent" : "magenta"}
          />
          <Kpi
            label={t("symbolDelta", { symbol: settings.kline.symbol })}
            value={`${candleDeltaPct >= 0 ? "+" : ""}${candleDeltaPct.toFixed(2)}%`}
            delta={<span className="text-ink-400">
              {settings.kline.venue.toUpperCase()} · {settings.kline.interval}
            </span>}
            tone={candleDeltaPct >= 0 ? "ok" : "danger"}
            icon={<OrdersIcon size={18} />}
            spark={candles.map((c) => c.close)}
            sparkTone={candleDeltaPct >= 0 ? "accent" : "magenta"}
          />
          <Kpi
            label={t("winRate")}
            value={`${winRate.toFixed(1)}%`}
            delta={<span className="text-ink-400">
              {t("winsLosses", { wins: strategies.reduce((a, s) => a + s.wins, 0), losses: strategies.reduce((a, s) => a + s.losses, 0) })}
            </span>}
            tone="brand"
            icon={<SkillsIcon size={18} />}
            spark={[]}
            sparkTone="brand"
          />
          <Kpi
            label={t("activeStrategies")}
            value={activeStrategiesCount}
            delta={<span className="text-ink-400">{t("totalStrategies", { count: strategies.length })}</span>}
            icon={<StrategiesIcon size={18} />}
            spark={[]}
            sparkTone="brand"
          />
          <Kpi
            label={t("openPositions")}
            value={openPositionList.length}
            delta={<span className="text-ink-400">
              {summary ? t("accountCount", { count: summary.accounts.length }) : "—"}
            </span>}
            icon={<AgentsIcon size={18} />}
            spark={[]}
            sparkTone="magenta"
          />
        </div>

        {/* K-line chart + venue/symbol picker */}
        <Card
          title={`${settings.kline.symbol}`}
          description={t("liveCandles", { venue: settings.kline.venue.toUpperCase(), interval: settings.kline.interval })}
          actions={
            <div className="flex items-center gap-2 flex-wrap">
              <select
                value={settings.kline.venue}
                onChange={(e) => patchSettings({ kline: { ...settings.kline, venue: e.target.value as typeof settings.kline.venue } })}
                className="bg-ink-900 border border-brand-500/25 rounded-md px-2 py-1 text-xs text-ink-100 focus:outline-none focus:border-brand-500/60"
                title={t("dataSource")}
              >
                {venues.length === 0 ? (
                  <option value={settings.kline.venue} disabled>
                    {t("noVenues")}
                  </option>
                ) : (
                  venues.map((v) => (
                    <option key={v.name} value={v.name}>{v.label}</option>
                  ))
                )}
              </select>
              <input
                value={settings.kline.symbol}
                onChange={(e) => patchSettings({ kline: { ...settings.kline, symbol: e.target.value.toUpperCase() } })}
                className="bg-ink-900 border border-brand-500/25 rounded-md px-2 py-1 text-xs text-ink-100 focus:outline-none focus:border-brand-500/60 w-28 font-mono"
                placeholder="BTCUSDT"
              />
              <div className="flex gap-1">
                {INTERVAL_OPTIONS.map((opt) => (
                  <button
                    key={opt.key}
                    onClick={() => patchSettings({ kline: { ...settings.kline, interval: opt.key as typeof settings.kline.interval } })}
                    className={`px-2 py-0.5 text-[10px] rounded-md font-medium tracking-wider ${
                      settings.kline.interval === opt.key
                        ? "bg-brand-500 text-white"
                        : "text-ink-400 hover:text-white hover:bg-brand-500/10"
                    }`}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
              <button
                onClick={() => loadCandles()}
                className="px-2 py-0.5 text-[10px] rounded-md text-brand-200 hover:bg-brand-500/10 border border-brand-500/25"
                title={t("refresh")}
              >
                ↻
              </button>
            </div>
          }
        >
          <CandleChart
            candles={candles}
            width={960}
            height={260}
            mode={settings.chartType}
            showVolume={settings.showVolume}
            loading={candleLoading}
            error={candleError || undefined}
          />
        </Card>

        {/* Equity curve + Allocation + System status */}
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <Card title={t("equityCurve")} description={t("equityPoints", { count: equity.length })}>
            <div className="h-[180px] relative">
              <div className="absolute inset-0">
                <Sparkline
                  values={equitySeries}
                  width={800}
                  height={180}
                  tone="brand"
                  fill
                />
              </div>
              <div className="absolute top-2 right-2 text-right">
                <div className="text-[10px] text-ink-400 uppercase tracking-wider">{t("equity")}</div>
                <div className="text-brand-200 font-mono text-sm">{fmtMoney(totalEquity)}</div>
              </div>
            </div>
          </Card>

          <Card title={t("portfolioAllocation")}>
            <div className="flex items-center gap-4">
              <DonutPreview slices={allocation} totalLabel={fmtMoney(totalEquity || allocation.reduce((a, b) => a + b.val, 0))} totalTitle={t("total")} />
              <div className="flex-1 space-y-2">
                {allocation.length === 0 ? (
                  <div className="text-[11.5px] text-ink-500">{t("noHoldings")}</div>
                ) : allocation.map((row) => (
                  <div key={row.label} className="flex items-center gap-2 text-[12px]">
                    <span className="w-2 h-2 rounded-sm" style={{ background: row.color }} />
                    <span className="text-ink-200 w-14">{row.label}</span>
                    <span className="text-ink-400 w-14">{row.pct.toFixed(1)}%</span>
                    <span className="text-white font-mono ml-auto">{fmtMoney(row.val)}</span>
                  </div>
                ))}
              </div>
            </div>
          </Card>

          <Card title={t("systemStatus")}>
            <div className="flex items-center gap-4">
              <HexScore score={apiOnline ? 100 : 0} />
              <div className="flex-1 space-y-2.5">
                {systemStatus.map((s) => (
                  <div key={s.label} className="flex items-center gap-2 text-[12px]">
                    <span className={`w-2 h-2 rounded-full ${
                      s.tone === "ok" ? "bg-accent-500" : s.tone === "warn" ? "bg-[#f5a524]" : "bg-[#ef4560]"
                    }`} />
                    <span className="text-ink-200">{s.label}</span>
                    <span className="ml-auto text-ink-400 font-mono">{s.pct}%</span>
                  </div>
                ))}
              </div>
            </div>
          </Card>
        </div>

        {/* Activity / Notifications */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <Card title={t("recentActivity")} actions={<a href="/inbox" className="text-[11px] text-brand-300 hover:text-brand-200">{t("viewAll")}</a>}>
            <ul className="embedded-list-scroll space-y-2">
              {recentActivity.map((a, i) => (
                <li key={i} className="flex items-start gap-3">
                  <div className={`mt-1 w-2 h-2 rounded-full ${
                    a.tone === "ok" ? "bg-accent-500" : a.tone === "warn" ? "bg-[#f5a524]" : "bg-brand-400"
                  }`} />
                  <div className="flex-1 min-w-0">
                    <div className="text-[12.5px] text-ink-100 truncate">{a.label}</div>
                    {a.sub ? <div className="text-[10.5px] text-ink-500">{a.sub}</div> : null}
                  </div>
                  <div className="text-[11px] text-ink-500 font-mono">{a.time}</div>
                </li>
              ))}
            </ul>
          </Card>

          <Card title={t("notifications")} actions={<a href="/inbox" className="text-[11px] text-brand-300 hover:text-brand-200">{t("viewAll")}</a>}>
            <ul className="embedded-list-scroll space-y-2">
              {notifications.map((n, i) => (
                <li key={i} className="flex items-center gap-2 text-[12.5px]">
                  <span className={`w-2 h-2 rounded-full ${
                    n.tone === "ok" ? "bg-accent-500"
                      : n.tone === "warn" ? "bg-[#f5a524]"
                      : n.tone === "danger" ? "bg-[#ef4560]"
                      : "bg-brand-400"
                  }`} />
                  <span className="flex-1 truncate text-ink-100">{n.label}</span>
                  <span className="text-[10px] text-ink-500 font-mono">{n.time}</span>
                </li>
              ))}
            </ul>
          </Card>
        </div>

        {/* Strategies + Positions + Quick Actions */}
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <Card
            title={t("activeStrategiesTitle")}
            actions={<a href="/strategies" className="text-[11px] text-brand-300 hover:text-brand-200">{t("viewAll")}</a>}
            padded={false}
          >
            {strategies.length === 0 ? (
              <div className="p-4 text-[12px] text-ink-500">{t("noStrategies")}</div>
            ) : (
              <div className="embedded-table-scroll max-h-72 rounded-none border-0">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t("strategyCol")}</th>
                      <th>{t("statusCol")}</th>
                      <th>{t("pnlCol")}</th>
                      <th>{t("winRateCol")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {strategies.slice(0, 6).map((s) => (
                      <tr key={s.id}>
                        <td className="font-mono text-[12px]" title={s.title}>{s.id}</td>
                        <td><Pill tone={s.status === "live" ? "warn" : s.status === "canary" ? "warn" : s.status === "paper" ? "ok" : "brand"}>{s.status.toUpperCase()}</Pill></td>
                        <td className={s.realized_pnl_usd >= 0 ? "text-accent-400 font-mono" : "text-[#ef4560] font-mono"}>
                          {fmtSigned(s.realized_pnl_usd)}
                        </td>
                        <td className="text-ink-200 font-mono">{s.win_rate_pct.toFixed(1)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card
            title={t("openPositionsTitle")}
            actions={<a href="/portfolio" className="text-[11px] text-brand-300 hover:text-brand-200">{t("viewAll")}</a>}
            padded={false}
          >
            {openPositionList.length === 0 ? (
              <div className="p-4 text-[12px] text-ink-500">{t("noOpenPositions")}</div>
            ) : (
              <div className="embedded-table-scroll max-h-72 rounded-none border-0">
                <table className="table">
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
                    {openPositionList.slice(0, 6).map((row) => {
                      const p = row.pos;
                      const size = Number((p.size as number) || 0);
                      const side = size >= 0 ? "LONG" : "SHORT";
                      const realized = Number((p.realized_pnl_usd as number) || 0);
                      const entry = Number((p.avg_entry_price as number) || 0);
                      return (
                        <tr key={`${row.account_id}:${row.market}`}>
                          <td className="font-mono text-[12px]">{row.market}</td>
                          <td><Pill tone={side === "LONG" ? "ok" : "danger"}>{side}</Pill></td>
                          <td className="text-ink-200 font-mono">{size.toFixed(4)}</td>
                          <td className="text-ink-200 font-mono">{entry ? entry.toFixed(2) : "—"}</td>
                          <td className={realized >= 0 ? "text-accent-400 font-mono" : "text-[#ef4560] font-mono"}>
                            {fmtSigned(realized)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card title={t("quickActions")}>
            <div className="grid grid-cols-2 gap-3">
              {quickActions.map((q) => (
                <a
                  key={q.label}
                  href={q.href}
                  className="group flex flex-col items-center justify-center gap-2 py-5 rounded-xl border border-brand-500/15 bg-gradient-to-br from-brand-500/5 to-transparent hover:border-brand-500/40 hover:shadow-glow transition-all"
                >
                  <div className="text-brand-300 group-hover:text-brand-200 transition-colors">{q.icon}</div>
                  <div className="text-[11.5px] text-ink-200 text-center">{q.label}</div>
                </a>
              ))}
            </div>
          </Card>
        </div>

        {/* Recent Trades + Strategy Performance */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <Card
            title={t("recentTrades")}
            actions={<a href="/portfolio" className="text-[11px] text-brand-300 hover:text-brand-200">{t("viewAll")}</a>}
            padded={false}
          >
            {recentTrades.length === 0 ? (
              <div className="p-4 text-[12px] text-ink-500">{t("noFills")}</div>
            ) : (
              <div className="embedded-table-scroll max-h-80 rounded-none border-0">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t("timeCol")}</th>
                      <th>{t("marketCol")}</th>
                      <th>{t("sideCol")}</th>
                      <th>{t("typeCol")}</th>
                      <th>{t("priceCol")}</th>
                      <th>{t("statusCol")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recentTrades.slice(0, 8).map((t2, i) => (
                      <tr key={t2.order_id || i}>
                        <td className="font-mono text-[11.5px]">{formatTime(t2.ts)}</td>
                        <td className="font-mono text-[11.5px]">{t2.market || "—"}</td>
                        <td><Pill tone={t2.side?.toLowerCase() === "buy" ? "ok" : "danger"}>{t2.side?.toUpperCase() || "?"}</Pill></td>
                        <td className="text-ink-200">{t2.type || "MARKET"}</td>
                        <td className="text-ink-200 font-mono">{t2.price ? Number(t2.price).toFixed(4) : "—"}</td>
                        <td><Pill tone="brand">{t2.status || "FILLED"}</Pill></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card title={t("strategyPerformance")} actions={<a href="/strategies" className="text-[11px] text-brand-300 hover:text-brand-200">{t("viewAll")}</a>}>
            {strategies.length === 0 ? (
              <div className="text-[12px] text-ink-500">{t("noStrategiesYet")}</div>
            ) : (
              <ul className="embedded-list-scroll space-y-2.5">
                {strategies.slice(0, 6).map((s) => (
                  <li key={s.id} className="flex items-center gap-3">
                    <span className="font-mono text-[12px] text-ink-100 w-28 truncate">{s.id}</span>
                    <span className={`font-mono text-[12px] w-20 ${s.realized_pnl_usd >= 0 ? "text-accent-400" : "text-[#ef4560]"}`}>
                      {fmtSigned(s.realized_pnl_usd)}
                    </span>
                    <span className="ml-auto text-[10px] text-ink-500 font-mono">
                      {t("winsLosses", { wins: s.wins, losses: s.losses })}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        {/* Runtime footer */}
        <Card
          title={t("runtime")}
          description={t("runtimeDesc")}
          actions={
            <div className="flex items-center gap-2">
              <StatusDot
                tone={apiOnline ? "ok" : "danger"}
                label={apiOnline ? t("apiOnline") : t("apiOffline")}
              />
              <Pill tone={mode === "LIVE" ? "warn" : "brand"}>{mode}</Pill>
              <Pill tone={killed ? "danger" : "ok"}>{killed ? t("killSwitch") : t("normal")}</Pill>
              <button
                onClick={() => { loadCore(); loadCandles(); }}
                className="text-[10px] px-2 py-0.5 rounded-md text-brand-200 border border-brand-500/25 hover:bg-brand-500/10"
              >
                {t("refresh")}
              </button>
            </div>
          }
        >
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-[12px]">
            <RuntimeStat icon={<SkillsIcon size={15} />} label={t("skills")} value={skills.length} />
            <RuntimeStat icon={<TriggersIcon size={15} />} label={t("routes")} value={routes.length} />
            <RuntimeStat
              icon={<EvolutionIcon size={15} />}
              label={t("proposals")}
              value={proposals.length}
              tone={proposals.length > 0 ? "warn" : "neutral"}
            />
            <RuntimeStat
              icon={<SubagentsIcon size={15} />}
              label={t("workspace")}
              value={workspace?.root?.split(/[\\/]/).pop() || "—"}
              mono
            />
          </div>
        </Card>
      </PageBody>
    </div>
  );
}

/* ------------------------------------------------------------- helpers */

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
  const tone =
    profile.mode === "live"
      ? "from-rose-500/20 to-rose-700/10 border-rose-500/30"
      : profile.mode === "canary"
      ? "from-amber-500/20 to-amber-700/10 border-amber-500/30"
      : profile.mode === "shadow"
      ? "from-brand-500/20 to-brand-700/10 border-brand-500/30"
      : "from-emerald-500/15 to-emerald-700/10 border-emerald-500/30";
  return (
    <div
      className={`rounded-xl border bg-gradient-to-r ${tone} p-4 flex flex-wrap items-center gap-4`}
    >
      <div>
        <div className="text-[11px] uppercase tracking-widest text-ink-300">
          {t("focusedAccount")}
        </div>
        <div className="font-mono text-base text-ink-100 mt-0.5">
          {profile.id}
        </div>
        <div className="text-[11px] text-ink-400 mt-0.5">
          {profile.venue} · {profile.kind} · {profile.mode} · {profile.status}
        </div>
      </div>
      <div className="ml-auto flex flex-wrap items-center gap-6">
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
        <FocusedKpi
          label={t("activeExecutors")}
          value={String(account.active_executors.length)}
          tone={account.active_executors.length > 0 ? "brand" : "neutral"}
        />
        <a
          href={`/accounts/${encodeURIComponent(profile.id)}`}
          className="btn-ghost text-xs text-brand-200"
        >
          {t("openDriver")}
        </a>
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
      ? "text-amber-200"
      : tone === "brand"
      ? "text-brand-200"
      : "text-ink-100";
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-widest text-ink-400">
        {label}
      </span>
      <span className={`font-mono text-sm ${colour}`}>{value}</span>
    </div>
  );
}

function NoFocusedAccountStrip({ hasAccounts }: { hasAccounts: boolean }) {
  const t = useTranslations("home");
  return (
    <div className="rounded-xl border border-amber-400/20 bg-amber-400/5 px-4 py-3 text-sm text-amber-100">
      {hasAccounts ? (
        <>{t("noFocusPickOne")}</>
      ) : (
        <>
          {t("noAccountsPrefix")}{" "}
          <a
            href="/accounts"
            className="underline hover:text-amber-50"
          >
            {t("addAccountLink")}
          </a>
        </>
      )}
    </div>
  );
}

function fmtMoney(v: number | undefined): string {
  if (v === undefined || v === null || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `$${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  return `$${v.toFixed(2)}`;
}

function fmtSigned(v: number | undefined): string {
  if (v === undefined || v === null || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function RuntimeStat({ icon, label, value, mono, tone = "neutral" }: {
  icon: React.ReactNode; label: string; value: React.ReactNode; mono?: boolean; tone?: "neutral" | "warn";
}) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-brand-500/10 bg-ink-900/40 px-3 py-2">
      <span className="text-brand-300">{icon}</span>
      <div className="flex-1 min-w-0">
        <div className="text-[9.5px] uppercase tracking-[0.14em] text-ink-500">{label}</div>
        <div className={`truncate ${mono ? "font-mono text-[11px]" : ""} ${tone === "warn" ? "text-[#f5a524]" : "text-ink-100"}`}>
          {value}
        </div>
      </div>
    </div>
  );
}

function DonutPreview({ slices, totalLabel, totalTitle }: {
  slices: { label: string; pct: number; val: number; color: string }[];
  totalLabel: string;
  totalTitle: string;
}) {
  const size = 128;
  const stroke = 22;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  let offset = 0;
  const fallback = slices.length ? slices : [{ label: "—", pct: 100, val: 0, color: "#454560" }];
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="rgba(139,92,246,0.1)"
          strokeWidth={stroke}
        />
        {fallback.map((s, i) => {
          const len = (s.pct / 100) * c;
          const el = (
            <circle
              key={i}
              cx={size / 2}
              cy={size / 2}
              r={r}
              fill="none"
              stroke={s.color}
              strokeWidth={stroke}
              strokeDasharray={`${len} ${c - len}`}
              strokeDashoffset={-offset}
              transform={`rotate(-90 ${size / 2} ${size / 2})`}
              strokeLinecap="butt"
            />
          );
          offset += len;
          return el;
        })}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div className="text-[10px] text-ink-400 uppercase tracking-wider">{totalTitle}</div>
        <div className="text-white font-mono text-[13px]">{totalLabel}</div>
      </div>
    </div>
  );
}

function HexScore({ score }: { score: number }) {
  const pct = Math.max(0, Math.min(100, score));
  const size = 128;
  const cx = size / 2;
  const cy = size / 2;
  const rOuter = 50;
  const points: string[] = [];
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i - Math.PI / 2;
    points.push(`${cx + rOuter * Math.cos(a)},${cy + rOuter * Math.sin(a)}`);
  }
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <polygon
          points={points.join(" ")}
          fill="rgba(139,92,246,0.08)"
          stroke="rgba(139,92,246,0.35)"
          strokeWidth={1.5}
        />
        <polygon
          points={points.join(" ")}
          fill="none"
          stroke="rgba(180,139,255,0.25)"
          strokeWidth={1}
          transform={`scale(0.7) translate(${cx * 0.43} ${cy * 0.43})`}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div className="text-accent-400 font-semibold text-xl">{pct}%</div>
      </div>
    </div>
  );
}

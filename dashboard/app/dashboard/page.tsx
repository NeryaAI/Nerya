"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import {
  Advanced,
  Card,
  ErrorBanner,
  Kpi,
  PageBody,
  Pill,
  Section,
  StatusDot,
} from "../../components/Page";
import { SetupReadinessCard } from "../../components/SetupReadinessCard";
import { ModePill } from "../../components/ModePill";
import { Sparkline } from "../../components/Sparkline";
import { CandleChart } from "../../components/CandleChart";
import { Select } from "../../components/Select";
import { clientApi, invalidateReadCache } from "../../lib/clientApi";
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
import { WorkspaceCustomizeButton } from "../../components/workspace/WorkspaceCustomizeButton";
import { WorkspaceUiHome } from "../../components/workspace/WorkspaceUiHome";

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
  const [settings, patchSettings] = useUiSettings();
  const [currentAccountId, setCurrentAccountId] = useCurrentAccountId();

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
  // Local draft for the symbol input — committing to settings.kline.symbol
  // (which the candle effect depends on) refetches K-lines, so we only
  // commit on Enter/blur instead of on every keystroke.
  const [symbolDraft, setSymbolDraft] = useState(settings.kline.symbol);

  const [error, setError] = useState<string | null>(null);
  // First load gate: skeletons render until the initial loadCore round
  // lands; background 30s refreshes never flip it back (no flicker).
  const [coreLoaded, setCoreLoaded] = useState(false);
  // Single-flight guard for the 11-endpoint polling round: if the previous
  // round is still in flight, skip this tick instead of stacking slow
  // requests on top of each other.
  const coreInFlight = useRef(false);
  const candleSequence = useRef(0);

  const loadCore = useCallback(async () => {
    if (coreInFlight.current) return;
    coreInFlight.current = true;
    try {
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
        .then(() => ({ online: true, error: null as string | null }))
        .catch((error: unknown) => ({ online: false, error: error instanceof Error ? error.message : String(error) }));

      const [healthOk, ws, pr, msg, ps, pp, st, rt, ec, vn, accList, ov] = await Promise.all([
        healthP,
        safe(clientApi.workspace(), null),
        safe(
          clientApi.proposalsList().then((r) => ({
            proposals: (r?.proposals ?? []) as unknown as Proposal[],
          })),
          { proposals: [] },
        ),
        safe(
          clientApi.messagesList(30).then((r) => ({
            messages: (r?.messages ?? []) as unknown as Message[],
          })),
          { messages: [] },
        ),
        safe(clientApi.portfolioSummary(), null),
        safe(clientApi.portfolioPnl(), null),
        safe(clientApi.strategyList(), { strategies: [] }),
        safe(clientApi.recentTrades(20), { trades: [] }),
        safe(clientApi.portfolioEquityCurve(120), { points: [], equity_usd: 0 }),
        safe(clientApi.marketVenues(), { venues: [] }),
        safe(clientApi.accountsList(), { accounts: [] as AccountSummary[], ts: 0 }),
        safe<OperatorOverviewEnvelope | null>(clientApi.operatorOverview(), null),
      ]);

      setApiOnline(healthOk.online);
      setError(healthOk.error);
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
      setCoreLoaded(true);
    } finally {
      coreInFlight.current = false;
    }
  }, []);

  const loadCandles = useCallback(async () => {
    const sequence = ++candleSequence.current;
    setCandleLoading(true);
    setCandleError(null);
    try {
      const body = await clientApi.marketCandles({
        venue: settings.kline.venue,
        market: settings.kline.symbol,
        interval: settings.kline.interval,
        count: settings.kline.count,
      });
      if (sequence !== candleSequence.current) return;
      setCandles(body.candles || []);
      if (body.error) setCandleError(body.error);
    } catch (e) {
      if (sequence !== candleSequence.current) return;
      setCandleError(e instanceof Error ? e.message : String(e));
      setCandles([]);
    } finally {
      if (sequence === candleSequence.current) setCandleLoading(false);
    }
  }, [settings.kline.venue, settings.kline.symbol, settings.kline.interval, settings.kline.count]);

  useEffect(() => { loadCore(); }, [loadCore]);
  useEffect(() => {
    void loadCandles();
    return () => { candleSequence.current += 1; };
  }, [loadCandles]);

  // Keep the draft in sync when the committed symbol changes elsewhere
  // (settings hydration, another surface patching kline settings).
  useEffect(() => {
    setSymbolDraft(settings.kline.symbol);
  }, [settings.kline.symbol]);

  function commitSymbol() {
    const next = symbolDraft.trim().toUpperCase();
    if (next && next !== settings.kline.symbol) {
      patchSettings({ kline: { ...settings.kline, symbol: next } });
    } else {
      setSymbolDraft(settings.kline.symbol);
    }
  }

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

  // Range delta for the equity curve corner badge — the curve overlays the
  // first→last change instead of repeating the headline equity number that
  // already sits in the KPI row above.
  const curveDelta = useMemo(() => {
    if (equitySeries.length < 2) return null;
    const first = equitySeries[0];
    const last = equitySeries[equitySeries.length - 1];
    if (!Number.isFinite(first) || !Number.isFinite(last) || first === 0) return null;
    return { pct: ((last - first) / Math.abs(first)) * 100, up: last >= first };
  }, [equitySeries]);

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
  for (const [idx, p] of proposals.slice(0, 4).entries()) {
    derivedAttention.push({
      // Stable key: id/kind when present, else the loop index — never
      // Math.random(), which would remount the row on every render.
      id: `proposal:${p.id || p.kind || idx}`,
      type: "proposal",
      severity: "warn",
      title: t("proposalPending", { label: p.summary || p.kind || p.id || "–" }),
      summary: p.status || t("open"),
      href: "/inbox",
      requires_action: true,
    });
  }
  for (const [idx, m] of messages.slice(0, 4).entries()) {
    if (!m.text) continue;
    derivedAttention.push({
      id: `msg:${m.id || m.ts || idx}`,
      type: "message",
      severity: m.severity === "error" || m.severity === "warn" ? "warn" : "info",
      title: m.text.slice(0, 80),
      summary: m.channel ? t("channelSub", { channel: m.channel }) : "",
      href: "/inbox",
      requires_action: false,
    });
  }
  // Merge overview + derived attention, rank by severity (danger → warn →
  // info) BEFORE trimming to 6: without the rank, a chatty overview can
  // slice away the danger-level kill-switch reminder entirely. Sort is
  // stable, so original order is preserved within a severity tier.
  const SEVERITY_RANK: Record<AttentionItem["severity"], number> = {
    danger: 0,
    warn: 1,
    info: 2,
  };
  const attentionList: AttentionItem[] = [
    ...attentionFromOverview,
    ...derivedAttention,
  ]
    .sort(
      (a, b) =>
        (SEVERITY_RANK[a.severity] ?? 2) - (SEVERITY_RANK[b.severity] ?? 2),
    )
    .slice(0, 6);

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
      {error && <ErrorBanner error={error} onRetry={() => { invalidateReadCache(); void loadCore(); }} />}

      <PageBody>
        {/* Section 1 — Overview / greeting / KPIs / quick actions.
            No outer Card; sits directly at the top of the page.
            Marker is the heading typography itself (no decorative rail). */}
        <section className="min-w-0">
          <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
            <h1 className="text-[19px] leading-[1.25] font-medium tracking-tight text-[color:var(--text-base)]">
              {t(greetingKey)}, {operatorName}.
            </h1>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-[color:var(--text-muted)]">
              <WorkspaceCustomizeButton context="home" compact />
              <StatusDot
                tone={apiOnline ? "ok" : "danger"}
                label={apiOnline ? t("statusOnline") : t("statusOffline")}
              />
              <span>·</span>
              {/* Mode / kill-switch state is actionable, so it links to the
                  surface where the operator can act on it: /portfolio for
                  the trading mode, /incidents for the kill switch. */}
              <Link
                href={killed ? "/incidents" : "/portfolio"}
                className={`hover:underline ${
                  killed
                    ? "text-danger"
                    : mode === "live"
                    ? // live matches the console-wide ModePill semantics
                      // (live = danger red), not warn amber.
                      "text-danger"
                    : "text-[color:var(--text-muted)]"
                }`}
              >
                {killed
                  ? t("killEngaged")
                  : mode === "live"
                  ? t("modeLive")
                  : mode === "paper"
                  ? t("modePaper")
                  : t("modeUnknown")}
              </Link>
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

          {/* 3 inline KPIs — skeleton until the first loadCore round lands,
              so "$0.00 / 0 / 0" never masquerades as real data. */}
          <div className="mt-5 grid grid-cols-1 sm:grid-cols-3 gap-x-8 gap-y-4">
            <Kpi
              inline
              label={t("totalEquity")}
              value={coreLoaded ? fmtMoney(totalEquity) : <Skel className="h-6 w-32" />}
              delta={
                summary
                  ? t("accountCount", { count: summary.accounts.length })
                  : "–"
              }
            />
            <Kpi
              inline
              label={t("realizedPnl")}
              value={coreLoaded ? fmtSigned(totalRealizedPnl) : <Skel className="h-6 w-24" />}
              tone={coreLoaded ? (totalRealizedPnl >= 0 ? "ok" : "danger") : "neutral"}
              delta={t("strategiesCount", { count: strategies.length })}
            />
            <Kpi
              inline
              label={t("activeStrategies")}
              value={
                coreLoaded
                  ? `${activeStrategiesCount} / ${strategies.length || 0}`
                  : <Skel className="h-6 w-16" />
              }
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

        {/* Section 1.5 — Attention. Kill-switch, pending proposals and
            alerts sit directly under the KPI row — an operator console leads
            with "what needs me", and every row links to where the item is
            handled (/incidents, /inbox, /portfolio, …). Only rendered when
            there are signals. */}
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
                          : "neutral"
                      }
                    />
                  </span>
                  <div className="flex-1 min-w-0">
                    {item.href ? (
                      <Link
                        href={item.href}
                        className="block text-[13px] text-[color:var(--text-base)] truncate hover:text-brand-200 hover:underline"
                        title={item.title}
                      >
                        {item.title}
                      </Link>
                    ) : (
                      <div className="text-[13px] text-[color:var(--text-base)] truncate">
                        {item.title}
                      </div>
                    )}
                    {item.summary ? (
                      <div className="text-[12px] text-[color:var(--text-muted)] truncate">
                        {item.summary}
                      </div>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          </Card>
        ) : null}

        {/* Setup checklist — auto-hides when everything is ok. */}
        <SetupReadinessCard collapsed />

        {/* Agent-authored, read-only widgets from ui/workspace.yml. */}
        <WorkspaceUiHome />

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
          {!coreLoaded ? (
            <div className="space-y-2.5 py-2" aria-hidden>
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <div key={i} className="skeleton h-5 w-full" />
              ))}
            </div>
          ) : strategies.length === 0 ? (
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
                    <th className="text-right">{t("pnlCol")}</th>
                    <th className="text-right">{t("winRateCol")}</th>
                    <th className="text-right">{t("openPositions")}</th>
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
                            className="block max-w-[220px] truncate text-[13px] text-[color:var(--text-base)] hover:text-brand-200"
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
                        <td className={`text-right font-mono tabular-nums ${
                          pnl > 0 ? "text-ok" : pnl < 0 ? "text-danger" : "text-ink-300"
                        }`}>
                          {fmtSigned(pnl)}
                        </td>
                        <td className="text-right font-mono tabular-nums text-ink-200">
                          {Number.isFinite(s.win_rate_pct) ? `${Math.round(s.win_rate_pct)}%` : "–"}
                        </td>
                        <td className="text-right font-mono tabular-nums text-ink-200">{s.open_positions_count ?? 0}</td>
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
            {!coreLoaded ? (
              <div className="skeleton h-12 w-full" aria-hidden />
            ) : focusedAccount ? (
              <FocusedAccountStrip
                account={focusedAccount}
                nav={focusedNav}
                free={focusedFree}
                reserved={focusedReserved}
                currency={focusedCurrency}
              />
            ) : (
              <NoFocusedAccountStrip
                accounts={accounts}
                onPick={setCurrentAccountId}
              />
            )}

            {/* Equity curve */}
            <Section
              title={t("equityCurve")}
              description={t("equityPoints", { count: equity.length })}
              divider={false}
            >
              <div className="h-[120px] relative">
                <div className="absolute inset-0">
                  {coreLoaded ? (
                    <Sparkline
                      values={equitySeries}
                      width={800}
                      height={120}
                      tone={curveDelta && !curveDelta.up ? "danger" : "accent"}
                      fill
                    />
                  ) : (
                    <div className="skeleton h-full w-full" aria-hidden />
                  )}
                </div>
                {coreLoaded && curveDelta ? (
                  <div className="absolute top-1 right-1 text-right">
                    <div
                      className={`font-mono text-[13px] tabular-nums ${
                        curveDelta.up ? "text-ok" : "text-danger"
                      }`}
                    >
                      {curveDelta.up ? "+" : "-"}
                      {Math.abs(curveDelta.pct).toFixed(2)}%
                    </div>
                  </div>
                ) : null}
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
              {!coreLoaded ? (
                <div className="space-y-2.5 py-2" aria-hidden>
                  {[0, 1, 2].map((i) => (
                    <div key={i} className="skeleton h-5 w-full" />
                  ))}
                </div>
              ) : openPositionList.length === 0 ? (
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
                        <th className="text-right">{t("sizeCol")}</th>
                        <th className="text-right">{t("entryCol")}</th>
                        <th className="text-right">{t("pnlCol")}</th>
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
                            <td className="text-right font-mono tabular-nums text-ink-200">{size.toFixed(4)}</td>
                            <td className="text-right font-mono tabular-nums text-ink-200">{entry ? entry.toFixed(2) : "–"}</td>
                            <td className={`text-right font-mono tabular-nums ${
                              openPnl > 0 ? "text-ok" : openPnl < 0 ? "text-danger" : "text-ink-300"
                            }`}>
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
              {!coreLoaded ? (
                <div className="space-y-2.5 py-2" aria-hidden>
                  {[0, 1, 2].map((i) => (
                    <div key={i} className="skeleton h-5 w-full" />
                  ))}
                </div>
              ) : recentTrades.length === 0 ? (
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
                        <th className="text-right">{t("priceCol")}</th>
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
                            <td className="text-right font-mono tabular-nums text-ink-200">
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

        {/* Section 3.5 — Market (K-line). Reference info, not an operating
            surface, so it collapses behind <Advanced> by default; the range
            delta stays visible in the collapsed header. */}
        <Advanced
          title={t("sectionMarket")}
          count={
            firstClose
              ? `${candleDeltaPct >= 0 ? "+" : ""}${candleDeltaPct.toFixed(2)}%`
              : undefined
          }
          storageKey="dashboard-market-open"
        >
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
              value={symbolDraft}
              onChange={(e) => setSymbolDraft(e.target.value.toUpperCase())}
              onBlur={commitSymbol}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.nativeEvent.isComposing && e.keyCode !== 229) {
                  e.preventDefault();
                  commitSymbol();
                }
              }}
              className="bg-ink-900 border border-[color:var(--line)] rounded-md px-2 py-1 text-[12px] text-ink-100 focus:outline-none focus:border-brand-500/60 w-28 font-mono"
              placeholder={t("symbolPlaceholder")}
            />
            <div className="flex gap-1">
              {INTERVAL_OPTIONS.map((opt) => (
                <button
                  key={opt.key}
                  onClick={() => patchSettings({ kline: { ...settings.kline, interval: opt.key as typeof settings.kline.interval } })}
                  className={`px-2 py-1 text-[12px] rounded-md font-medium transition-colors ${
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
              className="px-2 py-1 text-[12px] rounded-md text-brand-200 hover:bg-brand-500/10 border border-brand-500/25 transition-colors"
              title={t("refresh")}
            >
              ↻
            </button>
          </div>
          <div className="mt-3">
            <CandleChart
              candles={candles}
              width={960}
              height={220}
              mode={settings.chartType}
              showVolume={settings.showVolume}
              loading={candleLoading}
              error={candleError || undefined}
            />
          </div>
        </Advanced>

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
        <div className="mt-0.5 flex items-center gap-1.5 text-[12px] text-[color:var(--text-muted)]">
          <ModePill mode={profile.mode} />
          <span>
            {profile.venue} · {profile.kind}
          </span>
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
      ? "text-warn"
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

/**
 * Inline focus picker — shown when no account is focused yet. The old
 * version was a dead-end hint pointing at a "dropdown in the top header"
 * that the Codex shell no longer renders; picking directly here removes
 * that hunt entirely.
 */
function NoFocusedAccountStrip({
  accounts,
  onPick,
}: {
  accounts: AccountSummary[];
  onPick: (id: string) => void;
}) {
  const t = useTranslations("home");
  if (accounts.length === 0) {
    return (
      <div className="rounded-lg border border-warn/20 bg-warn/5 px-3 py-2.5 text-[13px] text-warn">
        {t("noAccountsPrefix")}{" "}
        <Link href="/accounts" className="underline hover:text-warn/80">
          {t("addAccountLink")}
        </Link>
      </div>
    );
  }
  return (
    <div
      className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2.5 text-[13px]"
      style={{ borderColor: "var(--line)", background: "var(--card)" }}
    >
      <span className="text-[color:var(--text-muted)]">{t("pickFocusInline")}</span>
      {accounts.slice(0, 8).map((acc) => (
        <button
          key={acc.profile.id}
          type="button"
          onClick={() => onPick(acc.profile.id)}
          className="pill hover:border-brand-500/40 hover:text-[color:var(--text-base)]"
          title={`${acc.profile.venue} · ${acc.profile.kind} · ${acc.profile.mode}`}
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              acc.profile.mode === "live" ? "bg-danger" : "bg-brand-400"
            }`}
          />
          <span className="font-mono">{acc.profile.id}</span>
        </button>
      ))}
      {accounts.length > 8 ? (
        <Link href="/accounts" className="text-[12px] text-brand-300 hover:text-brand-200">
          {t("viewAll")}
        </Link>
      ) : null}
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

/**
 * Shimmer placeholder built on the shared `.skeleton` class. Used while the
 * first loadCore round is in flight so KPIs and tables never present
 * "$0.00 / no strategies" as if it were real (empty) data.
 */
function Skel({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={`skeleton inline-block rounded-md ${className ?? "h-4 w-24 align-middle"}`}
    />
  );
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

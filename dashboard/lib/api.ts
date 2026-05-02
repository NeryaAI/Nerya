// Thin server-side HTTP client for the Nerya local API.
// All calls run inside Next route handlers / Server Components so we never
// have to worry about CORS or exposing the backend to the browser.

const BASE = process.env.NERYA_API || "http://127.0.0.1:18317";

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, msg: string) {
    super(msg);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(
  path: string,
  init?: { method?: string; body?: unknown; search?: Record<string, string | number> }
): Promise<T> {
  let url = BASE + path;
  if (init?.search) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(init.search)) {
      if (v !== undefined && v !== null) qs.set(k, String(v));
    }
    const s = qs.toString();
    if (s) url += (url.includes("?") ? "&" : "?") + s;
  }

  const res = await fetch(url, {
    method: init?.method || "GET",
    headers: { "content-type": "application/json" },
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
    cache: "no-store",
  });

  let body: unknown = null;
  const text = await res.text();
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }

  if (!res.ok) {
    const msg = typeof body === "object" && body && "error" in body
      ? String((body as { error: unknown }).error)
      : `HTTP ${res.status}`;
    throw new ApiError(res.status, body, msg);
  }
  return body as T;
}

export const api = {
  // Workspace + health
  workspace: () => request<{ root: string; live_trading_enabled: boolean; kill_switch: boolean }>("/workspace"),
  health: () => request<{ status: string }>("/health"),

  // Skills
  skills: () => request<{ skills: Array<{ id: string; version?: string; permissions?: string[]; actions?: string[] }> }>("/skills"),
  skillCall: (skill_id: string, action: string, payload: unknown, caller = "dashboard") =>
    request<unknown>("/skills/call", { method: "POST", body: { skill_id, action, payload, caller } }),

  // Triggers
  triggerRoutes: async (): Promise<{ routes: unknown[] }> => {
    // Endpoint returns a bare list; normalise to {routes: [...]}.
    const body = await request<unknown>("/triggers/routes");
    if (Array.isArray(body)) return { routes: body };
    if (body && typeof body === "object" && "routes" in body) {
      return body as { routes: unknown[] };
    }
    return { routes: [] };
  },
  triggerEmit: (body: { source: string; kind: string; payload?: unknown; target?: string; strategy_id?: string; idempotency_key?: string; dry_run?: boolean }) =>
    request<unknown>("/triggers/emit", { method: "POST", body }),
  triggerDryRun: (body: { source: string; kind: string; payload?: unknown; target?: string; strategy_id?: string }) =>
    request<unknown>("/triggers/dry_run", { method: "POST", body }),

  // Trading
  tradingHistory: (strategy_id: string, limit = 20) =>
    request<unknown>("/trading/history", { method: "POST", body: { strategy_id, limit } }),

  // Strategy history
  strategyHistory: (strategy_id: string, limit = 50) =>
    request<{ events: unknown[] }>("/strategy/history", { method: "POST", body: { strategy_id, limit } }),
  strategyExplain: (strategy_id: string, order_id: string) =>
    request<unknown>("/strategy/explain", { method: "POST", body: { strategy_id, order_id } }),
  strategyReview: (strategy_id: string, limit = 50) =>
    request<unknown>("/strategy/review", { method: "POST", body: { strategy_id, limit } }),

  // Messages
  messagesList: (limit = 100) =>
    request<{ messages: unknown[] }>("/messages/list", { method: "POST", body: { limit } }),

  // Scripts
  scriptAnalyze: (source: string) =>
    request<unknown>("/scripts/analyze", { method: "POST", body: { source } }),

  // Evolution
  proposalsList: () =>
    request<{ proposals: unknown[] }>("/evolution/proposals", { method: "POST", body: {} }),

  // Security
  secretsList: () =>
    request<{ secrets: unknown[] }>("/security/secrets/list", { method: "POST", body: {} }),

  // Agent — // The response shape is the dashboard-shaped TurnPayload (see
  // ``dashboard/lib/chat.ts``); we pass `unknown` here so callers
  // explicitly opt into the typed cast where they consume it.
  agentRun: (trigger: { source: string; kind: string; payload?: unknown; target?: string; strategy_id?: string }) =>
    request<unknown>("/agent/run_turn", { method: "POST", body: trigger }),
  agentTools: () =>
    request<{
      ok: boolean;
      count: number;
      harness?: string;
      tools: Array<{
        name: string;
        description: string;
        namespace: string;
        risk: string;
        permission_scope: string;
        read_only: boolean;
        is_concurrency_safe: boolean;
        requires_fresh_read: boolean;
        mutates_paths: boolean;
        result_kind: string;
        auto_approve: boolean;
        tags: string[];
        input_schema: Record<string, unknown>;
      }>;
    }>("/agent/tools", { method: "GET" }),
  agentInterrupt: (sessionId: string, reason = "operator_interrupt") =>
    request<{ ok: boolean; cancelled: boolean; session_id: string }>(
      "/agent/interrupt",
      { method: "POST", body: { session_id: sessionId, reason } },
    ),

  // LLM
  llmClassify: (text: string, task = "classify") =>
    request<unknown>("/llm/classify", { method: "POST", body: { text, task } }),

  // Portfolio
  portfolioSummary: () =>
    request<PortfolioSummary>("/portfolio/summary", { method: "POST", body: {} }),
  portfolioPositions: () =>
    request<{ positions: PortfolioPosition[] }>("/portfolio/positions", { method: "POST", body: {} }),
  portfolioPnl: () =>
    request<{ realized_usd: number; equity_usd: number }>("/portfolio/pnl", { method: "POST", body: {} }),
  portfolioEquityCurve: (limit = 120) =>
    request<{ points: EquityPoint[]; equity_usd: number }>(
      "/portfolio/equity_curve", { method: "POST", body: { limit } },
    ),

  // Strategies (dashboard-friendly aggregate)
  strategyList: () =>
    request<{ strategies: StrategyCard[] }>("/strategy/list", { method: "POST", body: {} }),

  // Recent trades across all strategies
  tradingRecentTrades: (limit = 25) =>
    request<{ trades: RecentTrade[] }>("/trading/recent_trades", { method: "POST", body: { limit } }),

  // Market data
  marketVenues: () =>
    request<{ venues: { name: string; label: string; public: boolean }[] }>("/market/venues"),
  marketCandles: (body: { venue: string; market: string; interval: string; count?: number }) =>
    request<{ venue: string; market: string; interval: string; count: number; candles: Candle[]; error?: string }>(
      "/market/candles", { method: "POST", body },
    ),
  marketTicker: (body: { venue: string; market: string }) =>
    request<{ venue: string; market: string; bid?: number; ask?: number; mid?: number; last?: number; error?: string }>(
      "/market/ticker", { method: "POST", body },
    ),
};

export type Api = typeof api;

/* ---------------------------------------------------------------- types */

export type Candle = {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type EquityPoint = { ts: string; equity_usd: number };

export type PortfolioPosition = {
  account_id: string;
  market?: string;
  size?: number;
  avg_entry_price?: number;
  unrealized_pnl_usd?: number;
  realized_pnl_usd?: number;
  side?: string;
};

export type PortfolioSummary = {
  accounts: {
    id: string;
    mode: string;
    live_trading_enabled: boolean;
    cash_usd: number;
    equity_usd: number;
    positions: Record<string, PortfolioPosition>;
    trade_count: number;
    fees_paid_usd: number;
  }[];
  totals: { cash_usd: number; equity_usd: number };
};

export type StrategyCard = {
  id: string;
  title: string;
  status: string;
  account_id: string;
  markets: string[];
  paper_trading_enabled: boolean;
  live_trading_enabled: boolean;
  trigger_kinds: string[];
  fills_count: number;
  intents_count: number;
  realized_pnl_usd: number;
  fees_usd: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
};

export type RecentTrade = {
  strategy_id: string;
  ts?: string;
  market?: string;
  side?: string;
  type?: string;
  size?: number;
  price?: number;
  fee_usd?: number;
  status?: string;
  order_id?: string;
};

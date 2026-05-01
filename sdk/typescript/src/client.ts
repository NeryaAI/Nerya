import {
  TriggerEvent,
  RouterResult,
  TradeIntent,
  TradeResult,
  TriggerRouteView,
} from "./schemas.js";
import {
  AgentRunTurnRequest,
  AgentToolRegistryView,
  AgentTurnResult,
} from "./agent.js";

export interface NeryaClientOptions {
  /**
   * Base URL of the local Nerya HTTP API. Defaults to the daemon started
   * by `nerya serve` (port 8787).
   */
  baseUrl?: string;
  /**
   * Per-request timeout in milliseconds.
   */
  timeoutMs?: number;
  /**
   * If provided, every request will set this header as
   * `X-Nerya-Caller`. This is an operator-hint only; the daemon still
   * enforces skill permissions.
   */
  caller?: string;
  /**
   * Optional fetch implementation, primarily for test injection.
   */
  fetchImpl?: typeof fetch;
}

export class NeryaClient {
  readonly baseUrl: string;
  readonly timeoutMs: number;
  readonly caller?: string;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: NeryaClientOptions = {}) {
    this.baseUrl = (opts.baseUrl ?? "http://127.0.0.1:8787").replace(/\/+$/, "");
    this.timeoutMs = opts.timeoutMs ?? 10_000;
    this.caller = opts.caller;
    this.fetchImpl = opts.fetchImpl ?? globalThis.fetch;
    if (!this.fetchImpl) {
      throw new Error(
        "No fetch implementation available. Pass fetchImpl explicitly on Node <18.",
      );
    }
  }

  readonly triggers = {
    emit: (event: TriggerEvent) =>
      this.post<RouterResult>("/triggers/emit", event),
    dryRun: (event: TriggerEvent) =>
      this.post<RouterResult>("/triggers/dry_run", { ...event, dry_run: true }),
    listRoutes: () => this.get<TriggerRouteView[]>("/triggers/routes"),
    emitAndWait: async (
      event: TriggerEvent,
      opts: { timeoutMs?: number; pollMs?: number } = {},
    ) => {
      const res = await this.post<RouterResult>("/triggers/emit", event);
      if (!res.event_id) return res;
      const deadline = Date.now() + (opts.timeoutMs ?? 5_000);
      const poll = opts.pollMs ?? 150;
      while (Date.now() < deadline) {
        const out = await this.get<unknown>(
          `/triggers/result?event_id=${encodeURIComponent(res.event_id)}`,
        ).catch(() => null);
        if (out) return out;
        await sleep(poll);
      }
      return res;
    },
  };

  readonly trading = {
    submitIntent: (intent: TradeIntent) =>
      this.post<TradeResult>("/trading/submit", intent),
    cancelOrder: (strategyId: string, orderId: string) =>
      this.post<TradeResult>("/trading/cancel", {
        strategy_id: strategyId,
        order_id: orderId,
      }),
    strategyHistory: (strategyId: string, limit = 20) =>
      this.post("/trading/history", { strategy_id: strategyId, limit }),
  };

  readonly llm = {
    classify: (payload: {
      prompt: string;
      labels?: string[];
      caller?: string;
    }) => this.post("/llm/classify", payload),
    extractJson: (payload: {
      prompt: string;
      schema?: Record<string, unknown>;
      caller?: string;
    }) => this.post("/llm/extract_json", payload),
  };

  readonly agent = {
    /** Phase 15 — run a single agent turn through the workspace-native
     *  loop. Equivalent of `POST /agent/run_turn`. Returns the same
     *  block-aware shape the dashboard consumes, including
     *  `blocks` (provider-native envelopes) and `tool_trace`
     *  (executed tool calls). */
    runTurn: (req: AgentRunTurnRequest) => {
      const body = req.trigger
        ? {
            trigger: req.trigger,
            strategy_id: req.strategy_id,
            session_id: req.session_id,
            attached_skills: req.attached_skills,
          }
        : {
            trigger: {
              source: "sdk",
              kind: "agent.user_message",
              payload: { text: req.text ?? "" },
            },
            strategy_id: req.strategy_id,
            session_id: req.session_id,
            attached_skills: req.attached_skills,
          };
      return this.post<AgentTurnResult>("/agent/run_turn", body);
    },
    /** Enumerate every native + bridged tool the workspace-native
     *  loop is allowed to call. Equivalent of `GET /agent/tools`. */
    tools: () => this.get<AgentToolRegistryView>("/agent/tools"),
    /** Cooperatively cancel an in-flight turn. */
    interrupt: (sessionId: string, reason = "operator_interrupt") =>
      this.post<{ ok: boolean; cancelled: boolean; session_id: string }>(
        "/agent/interrupt",
        { session_id: sessionId, reason },
      ),
  };

  readonly strategy = {
    history: (strategyId: string, limit = 20) =>
      this.post("/strategy/history", { strategy_id: strategyId, limit }),
    review: (strategyId: string, sessionId: string, stage = "immediate") =>
      this.post("/strategy/review", {
        strategy_id: strategyId,
        session_id: sessionId,
        stage,
      }),
    explainTrade: (strategyId: string, orderId: string) =>
      this.post("/strategy/explain", {
        strategy_id: strategyId,
        order_id: orderId,
      }),
    attribution: (strategyId: string, sessionId: string) =>
      this.post("/strategy/attribution", {
        strategy_id: strategyId,
        session_id: sessionId,
      }),
    divergence: (strategyId: string, windowSessions = 25) =>
      this.post("/strategy/divergence", {
        strategy_id: strategyId,
        window_sessions: windowSessions,
      }),
    versions: (strategyId: string) =>
      this.post("/strategy/versions", { strategy_id: strategyId }),
    compareVersions: (strategyId: string, left: string, right: string) =>
      this.post("/strategy/versions/compare", {
        strategy_id: strategyId,
        left,
        right,
      }),
  };

  private async get<T = unknown>(path: string): Promise<T> {
    const res = await this.fetchImpl(this.url(path), {
      method: "GET",
      headers: this.headers(),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    return this.parse<T>(res, path);
  }

  private async post<T = unknown>(
    path: string,
    body: unknown,
  ): Promise<T> {
    const res = await this.fetchImpl(this.url(path), {
      method: "POST",
      headers: { ...this.headers(), "content-type": "application/json" },
      body: JSON.stringify(body ?? {}),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    return this.parse<T>(res, path);
  }

  private url(path: string): string {
    return `${this.baseUrl}${path.startsWith("/") ? "" : "/"}${path}`;
  }

  private headers(): Record<string, string> {
    const h: Record<string, string> = {};
    if (this.caller) h["X-Nerya-Caller"] = this.caller;
    return h;
  }

  private async parse<T>(res: Response, path: string): Promise<T> {
    if (!res.ok) {
      let body = "";
      try {
        body = await res.text();
      } catch {
        /* ignore */
      }
      throw new Error(
        `Nerya ${path} failed: ${res.status} ${res.statusText} ${body}`.trim(),
      );
    }
    if (res.headers.get("content-type")?.includes("application/json")) {
      return (await res.json()) as T;
    }
    return (await res.text()) as unknown as T;
  }
}

export function connect(opts: NeryaClientOptions = {}): NeryaClient {
  return new NeryaClient(opts);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

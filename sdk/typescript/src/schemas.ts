export type TriggerSource =
  | "script"
  | "schedule"
  | "price"
  | "user_command"
  | "webhook";

export interface TriggerEvent {
  source: TriggerSource;
  kind: string;
  payload: Record<string, unknown>;
  target?: string;
  strategy_id?: string | null;
  idempotency_key?: string | null;
  dry_run?: boolean;
}

export interface RouterResult {
  event_id: string;
  status:
    | "routed"
    | "dead_letter"
    | "dedup"
    | "cooldown"
    | "rate_limited"
    | "dry_run";
  target: string | null;
  route_id: string | null;
  strategy_id: string | null;
  reason: string | null;
}

export interface TradeIntent {
  strategy_id: string;
  account_id: string;
  market: string;
  side: "buy" | "sell";
  size: number;
  size_unit: "base" | "quote" | "usd";
  order_type: "market" | "limit" | "stop" | "stop_limit";
  limit_price?: number | null;
  confidence: number;
  reasoning: string;
  source?: string;
  trigger_event_id?: string | null;
}

export interface TradeResult {
  status: "filled" | "rejected" | "pending_approval";
  order_id: string | null;
  risk_decision: Record<string, unknown>;
  order?: Record<string, unknown>;
}

export interface TriggerRouteView {
  id: string;
  match: Record<string, unknown>;
  target: string;
  strategy_id: string | null;
  cooldown_seconds: number;
  max_per_minute: number;
  max_payload_bytes: number;
}

// TypeScript SDK shapes for the workspace-native agent.
//
// Mirrors `nerya.api.routes_agent.run_turn` and `nerya.api.routes_agent
// .tool_registry`. Kept intentionally permissive — providers / kernel
// versions evolve and we don't want the SDK to throw on unknown fields.

export interface AgentRunTurnRequest {
  /** Convenience field — when provided, the SDK synthesises an
   *  `agent.user_message` trigger. Mutually exclusive with `trigger`. */
  text?: string;
  /** Fully-formed trigger event accepted by the kernel. */
  trigger?: Record<string, unknown>;
  strategy_id?: string;
  session_id?: string;
  attached_skills?: string[];
}

export interface AgentActionRecord {
  action: string;
  skill_id?: string;
  payload?: Record<string, unknown>;
  result?: unknown;
  error?: string;
  error_kind?: string;
  call_id?: string;
  ok?: boolean;
}

export interface AgentToolTraceEntry {
  call_id?: string;
  skill_id?: string;
  action?: string;
  ok?: boolean;
  error?: string | null;
  error_kind?: string | null;
  elapsed_ms?: number | null;
  payload?: Record<string, unknown>;
  result?: unknown;
}

export type AgentBlockKind =
  | "text"
  | "thinking"
  | "tool_use"
  | "tool_result"
  | string;

export interface AgentBlock {
  kind?: AgentBlockKind;
  index?: number;
  iteration?: number;
  text?: string;
  call_id?: string;
  action?: string;
  skill_id?: string;
  payload?: Record<string, unknown>;
  result?: unknown;
  ok?: boolean;
  error?: string | null;
  error_kind?: string | null;
  elapsed_ms?: number | null;
  [key: string]: unknown;
}

export interface AgentBlockEnvelope {
  block?: AgentBlock;
  kind?: AgentBlockKind;
  ts?: string;
  [key: string]: unknown;
}

export interface AgentTurnResult {
  trigger_event_id?: string | null;
  decision?: Record<string, unknown>;
  actions: AgentActionRecord[];
  subagents?: Record<string, unknown>;
  plan?: { kind?: string; tier?: string };
  tool_trace: AgentToolTraceEntry[];
  budget?: Record<string, unknown>;
  reply_text?: string;
  events?: Array<Record<string, unknown>>;
  turn_id?: string;
  stopped_reason?: string | null;
  steps: Array<Record<string, unknown>>;
  blocks: AgentBlockEnvelope[];
  harness?: "legacy" | "native" | string;
}

export interface AgentToolDescriptor {
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
}

export interface AgentToolRegistryView {
  ok: boolean;
  count: number;
  tools: AgentToolDescriptor[];
  harness?: string;
}

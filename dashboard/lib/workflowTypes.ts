export type WorkflowKind = "strategy" | "source" | "script" | "agent" | "scheduler" | "account" | "risk" | "evidence" | "proposal" | "validation" | "approval" | "apply" | "observation";
export type WorkflowPosition = { x: number; y: number };
export type WorkflowNode = {
  id: string; kind: WorkflowKind; title: string; subtitle: string; resource: string;
  position: WorkflowPosition; config: unknown; content?: string; description?: string;
  binding: { file: string | null; path: Array<string | number> | null };
  editable: boolean; href?: string; status?: string;
  presentation?: { summary?: string };
  control?: { can_stop: boolean; paths: string[] };
  execution?: { mode: string; policy?: Record<string, unknown> };
};
export type WorkflowEdge = {
  id: string; source: string; target: string; relation: string;
  origin: "static" | "manifest" | "declared" | "annotation" | "runtime"; label: string;
};
export type WorkflowGraph = { id: string; nodes: WorkflowNode[]; edges: WorkflowEdge[]; enabled?: boolean };
export type WorkflowMetadata = {
  version: 1;
  nodes: Record<string, { title?: string; description?: string; position?: WorkflowPosition }>;
  edges: WorkflowEdge[];
};
export type WorkflowView = {
  ok: boolean; error?: string; strategy_id: string; revision: string;
  strategy: WorkflowGraph; evolution: WorkflowGraph; manifest: Record<string, unknown>;
  metadata: WorkflowMetadata; legacy: boolean; can_edit: boolean;
  agent_defaults?: { max_iterations?: number; max_tool_calls?: number; max_wall_seconds?: number; max_parallel?: number; tier?: string };
  source: { proposal_id: string | null; state: string; omitted_files: string[] };
};
export type WorkflowSummary = {
  key: string; strategy_id: string; proposal_id: string | null; title: string;
  description?: string; mode: string; status?: string; state: string; execution_mode?: string;
  counts: Partial<Record<WorkflowKind, number>>; markets?: string[]; error?: string; legacy?: boolean;
};
export type WorkflowChange = { node_id: string; config?: unknown; content?: string };
export type WorkflowAddition = { kind: "script" | "agent" | "account" | "source"; name: string; content?: string; config?: unknown };
export type WorkflowProposalRequest = {
  strategy_id: string; proposal_id?: string | null; base_revision: string;
  changes?: WorkflowChange[]; additions?: WorkflowAddition[]; metadata?: WorkflowMetadata;
};
export type WorkflowSaveResult = {
  ok: boolean; error?: string; strategy_id: string; proposal_id: string; state: string;
  workflow: WorkflowView; validation?: { ok: boolean; blockers: Array<{ code: string; message: string; where?: string }> };
};
export type WorkflowTemplate = "multi_script" | "script_agent" | "scheduler_agent";

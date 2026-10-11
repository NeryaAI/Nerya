import { copy as i18nCopy } from "./i18n";
export type ExternalSource = "mcp" | "tunnel";

export type ExternalCallNode = {
  call_id: string;
  parent_call_id?: string;
  tool: string;
  arguments?: unknown;
  result?: unknown;
  status: string;
  started_at?: string;
  elapsed_ms?: number | null;
  /** Server-resolved chart/artifact descriptors; execution data stays in result. */
  presentation_blocks?: unknown[];
};

export type ExternalCallTrace = ExternalCallNode & {
  turn_id?: string;
  turn_title?: string;
  sequence?: number;
  activity?: Partial<Record<"intent" | "hypothesis" | "evidence" | "conclusion" | "next" | "status", string>>;
  purpose?: string;
  description?: string;
  source: ExternalSource;
  remote_session_id: string;
  request_id?: unknown;
  client_name?: string;
  nodes?: ExternalCallNode[];
};

export function isExternalSource(source?: string): source is ExternalSource {
  return source === "mcp" || source === "tunnel";
}

/** Numeric DB timestamps are seconds; cached browser timestamps are milliseconds. */
export function conversationTimestamp(value: string | number | null | undefined): number | null {
  if (value == null || value === "") return null;
  const numeric = typeof value === "number" ? value : Number(value);
  if (Number.isFinite(numeric)) return numeric < 100_000_000_000 ? numeric * 1000 : numeric;
  const parsed = Date.parse(String(value));
  return Number.isFinite(parsed) ? parsed : null;
}

export function callDepth(node: ExternalCallNode, nodes: ExternalCallNode[], root: string): number {
  let parent = node.parent_call_id;
  let depth = 0;
  const seen = new Set<string>([node.call_id]);
  while (parent && parent !== root && !seen.has(parent) && depth < 8) {
    seen.add(parent);
    const ancestor = nodes.find(item => item.call_id === parent);
    if (!ancestor) break;
    depth += 1;
    parent = ancestor.parent_call_id;
  }
  return depth;
}

export function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function publicProgress(trace: ExternalCallTrace) {
  return trace.tool === "nerya_progress" ? recordOf(trace.arguments) : {};
}

export function actionTitle(trace: ExternalCallTrace, zh: boolean): string {
  const progress = publicProgress(trace);
  const authored = progress.current || trace.activity?.next || trace.purpose;
  if (typeof authored === "string" && authored.trim()) return authored;
  const name = trace.tool.replace(/^nerya_(native_)?/, "");
  const labels: Record<string, string> = {
    read_file: "copy.lib_externalCalls.001", write_file: "copy.lib_externalCalls.002",
    edit_file: "copy.lib_externalCalls.003", list_dir: "copy.lib_externalCalls.004",
    run_shell: "copy.lib_externalCalls.005", run_python: "copy.lib_externalCalls.006",
    role_list: "copy.lib_externalCalls.007", role_get: "copy.lib_externalCalls.008",
    config_get: "copy.lib_externalCalls.009",
    strategy_generate: "copy.lib_externalCalls.010",
    strategy_validate: "copy.lib_externalCalls.011",
    strategy_backtest: "copy.lib_externalCalls.012",
    evolve_proposals: "copy.lib_externalCalls.013", proposals_show: "copy.lib_externalCalls.014",
    progress: "copy.lib_externalCalls.015",
  };
  const title = i18nCopy(zh, labels[name] ?? "") || trace.tool;
  const args = recordOf(trace.arguments);
  const target = ["path", "target", "strategy_id", "proposal_id", "query"].map(k => args[k]).find(v => typeof v === "string" && v);
  return target ? `${title} · ${String(target).slice(0, 180)}` : title;
}

export type ExternalTask = { id: string; title: string; calls: ExternalCallTrace[] };
export function groupExternalCalls(traces: ExternalCallTrace[]): ExternalTask[] {
  const groups = new Map<string, ExternalTask>();
  // Update by call ID, never by turn ID: a task contains MANY tool calls.
  const unique = new Map(traces.map(trace => [trace.call_id, trace]));
  for (const trace of unique.values()) {
    const id = trace.turn_id || "legacy-calls";
    let task = groups.get(id);
    if (!task) { task = { id, title: trace.turn_title || trace.activity?.intent || "", calls: [] }; groups.set(id, task); }
    task.calls.push(trace);
  }
  for (const task of groups.values()) task.calls.sort((a, b) =>
    (a.sequence || 0) - (b.sequence || 0) || (Date.parse(a.started_at || "") || 0) - (Date.parse(b.started_at || "") || 0));
  return [...groups.values()];
}

import { copy as i18nCopy } from "./i18n";
import type { ChatThread, TurnPayload } from "./chat";
import type { ConversationCommand } from "./conversationCommands";
import { recordOf } from "./externalCalls";

/** Legacy session lists keep titles in meta; normalize at the API boundary. */
export function taskEntryTitle(entry: {title?: unknown; meta?: unknown}): string {
  const title = typeof entry.title === "string" ? entry.title.trim() : "";
  const legacy = recordOf(entry.meta).title;
  return title || (typeof legacy === "string" ? legacy.trim() : "");
}

export type TaskStatus = {
  execution: string; waiting_for: "user" | "approval" | "configuration" | null;
  completion: "external_reported" | "turn_finished" | null; validation: string;
  needs_attention: boolean; external: boolean; turn_id?: string;
};
export type InteractionQuestion = { id: string; question: string; options: string[]; multiple: boolean };
export type Interaction = {
  interaction_id: string; session_id: string; turn_id: string; revision: number;
  kind: "question" | "plan"; state: "pending" | "deferred" | "answered";
  payload: { questions?: InteractionQuestion[]; title: string; message?: string; choices?: string[]; multiple?: boolean;
    steps?: string[]; deliverables?: string[]; constraints?: string[] };
};
export type SessionView = {
  ok: boolean; session_id: string; revision: string; observed_at: number;
  status: TaskStatus; pending_interactions: Interaction[];
  queue: { count: number; paused: boolean }; approvals: { id: string; kind?: string }[];
  approval_resolutions?: {id:string; state:string; kind?:string; turn_id?:string}[];
  agents: { id: string; name: string; state: string; title: string; attempt: number }[];
  result_refs: { kind: string; path: string; turn_id: string; message_id: string }[];
  available_actions: { send: boolean; guide: boolean; stop: boolean };
};
export type RuntimeInfo = { ok: boolean; protocol_version: number; build_id: string; started_at: number; workspace_id: string; capabilities: string[] };
export type Connection = "connecting" | "online" | "offline" | "incompatible";
export type ArtifactIndex = {
  created?: string[]; modified?: string[]; read?: string[];
  commands?: Record<string, unknown>[]; tests_run?: Record<string, unknown>[];
  errors?: Record<string, unknown>[]; recovered_errors?: Record<string, unknown>[];
  unverified_risks?: Record<string, unknown>[]; counters?: Record<string, number>;
};
export type VerifierOutcome = { hard_passed?: boolean; hard_status?: string; transition_label?: string; [key: string]: unknown };
export type ExecutionState = { surfaces?: Record<string, unknown>; checkpoint_continue?: boolean; [key: string]: unknown };

/** Compatibility projection for old saved history. Prose is never completion evidence. */
export function taskStatus(thread: ChatThread | null, commands: ConversationCommand[] = [], view?: SessionView | null): TaskStatus {
  if (view && view.session_id === thread?.id) return view.status;
  const external = thread?.source === "mcp" || thread?.source === "tunnel";
  const messages = thread?.messages || [];
  const last = messages.findLast(m => m.role === "assistant");
  const turn: TurnPayload | undefined = last?.role === "assistant" ? last.turn : undefined;
  const work = commands.filter(c => c.kind !== "guide" && c.state !== "removed").sort((a,b) => a.created_at-b.created_at);
  const executed=work.findLast(c=>c.state!=="queued");
  const command = work.findLast(c => ["running","stopping","unconfirmed"].includes(c.state)) || (executed&&["failed","blocked","awaiting_approval","awaiting_input","interrupted"].includes(executed.state)?executed:work.at(-1));
  let execution = command?.state || (last?.role === "assistant" && last.loading ? "running" : last?.role === "assistant" && last.error ? "failed" : "idle");
  let completion: TaskStatus["completion"] = null;
  if (!command && turn?.stopped_reason) execution = ["end_turn","completed","stop"].includes(turn.stopped_reason) ? "succeeded" : turn.stopped_reason.includes("approval") ? "awaiting_approval" : turn.stopped_reason.includes("cancel") ? "interrupted" : "blocked";
  if (external) {
    const traces = messages.flatMap(m => m.role === "assistant" && m.turn?.external_call ? [m.turn.external_call] : []);
    const progress = traces.findLast(t => t.tool === "nerya_progress");
    const status = recordOf(progress?.arguments).status;
    if (status === "completed") { execution = "succeeded"; completion = "external_reported"; }
    else if (["failed","blocked","running"].includes(String(status))) execution = String(status);
    if (traces.at(-1)?.status === "running") { execution = "running"; completion = null; }
  }
  if (execution === "succeeded" && !completion) completion = "turn_finished";
  const waiting_for = execution === "awaiting_approval" ? "approval" : execution === "awaiting_input" ? "user" : null;
  return { execution, waiting_for, completion, external, validation: turn?.verifier_outcome?.hard_status || "unknown",
    needs_attention: !!waiting_for || ["failed","blocked","unconfirmed","interrupted"].includes(execution), turn_id: turn?.turn_id };
}

export function statusLabel(status: TaskStatus, zh: boolean): string {
  if (status.waiting_for === "user") return i18nCopy(zh, "copy.lib_workbench.001");
  if (status.waiting_for === "approval") return i18nCopy(zh, "copy.lib_workbench.002");
  if (status.completion === "external_reported") return i18nCopy(zh, "copy.lib_workbench.003");
  const labels: Record<string, string> = {
    idle: "copy.lib_workbench.004", running:"copy.lib_workbench.005", stopping:"copy.lib_workbench.006",
    queued:"copy.lib_workbench.007", succeeded:"copy.lib_workbench.008", failed:"copy.lib_workbench.009",
    interrupted:"copy.lib_workbench.010", blocked:"copy.lib_workbench.011", unconfirmed:"copy.lib_workbench.012",
  };
  return i18nCopy(zh, labels[status.execution] ?? "") || (i18nCopy(zh, "copy.lib_workbench.013"));
}

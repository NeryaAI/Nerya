import type { NativeBlockEnvelope } from "./chat";
import type { AgentOperation } from "../components/chat/useAgentWork";
import { pairOperations, record, type ToolStep } from "./agentConversation";

const ordinaryTools = new Set([
  "read_file", "write_file", "edit_file", "create_file", "list_dir", "list_files",
  "web_search", "web_search_fetch", "web_fetch", "fetch_url", "run_shell", "run_command",
  "grep", "glob", "search_files",
]);

/** null means a specialized renderer must keep ownership (approvals, charts, trades, etc.). */
export function readableTurnSteps(envelopes: NativeBlockEnvelope[], ts: number): ToolStep[] | null {
  if (!envelopes.length) return null;
  const events = new Map<string, AgentOperation>();
  for (const [index, envelope] of envelopes.entries()) {
    const block = record(envelope.block || envelope), kind = String(block.kind || "");
    if (kind === "thinking") continue; // Never project private reasoning into the activity list.
    if (!["text", "tool_use", "tool_result"].includes(kind)) return null;
    const call = String(block.call_id || block.tool_use_id || block.tool_call_id || "");
    const key = call ? `${block.attempt || 1}:${call}:${kind}` : `block:${index}`;
    const previous = events.get(key);
    events.set(key, { seq: previous?.seq ?? index, ts: ts / 1000, kind, data: block });
  }
  const steps = pairOperations([...events.values()]);
  if (steps.some((step) => step.event.kind !== "text" && !ordinaryTools.has(String(step.data.action || "").toLowerCase()))) return null;
  return steps;
}

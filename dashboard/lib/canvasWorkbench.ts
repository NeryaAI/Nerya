import { liveEventsToBlocks, type ChatThread } from "./chat";
import { contentValue, pairOperations, readableResult, record, toolPresentation } from "./agentConversation";
import type { AgentOperation } from "../components/chat/useAgentWork";

export type WorkbenchResource = {
  id: string; kind: string; title: string; subtitle?: string; path?: string;
  body?: string; html?: string; url?: string; seenAt: number; operation?: string;
};
export type TimelineItem = {
  id: string; turnId: string; title: string; subject: string; family: string;
  state: string; failed: boolean; pending: boolean; detail: string; elapsed?: number;
};

/** Public tool evidence only. Do not project system messages or hidden reasoning. */
export function workbenchTimeline(thread: ChatThread | null, zh: boolean): TimelineItem[] {
  if (!thread) return [];
  return thread.messages.flatMap((message) => {
    if (message.role !== "assistant") return [];
    const blocks = [...(message.turn?.blocks || []), ...liveEventsToBlocks([
      ...(message.turn?.activity_events || []), ...(message.live_events || []),
    ])];
    const events = new Map<string, AgentOperation>();
    for (const [index, env] of blocks.entries()) {
      const block = record(env.block || env), kind = String(block.kind || "");
      if (kind !== "tool_use" && kind !== "tool_result") continue;
      const call = String(block.call_id || block.tool_use_id || block.tool_call_id || "");
      const key = call ? `${block.attempt || 1}:${call}:${kind}` : `event:${index}`;
      // Updating a live record must not move it to the end of the timeline.
      const previous = events.get(key);
      events.set(key, { seq: previous?.seq ?? index, ts: message.ts / 1000, kind, data: block });
    }
    if (!events.size) {
      const fallback = message.turn?.tool_trace?.length ? message.turn.tool_trace : message.turn?.actions || [];
      fallback.forEach((value, i) => {
        const data = record(value);
        const kind = data.result !== undefined || data.ok !== undefined ? "tool_result" : "tool_use";
        events.set(`legacy:${i}`, { seq: i, ts: message.ts / 1000, kind, data });
      });
    }
    return pairOperations([...events.values()]).map((step) => {
      const p = toolPresentation(step, message.loading ? "running" : "completed", zh);
      const result = contentValue(step.data.result);
      return { id: `${message.id}:${step.key}`, turnId: message.id, title: p.title, subject: p.subject,
        family: p.family, state: p.state, failed: p.failed, pending: p.pending,
        detail: readableResult(step.data.error || result, zh),
        elapsed: typeof step.data.elapsed_ms === "number" ? step.data.elapsed_ms : undefined };
    });
  });
}

export type DiffLine = { kind: "meta" | "add" | "remove" | "context"; text: string; old?: number; next?: number };
/** Hunk headers and file headers are never counted as added/removed source lines. */
export function parseRecordedDiff(source: string) {
  let old = 0, next = 0, hunk = false, additions = 0, deletions = 0;
  const lines: DiffLine[] = source.split(/\r?\n/).map((text) => {
    const header = text.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (header) { old = Number(header[1]); next = Number(header[2]); hunk = true; return { kind: "meta", text }; }
    if (/^(diff --git|index |--- |\+\+\+ |\\ No newline|new file|deleted file|rename |similarity)/.test(text)) return { kind: "meta", text };
    if (text.startsWith("+")) { additions++; return { kind: "add", text, next: hunk ? next++ : undefined }; }
    if (text.startsWith("-")) { deletions++; return { kind: "remove", text, old: hunk ? old++ : undefined }; }
    if (hunk && text.startsWith(" ")) return { kind: "context", text, old: old++, next: next++ };
    return { kind: "meta", text };
  });
  return { lines, additions, deletions };
}

export type WorkbenchFile = {
  key: string; path: string; title: string; latest: WorkbenchResource; versions: WorkbenchResource[];
  diff?: WorkbenchResource; changed: boolean; status: "created" | "modified" | "read" | "artifact";
};
export function workbenchFiles(resources: WorkbenchResource[]): WorkbenchFile[] {
  const files = new Map<string, WorkbenchFile>();
  // Resources are newest first. Stable source order resolves ties within a turn.
  for (const item of [...resources].sort((a, b) => a.seenAt - b.seenAt)) {
    const path = item.path || item.subtitle || item.title;
    const key = item.path ? `path:${item.path}` : `item:${item.id}`;
    const previous = files.get(key);
    const modified = /^(edit_file|write_file|create_file)$/.test(item.operation || "");
    const status = item.operation === "create_file" ? "created" : modified ? "modified" : item.operation === "read_file" ? "read" : "artifact";
    files.set(key, { key, path, title: item.title, latest: item,
      versions: [...(previous?.versions || []), item], diff: item.kind === "diff" ? item : previous?.diff,
      changed: modified || Boolean(previous?.changed), status: modified || !previous?.changed ? status : previous.status });
  }
  return [...files.values()].sort((a, b) => Number(b.changed) - Number(a.changed) || b.latest.seenAt - a.latest.seenAt || a.path.localeCompare(b.path));
}

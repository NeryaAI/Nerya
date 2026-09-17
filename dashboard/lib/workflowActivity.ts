import { asObject, type WorkflowText } from "./workflowPresentation";
import { contentValue } from "./agentConversation";

export type Facts = Record<string, unknown>;
export type Activity = { id: string; kind: "script" | "agent"; status: string; ts: string; reason: string; row: Facts };
export type RuntimeTool = { id: string; name: string; input: unknown; output?: unknown; state: "running" | "returned" | "error"; elapsed?: number };
const text = (v: unknown) => typeof v === "string" ? v : "";
export const rows = (v: unknown): Facts[] => Array.isArray(v) ? v.map(asObject) : [];
export function activities(runs: unknown, tasks: unknown): Activity[] {
  const result = new Map<string, Activity>();
  for (const row of rows(runs)) {
    const id = text(row.run_id); if (!id) continue;
    result.set(`script:${id}`, { id, kind: "script", status: text(row.status), ts: text(row.started_at), reason: text(row.reason), row });
  }
  for (const entry of rows(tasks)) {
    const row = { ...entry, ...asObject(entry.task) };
    const id = text(row.task_id); if (!id) continue;
    const key = `agent:${id}`;
    const combined = { ...result.get(key)?.row, ...row };
    result.set(key, { id, kind: "agent", status: text(row.status), ts: text(row.started_at || row.ts), reason: text(row.reason), row: combined });
  }
  return [...result.values()].sort((a, b) => (Date.parse(b.ts) || 0) - (Date.parse(a.ts) || 0));
}
export function activityState(item: Activity, t: WorkflowText) {
  if (["failed", "error"].includes(item.status) || item.row.error) return { label: t("运行失败", "Failed"), tone: "error" };
  if (["running", "queued"].includes(item.status)) return { label: t("等待执行结束", "Awaiting completion"), tone: "running" };
  if (["skipped", "skip"].includes(item.status)) return { label: item.kind === "agent" ? t("已跳过 · 未调用 Agent", "Skipped · Agent not invoked") : t("脚本已跳过", "Script skipped"), tone: "muted" };
  const stop = text(item.row.stopped_reason);
  if (stop && !["end_turn", "stop", "completed"].includes(stop)) return { label: t("已结束 · 需检查", "Ended · review needed"), tone: "warning" };
  return { label: ({ hold: t("未采取行动", "No action"), ok: t("脚本已完成", "Script completed"), submitted: t("已提交请求", "Request submitted"), executed: t("Agent 已结束", "Agent ended") } as Record<string, string>)[item.status] || item.status || t("状态未记录", "Status unrecorded"), tone: "neutral" };
}
export function runtimeTools(item: Activity, events: Facts[]): RuntimeTool[] {
  const tools = new Map<string, RuntimeTool>();
  for (const [index, trace] of rows(item.row.tool_trace).entries()) {
    const call = asObject(trace.call); const result = asObject(contentValue(trace.result));
    const id = text(call.id || trace.call_id || trace.tool_call_id) || `trace:${index}`;
    tools.set(id, { id, name: text(trace.action || call.name || trace.name) || "tool", input: call.arguments || trace.payload || trace.arguments,
      output: trace.result, state: trace.ok === false || result.is_error === true || result.ok === false || !!trace.error || !!result.error ? "error" : "returned",
      elapsed: typeof trace.elapsed_ms === "number" ? trace.elapsed_ms : undefined });
  }
  const turnId = text(item.row.turn_id);
  if (turnId) for (const event of events) {
    if (event.turn_id !== turnId || !["tool.start", "tool.complete"].includes(text(event.kind))) continue;
    const id = text(event.call_id || event.tool_call_id); if (!id) continue;
    const prev = tools.get(id);
    const complete = event.kind === "tool.complete";
    if (!complete && prev?.state !== undefined && prev.state !== "running") continue;
    const result = asObject(contentValue(event.result));
    tools.set(id, { id, name: text(event.action) || prev?.name || "tool", input: event.payload || prev?.input,
      output: complete ? event.result ?? event.error : prev?.output,
      state: complete ? event.ok === false || !!event.error || result.ok === false || result.is_error === true || !!result.error ? "error" : "returned" : "running",
      elapsed: typeof event.elapsed_ms === "number" ? event.elapsed_ms : prev?.elapsed });
  }
  return [...tools.values()];
}
export function publicRuntimeEvents(events: unknown, turnId: string): Facts[] {
  const allowed = new Set(["tool.start", "tool.complete", "tool.progress", "approval.request", "message.delta", "turn.complete", "team.start", "team.member.start", "team.member.end", "team.end", "team.complete", "subagent.start", "subagent.end"]);
  return rows(events).filter((e) => !!turnId && e.turn_id === turnId && allowed.has(text(e.kind)));
}
export type ParallelMember = { id: string; name: string; state: "queued" | "running" | "returned" | "error" | "unknown"; output?: unknown; error?: unknown; elapsed?: number };
export function parallelMembers(item: Activity, events: Facts[], snapshot?: unknown): ParallelMember[] {
  const members = new Map<string, ParallelMember>();
  const persisted = asObject(snapshot);
  let team = persisted;
  if (!Array.isArray(team.results)) for (const trace of rows(item.row.tool_trace)) {
    if (trace.action === "team_run") { const raw = asObject(contentValue(trace.result)); team = asObject(raw.data || raw); }
  }
  for (const result of [...rows(team.results), ...rows(team.failures)]) {
    const name = text(result.subagent || result.role); if (!name) continue;
    members.set(name, { id: text(result.agent_id) || name, name, state: result.ok === false || result.error ? "error" : result.ok === true ? "returned" : "unknown", output: result.output, error: result.error, elapsed: typeof result.wall_ms === "number" ? result.wall_ms : undefined });
  }
  const recorded = new Set(members.keys());
  for (const event of [...events].sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0))) {
    if (!item.row.turn_id || event.turn_id !== item.row.turn_id) continue;
    const name = text(event.subagent || event.role || event.name); if (!name) continue;
    if (recorded.has(name)) continue;
    const old = members.get(name);
    if (old && ["returned", "error"].includes(old.state) && event.kind !== "team.member.end") continue;
    if (!["team.member.start", "team.member.end", "subagent.start", "subagent.end"].includes(text(event.kind))) continue;
    const ended = String(event.kind).endsWith(".end");
    members.set(name, { id: text(event.agent_id) || old?.id || name, name,
      state: ended ? event.ok === false || event.error ? "error" : event.kind === "team.member.end" && event.ok === true ? "returned" : "unknown" : event.kind === "subagent.start" ? "running" : old?.state || "queued",
      output: event.output ?? old?.output, error: event.error,
      elapsed: typeof event.wall_ms === "number" ? event.wall_ms : old?.elapsed });
  }
  if (!["running", "queued"].includes(item.status)) for (const member of members.values()) {
    if (["running", "queued"].includes(member.state)) member.state = "unknown";
  }
  return [...members.values()];
}
export function durationText(value: unknown, t: WorkflowText): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return t("未记录", "Not recorded");
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`;
}

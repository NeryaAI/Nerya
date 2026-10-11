import { copy as i18nCopy } from "./i18n";
import { toolSemantics, toolResultSummary } from "./toolSemantics";
import { legacyToolValue } from "./toolOutputPresentation";
import type { AgentDetail, AgentOperation, AgentWork } from "../components/chat/useAgentWork";
import type { ChatResult } from "./chatResults";

export const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const string = (value: unknown): string => typeof value === "string" ? value.trim() : "";
export const working = (state: string) => ["running", "stopping", "queued", "planned", "pending"].includes(state);

/** Providers may return JSON inside a text content block. Decode it, never display the transport envelope. */
export function contentValue(value: unknown, depth = 0): unknown {
  if (depth > 6) return value;
  if (typeof value === "string") {
    const legacy = legacyToolValue(value);
    if (legacy !== value) return contentValue(legacy, depth + 1);
    const source = value.trim().replace(/^```(?:json)?\s*\n([\s\S]*?)\n```$/, "$1");
    if (/^[{[]/.test(source)) {
      try { return contentValue(JSON.parse(source), depth + 1); } catch { return value; }
    }
    return value;
  }
  const obj = record(value);
  if (obj.structuredContent && typeof obj.structuredContent === "object") return contentValue(obj.structuredContent, depth + 1);
  if (obj.structured_content && typeof obj.structured_content === "object") return contentValue(obj.structured_content, depth + 1);
  if (obj.data && typeof obj.data === "object" && Object.keys(obj).every((k) => ["data", "ok", "success", "type"].includes(k))) return contentValue(obj.data, depth + 1);
  if (Object.keys(obj).length === 1 && "value" in obj) return contentValue(obj.value, depth + 1);
  // diff/code parts also have text. Flattening them destroys their type and turns
  // Python comments into Markdown headings. Only plain text envelopes may join.
  if (Array.isArray(obj.content) && obj.content.length > 0 && obj.content.every((p) => (!record(p).type || record(p).type === "text") && typeof record(p).text === "string")) {
    return contentValue(obj.content.map((p) => string(record(p).text)).join("\n\n"), depth + 1);
  }
  return value;
}

const labels: Record<string, string> = { summary: "copy.agentFieldLabels.001", findings: "copy.agentFieldLabels.002", evidence: "copy.agentFieldLabels.003", sources: "copy.agentFieldLabels.004", results: "copy.agentFieldLabels.005", recommendations: "copy.agentFieldLabels.006", next_steps: "copy.agentFieldLabels.007", risks: "copy.agentFieldLabels.008", notes: "copy.agentFieldLabels.009", path: "copy.agentFieldLabels.010", query: "copy.agentFieldLabels.011", command: "copy.agentFieldLabels.012", stdout: "copy.agentFieldLabels.013", stderr: "copy.agentFieldLabels.014", exit_code: "copy.agentFieldLabels.015", count: "copy.agentFieldLabels.016", status: "copy.agentFieldLabels.017", reason: "copy.agentFieldLabels.018", error: "copy.agentFieldLabels.019", title: "copy.agentFieldLabels.020", description: "copy.agentFieldLabels.021", name: "copy.agentFieldLabels.022", content: "copy.agentFieldLabels.023", text: "copy.agentFieldLabels.024", snippet: "copy.agentFieldLabels.025", url: "copy.agentFieldLabels.026", link: "copy.agentFieldLabels.027", files: "copy.agentFieldLabels.028", items: "copy.agentFieldLabels.029" };
export function fieldLabel(key: string, zh: boolean): string {
  return labels[key] ? i18nCopy(zh, labels[key]) : key.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
}
const transportKeys = new Set(["kind", "type", "role", "done", "ok", "success", "attempt", "call_id", "tool_use_id", "tokens", "usage", "model", "provider"]);
export function safeLink(value: unknown): string {
  try { const url = new URL(string(value)); return ["https:", "http:"].includes(url.protocol) ? url.href : ""; } catch { return ""; }
}
const escape = (value: string) => value.replace(/[\\[\]<>]/g, "\\$&");

/** Deterministic presentation only: no fabricated conclusions or model-generated summaries. */
export function readableResult(value: unknown, zh: boolean, depth = 0): string {
  value = contentValue(value);
  if (value == null) return "";
  if (typeof value === "string") {
    return /^(?:\{\s*"|\[\s*\{)/.test(value.trim()) ? (i18nCopy(zh, "copy.lib_agentConversation.001")) : value;
  }
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return i18nCopy(zh, value ? "copy.lib_agentConversation.booleanTrue" : "copy.lib_agentConversation.booleanFalse");
  if (depth > 5) return i18nCopy(zh, "copy.lib_agentConversation.002");
  if (Array.isArray(value)) {
    const visible = value.slice(0, 80).map((v) => readableResult(v, zh, depth + 1)).filter(Boolean);
    return visible.map((v) => `- ${v.replace(/\n/g, "\n  ")}`).join("\n")
      + (value.length > 80 ? `\n\n${i18nCopy(zh, "copy.lib_agentConversation.003")}` : "");
  }
  const obj = record(value);
  const parts: string[] = [];
  const handled = new Set(transportKeys);
  const url = safeLink(obj.url || obj.link || obj.href);
  if (url) {
    parts.push(`[${escape(string(obj.title) || string(obj.name) || url)}](<${url}>)`);
    ["url", "link", "href", "title", "name"].forEach((k) => handled.add(k));
  }
  for (const key of ["summary", "final_text", "answer", "markdown", "message"]) {
    if (typeof obj[key] === "string" && string(obj[key])) { parts.push(readableResult(obj[key], zh, depth + 1)); handled.add(key); }
  }
  const entries = Object.entries(obj).filter(([k, v]) => !handled.has(k) && v != null && v !== "");
  for (const [key, item] of entries.slice(0, 60)) {
    const body = readableResult(item, zh, depth + 1);
    if (body) parts.push(`${depth === 0 ? "### " : "**"}${fieldLabel(key, zh)}${depth === 0 ? "" : "**"}\n\n${body}`);
  }
  if (entries.length > 60) parts.push(i18nCopy(zh, "copy.lib_agentConversation.004"));
  return parts.join("\n\n");
}

export type AgentRun = { attempt: number; events: AgentOperation[]; instruction: string; output?: unknown; error?: string; state: string; ts: number };
export function agentRuns(row: AgentWork, detail: AgentDetail | null): AgentRun[] {
  const runs = new Map<number, AgentRun>();
  const ensure = (attempt: number, ts: number) => {
    if (!runs.has(attempt)) runs.set(attempt, { attempt, events: [], instruction: "", state: "unknown", ts });
    return runs.get(attempt)!;
  };
  for (const e of [...new Map((detail?.events || []).map((e) => [e.seq, e])).values()].sort((a, b) => a.seq - b.seq)) {
    const n = Number(e.data.attempt) || 1;
    const run = ensure(n, e.ts);
    if (e.kind === "instruction") run.instruction = string(e.data.text);
    else if (["completed", "failed", "cancelled", "blocked", "interrupted"].includes(e.kind)) {
      run.state = e.kind;
      if (e.data.output != null) run.output = e.data.output;
      if (e.data.error) { run.error = string(e.data.error); run.events.push(e); }
    } else if (!["started", "resumed"].includes(e.kind)) run.events.push(e);
  }
  const latest = ensure(row.attempt || 1, row.updated_at);
  latest.state = row.state;
  latest.error = row.error || latest.error;
  // begin() retains the last output while resuming. Never label that old output as this run's result.
  if (!working(row.state) && latest.output == null) latest.output = row.output;
  return [...runs.values()].sort((a, b) => a.attempt - b.attempt);
}

export type ToolStep = { key: string; event: AgentOperation; result?: AgentOperation; data: Record<string, unknown> };
export function operationIdentity(data: Record<string, unknown>): string {
  const call = string(data.call_id || data.tool_use_id || data.tool_call_id);
  return call ? `${data.attempt || 1}:${call}` : "";
}
export function pairOperations(events: AgentOperation[]): ToolStep[] {
  const steps: ToolStep[] = [];
  const calls = new Map<string, ToolStep>();
  for (const e of events) {
    const key = ["tool_use", "tool_result"].includes(e.kind) ? operationIdentity(e.data) : "";
    const previous = key ? calls.get(key) : undefined;
    if (previous) {
      if (e.kind === "tool_result") previous.result = e;
      else previous.event = e;
      // Result snapshots often omit action/input. Never erase the original request.
      const start = previous.event.data, result = previous.result?.data || {};
      previous.data = { ...start, ...result,
        action: result.action || start.action, skill_id: result.skill_id || start.skill_id,
        payload: { ...record(result.payload), ...record(start.payload || start.arguments) } };
    } else {
      const step = { key: key || `${e.seq}`, event: e, data: e.data, ...(e.kind === "tool_result" ? { result: e } : {}) };
      steps.push(step);
      if (key) calls.set(key, step);
    }
  }
  return steps;
}

export function toolPresentation(step: ToolStep, runState: string, zh: boolean) {
  const d = step.data, payload = record(d.payload || d.arguments), result = record(contentValue(d.result));
  const name = string(d.action || d.skill_id || d.name).toLowerCase();
  const status = string(result.status || result.state || d.status).toLowerCase();
  const rawResult = record(d.result);
  const failed = d.ok === false || rawResult.ok === false || rawResult.success === false || rawResult.isError === true || result.isError === true || result.ok === false || result.success === false || Boolean(d.error || rawResult.error || result.error) || ["failed","error","timed_out"].includes(status) || (typeof result.exit_code === "number" && result.exit_code !== 0);
  const waiting = !failed && (/awaiting_(approval|input)|pending_approval|requires_approval/.test(status) || !step.result && ["awaiting_approval","awaiting_input","blocked"].includes(runState));
  const pending = !step.result && working(runState) && !failed && !waiting;
  const preparing = pending && d.phase === "input_streaming";
  const state = failed ? (i18nCopy(zh, "copy.lib_agentConversation.005")) : waiting ? (i18nCopy(zh, "copy.lib_agentConversation.010")) : preparing ? (i18nCopy(zh, "copy.lib_agentConversation.011")) : pending ? (i18nCopy(zh, "copy.lib_agentConversation.006"))
    : step.result ? (d.ok === true ? (i18nCopy(zh, "copy.lib_agentConversation.007")) : (i18nCopy(zh, "copy.lib_agentConversation.008"))) : (i18nCopy(zh, "copy.lib_agentConversation.009"));
  const semantic = toolSemantics(name, payload, zh);
  return { name, ...semantic, state, failed, pending, waiting, preparing, payload, result, summary: toolResultSummary(result, zh) };
}

export function childResult(row: AgentWork, output: unknown, attempt: number, zh: boolean): ChatResult {
  return { id: `child-result:${row.id}:${attempt}`, title: `${row.name} · ${row.title}`, text: readableResult(output, zh),
    ts: row.updated_at * 1000, agentId: row.id, attempt };
}

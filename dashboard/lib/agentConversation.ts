import type { AgentDetail, AgentOperation, AgentWork } from "../components/chat/useAgentWork";
import type { ChatResult } from "./chatResults";

export const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const string = (value: unknown): string => typeof value === "string" ? value.trim() : "";
export const working = (state: string) => ["running", "queued", "planned", "pending"].includes(state);

/** Providers may return JSON inside a text content block. Decode it, never display the transport envelope. */
export function contentValue(value: unknown, depth = 0): unknown {
  if (depth > 6) return value;
  if (typeof value === "string") {
    const source = value.trim().replace(/^```(?:json)?\s*\n([\s\S]*?)\n```$/, "$1");
    if (/^[{[]/.test(source)) {
      try { return contentValue(JSON.parse(source), depth + 1); } catch { return value; }
    }
    return value;
  }
  const obj = record(value);
  if (obj.data && typeof obj.data === "object" && Object.keys(obj).every((k) => ["data", "ok", "success", "type"].includes(k))) return contentValue(obj.data, depth + 1);
  if (Object.keys(obj).length === 1 && "value" in obj) return contentValue(obj.value, depth + 1);
  if (Array.isArray(obj.content) && obj.content.length > 0 && obj.content.every((p) => typeof record(p).text === "string")) {
    return contentValue(obj.content.map((p) => string(record(p).text)).join("\n\n"), depth + 1);
  }
  return value;
}

const labels: Record<string, [string, string]> = {
  summary: ["结论", "Summary"], findings: ["发现", "Findings"], evidence: ["依据", "Evidence"],
  sources: ["来源", "Sources"], results: ["结果", "Results"], recommendations: ["建议", "Recommendations"],
  next_steps: ["下一步", "Next steps"], risks: ["风险", "Risks"], notes: ["补充说明", "Notes"],
  path: ["文件", "File"], query: ["搜索内容", "Search query"], command: ["命令", "Command"],
  stdout: ["输出", "Output"], stderr: ["错误输出", "Error output"], exit_code: ["退出状态", "Exit code"],
  count: ["数量", "Count"], status: ["状态", "Status"], reason: ["原因", "Reason"], error: ["错误", "Error"],
  title: ["标题", "Title"], description: ["说明", "Description"], name: ["名称", "Name"],
  content: ["内容", "Content"], text: ["内容", "Text"], snippet: ["摘要", "Excerpt"],
  url: ["链接", "Link"], link: ["链接", "Link"], files: ["文件", "Files"], items: ["条目", "Items"],
};
export function fieldLabel(key: string, zh: boolean): string {
  return labels[key]?.[zh ? 0 : 1] || key.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
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
    return /^(?:\{\s*"|\[\s*\{)/.test(value.trim()) ? (zh ? "返回了结构化数据，可在调试详情中查看原始记录。" : "Structured data returned. The original record is available in debug details.") : value;
  }
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return zh ? (value ? "是" : "否") : (value ? "Yes" : "No");
  if (depth > 5) return zh ? "更多嵌套内容见调试详情。" : "Further nested data is available in debug details.";
  if (Array.isArray(value)) {
    const visible = value.slice(0, 80).map((v) => readableResult(v, zh, depth + 1)).filter(Boolean);
    return visible.map((v) => `- ${v.replace(/\n/g, "\n  ")}`).join("\n")
      + (value.length > 80 ? `\n\n${zh ? "仅展示前 80 项，完整记录见调试详情。" : "Showing the first 80 items. Full data is available in debug details."}` : "");
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
  if (entries.length > 60) parts.push(zh ? "更多字段见调试详情。" : "Additional fields are available in debug details.");
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
export function pairOperations(events: AgentOperation[]): ToolStep[] {
  const steps: ToolStep[] = [];
  const pending = new Map<string, ToolStep>();
  for (const e of events) {
    const call = string(e.data.call_id || e.data.tool_use_id || e.data.tool_call_id);
    const key = `${e.data.attempt || 1}:${call}`;
    if (e.kind === "tool_result" && call && pending.has(key)) {
      const step = pending.get(key)!;
      step.result = e; step.data = { ...step.data, ...e.data, payload: step.data.payload || e.data.payload };
      pending.delete(key);
    } else {
      const step = { key: `${e.seq}`, event: e, data: e.data, ...(e.kind === "tool_result" ? { result: e } : {}) };
      steps.push(step);
      if (e.kind === "tool_use" && call) pending.set(key, step);
    }
  }
  return steps;
}

export function toolPresentation(step: ToolStep, runState: string, zh: boolean) {
  const d = step.data, payload = record(d.payload || d.arguments), result = record(contentValue(d.result));
  const name = string(d.action || d.skill_id || d.name).toLowerCase();
  const failed = d.ok === false || result.ok === false || result.success === false || Boolean(d.error || result.error) || (typeof result.exit_code === "number" && result.exit_code !== 0);
  const pending = !step.result && working(runState) && !failed;
  const state = failed ? (zh ? "失败" : "Failed") : pending ? (zh ? "进行中" : "Running")
    : step.result ? (d.ok === true ? (zh ? "完成" : "Done") : (zh ? "已返回" : "Returned")) : (zh ? "未收到返回" : "No response recorded");
  const family = /todo|plan/.test(name) ? "plan" : /message/.test(name) ? "message"
    : /search|grep|glob/.test(name) ? "search" : /read|fetch|view|list_dir/.test(name) ? "read"
    : /edit|write|patch|create/.test(name) ? "edit" : /shell|bash|exec|command/.test(name) ? "shell" : "tool";
  const names: Record<string, [string, string]> = { search: ["搜索", "Search"], read: ["读取", "Read"], edit: ["修改", "Edit"], shell: ["运行命令", "Run command"], message: ["发送协作消息", "Send collaborator message"], plan: ["更新计划", "Update plan"], tool: ["执行操作", "Run operation"] };
  const subject = string(payload.description || payload.query || payload.search_query || payload.path || payload.file_path || payload.file || payload.url || payload.command || payload.pattern || payload.task) || name.replace(/_/g, " ");
  return { name, family, subject, title: names[family][zh ? 0 : 1], state, failed, pending, payload, result };
}

export function childResult(row: AgentWork, output: unknown, attempt: number, zh: boolean): ChatResult {
  return { id: `child-result:${row.id}:${attempt}`, title: `${row.name} · ${row.title}`, text: readableResult(output, zh),
    ts: row.updated_at * 1000, agentId: row.id, attempt };
}

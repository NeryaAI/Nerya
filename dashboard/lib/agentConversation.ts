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
const deliveryReasonCopy: Record<string, string> = {
  required_checks_missing: "copy.deliveryEvidence.reason_required_checks_missing",
  required_artifacts_missing: "copy.deliveryEvidence.reason_required_artifacts_missing",
  completed_stage_missing: "copy.deliveryEvidence.reason_completed_stage_missing",
  producer_evidence_missing: "copy.deliveryEvidence.reason_producer_evidence_missing",
  verified_artifact_missing: "copy.deliveryEvidence.reason_verified_artifact_missing",
  required_stage_pending: "copy.deliveryEvidence.reason_required_stage_pending",
  stage_blocked: "copy.deliveryEvidence.reason_stage_blocked",
  check_missing: "copy.deliveryEvidence.reason_check_missing",
  check_failed: "copy.deliveryEvidence.reason_check_failed",
  check_stale: "copy.deliveryEvidence.reason_check_stale",
  check_unknown: "copy.deliveryEvidence.reason_check_unknown",
  check_pending: "copy.deliveryEvidence.reason_check_pending",
  check_unverified: "copy.deliveryEvidence.reason_check_unverified",
  artifact_missing: "copy.deliveryEvidence.reason_artifact_missing",
  artifact_unverified: "copy.deliveryEvidence.reason_artifact_unverified",
  trusted_delivery_contract_missing: "copy.deliveryEvidence.reason_trusted_delivery_contract_missing",
  trusted_delivery_contract_invalid: "copy.deliveryEvidence.reason_trusted_delivery_contract_invalid",
  trusted_delivery_workflow_missing: "copy.deliveryEvidence.reason_trusted_delivery_workflow_missing",
  untrusted_delivery_contract_source: "copy.deliveryEvidence.reason_untrusted_delivery_contract_source",
  delivery_contract_binding_mismatch: "copy.deliveryEvidence.reason_delivery_contract_binding_mismatch",
  delivery_contract_stale: "copy.deliveryEvidence.reason_delivery_contract_stale",
  request_scope_unverified: "copy.deliveryEvidence.reason_request_scope_unverified",
  invalid_span: "copy.deliveryEvidence.reason_invalid_span",
  capability_gap: "copy.deliveryEvidence.reason_capability_gap",
  decision_request_unconfirmed: "copy.deliveryEvidence.reason_decision_request_unconfirmed",
  data_coverage: "copy.deliveryEvidence.reason_data_coverage",
  historical_replay: "copy.deliveryEvidence.reason_historical_replay",
};

export function localizeDeliveryReasons(value: string, zh: boolean): string {
  // Accept one structured reason, never search/replace within result Markdown.
  const match = /^([a-z_]+|UNVERIFIED_SCOPE)(?::[A-Za-z0-9_.-]+)?$/.exec(value);
  if (!match) return value;
  const specific: Record<string, string> = {
    "check_missing:data_coverage": "data_coverage",
    "check_missing:historical_replay": "historical_replay",
    "check_missing:historical_run": "historical_replay",
    UNVERIFIED_SCOPE: "request_scope_unverified",
  };
  const key = deliveryReasonCopy[specific[value] || match[1]];
  return key ? i18nCopy(zh, key) : value;
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
  const genericFailed = d.ok === false || rawResult.ok === false || rawResult.success === false || rawResult.isError === true || result.isError === true || result.ok === false || result.success === false || Boolean(d.error || rawResult.error || result.error) || ["failed","error","timed_out"].includes(status) || (typeof result.exit_code === "number" && result.exit_code !== 0);
  const completion = record(result.completion), producer = record(step.result?.data);
  // Exact legacy executor contract: the interaction boundary prevented startup.
  // Other aborted calls remain failures; do not infer this from run state alone.
  const notStartedInteraction = producer.skill_id === "native" && producer.ok === false &&
    producer.error_kind === "aborted" && producer.elapsed_ms === 0 && producer.result === null &&
    producer.error === "Not started: waiting for user interaction" && d.result === null && d.error === producer.error;
  const deliveryHardFailure = name === "delivery_finalize" && [d, rawResult, result].some(row => row.success === false || row.isError === true || row.is_error === true
    || Boolean(row.error || row.error_kind || row.error_code || row.workflow_contract_error)
    || (Array.isArray(row.errors) ? row.errors.length > 0 : Boolean(row.errors))
    || [row.status, row.state].some(value => ["failed", "error", "timed_out", "timeout", "cancelled", "canceled"].includes(string(value).toLowerCase()))
    || (typeof row.exit_code === "number" && row.exit_code !== 0));
  // Only the native finalizer's computed projection can distinguish an unmet
  // delivery condition from a tool failure. Transport failures always win.
  const computedDelivery = Boolean(step.result) && name === "delivery_finalize" && producer.action === "delivery_finalize" && producer.skill_id === "native"
    && producer.ok === true && producer.result_protocol === "nerya.task_delivery.v1"
    && result.protocol === "nerya.task_delivery.v1" && !deliveryHardFailure && d.ok !== false
    && (rawResult === result || rawResult.ok !== false)
    && typeof completion.allowed === "boolean" && result.ok === completion.allowed
    && producer.semantic_success === completion.allowed && result.status === completion.status
    && typeof completion.computed_at === "number" && Number.isFinite(completion.computed_at)
    && Array.isArray(completion.reasons) && completion.reasons.every(reason => typeof reason === "string" && Boolean(reason.trim()))
    && (completion.allowed ? ["complete", "partial"].includes(String(completion.status)) && completion.reasons.length === 0
      : ["partial", "incomplete"].includes(String(completion.status)) && completion.reasons.length > 0);
  const failed = !notStartedInteraction && !computedDelivery && (genericFailed || (Boolean(step.result) && name === "delivery_finalize"));
  const warning = computedDelivery && completion.status !== "complete";
  let summary = toolResultSummary(result, zh);
  if (notStartedInteraction) summary = zh ? "未执行：等待用户回答后继续。" : "Not executed: waiting for the user's answer.";
  if (warning) {
    summary = (completion.reasons as string[]).slice(0, 3).map(reason => {
      const localized = localizeDeliveryReasons(reason, zh);
      return localized === reason ? i18nCopy(zh, "copy.lib_agentConversation.deliveryUnmet") : localized;
    }).join(i18nCopy(zh, "copy.deliveryEvidence.scopeSeparator")).slice(0, 240)
      || i18nCopy(zh, "copy.lib_agentConversation.deliveryPartialHint");
  }
  const waiting = !failed && (notStartedInteraction || /awaiting_(approval|input)|pending_approval|requires_approval/.test(status) || !step.result && ["awaiting_approval","awaiting_input","blocked"].includes(runState));
  const pending = !step.result && working(runState) && !failed && !waiting;
  const preparing = pending && d.phase === "input_streaming";
  const state = notStartedInteraction ? (zh ? "等待回答" : "Waiting for answer") : failed ? (i18nCopy(zh, "copy.lib_agentConversation.005")) : warning ? i18nCopy(zh, completion.status === "partial" ? "copy.deliveryEvidence.partialDelivery" : "copy.deliveryEvidence.incomplete") : waiting ? (i18nCopy(zh, "copy.lib_agentConversation.010")) : preparing ? (i18nCopy(zh, "copy.lib_agentConversation.011")) : pending ? (i18nCopy(zh, "copy.lib_agentConversation.006"))
    : step.result ? (d.ok === true ? (i18nCopy(zh, "copy.lib_agentConversation.007")) : (i18nCopy(zh, "copy.lib_agentConversation.008"))) : (i18nCopy(zh, "copy.lib_agentConversation.009"));
  const semantic = toolSemantics(name, payload, zh);
  return { name, ...semantic, state, failed, warning, pending, waiting, preparing, notStartedInteraction, payload, result, summary };
}

export function childResult(row: AgentWork, output: unknown, attempt: number, zh: boolean): ChatResult {
  return { id: `child-result:${row.id}:${attempt}`, title: `${row.name} · ${row.title}`, text: readableResult(output, zh),
    ts: row.updated_at * 1000, agentId: row.id, attempt };
}

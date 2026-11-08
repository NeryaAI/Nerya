import { copy as i18nCopy } from "./i18n";
import type { AssistantMessage, TurnPayload } from "./chat";
import { conversationError } from "./conversationError";

const value = (input: unknown): string => typeof input === "string" ? input.trim() : "";
const terminalStates = new Set(["aborted", "cancelled", "canceled", "interrupted", "stopped", "failed", "error", "timed_out"]);
const stopReasons = new Set([
  ...terminalStates, "runtime_error", "llm_error", "stream_interrupted", "empty_model_response",
  "model_output_truncated", "provider_content_filtered", "interrupted_max_tokens", "interrupted_length",
  "interrupted_content_filter", "timeout", "max_iterations", "max_tool_calls", "token_budget_exceeded",
  "diminishing_returns", "repeated_tool_call", "task_poll_no_progress", "needs_summarisation",
  "model_quota_exhausted", "insufficient_quota", "rate_limited",
]);

function terminalReason(message: AssistantMessage): string | null {
  if (message.role !== "assistant" || message.loading || message.turn?.external_call) return null;
  // Older payloads also persisted these at the turn root. Read without changing
  // the current delivery contract or adding inferred state to the saved turn.
  const turn = message.turn as (TurnPayload & { aborted?: boolean; abort_reason?: string }) | undefined;
  const budget = turn?.budget;
  const abort = value(budget?.abort_reason) || value(turn?.abort_reason);
  if (abort) return abort;
  for (const reason of [turn?.stopped_reason, turn?.transition_reason]) {
    if (stopReasons.has(value(reason).split(":", 1)[0])) return value(reason);
  }
  for (const status of [message.execution_status, turn?.execution_status]) {
    if (terminalStates.has(value(status))) return value(status);
  }
  return budget?.aborted === true || turn?.aborted === true ? "aborted" : null;
}

/** Machine termination is necessary; an error-looking body is never evidence. */
export function isAbortedTerminal(message: AssistantMessage): boolean {
  return terminalReason(message) !== null;
}

/** Exact old host templates, anchored at the start. Quotes, code, reports and
 * newly authored summaries are deliberately not a match. */
function isLegacyWrapper(body: string): boolean {
  return /^本轮任务未完成：[^\n]+。\n执行情况：\d+ 步，\d+ 次工具调用(?:，其中 \d+ 次报错)?。\n/.test(body)
    && /(?:^|\n)(?:此前生成的内容仅供参考，本轮没有获得可靠的完整结果。|本轮尚未形成有效的最终答复。)(?:\n|$)/.test(body)
    || /^本轮 Agent 在运行过程中异常退出，未能生成最终回复。\n\n错误类型: [^\n]+\n错误信息: /.test(body);
}

/** Old abort finalizers appended their wrapper to a partial model response.
 * Require the durable counters and full host footer before hiding that mixed
 * final; quoted examples and code remain ordinary prose. */
function isBoundLegacySuffix(message: AssistantMessage, body: string): boolean {
  const marker = body.lastIndexOf("\n\n本轮任务未完成：");
  if (marker < 0 || /```|~~~|(?:^|\n)\s*>/.test(body)) return false;
  const suffix = body.slice(marker + 2);
  if (!isLegacyWrapper(suffix) || !suffix.endsWith("已执行的工具调用不会自动重放；恢复前请核对已完成的写入或交易操作。")) return false;
  const counts = suffix.match(/\n执行情况：(\d+) 步，(\d+) 次工具调用/);
  const budget = message.turn?.budget;
  return Boolean(counts && budget?.aborted === true
    && Number(counts[1]) === budget.iterations && Number(counts[2]) === budget.tool_calls);
}

/** Display-only compatibility for known host-generated aborted summaries. */
export function legacyTerminalSummary(message: AssistantMessage, body: string, zh = true): string | null {
  const reason = terminalReason(message);
  if (reason === null || !(isLegacyWrapper(body.trim()) || isBoundLegacySuffix(message, body.trim()))) return null;
  const [code, detail] = reason.split(":", 2);
  const hostError = body.match(/(?:^|\n)错误详情：([^\n]*)/)?.[1]
    || body.match(/\n错误信息: ([\s\S]*)/)?.[1] || "";
  const diagnostic = conversationError([reason, message.error, message.turn?.error, hostError].filter(Boolean).join("\n"), zh).code;
  if (["model_quota_exhausted", "insufficient_quota", "rate_limited"].includes(code)
      || ["model_quota_exhausted", "rate_limited"].includes(diagnostic)) {
    return i18nCopy(zh, "copy.legacyTerminalSummaryCopy.quota");
  }
  if (code === "cancelled" && ["command_control_unavailable", "command_lease_lost", "event_persistence_failed"].includes(detail)) {
    return i18nCopy(zh, "copy.legacyTerminalSummaryCopy.control");
  }
  if (["cancelled", "canceled"].includes(code)) {
    return i18nCopy(zh, "copy.legacyTerminalSummaryCopy.cancelled");
  }
  if (code === "task_poll_no_progress") {
    return i18nCopy(zh, "copy.legacyTerminalSummaryCopy.stalled");
  }
  if (["repeated_tool_call", "diminishing_returns"].includes(code)) {
    return i18nCopy(zh, "copy.legacyTerminalSummaryCopy.repeated");
  }
  if (["model_output_truncated", "interrupted_max_tokens", "interrupted_length", "token_budget_exceeded"].includes(code)) {
    return i18nCopy(zh, "copy.legacyTerminalSummaryCopy.length");
  }
  if (["timeout", "timed_out"].includes(code) || diagnostic === "request_timeout") {
    return i18nCopy(zh, "copy.legacyTerminalSummaryCopy.timeout");
  }
  if (code === "stream_interrupted") {
    return i18nCopy(zh, "copy.legacyTerminalSummaryCopy.stream");
  }
  if (["max_iterations", "max_tool_calls", "needs_summarisation"].includes(code)) {
    return i18nCopy(zh, "copy.legacyTerminalSummaryCopy.runLimit");
  }
  return i18nCopy(zh, "copy.legacyTerminalSummaryCopy.unexpected");
}

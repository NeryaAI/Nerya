/**
 * Generic CSV-driven Playwright runner for Nerya prompt cases.
 *
 * Reads tests/e2e/cases.csv (or NERYA_CASES_CSV) and creates one
 * Playwright test() per row. Each test:
 *
 *   1) Optionally resets workspace (column reset_before=1).
 *   2) Opens a fresh chat session.
 *   3) Types prompt, waits for assistant reply.
 *   4) Asserts must_contain (regex i) in reply.
 *   5) Asserts must_not_contain (regex) NOT in reply.
 *   6) Runs structured api_check against runtime API.
 *   7) Saves screenshot -> test-results/screenshots/<id>.png
 *   8) Writes per-case log + reply -> test-results/logs/<id>.{jsonl,txt}
 *   9) Appends to summary -> test-results/summary.csv
 *
 * Filter:
 *   NERYA_CASES_FILTER='C-AT.*'   npx playwright test csv-runner
 *   NERYA_CASES_ONLY='C-AT1,GX6'  npx playwright test csv-runner
 */
import { test, expect } from "./fixtures";
import type { Page } from "@playwright/test";
import { readFileSync, mkdirSync, writeFileSync, existsSync, appendFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

interface Row {
  id: string;
  group: string;
  priority: string;
  prompt: string;
  must_contain: string;
  must_not_contain: string;
  api_check: string;
  timeout_ms: number;
  reset_before: boolean;
  notes: string;
}

const CSV_PATH = process.env.NERYA_CASES_CSV ?? resolve(__dirname, "cases.csv");
const RESULTS_DIR = resolve(__dirname, "..", "..", "test-results");
const SCREENSHOTS_DIR = join(RESULTS_DIR, "screenshots");
const LOGS_DIR = join(RESULTS_DIR, "logs");
const SUMMARY_CSV = join(RESULTS_DIR, "summary.csv");
const CHAT_RUN_SETTINGS_KEY = "nerya.chat.runSettings.v2";

function parseCsv(text: string): Row[] {
  const rows: string[][] = [];
  let cur: string[] = [];
  let field = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') { inQuotes = false; }
      else { field += c; }
    } else if (c === '"') { inQuotes = true; }
    else if (c === ",") { cur.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (field !== "" || cur.length) { cur.push(field); rows.push(cur); cur = []; field = ""; }
      if (c === "\r" && text[i + 1] === "\n") i++;
    } else { field += c; }
  }
  if (field !== "" || cur.length) { cur.push(field); rows.push(cur); }
  if (rows.length === 0) return [];
  const header = rows[0].map((h) => h.trim());
  return rows.slice(1).filter((r) => r.length && r[0]).map((r) => {
    const obj: Record<string, string> = {};
    header.forEach((h, idx) => (obj[h] = (r[idx] ?? "").trim()));
    return {
      id: obj.id,
      group: obj.group,
      priority: obj.priority,
      prompt: obj.prompt,
      must_contain: obj.must_contain,
      must_not_contain: obj.must_not_contain,
      api_check: obj.api_check,
      timeout_ms: Number(obj.timeout_ms || 120000),
      reset_before: obj.reset_before === "1" || obj.reset_before === "true",
      notes: obj.notes ?? "",
    };
  });
}

function ensureResultDirs() {
  mkdirSync(SCREENSHOTS_DIR, { recursive: true });
  mkdirSync(LOGS_DIR, { recursive: true });
  if (!existsSync(SUMMARY_CSV)) {
    writeFileSync(
      SUMMARY_CSV,
      "id,group,priority,status,duration_ms,reply_len,screenshot,log,api_check_pass,notes\n",
    );
  }
}
ensureResultDirs();

const allRows = parseCsv(readFileSync(CSV_PATH, "utf8"));
const onlySet = process.env.NERYA_CASES_ONLY
  ? new Set(process.env.NERYA_CASES_ONLY.split(",").map((s) => s.trim()))
  : null;
const filterRe = process.env.NERYA_CASES_FILTER ? new RegExp(process.env.NERYA_CASES_FILTER) : null;
// Priority gate, e.g. NERYA_CASES_PRIORITY='P0' or 'P0,P1' (case-insensitive).
const prioritySet = process.env.NERYA_CASES_PRIORITY
  ? new Set(process.env.NERYA_CASES_PRIORITY.split(",").map((s) => s.trim().toUpperCase()))
  : null;
const rows = allRows.filter((r) => {
  if (onlySet && !onlySet.has(r.id)) return false;
  if (filterRe && !filterRe.test(r.id)) return false;
  if (prioritySet && !prioritySet.has((r.priority || "").toUpperCase())) return false;
  return true;
});

function resetWorkspace(opts: { keepEvidence?: boolean } = {}): void {
  const repoRoot = resolve(__dirname, "..", "..", "..", "..");
  const script = join(repoRoot, "Nerya", "tools", "reset_workspace.py");
  const workspace =
    process.env.NERYA_WORKSPACE ?? join(resolve(__dirname, "..", ".."), ".nerya-test-workspace");
  const args = [script, "--workspace", workspace, "--clear-memory", "--sync-prompt-bundle", "--quiet"];
  // Parallel workers share one workspace+runtime. A mid-run wipe of
  // evolution/proposals, teams/, strategies/, agent_tasks/ or schedules
  // erases evidence that *sibling* cases still need for their api_check,
  // so per-case resets in parallel mode only clear agent memory state.
  if (opts.keepEvidence) args.push("--keep-evidence");
  const r = spawnSync("python", args, { stdio: "inherit" });
  if (r.status !== 0) {
    throw new Error(`reset_workspace.py exited ${r.status}`);
  }
}

type ApiGet = <T>(p: string) => Promise<T>;

type ScheduleRecord = {
  id?: string;
  kind?: string;
  target?: string;
  session_kind?: string;
  payload?: Record<string, unknown>;
};

function chatSessionIdFromPage(page: Page): string | null {
  try {
    const url = new URL(page.url());
    const match = url.pathname.match(/\/chat\/([^/?#]+)/);
    return match?.[1] ? decodeURIComponent(match[1]) : null;
  } catch {
    return null;
  }
}

function collectToolNames(turn: Record<string, unknown>): string[] {
  const names = new Set<string>();
  const visit = (value: unknown) => {
    if (Array.isArray(value)) {
      for (const item of value) visit(item);
      return;
    }
    if (!value || typeof value !== "object") return;
    const obj = value as Record<string, unknown>;
    const block = obj.block && typeof obj.block === "object" ? (obj.block as Record<string, unknown>) : obj;
    const kind = String(block.kind || block.type || obj.kind || "");
    const action = block.action || block.name || obj.action || obj.name;
    if ((kind === "tool_use" || kind === "tool_result" || action) && typeof action === "string" && action) {
      if (action !== "send_message") names.add(action);
    }
    for (const key of ["blocks", "tool_trace", "actions"]) visit(obj[key]);
  };
  visit(turn.blocks);
  visit(turn.tool_trace);
  visit(turn.actions);
  return [...names].sort();
}

function collectSuccessfulToolNames(turn: Record<string, unknown>): string[] {
  const names = new Set<string>();
  const seen = new WeakSet<object>();
  const visit = (value: unknown) => {
    if (Array.isArray(value)) {
      for (const item of value) visit(item);
      return;
    }
    if (!value || typeof value !== "object") return;
    if (seen.has(value)) return;
    seen.add(value);
    const obj = value as Record<string, unknown>;
    const block = obj.block && typeof obj.block === "object" ? (obj.block as Record<string, unknown>) : obj;
    const kind = String(block.kind || block.type || obj.kind || "");
    const action = block.action || block.name || obj.action || obj.name;
    if (
      kind === "tool_result" &&
      block.ok === true &&
      typeof action === "string" &&
      action &&
      action !== "send_message"
    ) {
      names.add(action);
    }
    for (const key of ["blocks", "tool_trace", "actions"]) visit(obj[key]);
  };
  const direct = turn.successful_tool_names;
  if (Array.isArray(direct)) {
    for (const name of direct) {
      const text = String(name || "");
      if (text && text !== "send_message") names.add(text);
    }
  }
  visit(turn.blocks);
  visit(turn.tool_trace);
  visit(turn.actions);
  return [...names].sort();
}

function parseJsonishObject(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value !== "string") return null;
  const text = value.trim();
  if (!text) return null;
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // Fall through to bounded substring parsing for compacted result text.
  }
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start < 0 || end <= start) return null;
  try {
    const parsed = JSON.parse(text.slice(start, end + 1));
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    return null;
  }
  return null;
}

function semanticToolResultStatus(payload: Record<string, unknown> | null, action = ""): string {
  const risk = payload?.risk_decision;
  const riskObj = risk && typeof risk === "object" && !Array.isArray(risk)
    ? (risk as Record<string, unknown>)
    : {};
  const decision = String(riskObj.decision || "").trim().toLowerCase();
  if (action === "risk_check" || action === "trade_intent_submit") {
    if (decision === "reject") return "rejected";
    if (decision) return decision;
  }
  const topStatus = String(payload?.status || "").trim().toLowerCase();
  if (topStatus) return topStatus;
  if (decision === "reject") return "rejected";
  if (decision) return decision;
  return payload ? "ok" : "";
}

function collectToolResultStatuses(turn: Record<string, unknown>): { action: string; status: string }[] {
  const rows: { action: string; status: string }[] = [];
  const seen = new WeakSet<object>();
  const visit = (value: unknown) => {
    if (Array.isArray(value)) {
      for (const item of value) visit(item);
      return;
    }
    if (!value || typeof value !== "object") return;
    if (seen.has(value)) return;
    seen.add(value);
    const obj = value as Record<string, unknown>;
    const block = obj.block && typeof obj.block === "object" ? (obj.block as Record<string, unknown>) : obj;
    const kind = String(block.kind || block.type || obj.kind || "");
    const action = block.action || block.name || obj.action || obj.name;
    if (
      kind === "tool_result" &&
      block.ok === true &&
      typeof action === "string" &&
      action &&
      action !== "send_message"
    ) {
      const payload =
        parseJsonishObject(block.result) ??
        parseJsonishObject(block.payload) ??
        parseJsonishObject(obj.result) ??
        parseJsonishObject(obj.payload);
      const status = semanticToolResultStatus(payload, action);
      if (status) rows.push({ action, status });
    }
    for (const key of ["blocks", "tool_trace", "actions"]) visit(obj[key]);
  };
  visit(turn.blocks);
  visit(turn.tool_trace);
  visit(turn.actions);
  return rows;
}

function collectProposalIds(value: unknown): string[] {
  const ids = new Set<string>();
  const seen = new WeakSet<object>();
  let visited = 0;
  const visit = (node: unknown) => {
    if (visited++ > 5000 || ids.size >= 100) return;
    if (typeof node === "string") {
      const matches = node.match(/\bprp_[a-z0-9]{6,}\b/gi) ?? [];
      for (const match of matches) ids.add(match);
      return;
    }
    if (!node || typeof node !== "object") return;
    if (seen.has(node)) return;
    seen.add(node);
    if (Array.isArray(node)) {
      for (const item of node) visit(item);
      return;
    }
    const obj = node as Record<string, unknown>;
    for (const [key, child] of Object.entries(obj)) {
      if (key.toLowerCase().includes("proposal") || typeof child === "string") {
        visit(child);
      } else if (key === "data" || key === "content" || key === "artifact_index" || key === "tool_trace" || key === "blocks") {
        visit(child);
      }
    }
  };
  visit(value);
  return [...ids].sort();
}

function scheduleGuardText(schedule: ScheduleRecord): string {
  const payload = schedule.payload && typeof schedule.payload === "object" ? schedule.payload : {};
  const parts = [
    schedule.id,
    schedule.kind,
    schedule.target,
    schedule.session_kind,
    ...Object.values(payload).filter((v) => ["string", "number", "boolean"].includes(typeof v)),
  ];
  return parts.map((v) => String(v ?? "")).join("\n");
}

function isRecursiveSchedule(schedule: ScheduleRecord): boolean {
  const compact = scheduleGuardText(schedule).toLowerCase().replace(/\s+/g, "");
  const createsSchedule =
    compact.includes("task_create") ||
    compact.includes("create_task") ||
    compact.includes("createschedule") ||
    compact.includes("createanewschedule") ||
    compact.includes("createnewtask") ||
    compact.includes("创建schedule") ||
    compact.includes("创建一个schedule") ||
    compact.includes("创建新的schedule") ||
    compact.includes("创建定时任务") ||
    compact.includes("创建一个定时任务") ||
    compact.includes("创建新的定时任务") ||
    compact.includes("创建调度") ||
    compact.includes("创建任务") ||
    compact.includes("新建schedule") ||
    compact.includes("新建定时任务");
  const repeatsItself =
    compact.includes("eachtick") ||
    compact.includes("everytick") ||
    compact.includes("oneachrun") ||
    compact.includes("everyrun") ||
    compact.includes("selfreplicat") ||
    compact.includes("forkbomb") ||
    compact.includes("recursive") ||
    compact.includes("clone") ||
    compact.includes("sameprompt") ||
    compact.includes("每tick") ||
    compact.includes("每个tick") ||
    compact.includes("每次tick") ||
    compact.includes("每次运行") ||
    compact.includes("每次执行") ||
    compact.includes("每次触发") ||
    compact.includes("自复制") ||
    compact.includes("复制自己") ||
    compact.includes("递归") ||
    compact.includes("无限") ||
    compact.includes("完全一致");
  return String(schedule.session_kind || "").toLowerCase() === "agent" && createsSchedule && repeatsItself;
}

async function readLatestTurnEvidence(
  page: Page,
  api: { get: ApiGet },
): Promise<Record<string, unknown>> {
  const sessionId = chatSessionIdFromPage(page);
  if (!sessionId) return { ok: false, error: "no chat session id in url" };
  const transcript = await api.get<{
    ok?: boolean;
    messages?: Array<{ role?: string; turn_id?: string; turn?: Record<string, unknown> | null }>;
  }>(`/agent/session/transcript?session_id=${encodeURIComponent(sessionId)}&full=1`);
  const assistantMessages = (transcript.messages ?? []).filter((m) => m.role === "assistant");
  const latest = assistantMessages[assistantMessages.length - 1];
  const turn = latest?.turn && typeof latest.turn === "object" ? latest.turn : {};
  const artifactIndex =
    turn.artifact_index && typeof turn.artifact_index === "object"
      ? (turn.artifact_index as Record<string, unknown>)
      : {};
  const verifierOutcome =
    turn.verifier_outcome && typeof turn.verifier_outcome === "object"
      ? (turn.verifier_outcome as Record<string, unknown>)
      : {};
  const executionState =
    turn.execution_state && typeof turn.execution_state === "object"
      ? (turn.execution_state as Record<string, unknown>)
      : {};
  const executionCounters =
    executionState.counters && typeof executionState.counters === "object"
      ? (executionState.counters as Record<string, unknown>)
      : {};
  return {
    ok: Boolean(transcript.ok),
    session_id: sessionId,
    turn_id: String(turn.turn_id || latest?.turn_id || ""),
    stopped_reason: turn.stopped_reason ?? null,
    transition_reason: turn.transition_reason ?? null,
    verifier_transition_label: verifierOutcome.transition_label ?? null,
    verifier_trusted: verifierOutcome.trusted ?? null,
    verifier_hard_status: verifierOutcome.hard_status ?? null,
    execution_state_counters: executionCounters,
    budget: turn.budget ?? null,
    tool_names: collectToolNames(turn),
    successful_tool_names: collectSuccessfulToolNames(turn),
    tool_result_statuses: collectToolResultStatuses(turn),
    proposal_ids: collectProposalIds({
      artifact_index: artifactIndex,
      blocks: turn.blocks,
      tool_trace: turn.tool_trace,
      actions: turn.actions,
    }),
    artifact_index_keys: Object.keys(artifactIndex).sort(),
  };
}

function objectField(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function splitCheckValues(value: string | undefined): string[] {
  return String(value || "")
    .split(/[|,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function proposalAfterText(summary: unknown, detail: unknown): string {
  const summaryObj = objectField(summary);
  const detailObj = objectField(detail);
  const proposalPath = String(detailObj.path ?? summaryObj.path ?? "").trim();
  const target = String(detailObj.target ?? summaryObj.target ?? "").trim();
  if (!proposalPath || !target || target.includes("..")) return "";
  const afterPath = join(
    proposalPath,
    "after",
    ...target.split(/[\\/]+/).filter(Boolean),
  );
  if (!existsSync(afterPath)) return "";
  return readFileSync(afterPath, "utf8");
}

function proposalAfterHasPath(afterText: string, dottedPath: string): boolean {
  const parts = dottedPath.split(".").map((part) => part.trim()).filter(Boolean);
  if (!parts.length || !afterText) return false;
  return parts.every((part) =>
    new RegExp("(^|\\n)\\s*" + escapeRegExp(part) + "\\s*:", "i").test(afterText),
  );
}

function turnStabilityFailures(evidence: Record<string, unknown>): string[] {
  const failures: string[] = [];
  if (!evidence.ok) {
    failures.push("turn evidence could not be loaded");
    return failures;
  }
  const stoppedReason = String(evidence.stopped_reason ?? "");
  const transitionReason = String(evidence.transition_reason ?? "");
  const budget = objectField(evidence.budget);
  const aborted = budget.aborted === true;
  const abortReason = String(budget.abort_reason ?? "");
  const unstableReasons = new Set([
    "cancelled",
    "max_iterations",
    "max_tool_calls",
    "repeated_tool_call",
    "timeout",
    "timeout_before_tool_call",
    "timeout_during_llm_call",
    "interrupted_max_tokens",
    "required_action_provider_exhausted",
  ]);
  for (const reason of [stoppedReason, transitionReason, abortReason]) {
    if (reason && unstableReasons.has(reason)) {
      failures.push("unstable turn termination: " + reason);
    }
  }
  if (aborted) {
    failures.push("turn budget marked aborted");
  }
  return [...new Set(failures)];
}

function replyQualityFailures(reply: string): string[] {
  const failures: string[] = [];
  const text = reply.trim();
  if (!text) {
    failures.push("assistant reply is empty");
    return failures;
  }
  const terminalToolGapPatterns = [
    /无法继续执行后续工具/i,
    /未执行的后续工具/i,
    /remaining time .*native tool/i,
    /insufficient .*native tool/i,
    /skipped late native tool/i,
  ];
  if (terminalToolGapPatterns.some((pattern) => pattern.test(text))) {
    failures.push("assistant reply is a terminal native-tool gap notice");
  }
  const internalLeakMarkers = [
    "AgentTeam evidence",
    "team_run_id",
    "duplicate_of_team_run_id",
    "completed_with_failures",
  ];
  const lowerText = text.toLowerCase();
  for (const marker of internalLeakMarkers) {
    if (lowerText.includes(marker.toLowerCase())) {
      failures.push(`assistant reply exposes internal runtime marker: ${marker}`);
    }
  }
  const internalDumpPatterns = [
    /data coverage:/i,
    /tools used:/i,
    /tool errors:/i,
    /\bskill:\s*[a-z_]+\s*;\s*action:/i,
    /this role did not produce a complete conclusion/i,
    /"executive_summary"/i,
    /"key_metrics_table"/i,
    /"status"\s*:/i,
  ];
  if (internalDumpPatterns.some((pattern) => pattern.test(text))) {
    failures.push("assistant reply exposes internal team/schema dump");
  }
  return [...new Set(failures)];
}

/**
 * Coarse user-visible language detector for reply-text assertions.
 * Code blocks, inline code, and URLs are stripped first so tickers,
 * identifiers, and links do not skew the ratio; CJK characters are then
 * compared against latin words. Intentionally binary (chinese/english) —
 * that is the only split the test matrix asserts on.
 */
function replyLanguageOf(text: string): "chinese" | "english" | "unknown" {
  const stripped = text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/https?:\/\/\S+/g, " ");
  const cjk = (stripped.match(/[\u4e00-\u9fff]/g) ?? []).length;
  const latinWords = (stripped.match(/[A-Za-z]{2,}/g) ?? []).length;
  if (cjk === 0 && latinWords === 0) return "unknown";
  return cjk > latinWords ? "chinese" : "english";
}

async function runApiChecks(
  api: { get: ApiGet },
  spec: string,
  turnEvidence?: Record<string, unknown> | null,
  replyText?: string,
): Promise<{ ok: boolean; details: string[] }> {
  if (!spec) return { ok: true, details: ["(no api_check)"] };
  const ctx = parseApiCheckSpec(spec);
  const tokens = apiCheckTokens(spec);
  if (ctx.cancel_inflight === "true") {
    return { ok: true, details: ["cancel_inflight checked by UI flow"] };
  }
  const details: string[] = [];
  let ok = true;

  const executionModeNeedles = (mode: string): string[] => {
    const want = mode.toLowerCase().replace(/-/g, "_");
    if (want === "agent" || want === "agent_task") return ["agent", "agent_task"];
    if (want === "agent_team" || want === "team") return ["agent_team", "team"];
    return [want];
  };

  const hasExecutionMode = (haystack: string, mode: string): boolean => {
    const normalized = executionModeNeedles(mode);
    if (normalized.some((candidate) => haystack.includes("agent_task") && candidate === "agent_task")) {
      return true;
    }
    if (normalized.includes("agent") && haystack.includes("class: **agent**")) {
      return true;
    }
    if (normalized.includes("agent_team") && haystack.includes("agentteam")) {
      return true;
    }
    return normalized.some((candidate) => {
      const quoted = '"execution_mode":"' + candidate + '"';
      const quotedSpaced = '"execution_mode": "' + candidate + '"';
      const yaml = "execution_mode: " + candidate;
      return haystack.includes(quoted) || haystack.includes(quotedSpaced) || haystack.includes(yaml);
    });
  };

  const proposalText = (proposal: unknown): string => JSON.stringify(proposal ?? {}).toLowerCase();
  const evidenceProposalIds = Array.isArray(turnEvidence?.proposal_ids)
    ? (turnEvidence?.proposal_ids ?? []).map((id) => String(id)).filter(Boolean)
    : [];
  const evidenceToolNames = Array.isArray(turnEvidence?.tool_names)
    ? (turnEvidence?.tool_names ?? []).map((name) => String(name)).filter(Boolean)
    : [];
  const evidenceSuccessfulToolNames = Array.isArray(turnEvidence?.successful_tool_names)
    ? (turnEvidence?.successful_tool_names ?? []).map((name) => String(name)).filter(Boolean)
    : [];
  const evidenceToolResultStatuses = Array.isArray(turnEvidence?.tool_result_statuses)
    ? (turnEvidence?.tool_result_statuses ?? []).map((item) => {
        const row = item && typeof item === "object" ? (item as Record<string, unknown>) : {};
        return {
          action: String(row.action || ""),
          status: String(row.status || "").toLowerCase(),
        };
      }).filter((item) => item.action && item.status)
    : [];

  if (ctx.tool_used) {
    const requiredTools = ctx.tool_used
      .split(/[|,]/)
      .map((name) => name.trim())
      .filter(Boolean);
    const missing = requiredTools.filter((name) => !evidenceSuccessfulToolNames.includes(name));
    if (missing.length) {
      ok = false;
      details.push(
        "tool_used missing: "
        + missing.join(",")
        + " (successful_seen="
        + (evidenceSuccessfulToolNames.join(",") || "none")
        + "; all_seen="
        + (evidenceToolNames.join(",") || "none")
        + ")",
      );
    } else {
      details.push("tool_used ok: " + requiredTools.join(",") + " (successful)");
    }
  }

  if (ctx.tool_result_status) {
    const requiredStatuses = ctx.tool_result_status
      .split(/[|,]/)
      .map((item) => item.trim())
      .filter(Boolean);
    for (const requiredStatus of requiredStatuses) {
      const dot = requiredStatus.lastIndexOf(".");
      const tool = dot > 0 ? requiredStatus.slice(0, dot) : requiredStatus;
      const status = dot > 0 ? requiredStatus.slice(dot + 1).toLowerCase() : "";
      const hit = evidenceToolResultStatuses.some(
        (item) => item.action === tool && (!status || item.status === status),
      );
      if (!hit) {
        ok = false;
        details.push(
          "tool_result_status missing: "
          + requiredStatus
          + " (seen="
          + (evidenceToolResultStatuses
              .map((item) => item.action + "." + item.status)
              .join(",") || "none")
          + ")",
        );
      } else {
        details.push("tool_result_status ok: " + requiredStatus);
      }
    }
  }

  if (ctx.tool_not_used) {
    const forbiddenTools = ctx.tool_not_used
      .split(/[|,]/)
      .map((name) => name.trim())
      .filter(Boolean);
    const seenForbidden = forbiddenTools.filter((name) => evidenceToolNames.includes(name));
    if (seenForbidden.length) {
      ok = false;
      details.push("tool_not_used violated: " + seenForbidden.join(","));
    } else {
      details.push("tool_not_used ok: " + forbiddenTools.join(","));
    }
  }

  if ("strategy_proposal_kind" in ctx) {
    const kind = ctx.strategy_proposal_kind || "strategy_package_proposal";
    const expectsBacktest = ctx.strategy_backtest !== "false";
    const preferred: { summary: { id: string; kind?: string; metadata?: unknown; ts?: string }; detail: unknown }[] = [];
    for (const id of evidenceProposalIds) {
      try {
        const detail = await api.get<Record<string, unknown>>("/evolution/proposals/" + encodeURIComponent(id));
        const detailKind = String(detail.kind ?? "");
        if (detailKind && detailKind.toLowerCase() !== kind.toLowerCase()) continue;
        preferred.push({
          summary: {
            id,
            kind: detailKind || kind,
            metadata: detail.metadata,
            ts: typeof detail.ts === "string" ? detail.ts : undefined,
          },
          detail,
        });
      } catch {
        // Fall back to list-based discovery below; the failure is logged in details.
      }
    }
    const r = await api.get<{ proposals: { id: string; kind: string; metadata?: unknown; ts?: string }[] }>(
      "/evolution/proposals?kind=" + kind + "&limit=20",
    );
    const list = r.proposals ?? [];
    const needles = tokens.slice(1).filter((t) => !t.includes("=")).map((t) => t.toLowerCase());
    const wants = tokens.filter((t) => t.startsWith("main_py_contains=")).map((t) => t.split("=", 2)[1]);
    const seenProposalIds = new Set(preferred.map((p) => p.summary.id));
    const enrichedFromList = await Promise.all(
      list.filter((p) => !seenProposalIds.has(p.id)).map(async (p) => {
        try {
          const detail = await api.get<{ proposal?: unknown }>("/evolution/proposals/" + p.id);
          return { summary: p, detail: detail.proposal ?? detail };
        } catch {
          return { summary: p, detail: null };
        }
      }),
    );
    const enriched = [...preferred, ...enrichedFromList];
    details.push(
      "strategy proposal candidates: evidence_ids="
      + (evidenceProposalIds.join(",") || "none")
      + "; list_count="
      + list.length,
    );
    const matches = needles.length
      ? enriched.filter((p) => {
        const text = proposalText(p.summary) + proposalText(p.detail);
        return needles.every((needle) => text.includes(needle));
      })
      : enriched;
    matches.sort((a, b) => {
      const at = Date.parse(String((a.summary as { ts?: string }).ts || ""));
      const bt = Date.parse(String((b.summary as { ts?: string }).ts || ""));
      return (Number.isFinite(bt) ? bt : 0) - (Number.isFinite(at) ? at : 0);
    });
    const evaluateMatch = (match: (typeof matches)[number]) => {
      const candidateDetails: string[] = ["strategy proposal: " + match.summary.id];
      let candidateOk = true;
      const fullProposalText = proposalText(match.summary) + proposalText(match.detail);
      if (ctx.execution_mode) {
        const want = ctx.execution_mode.toLowerCase();
        if (!hasExecutionMode(fullProposalText, want)) {
          candidateOk = false;
          candidateDetails.push("execution_mode != " + want);
        } else {
          candidateDetails.push("execution_mode = " + want + " ok");
        }
      }
      if (wants.length) {
        const proposal = (match.detail && typeof match.detail === "object" ? match.detail : {}) as {
          files?: Record<string, string>;
        };
        const files = proposal.files ?? {};
        const pyEntry = Object.entries(files).find(([k]) => /(^|\/)main\.py$/.test(k));
        const py = pyEntry?.[1] ?? "";
        for (const w of wants) {
          if (!py.toLowerCase().includes(w.toLowerCase())) {
            candidateOk = false;
            candidateDetails.push("main.py missing: " + w);
          } else {
            candidateDetails.push("main.py contains: " + w + " ok");
          }
        }
      }
      return { candidateOk, candidateDetails };
    };
    const evaluated = matches.map((match) => evaluateMatch(match));
    const selected = evaluated.find((entry) => entry.candidateOk) ?? evaluated[0];
    if (!selected) {
      ok = false;
      details.push("strategy proposal not found (kind=" + kind + ", needle=" + (needles.join(",") || "none") + ")");
    } else {
      ok = ok && selected.candidateOk;
      details.push(...selected.candidateDetails);
    }
    if (expectsBacktest) {
      const transition = String(turnEvidence?.transition_reason ?? "");
      const backtestTransitions = new Set([
        "strategy_backtest_finalized",
        "strategy_backtest_data_gap_finalized",
      ]);
      if (!evidenceToolNames.includes("strategy_backtest")) {
        ok = false;
        details.push("strategy_backtest tool missing from turn evidence");
      }
      if (!backtestTransitions.has(transition)) {
        ok = false;
        details.push("strategy_backtest not finalized: transition=" + (transition || "(missing)"));
      } else {
        details.push("strategy_backtest finalized: " + transition);
      }
    }
  }

  if (ctx.proposal_kind) {
    const preferred: { summary: { id: string; kind?: string; ts?: string }; detail: unknown }[] = [];
    for (const id of evidenceProposalIds) {
      try {
        const detail = await api.get<Record<string, unknown>>("/evolution/proposals/" + encodeURIComponent(id));
        const detailKind = String(detail.kind ?? "");
        if (detailKind && detailKind.toLowerCase() !== ctx.proposal_kind.toLowerCase()) continue;
        preferred.push({
          summary: {
            id,
            kind: detailKind || ctx.proposal_kind,
            ts: typeof detail.ts === "string" ? detail.ts : undefined,
          },
          detail,
        });
      } catch {
        details.push("proposal detail fetch failed for evidence id: " + id);
      }
    }
    const r = await api.get<{ proposals: { id: string }[] }>(
      "/evolution/proposals?kind=" + ctx.proposal_kind + "&limit=20",
    );
    const list = r.proposals ?? [];
    const seenProposalIds = new Set(preferred.map((p) => p.summary.id));
    const enrichedFromList = await Promise.all(
      list.filter((p) => !seenProposalIds.has(p.id)).map(async (p) => {
        try {
          const detail = await api.get<Record<string, unknown>>("/evolution/proposals/" + encodeURIComponent(p.id));
          return { summary: p, detail };
        } catch {
          return { summary: p, detail: null };
        }
      }),
    );
    const enriched = [...preferred, ...enrichedFromList];
    enriched.sort((a, b) => {
      const at = Date.parse(String((a.summary as { ts?: string }).ts || ""));
      const bt = Date.parse(String((b.summary as { ts?: string }).ts || ""));
      return (Number.isFinite(bt) ? bt : 0) - (Number.isFinite(at) ? at : 0);
    });
    const found = enriched.length > 0;
    if (!found) { ok = false; details.push("no proposal of kind=" + ctx.proposal_kind); }
    else {
      details.push(
        "proposal kind="
        + ctx.proposal_kind
        + " ok"
        + " (evidence_ids="
        + (evidenceProposalIds.join(",") || "none")
        + "; list_count="
        + list.length
        + ")",
      );
    }
    if (ctx.metadata_contains) {
      const hit = enriched.some((p) =>
        JSON.stringify(p).toLowerCase().includes(ctx.metadata_contains.toLowerCase()),
      );
      if (!hit) { ok = false; details.push("metadata missing: " + ctx.metadata_contains); }
      else { details.push("metadata contains: " + ctx.metadata_contains + " ok"); }
    }
    if (ctx.proposal_after_has) {
      const requiredAfterPaths = splitCheckValues(ctx.proposal_after_has);
      const matchesAfter = enriched.filter((p) => {
        const afterText = proposalAfterText(p.summary, p.detail);
        return requiredAfterPaths.every((path) => proposalAfterHasPath(afterText, path));
      });
      if (!matchesAfter.length) {
        ok = false;
        details.push(
          "proposal after missing path(s): "
          + requiredAfterPaths.join(",")
          + " (candidates="
          + (enriched.map((p) => p.summary.id).join(",") || "none")
          + ")",
        );
      } else {
        details.push(
          "proposal after has "
          + requiredAfterPaths.join(",")
          + ": "
          + matchesAfter[0].summary.id
          + " ok",
        );
      }
    }
  }

  if (ctx.account_created) {
    const r = await api.get<{ accounts: { id: string; venue?: string }[] }>("/accounts/list");
    const a = (r.accounts ?? []).find((x) => x.id === ctx.account_created);
    if (!a) { ok = false; details.push("account not found: " + ctx.account_created); }
    else {
      details.push("account exists: " + ctx.account_created + " ok");
      if (ctx.venue && a.venue !== ctx.venue) {
        ok = false; details.push("venue mismatch: " + a.venue + " vs " + ctx.venue);
      }
    }
  }

  if (ctx.account_matching) {
    const r = await api.get<{
      accounts: {
        id: string;
        venue?: string;
        mode?: string;
        profile?: { venue?: string; mode?: string };
        account?: { profile?: { venue?: string; mode?: string } };
      }[];
    }>("/accounts/list");
    const wantVenue = ctx.account_matching.toLowerCase();
    const wantMode = (ctx.mode || "").toLowerCase();
    const a = (r.accounts ?? []).find((x) => {
      const venue = String(
        x.venue || x.profile?.venue || x.account?.profile?.venue || "",
      ).toLowerCase();
      const mode = String(
        x.mode || x.profile?.mode || x.account?.profile?.mode || "",
      ).toLowerCase();
      if (venue !== wantVenue) return false;
      if (wantMode && mode !== wantMode) return false;
      return true;
    });
    if (!a) {
      ok = false;
      details.push(
        "account matching not found: venue=" + wantVenue + (wantMode ? " mode=" + wantMode : ""),
      );
    } else {
      details.push("account matching found: " + a.id + " ok");
    }
  }

  if (ctx.schedule_session_kind) {
    const r = await api.get<{ schedules: { session_kind?: string }[] }>("/triggers/schedules");
    const hit = (r.schedules ?? []).some((s) => s.session_kind === ctx.schedule_session_kind);
    if (!hit) { ok = false; details.push("no schedule with session_kind=" + ctx.schedule_session_kind); }
    else { details.push("schedule session_kind=" + ctx.schedule_session_kind + " ok"); }
  }

  if (ctx.recursive_schedule_absent === "true") {
    const r = await api.get<{ schedules: ScheduleRecord[] }>("/triggers/schedules");
    const hit = (r.schedules ?? []).find((s) => isRecursiveSchedule(s));
    if (hit) {
      ok = false;
      details.push("recursive schedule present: " + (hit.id || "(unnamed)"));
    } else {
      details.push("recursive schedule absent ok");
    }
  }

  if (ctx.team_template) {
    const r = await api.get<{ runs: { template?: string }[] }>("/teams/runs?limit=10");
    const hit = (r.runs ?? []).some((x) => (x.template ?? "").includes(ctx.team_template));
    if (!hit) { ok = false; details.push("no team run with template=" + ctx.team_template); }
    else { details.push("team_template=" + ctx.team_template + " ok"); }
  }

  if (ctx.team_run_exists === "true") {
    const r = await api.get<{ runs: { id?: string; template?: string; status?: string }[] }>("/teams/runs?limit=10");
    const hit = (r.runs ?? []).find((x) => x.id || x.template || x.status);
    if (!hit) { ok = false; details.push("no team run found"); }
    else { details.push("team_run_exists ok: " + (hit.id || hit.template || hit.status || "(run)")); }
  }

  if (ctx.team_roles_total) {
    const want = Math.max(1, Number.parseInt(ctx.team_roles_total, 10) || 1);
    const r = await api.get<{
      runs: {
        id?: string;
        template?: string;
        metrics?: { roles_total?: unknown };
      }[];
    }>("/teams/runs?limit=10");
    const hit = (r.runs ?? []).find((x) => Number(x.metrics?.roles_total) === want);
    if (!hit) { ok = false; details.push("no team run roles_total=" + want); }
    else { details.push("team roles_total=" + want + " ok"); }
  }

  if (ctx.team_output_language || ctx.team_analysis_language) {
    const r = await api.get<{
      runs: {
        id?: string;
        template?: string;
        metrics?: Record<string, unknown>;
      }[];
    }>("/teams/runs?limit=10");
    const runs = r.runs ?? [];
    const canonicalLanguage = (value: unknown): string => {
      const raw = String(value ?? "").trim().toLowerCase().replace("_", "-");
      if (!raw) return "";
      const aliases: Record<string, string[]> = {
        english: ["english", "en", "en-us", "en-gb"],
        chinese: ["chinese", "zh", "zh-cn", "zh-hans", "zh-hant", "中文", "mandarin"],
      };
      for (const [canonical, values] of Object.entries(aliases)) {
        if (values.includes(raw)) return canonical;
      }
      return raw;
    };
    const matchesLanguage = (value: unknown, want: string): boolean => {
      const actual = canonicalLanguage(value);
      const expected = canonicalLanguage(want);
      return Boolean(actual && expected && (actual === expected || actual.includes(expected)));
    };
    if (ctx.team_output_language) {
      const hit = runs.find((x) => matchesLanguage(x.metrics?.output_language, ctx.team_output_language));
      if (!hit) { ok = false; details.push("no team run output_language=" + ctx.team_output_language); }
      else { details.push("team output_language=" + ctx.team_output_language + " ok"); }
    }
    if (ctx.team_analysis_language) {
      const hit = runs.find((x) => matchesLanguage(x.metrics?.analysis_language, ctx.team_analysis_language));
      if (!hit) { ok = false; details.push("no team run analysis_language=" + ctx.team_analysis_language); }
      else { details.push("team analysis_language=" + ctx.team_analysis_language + " ok"); }
    }
  }

  // Unlike team_output_language (which reads run *metrics*, i.e. what the
  // run was configured to do), reply_language asserts the language of the
  // text the operator actually saw — the gap that let a Chinese fallback
  // dump pass a "final report in English" case.
  if (ctx.reply_language) {
    const want = ctx.reply_language.trim().toLowerCase();
    if (!replyText || !replyText.trim()) {
      ok = false;
      details.push("reply_language=" + want + " failed: no reply text captured");
    } else {
      const got = replyLanguageOf(replyText);
      if (got !== want) {
        ok = false;
        details.push("reply language is " + got + ", expected " + want);
      } else {
        details.push("reply_language=" + want + " ok");
      }
    }
  }

  if (ctx.wallet_provider_has) {
    const r = await api.get<{ providers: { id: string }[] }>("/wallet/providers");
    const hit = (r.providers ?? []).some((p) => p.id === ctx.wallet_provider_has);
    if (!hit) { ok = false; details.push("wallet provider missing: " + ctx.wallet_provider_has); }
    else { details.push("wallet provider " + ctx.wallet_provider_has + " ok"); }
  }

  if (ctx.exchange_provider_has) {
    const r = await api.get<{ providers: { id: string; aliases?: string[] }[] }>("/exchanges/providers");
    const want = ctx.exchange_provider_has.toLowerCase();
    const hit = (r.providers ?? []).some((p) => {
      const id = String(p.id || "").toLowerCase();
      const aliases = (p.aliases ?? []).map((a) => String(a).toLowerCase());
      return id === want || aliases.includes(want);
    });
    if (!hit) { ok = false; details.push("exchange provider missing: " + ctx.exchange_provider_has); }
    else { details.push("exchange provider " + ctx.exchange_provider_has + " ok"); }
  }

  if (ctx.data_source_status_min) {
    const min = Math.max(1, Number.parseInt(ctx.data_source_status_min, 10) || 1);
    const r = await api.get<{
      data?: { sources?: { source_id?: string }[]; total?: number };
      sources?: { source_id?: string }[];
    }>("/data-sources/status");
    const sources = r.data?.sources ?? r.sources ?? [];
    const total = Number(r.data?.total ?? sources.length);
    if (total < min) {
      ok = false;
      details.push("data source status count " + total + " < " + min);
    } else {
      details.push("data source status count " + total + " ok");
    }
  }

  if (ctx.data_source_event_source) {
    const want = ctx.data_source_event_source.toLowerCase();
    const r = await api.get<{
      data?: { events?: { source_id?: string }[]; count?: number };
      events?: { source_id?: string }[];
    }>("/data-sources/events?limit=50");
    const events = r.data?.events ?? r.events ?? [];
    const hit = events.some((event) => String(event.source_id || "").toLowerCase() === want);
    if (!hit) { ok = false; details.push("data source event missing: " + ctx.data_source_event_source); }
    else { details.push("data source event " + ctx.data_source_event_source + " ok"); }
  }

  if (ctx.financial_datasets_status) {
    const r = await api.get<{ ready?: boolean; sources?: unknown[] }>(
      "/data/financial_datasets/status",
    );
    if (typeof r.ready !== "boolean") {
      ok = false;
      details.push("financial datasets status missing ready boolean");
    } else {
      const expected = ctx.financial_datasets_status.toLowerCase();
      if (expected === "true" || expected === "false") {
        const want = expected === "true";
        if (r.ready !== want) {
          ok = false;
          details.push("financial datasets ready=" + String(r.ready) + " expected=" + expected);
        }
      }
      details.push("financial datasets ready=" + String(r.ready));
    }
  }

  if (ctx.gateway_platform_has) {
    const r = await api.get<{ platforms: { id: string }[] }>("/gateway/platforms");
    const hit = (r.platforms ?? []).some((p) => p.id === ctx.gateway_platform_has);
    if (!hit) { ok = false; details.push("gateway platform missing: " + ctx.gateway_platform_has); }
    else { details.push("gateway platform " + ctx.gateway_platform_has + " ok"); }
  }

  if (ctx.credential_schema_okx_has) {
    try {
      const r = await fetch(
        (process.env.NERYA_API ?? "http://127.0.0.1:18318") + "/exchanges/credential_schema",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ venue: "okx" }),
        },
      );
      const j = (await r.json()) as { schema?: { fields?: { name: string }[] } };
      const names = (j.schema?.fields ?? []).map((f) => f.name.toLowerCase());
      if (!names.some((n) => n.includes(ctx.credential_schema_okx_has.toLowerCase()))) {
        ok = false; details.push("OKX schema missing field: " + ctx.credential_schema_okx_has);
      } else {
        details.push("OKX schema has " + ctx.credential_schema_okx_has + " ok");
      }
    } catch (e) {
      ok = false; details.push("credential_schema error: " + String(e));
    }
  }

  if (ctx.task_created === "true") {
    const r = await api.get<{
      tasks?: { id?: string; task_id?: string }[];
      data?: { tasks?: { id?: string; task_id?: string }[] };
    }>("/agent/tasks?limit=10");
    const tasks = r.tasks ?? r.data?.tasks ?? [];
    if (!tasks.length) { ok = false; details.push("no agent task created"); }
    else { details.push("agent task created: " + (tasks[0]?.id ?? tasks[0]?.task_id ?? "?") + " ok"); }
  }

  return { ok, details };
}

function hasApiCheck(row: Row, key: string, value = "true"): boolean {
  const ctx = parseApiCheckSpec(row.api_check);
  return Object.prototype.hasOwnProperty.call(ctx, key) && (value === "" || ctx[key] === value);
}

function evidenceContractFromApiCheck(spec: string): Record<string, unknown> | undefined {
  const ctx = parseApiCheckSpec(spec);
  const tokens = apiCheckTokens(spec);
  const requiredArtifacts: Record<string, unknown>[] = [];
  if (ctx.team_run_exists === "true" || ctx.team_template) {
    const teamArtifact: Record<string, string> = {
      kind: "team_run",
      tool: "team_run",
      source: "csv.api_check",
    };
    if (ctx.team_output_language) {
      teamArtifact.output_language = ctx.team_output_language;
    }
    if (ctx.team_analysis_language) {
      teamArtifact.analysis_language = ctx.team_analysis_language;
    }
    if (ctx.team_template) {
      teamArtifact.team_template = ctx.team_template;
    }
    requiredArtifacts.push(teamArtifact);
  }
  if ("strategy_proposal_kind" in ctx) {
    const strategyKindTokenIndex = tokens.findIndex((token) =>
      token.startsWith("strategy_proposal_kind="),
    );
    const subjectTokens = strategyKindTokenIndex >= 0
      ? tokens.slice(strategyKindTokenIndex + 1).filter((token) => !token.includes("="))
      : [];
    const proposalArtifact: Record<string, string> = {
      kind: ctx.strategy_proposal_kind || "strategy_package_proposal",
      tool: "strategy_generate_proposal",
      source: "csv.api_check",
    };
    if (ctx.execution_mode) {
      proposalArtifact.execution_mode = ctx.execution_mode;
    }
    if (subjectTokens.length) {
      proposalArtifact.subject = subjectTokens.join(" ");
    }
    requiredArtifacts.push(proposalArtifact);
    if (ctx.strategy_backtest !== "false") {
      requiredArtifacts.push({
        kind: "strategy_backtest",
        tool: "strategy_backtest",
        source: "csv.api_check",
      });
    }
  }
  if ("proposal_kind" in ctx) {
    const kind = ctx.proposal_kind || "proposal";
    const proposalToolByKind: Record<string, string> = {
      core_config_patch: "evolve_core_config_patch",
      learning_update: "evolve_reflect",
      provider_proposal: "evolve_provider_proposal",
      skill_proposal: "evolve_skill_proposal",
    };
    const tool = proposalToolByKind[kind];
    if (tool) {
      const requiredArtifact: Record<string, string> = {
        kind,
        tool,
        source: "csv.api_check",
      };
      if (ctx.proposal_after_has) {
        requiredArtifact.after_has = ctx.proposal_after_has;
      }
      if (ctx.metadata_contains) {
        requiredArtifact.metadata_contains = ctx.metadata_contains;
        if (!requiredArtifact.subject) {
          requiredArtifact.subject = ctx.metadata_contains;
        }
      }
      requiredArtifacts.push(requiredArtifact);
    }
  }
  if (ctx.tool_used) {
    const requiredTools = ctx.tool_used
      .split(/[|,]/)
      .map((name) => name.trim())
      .filter(Boolean);
    for (const tool of requiredTools) {
      requiredArtifacts.push({
        kind: "tool_result",
        tool,
        source: "csv.api_check",
        defer_initial_tool_choice: true,
      });
    }
  }
  if (ctx.schedule_session_kind) {
    requiredArtifacts.push({
      kind: "tool_result",
      tool: "task_create",
      source: "csv.api_check",
      defer_initial_tool_choice: true,
    });
  }
  if (!requiredArtifacts.length) return undefined;
  return { required_artifacts: requiredArtifacts };
}

function parseApiCheckSpec(spec: string): Record<string, string> {
  const colonValueKeys = new Set(["data_source_event_source"]);
  const ctx: Record<string, string> = {};
  let activeKey = "";
  for (const part of apiCheckTokens(spec)) {
    const eq = part.indexOf("=");
    const maybeKey = eq >= 0 ? part.slice(0, eq).trim() : "";
    if (eq >= 0 && /^[A-Za-z_][A-Za-z0-9_]*$/.test(maybeKey)) {
      activeKey = maybeKey;
      ctx[activeKey] = part.slice(eq + 1).trim();
      continue;
    }
    if (activeKey && colonValueKeys.has(activeKey)) {
      ctx[activeKey] = `${ctx[activeKey]}:${part}`;
      continue;
    }
    activeKey = "";
  }
  return ctx;
}

function apiCheckTokens(spec: string): string[] {
  return spec.split(":").map((s) => s.trim()).filter(Boolean);
}

async function sendAndCancel(page: Page, prompt: string): Promise<string> {
  const ta = page.locator("textarea").first();
  await ta.click();
  await ta.fill(prompt);
  await page.getByRole("button", { name: /^(Send|发送)$/ }).click();
  const composer = ta.locator("..");
  const cancelBtn = composer.locator('button[aria-label*="Cancel in-flight"]');
  await expect(cancelBtn).toBeVisible({ timeout: 20_000 });
  await cancelBtn.click();
  await expect(cancelBtn).toBeHidden({ timeout: 60_000 });
  await expect(ta).toBeEnabled({ timeout: 60_000 });
  const last = page.locator('[data-turn-role="assistant"]').last();
  await expect(last).toHaveAttribute("data-turn-loading", "false", { timeout: 60_000 });
  const reply = last.locator('[data-turn-section="reply"]').first();
  if (await reply.count()) {
    await expect(reply).toBeVisible({ timeout: 5_000 });
    return (await reply.innerText()).trim();
  }
  const error = last.locator('[data-turn-section="error"]').first();
  if (await error.count()) {
    return (await error.innerText()).trim();
  }
  return (await last.innerText()).trim();
}

test.describe("CSV-driven prompt tests", () => {
  for (const row of rows) {
    test(row.id + " [" + row.group + "/" + row.priority + "] - " + (row.notes || row.prompt.slice(0, 60)), async ({
      page,
      openChat,
      chatSend,
      api,
    }, testInfo) => {
      const cancelWaitMs = Number(process.env.NERYA_E2E_CANCEL_WAIT_MS ?? 120_000);
      test.setTimeout(row.timeout_ms + Math.max(0, cancelWaitMs) + 30_000);
      const started = Date.now();
      const screenshot = join(SCREENSHOTS_DIR, row.id + ".png");
      const logFile = join(LOGS_DIR, row.id + ".jsonl");
      const replyFile = join(LOGS_DIR, row.id + ".reply.txt");

      function appendLog(entry: Record<string, unknown>) {
        mkdirSync(dirname(logFile), { recursive: true });
        appendFileSync(logFile, JSON.stringify({ ts: Date.now(), ...entry }) + "\n");
      }

      // Playwright clears outputDir at suite start, so make sure our
      // artifact directories exist before each test writes anything.
      ensureResultDirs();
      try {
        appendLog({ event: "case.start", id: row.id, prompt: row.prompt });
        // CSV rows marked reset_before=1 require a clean agent memory/state
        // slate; otherwise earlier preference-memory cases can poison later
        // strategy cases. Allow opt-out for one-off local debugging only.
        if (row.reset_before && process.env.NERYA_RESET_PER_CASE !== "0") {
          appendLog({ event: "workspace.reset.start" });
          try {
            resetWorkspace({ keepEvidence: testInfo.config.workers > 1 });
            appendLog({ event: "workspace.reset.done" });
          } catch (e) {
            appendLog({
              event: "workspace.reset.failed",
              error: String((e as Error)?.message ?? e),
            });
          }
        }

        const maxWallSeconds = Math.max(30, Math.floor((row.timeout_ms - 15_000) / 1000));
        const evidenceContract = evidenceContractFromApiCheck(row.api_check);
        await page.addInitScript(
          ([key, seconds, contract]) => {
            try {
              const raw = window.localStorage.getItem(key);
              const prev = raw ? JSON.parse(raw) : {};
              const next = { ...prev, max_wall_seconds: seconds };
              if (contract && typeof contract === "object") {
                next.evidence_contract = contract;
              } else {
                delete next.evidence_contract;
              }
              window.localStorage.setItem(
                key,
                JSON.stringify(next),
              );
            } catch {
              /* localStorage may be unavailable on about:blank; goto re-runs this */
            }
          },
          [CHAT_RUN_SETTINGS_KEY, maxWallSeconds, evidenceContract ?? null] as const,
        );
        await openChat();
        appendLog({
          event: "chat.opened",
          url: page.url(),
          max_wall_seconds: maxWallSeconds,
          evidence_contract: evidenceContract ?? null,
        });

        let reply = "";
        if (hasApiCheck(row, "cancel_inflight")) {
          reply = await sendAndCancel(page, row.prompt);
          appendLog({ event: "cancel_inflight.done", reply_len: reply.length });
        } else {
          reply = await chatSend(page, row.prompt, { timeoutMs: row.timeout_ms });
        }
        appendLog({ event: "reply.received", reply_len: reply.length });
        mkdirSync(dirname(replyFile), { recursive: true });
        writeFileSync(replyFile, reply, "utf8");
        let turnEvidence: Record<string, unknown> | null = null;
        try {
          turnEvidence = await readLatestTurnEvidence(page, api);
          appendLog({ event: "turn.evidence", ...turnEvidence });
        } catch (e) {
          appendLog({ event: "turn.evidence.failed", error: String((e as Error)?.message ?? e) });
        }

        if (!hasApiCheck(row, "cancel_inflight")) {
          const stabilityFailures = turnEvidence
            ? turnStabilityFailures(turnEvidence)
            : ["turn evidence missing"];
          appendLog({
            event: "assert.turn_stability",
            ok: stabilityFailures.length === 0,
            details: stabilityFailures,
          });
          expect(
            stabilityFailures,
            "turn ended in an unstable state: " + stabilityFailures.join("; "),
          ).toEqual([]);
        }

        const qualityFailures = replyQualityFailures(reply);
        appendLog({
          event: "assert.reply_quality",
          ok: qualityFailures.length === 0,
          details: qualityFailures,
        });
        expect(
          qualityFailures,
          "assistant reply failed quality gate: " + qualityFailures.join("; "),
        ).toEqual([]);

        if (row.must_contain) {
          const re = new RegExp(row.must_contain, "i");
          const ok = re.test(reply);
          appendLog({ event: "assert.must_contain", pattern: row.must_contain, ok });
          expect(ok, "must_contain failed: /" + row.must_contain + "/i").toBeTruthy();
        }

        if (row.must_not_contain) {
          const re = new RegExp(row.must_not_contain);
          const ok = !re.test(reply);
          appendLog({ event: "assert.must_not_contain", pattern: row.must_not_contain, ok });
          expect(ok, "must_not_contain matched: /" + row.must_not_contain + "/").toBeTruthy();
        }

        let apiOk = true;
        let apiDetails: string[] = [];
        if (row.api_check) {
          const r = await runApiChecks(api, row.api_check, turnEvidence, reply);
          apiOk = r.ok;
          apiDetails = r.details;
          appendLog({ event: "assert.api_check", ok: apiOk, details: apiDetails });
          expect(apiOk, "api_check failed: " + apiDetails.join("; ")).toBeTruthy();
        }

        await page.screenshot({ path: screenshot, fullPage: true });
        appendLog({ event: "screenshot.saved", path: screenshot });

        const duration = Date.now() - started;
        appendLog({ event: "case.pass", duration_ms: duration });
        ensureResultDirs();
        appendFileSync(
          SUMMARY_CSV,
          [row.id, row.group, row.priority, "pass", duration, reply.length, screenshot, logFile, apiOk ? "yes" : "no", JSON.stringify(row.notes)].join(",") + "\n",
        );
      } catch (err) {
        const duration = Date.now() - started;
        appendLog({
          event: "case.fail",
          error: String((err as Error)?.message ?? err),
          duration_ms: duration,
        });
        try {
          await page.screenshot({ path: screenshot, fullPage: true });
        } catch {
          /* ignore */
        }
        ensureResultDirs();
        appendFileSync(
          SUMMARY_CSV,
          [row.id, row.group, row.priority, "fail", duration, 0, screenshot, logFile, "no", JSON.stringify(row.notes)].join(",") + "\n",
        );
        throw err;
      }
    });
  }
});

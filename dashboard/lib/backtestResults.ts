import type { ChatThread } from "./chat";
import { projectMessageBlocks } from "./streamTranscript";

export type BacktestResultRef = {
  id: string; strategyId: string; proposalId: string | null; ts: string;
  title: string; seenAt: number; status: "completed" | "blocked" | "failed";
  verdict: string; metrics: Record<string, unknown>; message: string; dataKind: string;
  evaluationMode: string; executionMode: string; performanceEvidence?: boolean;
  decisionReplay?: Record<string, unknown>;
  replay: Record<string, unknown>; provenance: Record<string, unknown>; engine: string;
  start: string; end: string; nextAction: string; flags: string[];
  equityPreview: Array<{ time: number; value: number }>;
  coverage?: Record<string, unknown>;
  biasChecks?: Record<string, unknown>;
  researchChecks?: Record<string, unknown>;
  failureReason?: string;
  failureDiagnostics?: Record<string, unknown>;
};
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown): string => typeof value === "string" ? value.trim() : "";
const timestamp = (value: string) => /^(?:[A-Za-z][A-Za-z0-9-]*_)?\d{8}_\d{6}$/.test(value);
const hardFailureFlag = (flag: string) =>
  flag === "negative_net_return" ||
  flag === "strategy_returned_errors" ||
  flag === "sdk_order_errors" ||
  flag === "order_accounting_mismatch" ||
  flag === "observation_order_attempt" ||
  flag.startsWith("risk_breach:");

/** Old reports may persist FAIL solely because they trailed buy-and-hold. */
export function effectiveBacktestVerdict(verdict: string, flags: string[]): string {
  const normalized = verdict.trim().toUpperCase();
  if (
    normalized === "FAIL" &&
    flags.includes("benchmark_capture_below_threshold") &&
    !flags.some(hardFailureFlag)
  ) return "WARN";
  return normalized;
}

function locator(path: string): { strategyId: string; proposalId: string; ts: string } | null {
  if (!path || /[\r\n"<>]/.test(path)) return null;
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  const at = parts.lastIndexOf("backtests"), proposal = parts.lastIndexOf("proposals");
  if (at < 1 || !timestamp(parts[at + 1] || "")) return null;
  return { strategyId: parts[at - 1], ts: parts[at + 1], proposalId: proposal >= 0 && proposal < at ? parts[proposal + 1] : "" };
}

/** Tool stdout may prepend a summary or a truncated tail to its complete JSON. */
function structured(raw: string): unknown {
  if (raw.length > 1_000_000) return null;
  const value = raw.trim(), kept = value.lastIndexOf("[compacted_kept]");
  const candidates = [value, ...(kept >= 0 ? [value.slice(kept + "[compacted_kept]".length).trim()] : [])];
  const start = value.indexOf("{"), end = value.lastIndexOf("}");
  if (start >= 0 && end > start) candidates.push(value.slice(start, end + 1));
  candidates.push(...value.split("\n").filter(line => /^[\s]*[\[{]/.test(line)).slice(-16).reverse());
  for (const candidate of candidates) {
    try { const parsed: unknown = JSON.parse(candidate); if (parsed && typeof parsed === "object") return parsed; } catch { /* Only complete JSON, never eval. */ }
  }
  return null;
}

const pathKeys = ["out_dir", "backtest_dir", "metrics_path", "raw_metrics_file", "report_path", "chart_path", "equity_path", "trades_path", "result_path"];
const producerMetricKeys = [
  "accounting_currency",
  "coverage_reason", "coverage_ok", "requested_window_complete", "coverage_message",
  "requested_window_days", "requested_start_utc", "requested_end_utc",
  "limitations", "limitation_codes", "gaps", "modeled_coverage", "observation_coverage",
];

/** Results only: never visit tool arguments or infer a run from assistant prose. */
export function extractBacktestResults(value: unknown, seenAt = 0, action = ""): BacktestResultRef[] {
  const results = new Map<string, BacktestResultRef>(), seen = new Set<unknown>();
  function visit(value: unknown, depth: number, inherited: Record<string, unknown>, backtest: boolean, failed = false) {
    if (value == null || depth > 9 || seen.has(value)) return;
    if (typeof value === "string") {
      const parsed = structured(value);
      if (parsed) visit(parsed, depth + 1, inherited, backtest, failed);
      return;
    }
    if (typeof value !== "object") return;
    seen.add(value);
    if (Array.isArray(value)) { value.slice(0, 100).forEach(item => visit(item, depth + 1, inherited, backtest, failed)); return; }
    const row = object(value);
    if (row.kind === "tool_use" || row.type === "tool_use"
      || ["text", "text_delta", "thinking", "thinking_delta"].includes(text(row.kind))
      || !backtest && ["text", "thinking"].includes(text(row.type))) return;
    const named = [row.action, row.name, row.tool].map(text).join(" ");
    backtest ||= /backtest/i.test(named) || row.result_type === "backtest_result";
    // A policy read or an interaction boundary is not an executed replay.
    // Legacy traces omit the protocol, so also recognize its structured body.
    if (row.result_protocol === "user_interaction_pending"
      || row.result_type === "retrospective_authorization_policy"
      || backtest && (object(row.payload).policy_check_only === true || object(row.arguments).policy_check_only === true)
      || backtest && row.error_kind === "deduped" && row.elapsed_ms === 0 && row.result === null
      || row.reason === "retrospective_replay_authorization_pending"
        && object(row.interaction).kind === "question" && object(row.interaction).state === "pending") return;
    const rowFailed = row.ok === false || row.is_error === true || Boolean(row.error) || ["failed", "blocked"].includes(text(row.backtest_status));
    failed ||= rowFailed;
    const metadata = { ...inherited };
    for (const key of ["strategy_id", "proposal_id", "backtest_ts", "call_id"]) if (text(row[key])) metadata[key] = row[key];
    for (const key of ["attempt", "recovery", "verification"]) if (row[key] != null) metadata[key] = row[key];
    let strategyId = text(metadata.strategy_id || row.strategyId), proposalId = text(metadata.proposal_id || row.proposalId);
    // Failed tools can have no result body. Their producer receipt still binds
    // the candidate; request arguments are never evidence of an executed run.
    const verification = object(metadata.verification);
    const binding = verification.protocol === "nerya.verification.v1"
      ? text(verification.artifact_id).match(/^strategy:([^:]+):proposal:([^:]+)$/) : null;
    if (failed && binding) { strategyId ||= binding[1]; proposalId ||= binding[2]; }
    let ts = text(metadata.backtest_ts || row.backtestTs);
    for (const key of pathKeys) {
      const path = locator(text(row[key]));
      if (!path) continue;
      strategyId ||= path.strategyId; proposalId ||= path.proposalId; ts ||= path.ts;
    }
    const error = object(row.error);
    const message = text(row.message) || text(row.coverage_message) || text(error.message) || text(row.error);
    const before = results.size;
    for (const key of ["block", "result", "output", "payload", "data", "content", "text", "stdout_json", "stdout", "notes"]) {
      if (row[key] != null) visit(row[key], depth + 1, metadata, backtest, failed);
    }
    // Prefer a producer's detailed diagnostic to the enclosing generic error.
    const explicitFailure = results.size === before && backtest && failed && rowFailed;
    if ((!failed && strategyId && timestamp(ts)) || explicitFailure) {
      const reason = text(row.reason) || text(object(metadata.recovery).reason) || text(error.kind);
      const callId = text(metadata.call_id);
      const id = explicitFailure ? callId ? `error:call:${callId}:${metadata.attempt || 1}`
        : `error:${proposalId}:${strategyId}:${reason || message}` : `${proposalId ? proposalId + ":" : ""}${strategyId}:${ts}`;
      const old = results.get(id);
      const flags = Array.isArray(row.flags) ? row.flags.filter((v): v is string => typeof v === "string") : old?.flags || [];
      const rawVerdict = text(row.verdict) || old?.verdict || "";
      const evidence = { ...object(row.metrics), ...row };
      const producerMetrics = Object.fromEntries(producerMetricKeys
        .filter(key => row[key] !== undefined)
        .map(key => [key, row[key]]));
      results.set(id, {
        id, strategyId: strategyId || old?.strategyId || "", proposalId: proposalId || old?.proposalId || null, ts: explicitFailure ? "" : ts,
        title: text(row.title) || old?.title || strategyId || proposalId || "Backtest", seenAt,
        status: explicitFailure ? (row.backtest_status === "blocked" || !reason && old?.status === "blocked" || ["no_historical_data", "historical_provider_unsupported", "backtest_sdk_unsupported", "backtest_dependency_missing", "protocol_replay_model_required"].includes(reason) ? "blocked" : "failed") : "completed",
        verdict: effectiveBacktestVerdict(rawVerdict, flags),
        metrics: { ...old?.metrics, ...object(row.metrics), ...object(row.metrics_display), ...producerMetrics },
        message: message || old?.message || "",
        failureReason: reason || old?.failureReason || "",
        failureDiagnostics: { ...old?.failureDiagnostics, ...object(row.diagnostics), ...object(row.recovery) },
        dataKind: text(object(evidence.provenance).data_kind) || old?.dataKind || "",
        evaluationMode: text(evidence.evaluation_mode) || old?.evaluationMode || "",
        executionMode: text(evidence.execution_mode) || old?.executionMode || "",
        performanceEvidence: typeof evidence.performance_evidence === "boolean" ? evidence.performance_evidence : old?.performanceEvidence,
        decisionReplay: { ...old?.decisionReplay, ...object(evidence.decision_replay) },
        replay: { ...old?.replay, ...object(evidence.replay) },
        provenance: { ...old?.provenance, ...object(evidence.provenance),
          ...(failed && binding && text(verification.revision) ? { source_revision: text(verification.revision) } : {}) },
        biasChecks: { ...old?.biasChecks, ...object(object(row.metrics).bias_checks), ...object(row.bias_checks) },
        researchChecks: { ...old?.researchChecks, ...object(object(row.metrics).research_checks), ...object(row.research_checks) },
        engine: text(row.engine) || old?.engine || "",
        coverage: { ...old?.coverage, ...Object.fromEntries(["requested_window_days", "requested_window_complete", "requested_start_utc", "requested_end_utc", "data_manifest", "tf", "backtest_days"].filter(key => evidence[key] !== undefined).map(key => [key, evidence[key]])) },
        start: text(evidence.start || evidence.start_utc || object(row.metrics_display).start_utc) || old?.start || "",
        end: text(evidence.end || evidence.end_utc || object(row.metrics_display).end_utc) || old?.end || "",
        flags,
        nextAction: text(object(row.next_required_action).message) || old?.nextAction || "",
        equityPreview: Array.isArray(row.equity_preview) ? row.equity_preview.flatMap(value => {
          const p = object(value);
          return typeof p.time === "number" && typeof p.value === "number" && Number.isFinite(p.time) && Number.isFinite(p.value)
            ? [{ time: p.time, value: p.value }] : [];
        }) : old?.equityPreview || [],
      });
    }
  }
  visit(value, 0, {}, /backtest/i.test(action));
  return [...results.values()];
}

export function collectBacktestResults(thread: Pick<ChatThread, "messages"> | null | undefined): BacktestResultRef[] {
  const results = new Map<string, BacktestResultRef>();
  for (const message of thread?.messages || []) {
    if (message.role !== "assistant") continue;
    const values: unknown[] = [
      ...projectMessageBlocks(message),
      ...(message.turn?.tool_trace || []), ...(message.turn?.actions || []),
    ];
    for (const value of values) for (const result of extractBacktestResults(value, message.ts)) {
      // Call IDs identify one operation across transcript and final trace, not
      // across independent commands. Keep legacy run IDs unchanged.
      const key = result.id.startsWith("error:call:") ? `${message.command_id || message.id}:${result.id}` : result.id;
      const old = results.get(key);
      const flags = result.flags.length ? result.flags : old?.flags || [];
      results.set(key, { ...result, id: key,
        strategyId: result.strategyId || old?.strategyId || "", proposalId: result.proposalId || old?.proposalId || null,
        status: result.status === "failed" && old?.status === "blocked" && !result.failureReason ? old.status : result.status,
        failureReason: result.failureReason || old?.failureReason || "",
        failureDiagnostics: { ...old?.failureDiagnostics, ...result.failureDiagnostics },
        verdict: effectiveBacktestVerdict(result.verdict || old?.verdict || "", flags),
        title: (result.title === "Backtest" || result.title === result.strategyId) && old?.title && old.title !== "Backtest" ? old.title : result.title,
        message: result.message || old?.message || "", dataKind: result.dataKind || old?.dataKind || "",
        metrics: { ...old?.metrics, ...result.metrics },
        evaluationMode: result.evaluationMode || old?.evaluationMode || "", executionMode: result.executionMode || old?.executionMode || "",
        performanceEvidence: result.performanceEvidence ?? old?.performanceEvidence,
        decisionReplay: { ...old?.decisionReplay, ...result.decisionReplay },
        replay: { ...old?.replay, ...result.replay }, provenance: { ...old?.provenance, ...result.provenance },
        coverage: { ...old?.coverage, ...result.coverage },
        biasChecks: { ...old?.biasChecks, ...result.biasChecks },
        researchChecks: { ...old?.researchChecks, ...result.researchChecks },
        start: result.start || old?.start || "", end: result.end || old?.end || "", engine: result.engine || old?.engine || "",
        flags, nextAction: result.nextAction || old?.nextAction || "",
        equityPreview: result.equityPreview.length ? result.equityPreview : old?.equityPreview || [] });
    }
  }
  return [...results.values()];
}

import type { ChatResult } from "./chatResults";
import { recordOf } from "./externalCalls";

export type CheckState = "passed" | "failed" | "pending" | "stale" | "missing" | "partial" | "unknown" | "unverified";
export type EvidenceCheck = { id: string; label: string; state: CheckState; message: string; revision: string; artifact: string; evidenceRef: string };
const states: CheckState[] = ["failed", "pending", "stale", "missing", "unknown", "unverified", "partial", "passed"];
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && !!item) : [];
const text = (value: unknown): string => typeof value === "string" ? value : "";

/** Presentation of host evidence only. This never grants authority or infers
 * completion from assistant prose; missing validation is not a failed test. */
export function deliveryEvidence(result: ChatResult) {
  const evidence = result.evidence, artifacts = evidence?.artifacts, verifier = evidence?.verifier;
  const delivery = result.delivery;
  const hard = recordOf(recordOf(verifier?.details).hard);
  const receiptRows = Array.isArray(hard.checks) ? hard.checks.map(recordOf) : [];
  const checks: EvidenceCheck[] = receiptRows.length ? receiptRows.map((row, index) => ({
    id: `${text(row.artifact_id)}:${text(row.check_id)}:${index}`, label: text(row.label) || text(row.check_id),
    state: states.includes(row.status as CheckState) ? row.status as CheckState : row.status ? "unknown" : "missing",
    message: text(row.message), revision: text(row.revision), artifact: text(row.artifact_id), evidenceRef: text(row.evidence_ref),
  })) : (artifacts?.tests_run || []).map((raw, index) => {
    const row = recordOf(raw);
    return { id: `legacy-${index}`, label: String(row.command || row.cmd || row.action || row.tool || "Validation"),
      state: row.ok === false || typeof row.exit_code === "number" && row.exit_code !== 0 ? "failed" as const
        : row.ok === true || row.exit_code === 0 ? "passed" as const : "pending" as const,
      message: typeof row.exit_code === "number" ? `exit ${row.exit_code}` : "", revision: "", artifact: "", evidenceRef: "" };
  });
  // Use the same authoritative checks for the heading, rows and counters.
  // An empty contract is not permission to substitute older verifier receipts.
  const projectedChecks: EvidenceCheck[] = delivery ? (delivery.checks || []).map((row, index) => ({
    id: `${row.check_id}:${index}`, label: row.label || row.check_id, state: states.includes(row.status as CheckState) ? row.status as CheckState : row.status ? "unknown" : "missing",
    message: row.message || "", revision: row.revision || "", artifact: "", evidenceRef: row.evidence_ref || "",
  })) : checks;
  const errors = artifacts?.errors || [];
  const completionReasons = [...new Set(strings(delivery?.completion?.reasons))].map(raw => {
    const separator = raw.indexOf(":");
    return { raw, code: separator < 0 ? raw : raw.slice(0, separator), subject: separator < 0 ? "" : raw.slice(separator + 1) };
  });
  const declared = states.includes(verifier?.hard_status as CheckState) ? verifier?.hard_status as CheckState : null;
  // A malformed/contradictory saved record must not paint a green status.
  const unresolvedState = states.find(state => state !== "passed" && (declared === state || projectedChecks.some(check => check.state === state)));
  const deliveryIncomplete = delivery && (completionReasons.length > 0 || !delivery.completion?.allowed || delivery.completion.status !== "complete"
    || !delivery.stages?.length || delivery.stages.some(stage => !["completed", "skipped"].includes(stage.state)));
  const partialDelivery = Boolean(delivery?.completion?.status === "partial"
    && (!declared || declared === "passed" || declared === "partial"
      || declared === "missing" && verifier?.trusted === false)
    && delivery.stages?.length && delivery.checks?.length
    && delivery.stages.every(stage => ["completed", "skipped"].includes(stage.state))
    && !delivery.checks.some(check => ["failed", "stale", "unknown"].includes(check.status))
    && (delivery.completion.allowed
      || completionReasons.length > 0
      || delivery.checks.some(check => check.required !== false && check.status !== "passed"))
    && (!delivery.completion.allowed
      || delivery.checks.every(check => check.required === false || check.status === "passed")));
  // A producer-admitted partial result remains partial after recovered tool
  // errors. Keep those diagnostics in limitations, not as failed check counts.
  const status = partialDelivery ? "pending" : errors.length ? "failed" : unresolvedState === "partial" ? "pending" : unresolvedState
    || (deliveryIncomplete ? "pending" : delivery ? (projectedChecks.length ? "passed" : "missing")
      : declared === "passed" && verifier?.hard_passed === true && verifier?.trusted !== false ? "passed" : "missing");
  const risks = [...(artifacts?.unverified_risks || []), ...errors].map(raw => {
    const row = recordOf(raw);
    return typeof raw === "string" ? raw : String(row.message || row.reason || row.kind || row.path || row.error || "Unverified");
  });
  return {
    status, checks: projectedChecks, completionReasons, partialDelivery,
    executionErrors: status === "failed" && errors.length > 0 && unresolvedState !== "failed"
      && !completionReasons.some(reason => reason.code === "check_failed"),
    files: [...new Set([...(delivery?.artifacts || []).map(row => row.ref || row.artifact_id), ...strings(artifacts?.created), ...strings(artifacts?.modified)])],
    risks: [...new Set([...risks, ...(delivery?.limitations || [])])], unverified: strings(hard.unverified),
    passed: projectedChecks.filter(check => check.state === "passed").length,
    unresolved: projectedChecks.filter(check => check.state !== "passed").length,
    versionBound: projectedChecks.some(check => !!check.revision), scope: text(hard.scope),
    delivery,
  };
}

"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import {
  clientApi,
  type EvolutionProposal,
  type StrategyRuntimePromotionResult,
} from "../../lib/clientApi";
import type { StrategyValidationReport } from "../../lib/strategyTypes";
import { formatTsShort } from "../../lib/format";
import { Pill } from "../Page";
import { ShieldCheckIcon, StrategiesIcon } from "../icons";

export type StrategyProposalView = {
  id: string;
  kind?: string;
  state?: string;
  summary?: string;
  ts?: string;
  target?: string | null;
  strategy_id?: string | null;
  validation?: unknown;
  files?: unknown;
  metadata?: Record<string, unknown> | null;
  [key: string]: unknown;
};

const ACTIVE_STATES = new Set([
  "draft",
  "pending_review",
  "proposed",
  "approved",
]);

const TERMINAL_STATES = new Set(["applied", "rejected", "rolled_back"]);

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (!text) return value;
  if (!text.startsWith("{") && !text.startsWith("[")) return value;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return value;
  }
}

function firstProposalRecord(value: unknown, actionHint = "", depth = 0): Record<string, unknown> | null {
  if (depth > 4) return null;
  const parsed = parseMaybeJson(value);
  const record = recordOf(parsed);
  if (!Object.keys(record).length) return null;

  const action = stringValue(record.action) || stringValue(record.name) || actionHint;
  const kind = stringValue(record.kind);
  const hasStrategyPackageKind = kind === "strategy_package_proposal";
  const hasGenerateAction = action === "strategy_generate_proposal";
  const hasPackageShape =
    !!stringValue(record.proposal_id || record.id) &&
    !!stringValue(record.strategy_id) &&
    (Array.isArray(record.files) || record.validation !== undefined);

  if (hasStrategyPackageKind || hasGenerateAction || hasPackageShape) {
    return record;
  }

  for (const key of ["result", "data", "payload", "output"]) {
    const nested = firstProposalRecord(record[key], action, depth + 1);
    if (nested) return nested;
  }

  const content = Array.isArray(record.content) ? record.content : [];
  for (const part of content) {
    const partRecord = recordOf(part);
    const nested =
      firstProposalRecord(partRecord.data, action, depth + 1) ||
      firstProposalRecord(partRecord.text, action, depth + 1);
    if (nested) return nested;
  }

  return null;
}

function strategyIdFromTarget(target: unknown): string {
  const text = stringValue(target);
  const match = text.match(/(?:^|\/)strategies\/([^/]+)/);
  if (match?.[1]) return match[1];
  const parts = text.split("/").filter(Boolean);
  return parts[0] === "strategies" && parts[1] ? parts[1] : "";
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => String(item)).filter(Boolean)
    : [];
}

function validationItems(value: unknown, key: "blockers" | "warnings"): string[] {
  const validation = recordOf(value);
  const items: unknown[] = Array.isArray(validation[key])
    ? (validation[key] as unknown[])
    : [];
  return items
    .map((item) => {
      if (typeof item === "string") return item;
      const row = recordOf(item);
      return stringValue(row.message) || stringValue(row.code) || "";
    })
    .filter(Boolean);
}

function stateTone(state: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  switch (state) {
    case "applied":
    case "approved":
      return "ok";
    case "pending_review":
    case "proposed":
      return "warn";
    case "rejected":
    case "rolled_back":
      return "danger";
    case "draft":
      return "brand";
    default:
      return "neutral";
  }
}

export function strategyProposalFromToolResult(
  value: unknown,
  actionHint = "",
): StrategyProposalView | null {
  const record = firstProposalRecord(value, actionHint);
  if (!record) return null;
  const id = stringValue(record.proposal_id) || stringValue(record.id);
  if (!id) return null;
  const strategyId =
    stringValue(record.strategy_id) || strategyIdFromTarget(record.target);
  const target = stringValue(record.target) || (strategyId ? `strategies/${strategyId}` : "");
  const promotion = recordOf(record.promotion);
  const promotionOk = promotion.ok === true || record.ok === true;
  const state =
    stringValue(record.state) ||
    (promotionOk && actionHint === "strategy_promote" ? "applied" : "pending_review");

  return {
    ...record,
    id,
    kind: "strategy_package_proposal",
    state,
    target: target || null,
    strategy_id: strategyId || null,
    summary:
      stringValue(record.summary) ||
      stringValue(record.title) ||
      (strategyId ? `Strategy package ${strategyId}` : id),
  };
}

export function isActiveStrategyProposal(
  proposal: EvolutionProposal | StrategyProposalView,
): boolean {
  const kind = stringValue(proposal.kind);
  const state = stringValue(proposal.state || "draft");
  return kind === "strategy_package_proposal" && ACTIVE_STATES.has(state);
}

export function StrategyProposalApprovalCard({
  proposal,
  compact = false,
  approveNote = "approved from dashboard",
  onApproved,
  onError,
  onNotice,
}: {
  proposal: EvolutionProposal | StrategyProposalView;
  compact?: boolean;
  approveNote?: string;
  onApproved?: (result: StrategyRuntimePromotionResult) => Promise<void> | void;
  onError?: (message: string | null) => void;
  onNotice?: (message: string | null) => void;
}) {
  const t = useTranslations("strategyProposal");
  const tCommon = useTranslations("common");
  const normalized = strategyProposalFromToolResult(proposal) ?? proposal;
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [localNotice, setLocalNotice] = useState<string | null>(null);
  const [appliedStrategyId, setAppliedStrategyId] = useState<string | null>(null);
  const [stateOverride, setStateOverride] = useState<string | null>(null);
  const [validationResult, setValidationResult] =
    useState<StrategyValidationReport | null>(null);
  const [validationChecked, setValidationChecked] = useState(false);
  const [validating, setValidating] = useState(false);

  const proposalId = stringValue(normalized.id);
  const state = (stateOverride || stringValue(normalized.state) || "draft").toLowerCase();
  const strategyId =
    appliedStrategyId ||
    stringValue(normalized.strategy_id) ||
    strategyIdFromTarget(normalized.target);
  const files = stringArray(normalized.files);
  const effectiveValidation = validationResult ?? normalized.validation;
  const blockers = validationItems(effectiveValidation, "blockers");
  const warnings = validationItems(effectiveValidation, "warnings");
  const validation = recordOf(effectiveValidation);
  const validationOk = validation.ok === true;
  const hasBlockers = blockers.length > 0 || validation.ok === false;
  const shouldValidate =
    !!proposalId &&
    !normalized.validation &&
    !TERMINAL_STATES.has(state);
  const validationPending = shouldValidate && !validationChecked;
  const canApprove =
    proposalId &&
    !TERMINAL_STATES.has(state) &&
    !hasBlockers &&
    !validationPending &&
    !validating;
  const notice = onNotice ? null : localNotice;
  const error = localError;

  async function validateProposal(): Promise<StrategyValidationReport | null> {
    if (!proposalId) return null;
    setValidating(true);
    setLocalError(null);
    try {
      const out = await clientApi.strategyRuntimeValidate({ proposal_id: proposalId });
      const report = out as StrategyValidationReport;
      setValidationResult(report);
      setValidationChecked(true);
      return report;
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setLocalError(msg);
      onError?.(msg);
      setValidationChecked(true);
      return null;
    } finally {
      setValidating(false);
    }
  }

  useEffect(() => {
    setValidationResult(null);
    setValidationChecked(false);
    setLocalError(null);
  }, [proposalId]);

  useEffect(() => {
    if (!shouldValidate || validationChecked || validating) return;
    void validateProposal();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proposalId, shouldValidate, validationChecked, validating]);

  async function approve() {
    if (!proposalId) return;
    setBusy(true);
    setLocalError(null);
    onError?.(null);
    try {
      const checked = validationResult ?? (normalized.validation as StrategyValidationReport | undefined) ?? await validateProposal();
      const checkedBlockers = validationItems(checked, "blockers");
      if (!checked || checked.ok === false || checkedBlockers.length > 0) {
        throw new Error(t("validationBlockedNotice"));
      }
      const out = await clientApi.strategyRuntimePromote(proposalId, approveNote);
      if (!out.ok) {
        throw new Error(out.error || out.reason || "strategy_promote_failed");
      }
      const nextStrategyId = stringValue(out.strategy_id) || strategyId;
      setAppliedStrategyId(nextStrategyId || null);
      setStateOverride("applied");
      const msg = t("appliedNotice", { strategy: nextStrategyId || proposalId });
      setLocalNotice(msg);
      onNotice?.(msg);
      await onApproved?.(out);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setLocalError(msg);
      onError?.(msg);
    } finally {
      setBusy(false);
    }
  }

  if (!proposalId) return null;

  return (
    <div
      className={[
        "rounded-lg border border-[#f5a524]/30 bg-[#f5a524]/[0.055] text-xs",
        compact ? "p-3" : "p-4",
      ].join(" ")}
      data-proposal-id={proposalId}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <StrategiesIcon size={15} className="text-[#f5a524]" />
            <span className="font-mono text-sm text-ink-100">
              {strategyId || proposalId}
            </span>
            <Pill tone={stateTone(state)}>{state}</Pill>
            {validationOk ? <Pill tone="ok">{t("validationOk")}</Pill> : null}
            {validating || validationPending ? <Pill tone="brand">{t("validating")}</Pill> : null}
            {hasBlockers ? <Pill tone="danger">{t("blocked")}</Pill> : null}
          </div>
          <div className="mt-1 text-ink-300 leading-relaxed">
            {stringValue(normalized.summary) || t("fallbackSummary")}
          </div>
          <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-500 font-mono">
            <span>{t("proposalId")}: {proposalId}</span>
            {normalized.target ? <span>{t("target")}: {String(normalized.target)}</span> : null}
            {normalized.ts ? <span>{formatTsShort(String(normalized.ts))}</span> : null}
            {files.length ? <span>{t("files", { count: files.length })}</span> : null}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 shrink-0">
          {state === "applied" && strategyId ? (
            <Link
              href={`/strategies/${encodeURIComponent(strategyId)}`}
              className="btn btn-ghost cursor-pointer text-xs"
            >
              {t("openStrategy")}
            </Link>
          ) : null}
          {!TERMINAL_STATES.has(state) ? (
            <button
              onClick={() => void approve()}
              disabled={!canApprove || busy}
              className="btn btn-primary cursor-pointer text-xs disabled:opacity-50 disabled:cursor-not-allowed"
              title={hasBlockers ? t("blocked") : undefined}
            >
              <ShieldCheckIcon size={14} />
              {busy ? tCommon("working") : t("approveAdd")}
            </button>
          ) : null}
        </div>
      </div>

      {blockers.length || warnings.length ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {blockers.length ? (
            <IssueList title={t("blockers", { count: blockers.length })} items={blockers} tone="danger" />
          ) : null}
          {warnings.length ? (
            <IssueList title={t("warnings", { count: warnings.length })} items={warnings} tone="warn" />
          ) : null}
        </div>
      ) : null}

      {notice ? (
        <div className="mt-3 rounded-md border border-accent-500/30 bg-accent-500/10 px-3 py-2 text-accent-200">
          {notice}
        </div>
      ) : null}
      {error ? (
        <div className="mt-3 rounded-md border border-[#ef4560]/40 bg-[#ef4560]/10 px-3 py-2 text-[#ffb3bd]">
          {error}
        </div>
      ) : null}
    </div>
  );
}

function IssueList({
  title,
  items,
  tone,
}: {
  title: string;
  items: string[];
  tone: "warn" | "danger";
}) {
  const cls =
    tone === "danger"
      ? "border-[#ef4560]/30 bg-[#ef4560]/10 text-[#ffb3bd]"
      : "border-[#f5a524]/30 bg-[#f5a524]/10 text-[#f5a524]";
  return (
    <div className={`rounded-md border px-3 py-2 ${cls}`}>
      <div className="text-[10px] uppercase tracking-[0.18em]">{title}</div>
      <ul className="mt-1 space-y-1 text-[11px] leading-relaxed">
        {items.slice(0, 4).map((item, index) => (
          <li key={`${item}-${index}`} className="break-words">
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}

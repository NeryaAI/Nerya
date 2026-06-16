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
import { confirm, toast } from "../../lib/dialogs";
import { Pill } from "../Page";
import { ShieldCheckIcon, StrategiesIcon, TrashIcon } from "../icons";

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
  // Latest backtest verdict for this proposal (PASS / WARN / FAIL), attached by
  // ``activeProposalsFromTurn`` so the card can warn before approving a strategy
  // that failed its backtest.
  backtest_verdict?: string;
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

// A draft proposal is still being authored (the agent is editing the staged
// files). It should NOT pop up an approve/delete card in chat yet — only once
// it has been submitted into the review queue. The chat hoist uses this; the
// strategies page keeps showing drafts via isActiveStrategyProposal.
export function isHoistableStrategyProposal(
  proposal: EvolutionProposal | StrategyProposalView,
): boolean {
  return (
    isActiveStrategyProposal(proposal) &&
    stringValue(proposal.state || "draft") !== "draft"
  );
}

export function StrategyProposalApprovalCard({
  proposal,
  compact = false,
  approveNote = "approved from dashboard",
  onApproved,
  onDeleted,
  onError,
  onNotice,
}: {
  proposal: EvolutionProposal | StrategyProposalView;
  compact?: boolean;
  approveNote?: string;
  onApproved?: (result: StrategyRuntimePromotionResult) => Promise<void> | void;
  onDeleted?: (proposalId: string) => Promise<void> | void;
  onError?: (message: string | null) => void;
  onNotice?: (message: string | null) => void;
}) {
  const t = useTranslations("strategyProposal");
  const tCommon = useTranslations("common");
  const normalized = strategyProposalFromToolResult(proposal) ?? proposal;
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [removed, setRemoved] = useState(false);
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
  const backtestVerdict = stringValue(normalized.backtest_verdict).toUpperCase();
  const verdictFailed = backtestVerdict === "FAIL";
  const verdictTone =
    backtestVerdict === "PASS"
      ? "ok"
      : backtestVerdict === "WARN"
      ? "warn"
      : "danger";
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
    // Soft-gate: the package can be code-valid ("validation ok") while the
    // strategy still FAILED its backtest. Make the operator explicitly confirm
    // before adding a strategy the agent judged a failure.
    if (verdictFailed) {
      const proceed = await confirm({
        title: t("backtestFailConfirmTitle"),
        message: t("backtestFailConfirmMessage", {
          strategy: strategyId || proposalId,
        }),
        okLabel: t("backtestFailConfirmOk"),
        cancelLabel: tCommon("cancel"),
        tone: "danger",
      });
      if (!proceed) return;
    }
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

  async function remove() {
    if (!proposalId) return;
    const ok = await confirm({
      title: t("deleteConfirmTitle"),
      message: t("deleteConfirmMessage", { strategy: strategyId || proposalId }),
      okLabel: t("deleteConfirmOk"),
      cancelLabel: tCommon("cancel"),
      tone: "danger",
    });
    if (!ok) return;
    setDeleting(true);
    setLocalError(null);
    onError?.(null);
    try {
      const out = await clientApi.proposalDelete(proposalId);
      if (!out.ok && !out.deleted) {
        throw new Error(out.error || out.reason || "strategy_delete_failed");
      }
      setRemoved(true);
      const msg = t("deletedNotice", { strategy: strategyId || proposalId });
      setLocalNotice(msg);
      onNotice?.(msg);
      toast({ message: msg, tone: "ok" });
      await onDeleted?.(proposalId);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setLocalError(msg);
      onError?.(msg);
    } finally {
      setDeleting(false);
    }
  }

  if (!proposalId) return null;

  if (removed) {
    return (
      <div
        className={[
          "rounded-lg border border-brand-500/15 bg-white/[0.02] text-xs text-ink-400",
          compact ? "p-3" : "p-4",
        ].join(" ")}
        data-proposal-id={proposalId}
        data-proposal-removed="true"
      >
        <div className="flex flex-wrap items-center gap-2">
          <TrashIcon size={14} className="text-ink-500" />
          <span>{t("deletedNotice", { strategy: strategyId || proposalId })}</span>
        </div>
      </div>
    );
  }

  return (
    <div
      className={[
        "rounded-lg border border-warn/30 bg-warn/[0.055] text-xs [overflow-wrap:anywhere]",
        compact ? "p-3" : "p-4",
      ].join(" ")}
      data-proposal-id={proposalId}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <StrategiesIcon size={15} className="text-warn" />
            <span className="font-mono text-sm text-ink-100">
              {strategyId || proposalId}
            </span>
            <Pill tone={stateTone(state)}>{state}</Pill>
            {backtestVerdict ? (
              <Pill tone={verdictTone}>
                {t("backtestVerdict", { verdict: backtestVerdict })}
              </Pill>
            ) : null}
            {validationOk ? <Pill tone="ok">{t("validationOk")}</Pill> : null}
            {validating || validationPending ? <Pill tone="brand">{t("validating")}</Pill> : null}
            {hasBlockers ? <Pill tone="danger">{t("blocked")}</Pill> : null}
          </div>
          <div className="mt-1 text-ink-300 leading-relaxed">
            {stringValue(normalized.summary) || t("fallbackSummary")}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-500">
            {normalized.ts ? <span>{formatTsShort(String(normalized.ts))}</span> : null}
            {files.length ? <span>{t("files", { count: files.length })}</span> : null}
            {/* Raw ids are reviewer/debug detail; keep them one click away. */}
            <details className="inline-block">
              <summary className="cursor-pointer list-none text-ink-500 hover:text-ink-300 underline decoration-dotted underline-offset-2">
                {t("detailsToggle")}
              </summary>
              <span className="mt-1 block font-mono text-ink-500">
                {t("proposalId")}: {proposalId}
                {normalized.target ? ` · ${t("target")}: ${String(normalized.target)}` : ""}
              </span>
            </details>
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
              onClick={() => void remove()}
              disabled={busy || deleting}
              className="btn btn-ghost cursor-pointer text-xs text-rose-300 hover:text-rose-200 disabled:opacity-50 disabled:cursor-not-allowed"
              title={t("deleteConfirmOk")}
            >
              <TrashIcon size={14} />
              {deleting ? tCommon("working") : t("delete")}
            </button>
          ) : null}
          {!TERMINAL_STATES.has(state) ? (
            <button
              onClick={() => void approve()}
              disabled={!canApprove || busy || deleting}
              className="btn btn-primary cursor-pointer text-xs disabled:opacity-50 disabled:cursor-not-allowed"
              title={
                hasBlockers
                  ? t("blocked")
                  : verdictFailed
                  ? t("backtestFailConfirmTitle")
                  : undefined
              }
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
        <div className="mt-3 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-rose-300">
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
      ? "border-danger/30 bg-danger/10 text-rose-300"
      : "border-warn/30 bg-warn/10 text-warn";
  return (
    <div className={`rounded-md border px-3 py-2 ${cls}`}>
      <div className="text-[11px] font-medium">{title}</div>
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

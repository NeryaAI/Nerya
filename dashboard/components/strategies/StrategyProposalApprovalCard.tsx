"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { Icon as NeryaGlyph } from "../icons";

import Link from "next/link";
import { StrategyChatReference } from "../workflows/StrategyChatReference";
import { WorkflowPromotionReceipt, promotionReceiptFacts } from "../workflows/WorkflowPromotionReceipt";
import { useContext, useEffect, useMemo, useState } from "react";
import { StrategyDetailContext } from "../chat/StrategyDetailContext";
import { strategyDetailId } from "../../lib/strategyDetail";
import { useLocale, useTranslations } from "next-intl";

import {
  clientApi,
  type EvolutionProposal,
  type StrategyRuntimePromotionResult,
} from "../../lib/clientApi";
import type { StrategyValidationReport } from "../../lib/strategyTypes";
import { formatTsShort } from "../../lib/format";
import { workflowApi } from "../../lib/workflowApi";
import type { WorkflowView } from "../../lib/workflowTypes";
import { confirm, toast } from "../../lib/dialogs";
import { Pill } from "../Page";
import { ShieldCheckIcon, StrategiesIcon, TrashIcon } from "../icons";
import cardStyles from "./StrategyProposalCard.module.css";

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

const TERMINAL_STATES = new Set(["applied", "rejected", "rolled_back", "superseded"]);

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
  const text = value.includes("[compacted_kept]") ? value.slice(value.lastIndexOf("[compacted_kept]") + "[compacted_kept]".length).trim() : value.trim();
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
  const hasStrategyPackageKind = ["strategy_package_proposal", "strategy_tuning_proposal"].includes(kind) || kind === "prompt_patch" && !!strategyIdFromTarget(record.target);
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
    kind: stringValue(record.kind) || "strategy_package_proposal",
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

// Every authored package is visible in chat, including drafts and applied versions.
export function isHoistableStrategyProposal(proposal: EvolutionProposal | StrategyProposalView): boolean {
  return ["strategy_package_proposal", "strategy_tuning_proposal"].includes(stringValue(proposal.kind)) || stringValue(proposal.kind) === "prompt_patch" && !!strategyIdFromTarget(proposal.target);
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
  const details = useContext(StrategyDetailContext);
  const zh = useLocale().startsWith("zh");
  const tCommon = useTranslations("common");
  const [editedProposal, setEditedProposal] = useState<StrategyProposalView | null>(null);
  const normalized = editedProposal || strategyProposalFromToolResult(proposal) || proposal;
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [removed, setRemoved] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [localNotice, setLocalNotice] = useState<string | null>(null);
  const [appliedStrategyId, setAppliedStrategyId] = useState<string | null>(null);
  const [stateOverride, setStateOverride] = useState<string | null>(null);
  const [promotionReceipt, setPromotionReceipt] = useState<StrategyRuntimePromotionResult | null>(null);
  const recordedPromotion = useMemo(() => {
    const parsed = strategyProposalFromToolResult(proposal);
    return parsed?.promotion ? parsed : undefined;
  }, [proposal]);
  const [validationResult, setValidationResult] =
    useState<StrategyValidationReport | null>(null);
  const [validationChecked, setValidationChecked] = useState(false);
  const [validating, setValidating] = useState(false);
  const [snapshot, setSnapshot] = useState<WorkflowView | null>(null);

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
  const packageProposal = normalized.kind === "strategy_package_proposal";
  const shouldValidate =
    packageProposal && !!proposalId &&
    !normalized.validation &&
    !TERMINAL_STATES.has(state);
  const validationPending = shouldValidate && !validationChecked;
  const manifest = snapshot ? { ...recordOf(snapshot.manifest.extras), ...snapshot.manifest } : {};
  const agentMode = recordOf(manifest.agent_task).enabled === true;
  const gated = snapshot?.strategy.nodes.some(node => node.kind === "script" && node.control?.can_stop);
  const typeLabel = snapshot ? agentMode ? gated ? (i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.001")) : (i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.002")) : (i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.003")) : "";
  const displayTitle = stringValue(manifest.title) || stringValue(normalized.summary) || strategyId || proposalId;
  const states: Record<string, string> = { draft: i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.004"), pending_review: i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.005"), proposed: i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.006"), approved: i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.007"), applied: i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.008"), rejected: i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.009"), rolled_back: i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.010"), superseded: i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.011") };
  const canApprove =
    proposalId &&
    !TERMINAL_STATES.has(state) && state !== "draft" &&
    !hasBlockers &&
    !validationPending &&
    !validating;
  const notice = onNotice ? null : localNotice;
  // When the parent handles errors (toast on the strategies page), the
  // inline bar is suppressed so the failure isn't reported twice.
  const error = onError ? null : localError;

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
    let cancelled = false;
    setSnapshot(null);
    if (strategyId && packageProposal) workflowApi.get(strategyId, proposalId).then(view => {
      if (!cancelled) setSnapshot(view);
    }).catch(() => { /* The saved receipt remains readable if its source is unavailable. */ });
    return () => { cancelled = true; };
  }, [proposalId, strategyId, packageProposal]);

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
      setPromotionReceipt(out);
      if (!out.ok) {
        throw new Error(out.error || out.reason || "strategy_promote_failed");
      }
      const nextStrategyId = stringValue(out.strategy_id) || strategyId;
      setAppliedStrategyId(nextStrategyId || null);
      setStateOverride("applied");
      const msg = promotionReceiptFacts(out).syncFailed
        ? (i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.012"))
        : t("appliedNotice", { strategy: nextStrategyId || proposalId });
      setLocalNotice(t("appliedNotice", { strategy: nextStrategyId || proposalId }));
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
      className={cardStyles.card}
      data-proposal-id={proposalId}
      data-testid="strategy-proposal-card"
    >
      <div className={cardStyles.layout}>
        <div className={cardStyles.identity}>
          <div className={cardStyles.heading}>
            <div className={cardStyles.kind}><StrategiesIcon size={15}/>{typeLabel || (i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.013"))}</div>
            <h3 className={cardStyles.title}>{displayTitle}</h3>
          </div>
          <div className={cardStyles.states}>
            <Pill tone={stateTone(state)}>{states[state] || state}</Pill>
            {backtestVerdict ? (
              <span className={verdictTone === "danger" ? "text-danger" : verdictTone === "warn" ? "text-warn" : "text-[color:var(--text-muted)]"}>
                {t("backtestVerdict", { verdict: backtestVerdict })}
              </span>
            ) : null}
            {validationOk ? <span className="inline-flex items-center gap-1"><ShieldCheckIcon size={13}/>{i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.014")}</span> : null}
            {validating ? <Pill tone="brand">{t("validating")}</Pill> : null}
            {hasBlockers ? <Pill tone="danger">{t("blocked")}</Pill> : null}
          </div>
          <div className={cardStyles.facts} data-testid="strategy-card-facts">
            {Array.isArray(manifest.markets) && <span>{manifest.markets.map(String).join(" · ")}</span>}
            {Boolean(manifest.mode) && <span>{manifest.mode === "paper" ? (i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.015")) : String(manifest.mode)}</span>}
            {recordOf(manifest.schedule).enabled === false && <span>{i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.016")}</span>}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[color:var(--text-muted)]">
            {normalized.ts ? <span>{formatTsShort(String(normalized.ts))}</span> : null}
            {files.length ? <span>{t("files", { count: files.length })}</span> : null}
            {/* Raw ids are reviewer/debug detail; keep them one click away. */}
            <details className="inline-block">
              <summary className="cursor-pointer list-none text-[color:var(--text-muted)] hover:text-[color:var(--text-base)] underline decoration-dotted underline-offset-2">
                {t("detailsToggle")}
              </summary>
              <span className="mt-1 block font-mono text-[color:var(--text-muted)]">
                {t("proposalId")}: {proposalId}
                {normalized.target ? ` · ${t("target")}: ${String(normalized.target)}` : ""}
              </span>
            </details>
          </div>
          {strategyId && <div className="mt-4 border-t border-[color:var(--line)] pt-3"><StrategyChatReference key={proposalId} strategyId={strategyId} proposalId={proposalId} view={snapshot} onSaved={(view) => { setEditedProposal({ ...normalized, id: view.source.proposal_id || proposalId, strategy_id: strategyId, state: view.source.state, summary: String(view.manifest.title || strategyId), validation: undefined }); setStateOverride(null); }} /></div>}
        </div>

        <div className={cardStyles.actions}>
          {details && strategyId && <button type="button" className={cardStyles.expand} title={i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.017")} aria-label={i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.018")} aria-expanded={details.active === strategyDetailId({ kind: "strategy", strategyId, proposalId })} aria-controls={'task-dock-panel-' + strategyDetailId({ kind: "strategy", strategyId, proposalId })} onClick={() => details.open({ kind: "strategy", strategyId, proposalId, title: displayTitle })} data-testid="expand-strategy-details"><NeryaGlyph name="arrowUpRight" size={18}/></button>}
          {validationPending && <button type="button" className="btn btn-ghost text-xs" disabled={validating} onClick={() => void validateProposal()}>{i18nCopy(zh, "copy.components_strategies_StrategyProposalApprovalCard.019")}</button>}
          {!packageProposal && !details && <Link className="btn btn-ghost text-xs" href={"/self-evolution?tab=proposals&proposal_id=" + encodeURIComponent(proposalId)}>{t("detailsToggle")} <NeryaGlyph name="arrowUpRight" size={16} /></Link>}
          {state === "applied" && strategyId && !details ? (
            <Link
              href={`/strategies/${encodeURIComponent(strategyId)}`}
              className="btn btn-ghost cursor-pointer text-xs"
            >
              {t("openStrategy")}
            </Link>
          ) : null}
          {!TERMINAL_STATES.has(state) ? (
            // Icon-only: delete is the rare escape hatch here, so it
            // shouldn't carry the same visual weight as the primary
            // "Approve and add" action on every proposal row.
            <button
              onClick={() => void remove()}
              disabled={busy || deleting}
              className="btn btn-ghost cursor-pointer text-xs text-rose-300 hover:text-rose-200 disabled:opacity-50 disabled:cursor-not-allowed"
              title={t("delete")}
              aria-label={t("delete")}
            >
              <TrashIcon size={14} />
              {deleting ? tCommon("working") : null}
            </button>
          ) : null}
          {!TERMINAL_STATES.has(state) && state !== "draft" && packageProposal ? (
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

      {state === "applied" && strategyId && <WorkflowPromotionReceipt key={proposalId} strategyId={strategyId} proposalId={proposalId} receipt={promotionReceipt || recordedPromotion} />}
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
      ? "border-danger/30 bg-danger/10 text-danger"
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

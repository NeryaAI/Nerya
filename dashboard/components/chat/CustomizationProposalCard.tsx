"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { clientApi } from "../../lib/clientApi";
import { confirm, toast } from "../../lib/dialogs";
import { Pill } from "../Page";
import {
  AgentsIcon,
  EvolutionIcon,
  OverviewIcon,
  PuzzleIcon,
  SettingsIcon,
  ShieldCheckIcon,
  SkillsIcon,
  XIcon,
} from "../icons";

export type CustomizationResourceKind =
  | "workspace_ui"
  | "skill"
  | "agent"
  | "config"
  | "provider";

export type CustomizationCardView = {
  mode: "proposal" | "outcome";
  id: string;
  action: string;
  kind: string;
  state: string;
  summary: string;
  target?: string;
  resource: CustomizationResourceKind;
  href: string;
  pages: string[];
  widgets: string[];
  operations: string[];
  warnings: string[];
  diff?: string;
  metadata: Record<string, unknown>;
  raw: unknown;
};

const PROPOSAL_ACTIONS = new Set([
  "workspace_ui_propose",
  "evolve_skill_proposal",
  "evolve_core_config_patch",
  "evolve_provider_proposal",
]);

const ROLE_ACTIONS = new Set(["role_save", "role_delete"]);

const CUSTOMIZATION_PROPOSAL_KINDS = new Set([
  "core_config_patch",
  "skill_proposal",
  "provider_proposal",
  "learning_update",
  "prompt_patch",
  "script_proposal",
  "trigger_route_patch",
  "skill_install_request",
  "skill_scaffold",
  "gateway_platform_proposal",
  "core_feature_proposal",
  "evolution_asset_proposal",
]);

const TERMINAL_STATES = new Set([
  "applied",
  "rejected",
  "rolled_back",
  "superseded",
]);

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function normalizedAction(value: unknown): string {
  const text = stringValue(value).toLowerCase();
  if (!text) return "";
  const parts = text.split(".").filter(Boolean);
  return parts[parts.length - 1] || text;
}

function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const raw = value.trim();
  if (!raw) return value;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    // Compacted tool results often keep prose before one JSON object. Recover
    // the first complete object without being confused by braces in strings.
  }
  const start = raw.indexOf("{");
  if (start < 0) return value;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < raw.length; index += 1) {
    const char = raw[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') inString = true;
    else if (char === "{") depth += 1;
    else if (char === "}") {
      depth -= 1;
      if (depth === 0) {
        try {
          return JSON.parse(raw.slice(start, index + 1)) as unknown;
        } catch {
          return value;
        }
      }
    }
  }
  return value;
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (typeof item === "string" || typeof item === "number") {
        return String(item).trim();
      }
      const record = recordOf(item);
      return stringValue(record.id) || stringValue(record.name);
    })
    .filter(Boolean);
}

function firstNonEmptyRecord(...values: unknown[]): Record<string, unknown> {
  for (const value of values) {
    const record = recordOf(value);
    if (Object.keys(record).length) return record;
  }
  return {};
}

function diffText(value: unknown): string {
  if (typeof value === "string") return value.trim();
  const record = recordOf(value);
  return (
    stringValue(record.text) ||
    stringValue(record.diff_patch) ||
    stringValue(record.patch)
  );
}

function operationNames(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (typeof item === "string") return item.trim();
      const record = recordOf(item);
      return stringValue(record.op) || stringValue(record.action);
    })
    .filter(Boolean);
}

function isStrategyProposal(record: Record<string, unknown>, action: string): boolean {
  const kind = stringValue(record.kind || record.proposal_kind).toLowerCase();
  return kind.includes("strategy") || action.startsWith("strategy_");
}

function isCustomizationProposalRecord(
  record: Record<string, unknown>,
  action: string,
): boolean {
  const id = stringValue(record.proposal_id) || stringValue(record.id);
  if (!id || isStrategyProposal(record, action)) return false;
  const kind = stringValue(record.kind || record.proposal_kind).toLowerCase();
  return CUSTOMIZATION_PROPOSAL_KINDS.has(kind) || PROPOSAL_ACTIONS.has(action);
}

type FoundProposal = {
  proposal: Record<string, unknown>;
  context: Record<string, unknown>;
};

function firstProposalRecord(
  value: unknown,
  actionHint = "",
  depth = 0,
): FoundProposal | null {
  if (depth > 6) return null;
  const parsed = parseMaybeJson(value);
  if (Array.isArray(parsed)) {
    for (const item of parsed) {
      const found = firstProposalRecord(item, actionHint, depth + 1);
      if (found) return found;
    }
    return null;
  }
  const record = recordOf(parsed);
  if (!Object.keys(record).length) return null;
  const action =
    normalizedAction(record.action) ||
    normalizedAction(actionHint) ||
    normalizedAction(record.name);

  // Most proposal tools return a wrapper with useful affected-resource data
  // plus a nested canonical Proposal. Inspect it first, then merge the wrapper
  // into the context used by the card.
  if (record.proposal !== undefined) {
    const nested = firstProposalRecord(record.proposal, action, depth + 1);
    if (nested) {
      return {
        proposal: nested.proposal,
        context: { ...record, ...nested.context },
      };
    }
  }

  if (isCustomizationProposalRecord(record, action)) {
    return { proposal: record, context: record };
  }

  for (const key of ["result", "data", "payload", "output"]) {
    const nested = firstProposalRecord(record[key], action, depth + 1);
    if (nested) {
      return {
        proposal: nested.proposal,
        context: { ...record, ...nested.context },
      };
    }
  }

  const content = Array.isArray(record.content) ? record.content : [];
  for (const part of content) {
    const partRecord = recordOf(part);
    for (const candidate of [partRecord.data, partRecord.text, part]) {
      const nested = firstProposalRecord(candidate, action, depth + 1);
      if (nested) {
        return {
          proposal: nested.proposal,
          context: { ...record, ...nested.context },
        };
      }
    }
  }
  return null;
}

function firstRoleRecord(value: unknown, actionHint: string, depth = 0): Record<string, unknown> | null {
  if (depth > 6) return null;
  const parsed = parseMaybeJson(value);
  if (Array.isArray(parsed)) {
    for (const item of parsed) {
      const found = firstRoleRecord(item, actionHint, depth + 1);
      if (found) return found;
    }
    return null;
  }
  const record = recordOf(parsed);
  if (!Object.keys(record).length) return null;
  // Role payloads legitimately use `name` for the role id.  Do not treat it
  // as a tool action or it would shadow the enclosing role_save/role_delete
  // action hint and prevent the success outcome card from being recognised.
  const action =
    normalizedAction(record.action) ||
    normalizedAction(actionHint);
  const roleName =
    stringValue(record.role_name) ||
    stringValue(record.name) ||
    stringValue(record.id);
  if (ROLE_ACTIONS.has(action) && roleName) return record;

  for (const key of ["role", "result", "data", "payload", "output"]) {
    const nested = firstRoleRecord(record[key], action, depth + 1);
    if (nested) return nested;
  }
  const content = Array.isArray(record.content) ? record.content : [];
  for (const part of content) {
    const partRecord = recordOf(part);
    const nested =
      firstRoleRecord(partRecord.data, action, depth + 1) ||
      firstRoleRecord(partRecord.text, action, depth + 1);
    if (nested) return nested;
  }
  return null;
}

function resourceFor(
  proposal: Record<string, unknown>,
  context: Record<string, unknown>,
  metadata: Record<string, unknown>,
  action: string,
): CustomizationResourceKind {
  const kind = stringValue(proposal.kind || proposal.proposal_kind).toLowerCase();
  const target = (
    stringValue(proposal.target) || stringValue(context.target)
  ).toLowerCase();
  const explicit = stringValue(context.resource_kind).toLowerCase();
  if (
    action === "workspace_ui_propose" ||
    explicit === "workspace_ui" ||
    target === "ui/workspace.yml" ||
    metadata.workspace_ui === true
  ) {
    return "workspace_ui";
  }
  if (target.includes("agents.yml") || target.startsWith("agents/")) return "agent";
  if (
    action === "evolve_skill_proposal" ||
    kind === "skill_proposal" ||
    kind === "skill_install_request" ||
    kind === "skill_scaffold" ||
    target.startsWith("skills/")
  ) {
    return "skill";
  }
  if (action === "evolve_provider_proposal" || kind === "provider_proposal") {
    return "provider";
  }
  return "config";
}

function hrefFor(
  resource: CustomizationResourceKind,
  pages: string[],
  operations: string[],
): string {
  if (resource === "workspace_ui") {
    const removesPage = operations.some((operation) =>
      ["remove_page", "page.remove"].includes(operation.toLowerCase()),
    );
    return pages.length && !removesPage
      ? `/workspace/pages/${encodeURIComponent(pages[0])}`
      : "/dashboard";
  }
  if (resource === "skill") return "/skills";
  if (resource === "agent") return "/agents";
  return "/self-evolution";
}

export function isCustomizationTool(value: unknown): boolean {
  const action =
    typeof value === "string"
      ? normalizedAction(value)
      : normalizedAction(recordOf(value).action || recordOf(value).name);
  return PROPOSAL_ACTIONS.has(action) || ROLE_ACTIONS.has(action);
}

export function customizationFromToolResult(
  value: unknown,
  actionHint = "",
): CustomizationCardView | null {
  const action = normalizedAction(actionHint);
  if (ROLE_ACTIONS.has(action)) {
    const role = firstRoleRecord(value, action);
    if (!role) return null;
    const roleName =
      stringValue(role.role_name) ||
      stringValue(role.name) ||
      stringValue(role.id);
    if (!roleName) return null;
    const deleteAction = action === "role_delete";
    const deleted = role.deleted === true;
    return {
      mode: "outcome",
      id: `role:${roleName}`,
      action,
      kind: deleteAction
        ? deleted
          ? "agent_role_deleted"
          : "agent_role_missing"
        : "agent_role_saved",
      state: "applied",
      summary: roleName,
      target: roleName,
      resource: "agent",
      href: "/agents",
      pages: [],
      widgets: [],
      operations: [action],
      warnings: [],
      metadata: role,
      raw: value,
    };
  }

  const found = firstProposalRecord(value, action);
  if (!found) return null;
  const { proposal, context } = found;
  const proposalAction =
    normalizedAction(context.action) ||
    action ||
    normalizedAction(context.name);
  if (isStrategyProposal(proposal, proposalAction)) return null;

  const id = stringValue(proposal.proposal_id) || stringValue(proposal.id);
  if (!id) return null;
  const metadata = {
    ...recordOf(context.metadata),
    ...recordOf(proposal.metadata),
  };
  const affected = firstNonEmptyRecord(context.affected, metadata.affected);
  const pages = stringArray(affected.pages);
  const widgets = stringArray(affected.widgets);
  const resource = resourceFor(proposal, context, metadata, proposalAction);
  const target = stringValue(proposal.target) || stringValue(context.target);
  const kind =
    stringValue(proposal.kind || proposal.proposal_kind) ||
    (resource === "workspace_ui" ? "core_config_patch" : "proposal");
  const state =
    stringValue(proposal.state) ||
    stringValue(context.state) ||
    "pending_review";
  const summary =
    stringValue(proposal.summary) ||
    stringValue(context.summary) ||
    stringValue(context.title) ||
    id;
  const operations = operationNames(context.operations);
  const warnings = stringArray(context.warnings);
  const diff =
    diffText(context.diff) ||
    diffText(proposal.diff) ||
    stringValue(context.diff_patch) ||
    stringValue(proposal.diff_patch);

  return {
    mode: "proposal",
    id,
    action: proposalAction,
    kind,
    state,
    summary,
    target: target || undefined,
    resource,
    href: hrefFor(resource, pages, operations),
    pages,
    widgets,
    operations,
    warnings,
    diff: diff || undefined,
    metadata,
    raw: value,
  };
}

function stateTone(state: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  switch (state) {
    case "applied":
      return "ok";
    case "approved":
      return "brand";
    case "pending_review":
    case "proposed":
      return "warn";
    case "rejected":
    case "rolled_back":
      return "danger";
    default:
      return "neutral";
  }
}

function responseError(value: unknown, fallback: string, depth = 0): string {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (depth > 4) return fallback;
  const record = recordOf(value);
  const direct =
    stringValue(record.error) ||
    stringValue(record.reason) ||
    stringValue(record.message);
  if (direct) return direct;
  if (record.detail !== undefined) {
    const nested = responseError(record.detail, "", depth + 1);
    if (nested) return nested;
  }
  const blockers = stringArray(recordOf(record.action_gates).blockers);
  if (blockers.length) return blockers.join(", ");
  return fallback;
}

function ResourceIcon({ resource }: { resource: CustomizationResourceKind }) {
  const className = "text-brand-300";
  if (resource === "workspace_ui") return <OverviewIcon size={15} className={className} />;
  if (resource === "skill") return <SkillsIcon size={15} className={className} />;
  if (resource === "agent") return <AgentsIcon size={15} className={className} />;
  if (resource === "provider") return <EvolutionIcon size={15} className={className} />;
  return <SettingsIcon size={15} className={className} />;
}

export function CustomizationProposalCard({
  view,
  compact = false,
}: {
  view: CustomizationCardView;
  compact?: boolean;
}) {
  const t = useTranslations("customizationProposal");
  const tCommon = useTranslations("common");
  const [stateOverride, setStateOverride] = useState<string | null>(null);
  const [busy, setBusy] = useState<"apply" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const state = (stateOverride || view.state || "pending_review").toLowerCase();
  const terminal = TERMINAL_STATES.has(state);
  const resourceLabel = t(`resource.${view.resource}`);
  const summary =
    view.mode === "outcome"
      ? view.kind === "agent_role_deleted"
        ? t("agentDeleted", { name: view.summary })
        : view.kind === "agent_role_missing"
        ? t("agentMissing", { name: view.summary })
        : t("agentSaved", { name: view.summary })
      : view.summary || t("fallbackSummary");
  const stateLabel = t.has(`state.${state}`)
    ? t(`state.${state}`)
    : state;
  useEffect(() => {
    const initialState = (view.state || "pending_review").toLowerCase();
    if (
      stateOverride !== null ||
      view.mode !== "proposal" ||
      TERMINAL_STATES.has(initialState)
    ) {
      return;
    }
    let active = true;
    void clientApi
      .proposalDetail(view.id)
      .then((proposal) => {
        if (!active) return;
        const currentState = stringValue(recordOf(proposal).state).toLowerCase();
        if (currentState) setStateOverride(currentState);
      })
      .catch(() => {
        // The original tool result remains a usable offline snapshot. A
        // transient status refresh failure must not hide the review card.
      });
    return () => {
      active = false;
    };
  }, [stateOverride, view.id, view.mode, view.state]);

  const cardTone =
    state === "applied"
      ? "border-accent-500/30 bg-accent-500/[0.055]"
      : state === "rejected" || state === "rolled_back"
      ? "border-danger/30 bg-danger/[0.05]"
      : "border-brand-500/25 bg-brand-500/[0.045]";

  async function approveAndApply() {
    if (view.mode !== "proposal") return;
    const proceed = await confirm({
      title: t("confirmTitle"),
      message: t("confirmMessage", { summary }),
      okLabel: state === "approved" ? t("apply") : t("approveApply"),
      cancelLabel: tCommon("cancel"),
      tone: "brand",
    });
    if (!proceed) return;

    setBusy("apply");
    setError(null);
    setNotice(null);
    try {
      if (state !== "approved") {
        const approved = await clientApi.proposalApprove(view.id);
        const approvedState = stringValue(recordOf(approved).state).toLowerCase();
        if (approvedState === "applied") {
          setStateOverride("applied");
          const message = t("appliedNotice", { resource: resourceLabel });
          setNotice(message);
          toast({ message, tone: "ok" });
          return;
        }
        if (approvedState !== "approved") {
          throw new Error(responseError(approved, t("approveFailed")));
        }
        setStateOverride("approved");
      }

      const applied =
        view.resource === "workspace_ui"
          ? await clientApi.workspaceUiApply({ proposal_id: view.id })
          : await clientApi.proposalApply(view.id);
      const appliedRecord = recordOf(applied);
      const appliedState = stringValue(appliedRecord.state).toLowerCase();
      const applySucceeded =
        appliedRecord.ok === true &&
        (view.resource !== "workspace_ui" ||
          appliedRecord.applied === true ||
          appliedState === "applied");
      if (!applySucceeded) {
        throw new Error(responseError(applied, t("applyFailed")));
      }
      setStateOverride("applied");
      const message = t("appliedNotice", { resource: resourceLabel });
      setNotice(message);
      toast({ message, tone: "ok" });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  async function rejectProposal() {
    if (view.mode !== "proposal") return;
    const proceed = await confirm({
      title: t("rejectTitle"),
      message: t("rejectMessage", { summary }),
      okLabel: t("reject"),
      cancelLabel: tCommon("cancel"),
      tone: "danger",
    });
    if (!proceed) return;

    setBusy("reject");
    setError(null);
    setNotice(null);
    try {
      const rejected = await clientApi.proposalReject(view.id, "rejected from chat customization card");
      const rejectedState = stringValue(recordOf(rejected).state).toLowerCase();
      if (rejectedState !== "rejected") {
        throw new Error(responseError(rejected, t("rejectFailed")));
      }
      setStateOverride("rejected");
      const message = t("rejectedNotice", { resource: resourceLabel });
      setNotice(message);
      toast({ message, tone: "default" });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div
      className={[
        "rounded-lg border text-xs [overflow-wrap:anywhere]",
        cardTone,
        compact ? "p-3" : "p-4",
      ].join(" ")}
      data-customization-card={view.resource}
      data-proposal-id={view.mode === "proposal" ? view.id : undefined}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <ResourceIcon resource={view.resource} />
            <span className="font-medium text-ink-100">{resourceLabel}</span>
            <Pill tone={stateTone(state)}>{stateLabel}</Pill>
            {view.mode === "proposal" ? (
              <Pill tone="neutral">{view.kind}</Pill>
            ) : null}
          </div>
          <p className="mt-1.5 text-[12px] leading-relaxed text-ink-300">
            {summary}
          </p>

          {view.pages.length || view.widgets.length || view.operations.length ? (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              {view.pages.length ? (
                <Pill tone="neutral">{t("pages", { count: view.pages.length })}</Pill>
              ) : null}
              {view.widgets.length ? (
                <Pill tone="neutral">{t("widgets", { count: view.widgets.length })}</Pill>
              ) : null}
              {view.operations.slice(0, 5).map((operation, index) => (
                <code
                  key={`${operation}-${index}`}
                  className="rounded border border-ink-700/70 bg-ink-900/60 px-1.5 py-0.5 text-[10px] text-ink-300"
                >
                  {operation}
                </code>
              ))}
            </div>
          ) : null}
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {view.mode === "proposal" && !terminal ? (
            <button
              type="button"
              onClick={() => void rejectProposal()}
              disabled={busy !== null}
              className="btn btn-ghost cursor-pointer text-xs text-rose-300 hover:text-rose-200 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <XIcon size={14} />
              {busy === "reject" ? tCommon("working") : t("reject")}
            </button>
          ) : null}
          {view.mode === "proposal" && !terminal ? (
            <button
              type="button"
              onClick={() => void approveAndApply()}
              disabled={busy !== null}
              className="btn btn-primary cursor-pointer text-xs disabled:cursor-not-allowed disabled:opacity-50"
            >
              <ShieldCheckIcon size={14} />
              {busy === "apply"
                ? tCommon("working")
                : state === "approved"
                ? t("apply")
                : t("approveApply")}
            </button>
          ) : null}
          {state === "applied" || view.mode === "outcome" ? (
            <Link href={view.href} className="btn btn-ghost cursor-pointer text-xs">
              {t("openResource", { resource: resourceLabel })}
            </Link>
          ) : null}
        </div>
      </div>

      {view.pages.length || view.widgets.length ? (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {view.pages.length ? (
            <div className="rounded-md border border-ink-700/60 bg-ink-900/35 px-3 py-2">
              <div className="text-[10px] font-medium uppercase tracking-wide text-ink-500">
                {t("affectedPages")}
              </div>
              <div className="mt-1 font-mono text-[11px] text-ink-300">
                {view.pages.join(", ")}
              </div>
            </div>
          ) : null}
          {view.widgets.length ? (
            <div className="rounded-md border border-ink-700/60 bg-ink-900/35 px-3 py-2">
              <div className="text-[10px] font-medium uppercase tracking-wide text-ink-500">
                {t("affectedWidgets")}
              </div>
              <div className="mt-1 font-mono text-[11px] text-ink-300">
                {view.widgets.join(", ")}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {view.warnings.length ? (
        <div className="mt-3 rounded-md border border-warn/30 bg-warn/10 px-3 py-2 text-[11px] leading-relaxed text-warn">
          <div className="font-medium">{t("warnings", { count: view.warnings.length })}</div>
          <div className="mt-1">{view.warnings.slice(0, 4).join(" · ")}</div>
        </div>
      ) : null}

      {view.mode === "proposal" ? (
        <details className="mt-3 rounded-md border border-ink-700/60 bg-ink-900/30">
          <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-[11px] font-medium text-ink-400 hover:text-ink-200">
            <PuzzleIcon size={13} />
            {t("reviewDetails")}
          </summary>
          <div className="space-y-2 border-t border-ink-700/60 px-3 py-2.5">
            <div className="font-mono text-[10px] leading-relaxed text-ink-500">
              {t("proposalId")}: {view.id}
              {view.target ? ` · ${t("target")}: ${view.target}` : ""}
            </div>
            {view.diff ? (
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md border border-ink-700/70 bg-ink-950/70 p-2.5 text-[10px] leading-relaxed text-ink-300">
                {view.diff}
              </pre>
            ) : (
              <div className="text-[11px] text-ink-500">{t("diffUnavailable")}</div>
            )}
            <Link href="/inbox" className="inline-flex text-[11px] text-brand-300 hover:text-brand-200">
              {t("reviewInbox")}
            </Link>
          </div>
        </details>
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


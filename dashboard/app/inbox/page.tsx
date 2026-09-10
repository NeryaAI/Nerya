"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Advanced,
  Card,
  Empty,
  ErrorBanner,
  Json,
  PageBody,
  PageHeader,
  Pill,
  StatusDot,
} from "../../components/Page";
import { PromptGuardReviewCard } from "../../components/PromptGuardReviewCard";
import { RefreshIcon } from "../../components/icons";
import { clientApi } from "../../lib/clientApi";
import { confirm, toast } from "../../lib/dialogs";
import { formatTs, formatTsShort } from "../../lib/format";
import type {
  EnvelopeSeverity,
  InboxItem,
  InboxItemType,
  InboxItemsEnvelope,
  InboxResolveRequest,
  OperatorAction,
} from "../../lib/operatorTypes";

// One severity → one tone, shared by the row dot and the Pill so the two
// encodings can never disagree. ``info`` is neutral — green is reserved
// for success/health across the app.
const SEVERITY_TONE: Record<EnvelopeSeverity, "neutral" | "warn" | "danger"> = {
  info: "neutral",
  warn: "warn",
  danger: "danger",
};
const BATCH_BUSY_KEY = "__batch__";
const BATCH_DECISION_ORDER = [
  "approve",
  "reject",
  "apply",
  "rollback",
  "dismiss",
] as const;
// Backend deep links look like ``/inbox?type=approval&id=…`` — only
// these ``type`` values map onto the filter tabs.
const DEEP_LINK_TYPES = new Set<InboxItemType>([
  "approval",
  "proposal",
  "failed_task",
  "notification",
  "provider_error",
]);
type BatchDecision = (typeof BATCH_DECISION_ORDER)[number];
type TranslateFn = (key: string, values?: Record<string, string | number>) => string;

export default function InboxPage() {
  const t = useTranslations("inbox");
  const tCommon = useTranslations("common");
  const router = useRouter();
  const [env, setEnv] = useState<InboxItemsEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [filterType, setFilterType] = useState<InboxItemType | "all">("all");
  const [onlyAction, setOnlyAction] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  // Item id from a backend deep link (``/inbox?type=approval&id=…``),
  // consumed once the list has loaded.
  const [deepLinkId, setDeepLinkId] = useState<string | null>(null);
  // Detail pane body is expanded by default; Enter collapses/expands it.
  const [detailOpen, setDetailOpen] = useState(true);
  // Row element for the currently selected item — used to keep the
  // selection in view during j/k navigation.
  const selectedRowRef = useRef<HTMLLIElement | null>(null);

  const ALL_TYPES: { id: InboxItemType | "all"; label: string }[] = [
    { id: "all", label: t("typeAll") },
    { id: "approval", label: t("typeApproval") },
    { id: "proposal", label: t("typeProposal") },
    { id: "failed_task", label: t("typeFailedTask") },
    { id: "notification", label: t("typeNotification") },
    { id: "provider_error", label: t("typeProviderError") },
  ];

  const load = useCallback(async () => {
    try {
      const next = await clientApi.inboxItems({
        type: filterType === "all" ? undefined : filterType,
        requires_action: onlyAction || undefined,
        limit: 200,
      });
      setEnv(next);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [filterType, onlyAction]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);

  useEffect(() => {
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  // Deep-link entry: read ``?type=``/``?id=`` from the URL once on mount
  // (window.location instead of useSearchParams so this page needs no
  // Suspense boundary) and apply the filter / pending selection.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const type = params.get("type");
    if (type && DEEP_LINK_TYPES.has(type as InboxItemType)) {
      setFilterType(type as InboxItemType);
    }
    const id = params.get("id");
    if (id) setDeepLinkId(id);
  }, []);

  const items = useMemo(
    () =>
      [...(env?.data?.items ?? [])].sort(
        (a, b) => parseCreatedAt(b.created_at) - parseCreatedAt(a.created_at),
      ),
    [env?.data?.items],
  );
  const batchableIds = useMemo(
    () =>
      items
        .filter((item) => getBatchDecisions(item.actions).length > 0)
        .map((item) => item.id),
    [items],
  );
  const selectedBatchItems = useMemo(() => {
    const selectedIdSet = new Set(selectedIds);
    return items.filter((item) => selectedIdSet.has(item.id));
  }, [items, selectedIds]);
  const sharedBatchDecisions = useMemo(
    () => getSharedBatchDecisions(selectedBatchItems),
    [selectedBatchItems],
  );
  const selected = useMemo(
    () => items.find((i) => i.id === selectedId) ?? items[0] ?? null,
    [items, selectedId],
  );

  useEffect(() => {
    const valid = new Set(batchableIds);
    setSelectedIds((prev) => prev.filter((id) => valid.has(id)));
  }, [batchableIds]);

  // Select + expand the deep-linked item once the list is loaded.
  useEffect(() => {
    if (!deepLinkId || items.length === 0) return;
    setSelectedId(deepLinkId);
    setDeepLinkId(null);
  }, [deepLinkId, items]);

  // Keyboard navigation: j/k moves the selection through the list, Enter
  // expands/collapses the detail pane. Page-level listener, suppressed
  // while typing in form fields, pressing buttons, or when a dialog is
  // open.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.tagName === "BUTTON" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (document.querySelector("[role='dialog']")) return;
      if (event.key === "j" || event.key === "k") {
        event.preventDefault();
        if (items.length === 0) return;
        const delta = event.key === "j" ? 1 : -1;
        const currentIdx = items.findIndex((i) => i.id === selectedId);
        const nextIdx =
          currentIdx < 0
            ? 0
            : Math.min(items.length - 1, Math.max(0, currentIdx + delta));
        setSelectedId(items[nextIdx].id);
        setDetailOpen(true);
      } else if (event.key === "Enter") {
        setDetailOpen((open) => !open);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [items, selectedId]);

  // Keep the keyboard-selected row visible while j/k-walking the list.
  useEffect(() => {
    selectedRowRef.current?.scrollIntoView({ block: "nearest" });
  }, [selected?.id]);

  async function runAction(item: InboxItem, act: OperatorAction) {
    setBusyKey(item.id);
    try {
      // Map the inbox action semantically onto /inbox/resolve. The
      // backend dispatches on the item-id prefix to the right
      // subsystem (approvals, evolution, recovery, …) so the UI
      // doesn't need to know which subsystem owns each item.
      const decision = resolveDecision(act);
      const result = await clientApi.inboxResolve({ id: item.id, decision });
      if (!result.ok) {
        setError(result.summary || t("resolveFailed"));
        return;
      }
      setError(null);
      toast({ tone: toastTone(result.status), message: result.summary });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  async function runBatch(decision: BatchDecision) {
    if (selectedBatchItems.length === 0) return;
    if (decision === "approve" || decision === "rollback") {
      // Batch approve/rollback fans out to N items at once — require an
      // explicit confirmation before dispatching.
      const okToRun = await confirm({
        title: tCommon("confirm"),
        message: `${t("selectedCount", {
          count: selectedBatchItems.length,
        })} · ${batchDecisionLabel(decision, t, tCommon)}`,
        okLabel: batchDecisionLabel(decision, t, tCommon),
        cancelLabel: tCommon("cancel"),
        tone: "danger",
      });
      if (!okToRun) return;
    }
    setBusyKey(BATCH_BUSY_KEY);
    try {
      const result = await clientApi.inboxResolve({
        ids: selectedBatchItems.map((item) => item.id),
        decision,
      });
      if (!result.ok) {
        setError(result.summary || t("resolveFailed"));
        return;
      }
      setError(null);
      toast({ tone: toastTone(result.status), message: result.summary });
      const resolvedIds = new Set(
        (result.data.results ?? [])
          .filter((row) => row.ok)
          .map((row) => row.id),
      );
      if (resolvedIds.size > 0) {
        setSelectedIds((prev) => prev.filter((id) => !resolvedIds.has(id)));
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  function toggleBatchSelection(itemId: string, checked: boolean) {
    setSelectedIds((prev) => {
      if (checked) {
        if (prev.includes(itemId)) return prev;
        return [...prev, itemId];
      }
      return prev.filter((id) => id !== itemId);
    });
  }

  function selectAllBatchable() {
    setSelectedIds(batchableIds);
    if (!selectedId && batchableIds[0]) {
      setSelectedId(batchableIds[0]);
    }
  }

  return (
    <div>
      {error ? <ErrorBanner error={error} /> : null}
      <PageBody>
        <PageHeader
          eyebrow={t("eyebrow")}
          title={t("title")}
          description={
            env?.summary ||
            t("description")
          }
          actions={
            <div className="flex items-center gap-2">
              {env ? (
                <Pill tone={statusTone(env.status)}>{statusLabel(env.status, t)}</Pill>
              ) : null}
              <button
                onClick={load}
                className="btn btn-ghost"
              >
                <RefreshIcon size={14} />
                {tCommon("refresh")}
              </button>
            </div>
          }
        />

        <PromptGuardReviewCard />

        <div className="flex flex-wrap items-center gap-2 border-b border-brand-500/10 pb-3">
          {ALL_TYPES.map((opt) => (
            <button
              key={opt.id}
              onClick={() => setFilterType(opt.id)}
              className={`text-[12px] px-2.5 py-1 rounded-md border transition ${
                filterType === opt.id
                  ? "bg-brand-500/15 text-brand-100 border-brand-500/40"
                  : "text-ink-400 border-transparent hover:text-ink-200 hover:border-brand-500/20"
              }`}
            >
              {opt.label}
            </button>
          ))}
          <label className="ml-auto flex items-center gap-2 text-[12px] text-ink-400">
            <input
              type="checkbox"
              checked={onlyAction}
              onChange={(e) => setOnlyAction(e.target.checked)}
            />
            {t("onlyItemsNeedingAction")}
          </label>
        </div>

        {selectedIds.length > 0 ? (
          // Contextual batch bar: only exists while a selection is active,
          // so the idle page keeps a single clean column rhythm instead of
          // a permanent toolbar band.
          <div className="flex flex-wrap items-center gap-2 rounded-xl border border-brand-500/25 bg-brand-500/[0.06] px-3 py-2">
            <span className="text-[12px] text-brand-100">
              {t("selectedCount", { count: selectedIds.length })}
            </span>
            <button
              onClick={() => setSelectedIds([])}
              disabled={busyKey !== null}
              className="text-[11px] px-2 py-1 rounded-md border border-brand-500/15 text-ink-300 hover:bg-brand-500/10 disabled:opacity-40"
            >
              {t("clearSelection")}
            </button>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              {sharedBatchDecisions.map((decision) => (
                <button
                  key={decision}
                  onClick={() => runBatch(decision)}
                  disabled={busyKey !== null || selectedBatchItems.length === 0}
                  className={`text-[11px] px-2 py-1 rounded-md border ${
                    decision === "reject" || decision === "rollback"
                      ? "border-danger/40 text-danger hover:bg-danger/10"
                      : decision === "apply"
                      ? "border-warn/40 text-warn hover:bg-warn/10"
                      : "border-brand-500/40 text-brand-200 hover:bg-brand-500/10"
                  } disabled:opacity-40`}
                >
                  {batchDecisionLabel(decision, t, tCommon)}
                </button>
              ))}
              {sharedBatchDecisions.length === 0 ? (
                <span className="text-[11px] text-warn">
                  {t("noSharedBatchAction")}
                </span>
              ) : null}
            </div>
          </div>
        ) : null}

        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <Card
            title={t("itemsCount", { count: items.length })}
            description={t("needAction", { count: env?.data.needs_action ?? 0 })}
            padded={false}
            actions={
              <button
                onClick={selectAllBatchable}
                disabled={batchableIds.length === 0 || busyKey !== null}
                className="text-[11px] px-2.5 py-1 rounded-md border border-brand-500/25 text-brand-200 hover:bg-brand-500/10 disabled:opacity-40"
              >
                {t("selectAllBatchable", { count: batchableIds.length })}
              </button>
            }
          >
            {loading && items.length === 0 ? (
              <div className="space-y-2 p-3" aria-hidden>
                {[0, 1, 2, 3, 4].map((row) => (
                  <div
                    key={row}
                    className="rounded-lg border border-brand-500/10 px-3 py-2.5"
                  >
                    <div className="skeleton h-3 w-2/3" />
                    <div className="skeleton mt-2 h-2.5 w-1/3" />
                  </div>
                ))}
              </div>
            ) : items.length === 0 ? (
              <Empty label={t("inboxEmpty")} />
            ) : (
              <ul role="listbox" className="embedded-list-scroll-lg">
                {items.map((item) => {
                  const batchable = getBatchDecisions(item.actions).length > 0;
                  const batchSelected = selectedIds.includes(item.id);
                  const isSelected =
                    (selected?.id ?? items[0]?.id) === item.id;
                  return (
                    <li
                      key={item.id}
                      ref={(el) => {
                        if (el && isSelected) selectedRowRef.current = el;
                      }}
                      role="option"
                      aria-selected={isSelected}
                      tabIndex={0}
                      className={`border-b border-l-2 border-brand-500/5 px-3 py-2.5 last:border-b-0 cursor-pointer hover:bg-brand-500/5 ${
                        isSelected
                          ? "border-l-brand-400 bg-brand-500/10"
                          : "border-l-transparent"
                      }`}
                      onClick={() => {
                        setSelectedId(item.id);
                        setDetailOpen(true);
                      }}
                    >
                      <div className="flex items-start gap-2">
                        <div
                          className="pt-0.5"
                          onClick={(event) => event.stopPropagation()}
                        >
                          <input
                            type="checkbox"
                            checked={batchSelected}
                            disabled={!batchable || busyKey !== null}
                            onChange={(event) =>
                              toggleBatchSelection(item.id, event.target.checked)
                            }
                            title={batchable ? item.title : t("batchUnavailable")}
                          />
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <StatusDot tone={SEVERITY_TONE[item.severity]} />
                            <span className="text-[12px] text-ink-100 truncate flex-1">
                              {item.title}
                            </span>
                            <span className="text-[11px] text-ink-500 shrink-0">
                              {itemTypeLabel(item.type, t)}
                            </span>
                            <span className="text-[11px] text-ink-500 shrink-0 font-mono">
                              {formatTsShort(item.created_at)}
                            </span>
                            {item.requires_action ? (
                              <Pill tone="warn">{t("action")}</Pill>
                            ) : null}
                          </div>
                          {item.summary ? (
                            <div className="text-[11px] text-ink-500 mt-1 truncate">
                              {item.summary}
                            </div>
                          ) : null}
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </Card>

          <div className="xl:col-span-2 space-y-4">
            {selected ? (
              <Card
                title={selected.title}
                description={`${itemTypeLabel(selected.type, t)} · ${statusLabel(selected.status, t)} · ${t("createdAt", {
                  time: formatTs(selected.created_at),
                })}`}
                actions={
                  <Pill tone={SEVERITY_TONE[selected.severity]}>
                    {severityLabel(selected.severity, t)}
                  </Pill>
                }
              >
                {detailOpen ? (
                  <>
                    <div className="text-[12px] text-ink-200 whitespace-pre-wrap leading-relaxed mb-3">
                      {selected.summary || t("noSummary")}
                    </div>

                    <div className="flex flex-wrap items-center gap-2 mb-4">
                      {selected.actions.map((act) => {
                        // Approve is the core operator action — give it the
                        // standard `.btn` primary spec; reject gets the err
                        // outline. Everything else stays a compact secondary
                        // button.
                        const isPrimary = act.id === "approve";
                        const actionBtnClass = isPrimary
                          ? "btn-primary"
                          : act.id === "reject"
                          ? "btn border border-danger/40 text-danger hover:bg-danger/10"
                          : `text-[11px] px-2 py-1 rounded-md border ${
                              act.severity === "danger"
                                ? "border-danger/40 text-danger hover:bg-danger/10"
                                : act.severity === "warn"
                                ? "border-warn/40 text-warn hover:bg-warn/10"
                                : "border-brand-500/40 text-brand-200 hover:bg-brand-500/10"
                            }`;
                        return (
                          <button
                            key={act.id}
                            disabled={busyKey !== null}
                            onClick={() => {
                              if (act.href && !act.method) {
                                // Pure-navigation action: Next.js link target —
                                // use the client-side router instead of a full
                                // page reload.
                                router.push(act.href);
                                return;
                              }
                              runAction(selected, act);
                            }}
                            className={`${actionBtnClass} disabled:opacity-50`}
                            title={act.disabled_reason || actionLabel(act, t, tCommon)}
                          >
                            {actionLabel(act, t, tCommon)}
                          </button>
                        );
                      })}
                      {selected.source_refs.map((ref) =>
                        ref.href ? (
                          <Link
                            key={`${ref.kind}:${ref.id}`}
                            href={ref.href}
                            className="text-[11px] px-2 py-1 rounded-md border border-brand-500/25 text-ink-300 hover:bg-brand-500/10"
                          >
                            {ref.label || `${ref.kind}:${ref.id}`}
                          </Link>
                        ) : null,
                      )}
                    </div>

                    <Advanced
                      title={t("rawPayload")}
                      storageKey="nerya.inbox.advanced.raw"
                    >
                      <Json value={selected.data} />
                    </Advanced>
                  </>
                ) : null}
              </Card>
            ) : items.length > 0 ? (
              // Only reachable when an item exists but cannot be resolved —
              // never show "pick an item on the left" against an empty list.
              <Card title={t("selectItem")}>
                <div className="text-[12px] text-ink-500">
                  {t("selectItemHint")}
                </div>
              </Card>
            ) : null}
          </div>
        </div>
      </PageBody>
    </div>
  );
}

function statusTone(status: string): "ok" | "warn" | "danger" | "brand" {
  if (status === "ok") return "ok";
  if (status === "warn") return "warn";
  if (status === "blocked" || status === "error") return "danger";
  return "brand";
}

function resolveDecision(
  act: OperatorAction,
): Exclude<InboxResolveRequest["decision"], undefined> {
  if (act.id === "approve") return "approve";
  if (act.id === "reject") return "reject";
  if (act.id === "apply" || act.id === "promote") return "apply";
  if (act.id === "rollback") return "rollback";
  return "dismiss";
}

function getBatchDecisions(actions: OperatorAction[]): BatchDecision[] {
  const allowed = new Set<BatchDecision>();
  for (const act of actions) {
    const decision = normaliseBatchDecision(act);
    if (decision) allowed.add(decision);
  }
  return BATCH_DECISION_ORDER.filter((decision) => allowed.has(decision));
}

function getSharedBatchDecisions(items: InboxItem[]): BatchDecision[] {
  if (items.length === 0) return [];
  let shared = new Set(getBatchDecisions(items[0].actions));
  for (const item of items.slice(1)) {
    const current = new Set(getBatchDecisions(item.actions));
    shared = new Set([...shared].filter((decision) => current.has(decision)));
  }
  return BATCH_DECISION_ORDER.filter((decision) => shared.has(decision));
}

function normaliseBatchDecision(act: OperatorAction): BatchDecision | null {
  if (act.id === "approve") return "approve";
  if (act.id === "reject") return "reject";
  if (act.id === "apply" || act.id === "promote") return "apply";
  if (act.id === "rollback") return "rollback";
  if (act.id === "dismiss") return "dismiss";
  return null;
}

function batchDecisionLabel(
  decision: BatchDecision,
  t: TranslateFn,
  tCommon: TranslateFn,
) {
  if (decision === "approve") return tCommon("approve");
  if (decision === "reject") return tCommon("reject");
  if (decision === "apply") return t("apply");
  if (decision === "rollback") return t("rollback");
  return t("dismiss");
}

function actionLabel(
  act: OperatorAction,
  t: TranslateFn,
  tCommon: TranslateFn,
) {
  if (act.id === "approve") return tCommon("approve");
  if (act.id === "reject") return tCommon("reject");
  if (act.id === "apply" || act.id === "promote") return t("apply");
  if (act.id === "rollback") return t("rollback");
  if (act.id === "dismiss") return t("dismiss");
  if (act.id === "open") return t("open");
  if (act.id === "resume") return t("resume");
  if (act.id === "explain") return t("explain");
  if (act.id === "fix_provider") return t("configureProvider");
  return act.label;
}

function itemTypeLabel(
  type: InboxItemType,
  t: TranslateFn,
) {
  if (type === "approval") return t("typeApproval");
  if (type === "proposal") return t("typeProposal");
  if (type === "failed_task") return t("typeFailedTask");
  if (type === "notification") return t("typeNotification");
  if (type === "provider_error") return t("typeProviderError");
  return type;
}

// Severity/status raw enums get localized labels so the zh UI never
// shows bare "warn"/"pending_review" strings; unknown values fall
// through to the raw enum.
function severityLabel(
  severity: EnvelopeSeverity,
  t: TranslateFn,
) {
  const map: Record<EnvelopeSeverity, string> = {
    info: "severityInfo",
    warn: "severityWarn",
    danger: "severityDanger",
  };
  return t(map[severity]);
}

function statusLabel(status: string, t: TranslateFn): string {
  const map: Record<string, string> = {
    ok: "statusOk",
    pending: "statusPending",
    pending_review: "statusPendingReview",
    open: "statusOpen",
    resolved: "statusResolved",
    applied: "statusApplied",
    rejected: "statusRejected",
    blocked: "statusBlocked",
    failed: "statusFailed",
    error: "statusFailed",
  };
  const key = map[status];
  return key ? t(key) : status;
}

function parseCreatedAt(value: string | undefined) {
  if (!value) return 0;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : 0;
}

function toastTone(status: string): "ok" | "warn" | "error" {
  if (status === "warn") return "warn";
  if (status === "error" || status === "blocked") return "error";
  return "ok";
}

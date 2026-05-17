"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Advanced,
  Card,
  Empty,
  ErrorBanner,
  Json,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { PromptGuardReviewCard } from "../../components/PromptGuardReviewCard";
import { clientApi } from "../../lib/clientApi";
import { toast } from "../../lib/dialogs";
import { formatTs, formatTsShort } from "../../lib/format";
import type {
  EnvelopeSeverity,
  InboxItem,
  InboxItemType,
  InboxItemsEnvelope,
  InboxResolveRequest,
  OperatorAction,
} from "../../lib/operatorTypes";

const SEVERITY_TONE: Record<EnvelopeSeverity, "ok" | "warn" | "danger" | "brand"> = {
  info: "ok",
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
type BatchDecision = (typeof BATCH_DECISION_ORDER)[number];
type TranslateFn = (key: string, values?: Record<string, string | number>) => string;

export default function InboxPage() {
  const t = useTranslations("inbox");
  const tCommon = useTranslations("common");
  const [env, setEnv] = useState<InboxItemsEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [filterType, setFilterType] = useState<InboxItemType | "all">("all");
  const [onlyAction, setOnlyAction] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

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
                <Pill tone={statusTone(env.status)}>{env.status}</Pill>
              ) : null}
              <button
                onClick={load}
                className="text-[11px] px-2 py-0.5 rounded-md text-brand-200 border border-brand-500/25 hover:bg-brand-500/10"
              >
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

        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-brand-500/10 bg-brand-500/[0.04] px-3 py-2">
          <button
            onClick={selectAllBatchable}
            disabled={batchableIds.length === 0 || busyKey !== null}
            className="text-[11px] px-2.5 py-1 rounded-md border border-brand-500/25 text-brand-200 hover:bg-brand-500/10 disabled:opacity-40"
          >
            {t("selectAllBatchable", { count: batchableIds.length })}
          </button>
          <button
            onClick={() => setSelectedIds([])}
            disabled={selectedIds.length === 0 || busyKey !== null}
            className="text-[11px] px-2.5 py-1 rounded-md border border-brand-500/15 text-ink-300 hover:bg-brand-500/10 disabled:opacity-40"
          >
            {t("clearSelection")}
          </button>
          <span className="text-[12px] text-ink-300">
            {t("selectedCount", { count: selectedIds.length })}
          </span>
          <span className="text-[11px] text-ink-500">{t("sortedNewestFirst")}</span>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {sharedBatchDecisions.map((decision) => (
              <button
                key={decision}
                onClick={() => runBatch(decision)}
                disabled={busyKey !== null}
                className={`text-[11px] px-2 py-1 rounded-md border ${
                  decision === "reject" || decision === "rollback"
                    ? "border-rose-500/40 text-rose-200 hover:bg-rose-500/10"
                    : decision === "apply"
                    ? "border-amber-400/40 text-amber-200 hover:bg-amber-400/10"
                    : "border-brand-500/40 text-brand-200 hover:bg-brand-500/10"
                } disabled:opacity-40`}
              >
                {batchDecisionLabel(decision, t, tCommon)}
              </button>
            ))}
            {selectedIds.length > 0 && sharedBatchDecisions.length === 0 ? (
              <span className="text-[11px] text-amber-200">
                {t("noSharedBatchAction")}
              </span>
            ) : null}
          </div>
        </div>

        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <Card
            title={t("itemsCount", { count: items.length })}
            description={t("needAction", { count: env?.data.needs_action ?? 0 })}
            padded={false}
          >
            {loading && items.length === 0 ? (
              <div className="p-4 text-[12px] text-ink-500">{tCommon("loading")}</div>
            ) : items.length === 0 ? (
              <Empty label={t("inboxEmpty")} />
            ) : (
              <ul className="embedded-list-scroll-lg">
                {items.map((item) => {
                  const batchable = getBatchDecisions(item.actions).length > 0;
                  const batchSelected = selectedIds.includes(item.id);
                  return (
                    <li
                      key={item.id}
                      className={`px-3 py-2.5 border-b border-brand-500/5 last:border-b-0 cursor-pointer hover:bg-brand-500/5 ${
                        (selected?.id ?? items[0]?.id) === item.id
                          ? "bg-brand-500/10"
                          : ""
                      }`}
                      onClick={() => setSelectedId(item.id)}
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
                            <span className={`w-2 h-2 rounded-full ${dotColor(item.severity)}`} />
                            <Pill tone={SEVERITY_TONE[item.severity]}>
                              {itemTypeLabel(item.type, t)}
                            </Pill>
                            <span className="text-[12px] text-ink-100 truncate flex-1">
                              {item.title}
                            </span>
                            <span className="text-[11px] text-ink-500 shrink-0">
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
                description={`${itemTypeLabel(selected.type, t)} · ${selected.status} · ${t("createdAt", {
                  time: formatTs(selected.created_at),
                })}`}
                actions={
                  <Pill tone={SEVERITY_TONE[selected.severity]}>
                    {selected.severity}
                  </Pill>
                }
              >
                <div className="text-[12px] text-ink-200 whitespace-pre-wrap mb-3">
                  {selected.summary || t("noSummary")}
                </div>

                <div className="flex flex-wrap items-center gap-2 mb-4">
                  {selected.actions.map((act) => (
                    <button
                      key={act.id}
                      disabled={busyKey !== null}
                      onClick={() => {
                        if (act.href && !act.method) {
                          // Pure-navigation action: Next.js link target.
                          window.location.href = act.href;
                          return;
                        }
                        runAction(selected, act);
                      }}
                      className={`text-[11px] px-2 py-1 rounded-md border ${
                        act.severity === "danger"
                          ? "border-rose-500/40 text-rose-200 hover:bg-rose-500/10"
                          : act.severity === "warn"
                          ? "border-amber-400/40 text-amber-200 hover:bg-amber-400/10"
                          : "border-brand-500/40 text-brand-200 hover:bg-brand-500/10"
                      } disabled:opacity-50`}
                      title={act.disabled_reason || actionLabel(act, t, tCommon)}
                    >
                      {actionLabel(act, t, tCommon)}
                    </button>
                  ))}
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
              </Card>
            ) : (
              <Card title={t("selectItem")}>
                <div className="text-[12px] text-ink-500">
                  {t("selectItemHint")}
                </div>
              </Card>
            )}
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

function dotColor(severity: EnvelopeSeverity) {
  if (severity === "danger") return "bg-rose-500";
  if (severity === "warn") return "bg-amber-400";
  return "bg-brand-400";
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

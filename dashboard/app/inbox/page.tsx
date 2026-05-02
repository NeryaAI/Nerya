"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Card,
  Empty,
  ErrorBanner,
  Json,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { clientApi } from "../../lib/clientApi";
import type {
  EnvelopeSeverity,
  InboxItem,
  InboxItemType,
  InboxItemsEnvelope,
  OperatorAction,
} from "../../lib/operatorTypes";

const SEVERITY_TONE: Record<EnvelopeSeverity, "ok" | "warn" | "danger" | "brand"> = {
  info: "ok",
  warn: "warn",
  danger: "danger",
};

export default function InboxPage() {
  const t = useTranslations("inbox");
  const tCommon = useTranslations("common");
  const [env, setEnv] = useState<InboxItemsEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [filterType, setFilterType] = useState<InboxItemType | "all">("all");
  const [onlyAction, setOnlyAction] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

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

  const items = env?.data?.items ?? [];
  const selected = useMemo(
    () => items.find((i) => i.id === selectedId) ?? items[0] ?? null,
    [items, selectedId],
  );

  async function runAction(item: InboxItem, act: OperatorAction) {
    setBusyId(item.id);
    try {
      // Map the inbox action semantically onto /inbox/resolve. The
      // backend dispatches on the item-id prefix to the right
      // subsystem (approvals, evolution, recovery, …) so the UI
      // doesn't need to know which subsystem owns each item.
      const decision =
        act.id === "approve"
          ? "approve"
          : act.id === "reject"
          ? "reject"
          : act.id === "apply" || act.id === "promote"
          ? "apply"
          : act.id === "rollback"
          ? "rollback"
          : "dismiss";
      const result = await clientApi.inboxResolve({ id: item.id, decision });
      if (!result.ok) {
        setError(result.summary || t("resolveFailed"));
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
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
                <Pill tone={statusTone(env.status)}>{env.status.toUpperCase()}</Pill>
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

        <Card title={t("filter")} padded>
          <div className="flex flex-wrap items-center gap-2">
            {ALL_TYPES.map((opt) => (
              <button
                key={opt.id}
                onClick={() => setFilterType(opt.id)}
                className={`text-[11px] px-2 py-1 rounded-md border ${
                  filterType === opt.id
                    ? "bg-brand-500 text-white border-brand-500"
                    : "text-ink-300 border-brand-500/25 hover:bg-brand-500/10"
                }`}
              >
                {opt.label}
              </button>
            ))}
            <label className="ml-auto flex items-center gap-2 text-[11px] text-ink-300">
              <input
                type="checkbox"
                checked={onlyAction}
                onChange={(e) => setOnlyAction(e.target.checked)}
              />
              {t("onlyItemsNeedingAction")}
            </label>
          </div>
        </Card>

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
                {items.map((item) => (
                  <li
                    key={item.id}
                    className={`px-3 py-2.5 border-b border-brand-500/5 last:border-b-0 cursor-pointer hover:bg-brand-500/5 ${
                      (selected?.id ?? items[0]?.id) === item.id
                        ? "bg-brand-500/10"
                        : ""
                    }`}
                    onClick={() => setSelectedId(item.id)}
                  >
                    <div className="flex items-center gap-2">
                      <span className={`w-2 h-2 rounded-full ${dotColor(item.severity)}`} />
                      <Pill tone={SEVERITY_TONE[item.severity]}>
                        {item.type.replace("_", " ")}
                      </Pill>
                      <span className="text-[12px] text-ink-100 truncate flex-1">
                        {item.title}
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
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <div className="xl:col-span-2 space-y-4">
            {selected ? (
              <Card
                title={selected.title}
                description={`${selected.type} · ${selected.status}`}
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
                      disabled={busyId === selected.id}
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
                      title={act.disabled_reason || act.label}
                    >
                      {act.label}
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

                <div className="text-[10px] text-ink-500 uppercase tracking-widest mb-1">
                  {t("rawPayload")}
                </div>
                <Json value={selected.data} />
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

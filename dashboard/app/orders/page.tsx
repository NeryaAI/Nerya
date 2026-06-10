"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { clientApi } from "../../lib/clientApi";
import type {
  ControlPlaneExecutor,
  ControlPlaneOrder,
} from "../../lib/clientApi";
import {
  Advanced,
  Card,
  Empty,
  ErrorBanner,
  Kpi,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { JsonView } from "../../components/JsonView";
import { SectionTabs } from "../../components/SectionTabs";
import { Select } from "../../components/Select";
import { formatTsShort } from "../../lib/format";
import { confirm as confirmDialog } from "../../lib/dialogs";

type OrderState = "active" | "cached" | "lost" | "recent";

function orderStateTone(state: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  if (state === "filled") return "ok";
  if (state === "canceled" || state === "lost") return "danger";
  if (state === "rejected" || state === "expired") return "danger";
  if (state === "partially_filled") return "warn";
  if (state === "open" || state === "submitted") return "brand";
  return "neutral";
}

function executorStateTone(state: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  if (state === "completed") return "ok";
  if (state === "canceled" || state === "failed") return "danger";
  if (state === "running") return "brand";
  if (state === "pending" || state === "submitted") return "warn";
  return "neutral";
}

function ageMs(ts?: number | null): string {
  if (!ts) return "–";
  const ms = Date.now() - Number(ts) * (Number(ts) < 1e12 ? 1000 : 1);
  if (ms < 0) return "now";
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

function fmtTs(ts?: number | null): string {
  if (!ts) return "–";
  const seconds = Number(ts) > 1e12 ? Number(ts) / 1000 : Number(ts);
  return formatTsShort(new Date(seconds * 1000).toISOString());
}

function num(v: unknown, digits = 6): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return "–";
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export default function OrdersPage() {
  const t = useTranslations("orders");
  const tCommon = useTranslations("common");
  const [stateFilter, setStateFilter] = useState<OrderState>("recent");
  const [accountFilter, setAccountFilter] = useState<string>("");
  const [orders, setOrders] = useState<ControlPlaneOrder[]>([]);
  const [executors, setExecutors] = useState<ControlPlaneExecutor[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [selected, setSelected] = useState<ControlPlaneOrder | null>(null);

  const STATE_LABELS: { value: OrderState; label: string; tone: "neutral" | "ok" | "warn" | "danger" | "brand" }[] = [
    { value: "active", label: t("stateActive"), tone: "ok" },
    { value: "recent", label: t("stateRecent"), tone: "brand" },
    { value: "cached", label: t("stateCached"), tone: "neutral" },
    { value: "lost", label: t("stateLost"), tone: "danger" },
  ];

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [ordersRes, executorsRes] = await Promise.all([
        clientApi.controlOrdersList({
          state: stateFilter,
          account_id: accountFilter || undefined,
          limit: 200,
        }),
        clientApi.controlExecutorsList({
          state: stateFilter === "lost" ? "recent" : "active",
          account_id: accountFilter || undefined,
          limit: 100,
        }),
      ]);
      setOrders(ordersRes.orders || []);
      setExecutors(executorsRes.executors || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 15_000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stateFilter, accountFilter]);

  async function cancelOrder(order: ControlPlaneOrder) {
    const ok = await confirmDialog({
      message: t("cancelOrderConfirm", {
        orderId: order.order_id,
        market: order.market,
        side: order.side,
      }),
      tone: "warning",
    });
    if (!ok) return;
    setBusy(order.order_id);
    try {
      await clientApi.controlOrderCancel({
        order_id: order.order_id,
        operator: "dashboard",
        reason: "operator_cancel",
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function cancelExecutor(exec: ControlPlaneExecutor) {
    const ok = await confirmDialog({
      message: t("cancelExecutorConfirm", {
        executorId: exec.executor_id,
        market: exec.market,
      }),
      tone: "warning",
    });
    if (!ok) return;
    setBusy(exec.executor_id);
    try {
      await clientApi.controlExecutorCancel({
        executor_id: exec.executor_id,
        operator: "dashboard",
        reason: "operator_cancel",
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const accountIds = useMemo(() => {
    const ids = new Set<string>();
    for (const o of orders) ids.add(o.account_id);
    for (const e of executors) ids.add(e.account_id);
    return Array.from(ids).sort();
  }, [orders, executors]);

  const totals = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const o of orders) counts[o.state] = (counts[o.state] || 0) + 1;
    return counts;
  }, [orders]);

  return (
    <div>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <button
            onClick={load}
            disabled={loading}
            className="btn-ghost text-xs"
          >
            {loading ? tCommon("refreshing") : tCommon("refresh")}
          </button>
        }
      />
      <SectionTabs section="trading" />
      <PageBody>
        {error && <ErrorBanner error={error} />}

        <div className="flex flex-wrap items-end gap-x-8 gap-y-3 px-1">
          <Kpi
            inline
            label={t("kpiActiveOrders")}
            value={`${(totals.open || 0) + (totals.submitted || 0) + (totals.partially_filled || 0)}`}
            tone="brand"
          />
          <Kpi
            inline
            label={t("kpiFilled")}
            value={`${totals.filled || 0}`}
            tone="ok"
          />
          <Kpi
            inline
            label={t("kpiLostCanceled")}
            value={`${(totals.lost || 0) + (totals.canceled || 0)}`}
            tone={(totals.lost || 0) > 0 ? "danger" : "neutral"}
          />
          <Kpi
            inline
            label={t("kpiActiveExecutors")}
            value={`${executors.filter((e) => e.state === "running" || e.state === "pending" || e.state === "submitted").length}`}
            tone="warn"
          />
        </div>

        <div className="flex flex-wrap gap-2 items-center text-[12px] border-b border-brand-500/10 pb-3">
          <span className="text-ink-500">{t("stateLabel")}</span>
          {STATE_LABELS.map((s) => (
            <button
              key={s.value}
              onClick={() => setStateFilter(s.value)}
              className={`px-2.5 py-1 rounded-md border transition ${
                stateFilter === s.value
                  ? "bg-brand-500/15 text-brand-100 border-brand-500/40"
                  : "text-ink-400 border-transparent hover:text-ink-200 hover:border-brand-500/20"
              }`}
            >
              {s.label}
            </button>
          ))}
          <span className="ml-4 text-ink-500">{t("accountLabel")}</span>
          <div className="min-w-[180px]">
            <Select
              value={accountFilter || ""}
              onChange={(value) => setAccountFilter(value)}
              options={[
                { value: "", label: t("accountAll") },
                ...accountIds.map((id) => ({ value: id, label: id })),
              ]}
              size="sm"
              ariaLabel={t("accountLabel")}
            />
          </div>
        </div>

        <Card
          title={t("ordersTitle", { count: orders.length })}
          description={t("ordersDescription")}
        >
          {orders.length === 0 ? (
            <Empty label={loading ? t("loadingOrders") : t("noOrdersMatch")} />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>{t("colState")}</th>
                    <th>{t("colAccount")}</th>
                    <th>{t("colMarket")}</th>
                    <th>{t("colSide")}</th>
                    <th>{t("colType")}</th>
                    <th>{t("colSize")}</th>
                    <th>{t("colFilled")}</th>
                    <th>{t("colAvgPrice")}</th>
                    <th>{t("colAge")}</th>
                    <th>{t("colStrategy")}</th>
                    <th>{t("colExecutor")}</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {orders.map((o) => {
                    const remaining =
                      Number(o.size_base || 0) - Number(o.filled_size || 0);
                    return (
                      <tr key={o.order_id} className="text-xs">
                        <td>
                          <Pill tone={orderStateTone(o.state)}>{o.state}</Pill>
                        </td>
                        <td className="font-mono">{o.account_id}</td>
                        <td className="font-mono">{o.market}</td>
                        <td>
                          <Pill
                            tone={
                              String(o.side).toLowerCase() === "sell"
                                ? "danger"
                                : "ok"
                            }
                          >
                            {o.side}
                          </Pill>
                        </td>
                        <td>{o.order_type}</td>
                        <td>{num(o.size_base)}</td>
                        <td>
                          {num(o.filled_size)}
                          {remaining > 0 && o.size_base ? (
                            <span className="text-ink-500">
                              {" "}
                              / {num(remaining)}
                            </span>
                          ) : null}
                        </td>
                        <td>{num(o.avg_price, 4)}</td>
                        <td className="font-mono text-ink-400">
                          {ageMs(o.created_at)}
                        </td>
                        <td className="font-mono text-ink-400">
                          {o.strategy_id || "–"}
                        </td>
                        <td className="font-mono text-ink-400 truncate max-w-[120px]">
                          {o.executor_id || "–"}
                        </td>
                        <td className="flex gap-1">
                          <button
                            onClick={() => setSelected(o)}
                            className="btn-ghost text-[11px] py-0.5"
                          >
                            {t("inspect")}
                          </button>
                          {(o.state === "open" ||
                            o.state === "submitted" ||
                            o.state === "partially_filled") && (
                            <button
                              onClick={() => cancelOrder(o)}
                              disabled={busy === o.order_id}
                              className="btn-ghost text-[11px] py-0.5 text-danger"
                            >
                              {busy === o.order_id ? "…" : t("cancel")}
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Advanced
          title={t("executorsTitle", { count: executors.length })}
          description={t("executorsDescription")}
          count={executors.length || undefined}
          storageKey="nerya.orders.advanced.executors"
        >
          {executors.length === 0 ? (
            <Empty label={t("noExecutors")} />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>{t("colState")}</th>
                    <th>{t("colKind")}</th>
                    <th>{t("colAccount")}</th>
                    <th>{t("colStrategy")}</th>
                    <th>{t("colMarket")}</th>
                    <th>{t("colCreated")}</th>
                    <th>{t("colLastHeartbeat")}</th>
                    <th>{t("colOrders")}</th>
                    <th>{t("colExecutorId")}</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {executors.map((e) => (
                    <tr key={e.executor_id} className="text-xs">
                      <td>
                        <Pill tone={executorStateTone(e.state)}>{e.state}</Pill>
                      </td>
                      <td>{e.kind}</td>
                      <td className="font-mono">{e.account_id}</td>
                      <td className="font-mono text-ink-300">
                        {e.strategy_id}
                      </td>
                      <td className="font-mono">{e.market}</td>
                      <td className="font-mono text-ink-400">
                        {fmtTs(e.created_at)}
                      </td>
                      <td className="font-mono text-ink-400">
                        {fmtTs(e.last_heartbeat)}
                      </td>
                      <td className="font-mono text-ink-400">
                        {(e.order_ids || []).length}
                      </td>
                      <td className="font-mono text-ink-400 truncate max-w-[160px]">
                        {e.executor_id}
                      </td>
                      <td>
                        {(e.state === "running" ||
                          e.state === "pending" ||
                          e.state === "submitted") && (
                          <button
                            onClick={() => cancelExecutor(e)}
                            disabled={busy === e.executor_id}
                            className="btn-ghost text-[11px] py-0.5 text-danger"
                          >
                            {busy === e.executor_id ? "…" : t("cancel")}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Advanced>

        {selected ? (
          <Card
            title={t("orderDetailTitle", { orderId: selected.order_id })}
            description={`${selected.market} · ${selected.side} ${selected.order_type}`}
            actions={
              <button
                onClick={() => setSelected(null)}
                className="btn-ghost text-xs"
              >
                {tCommon("close")}
              </button>
            }
          >
            <OrderDetail order={selected} />
          </Card>
        ) : null}
      </PageBody>
    </div>
  );
}

function OrderDetail({ order }: { order: ControlPlaneOrder }) {
  const t = useTranslations("orders");
  const filled = Number(order.filled_size || 0);
  const size = Number(order.size_base || 0);
  const remaining = Math.max(0, size - filled);
  const fillPct = size > 0 ? Math.min(100, (filled / size) * 100) : 0;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <DetailStat
          label={t("detailStateLabel")}
          value={<Pill tone={orderStateTone(order.state)}>{order.state}</Pill>}
        />
        <DetailStat
          label={t("detailSideTypeLabel")}
          value={
            <span className="font-mono text-ink-100 text-[12px]">
              {order.side} · {order.order_type}
            </span>
          }
        />
        <DetailStat
          label={t("detailAvgPriceLabel")}
          value={<span className="font-mono text-ink-100 text-[12px]">{num(order.avg_price, 4)}</span>}
        />
        <DetailStat
          label={t("detailFillLabel")}
          value={
            <div className="space-y-1">
              <div className="font-mono text-ink-100 text-[12px]">
                {num(filled)} <span className="text-ink-500">/ {num(size)}</span>
              </div>
              <div className="h-1.5 rounded-full bg-ink-900 overflow-hidden">
                <div
                  className="h-full rounded-full bg-brand-500/70 transition-[width] duration-300"
                  style={{ width: `${fillPct.toFixed(1)}%` }}
                />
              </div>
              {remaining > 0 ? (
                <div className="text-[10px] text-ink-500 font-mono">
                  {t("detailRemaining", { value: num(remaining) })}
                </div>
              ) : null}
            </div>
          }
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <DetailGroup title={t("detailIdentity")}>
          <DetailRow label={t("detailRowOrderId")} value={order.order_id} mono />
          {order.client_order_id ? (
            <DetailRow label={t("detailRowClientOrderId")} value={order.client_order_id} mono />
          ) : null}
          {order.exchange_order_id ? (
            <DetailRow label={t("detailRowExchangeOrderId")} value={order.exchange_order_id} mono />
          ) : null}
          <DetailRow label={t("detailRowAccount")} value={order.account_id} mono />
          <DetailRow label={t("detailRowMarket")} value={order.market} mono />
          {order.strategy_id ? (
            <DetailRow label={t("detailRowStrategy")} value={order.strategy_id} mono />
          ) : null}
          {order.executor_id ? (
            <DetailRow label={t("detailRowExecutor")} value={order.executor_id} mono />
          ) : null}
          {order.intent_id ? (
            <DetailRow label={t("detailRowIntent")} value={order.intent_id} mono />
          ) : null}
          {order.plan_id ? (
            <DetailRow label={t("detailRowPlan")} value={order.plan_id} mono />
          ) : null}
        </DetailGroup>

        <DetailGroup title={t("detailTimeline")}>
          <DetailRow label={t("detailRowCreated")} value={fmtTs(order.created_at)} mono />
          {order.submitted_at ? (
            <DetailRow label={t("detailRowSubmitted")} value={fmtTs(order.submitted_at)} mono />
          ) : null}
          {order.last_seen_at ? (
            <DetailRow label={t("detailRowLastSeen")} value={fmtTs(order.last_seen_at)} mono />
          ) : null}
          {order.terminal_at ? (
            <DetailRow label={t("detailRowTerminal")} value={fmtTs(order.terminal_at)} mono />
          ) : null}
          <DetailRow label={t("detailRowAge")} value={ageMs(order.created_at)} mono />
        </DetailGroup>
      </div>

      <details>
        <summary className="cursor-pointer text-[12px] text-ink-500 font-medium hover:text-ink-300">
          {t("detailRawEnvelope")}
        </summary>
        <div className="mt-2">
          <JsonView value={order} initialCollapsed showRawToggle />
        </div>
      </details>
    </div>
  );
}

function DetailStat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-900/40 px-3 py-2">
      <div className="text-[11px] text-ink-500 font-medium">{label}</div>
      <div className="mt-1.5">{value}</div>
    </div>
  );
}

function DetailGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-900/40 px-3 py-2">
      <div className="text-[11px] text-ink-500 font-medium mb-2">{title}</div>
      <dl className="space-y-1">{children}</dl>
    </div>
  );
}

function DetailRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="grid grid-cols-[110px_minmax(0,1fr)] items-baseline gap-2 text-[11px]">
      <dt className="text-ink-500">{label}</dt>
      <dd className={`min-w-0 truncate text-ink-100 ${mono ? "font-mono" : ""}`}>{value}</dd>
    </div>
  );
}

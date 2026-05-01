"use client";

import { useEffect, useMemo, useState } from "react";
import { clientApi } from "../../lib/clientApi";
import type {
  ControlPlaneExecutor,
  ControlPlaneOrder,
} from "../../lib/clientApi";
import {
  Card,
  Empty,
  ErrorBanner,
  Json,
  Kpi,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import { formatTsShort } from "../../lib/format";

type OrderState = "active" | "cached" | "lost" | "recent";

const STATE_LABELS: { value: OrderState; label: string; tone: "neutral" | "ok" | "warn" | "danger" | "brand" }[] = [
  { value: "active", label: "Active", tone: "ok" },
  { value: "recent", label: "Recent", tone: "brand" },
  { value: "cached", label: "Cached", tone: "neutral" },
  { value: "lost", label: "Lost", tone: "danger" },
];

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
  if (!ts) return "—";
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
  if (!ts) return "—";
  const seconds = Number(ts) > 1e12 ? Number(ts) / 1000 : Number(ts);
  return formatTsShort(new Date(seconds * 1000).toISOString());
}

function num(v: unknown, digits = 6): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export default function OrdersPage() {
  const [stateFilter, setStateFilter] = useState<OrderState>("recent");
  const [accountFilter, setAccountFilter] = useState<string>("");
  const [orders, setOrders] = useState<ControlPlaneOrder[]>([]);
  const [executors, setExecutors] = useState<ControlPlaneExecutor[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [selected, setSelected] = useState<ControlPlaneOrder | null>(null);

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
    if (
      !confirm(
        `Cancel order ${order.order_id} (${order.market} ${order.side})?`,
      )
    )
      return;
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
    if (!confirm(`Cancel executor ${exec.executor_id} (${exec.market})?`)) return;
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
        title="Orders & Executors"
        description="Live order book through the durable order tracker. Each row shows account, market, executor, age and gives operator cancel controls."
        actions={
          <button
            onClick={load}
            disabled={loading}
            className="btn-ghost text-xs"
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        }
      />
      <SectionTabs section="trading" />
      <PageBody>
        {error && <ErrorBanner error={error} />}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Kpi
            label="Active orders"
            value={`${(totals.open || 0) + (totals.submitted || 0) + (totals.partially_filled || 0)}`}
            tone="brand"
          />
          <Kpi
            label="Filled"
            value={`${totals.filled || 0}`}
            tone="ok"
          />
          <Kpi
            label="Lost / canceled"
            value={`${(totals.lost || 0) + (totals.canceled || 0)}`}
            tone={(totals.lost || 0) > 0 ? "danger" : "neutral"}
          />
          <Kpi
            label="Active executors"
            value={`${executors.filter((e) => e.state === "running" || e.state === "pending" || e.state === "submitted").length}`}
            tone="warn"
          />
        </div>

        <Card title="Filters">
          <div className="flex flex-wrap gap-2 items-center text-xs">
            <span className="text-ink-400">state:</span>
            {STATE_LABELS.map((s) => (
              <button
                key={s.value}
                onClick={() => setStateFilter(s.value)}
                className={`px-2 py-1 rounded-md border ${
                  stateFilter === s.value
                    ? "border-brand-400 bg-brand-500/20 text-brand-100"
                    : "border-brand-500/20 text-ink-300 hover:bg-brand-500/10"
                }`}
              >
                {s.label}
              </button>
            ))}
            <span className="ml-4 text-ink-400">account:</span>
            <select
              value={accountFilter}
              onChange={(e) => setAccountFilter(e.target.value)}
              className="bg-ink-900 border border-brand-500/20 rounded-md px-2 py-1 text-ink-200"
            >
              <option value="">all</option>
              {accountIds.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </div>
        </Card>

        <Card
          title={`Orders (${orders.length})`}
          description="Durable order tracker view. Cancel actions are journaled to journals/operator.jsonl."
        >
          {orders.length === 0 ? (
            <Empty label={loading ? "Loading orders…" : "No orders match the filters."} />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>State</th>
                    <th>Account</th>
                    <th>Market</th>
                    <th>Side</th>
                    <th>Type</th>
                    <th>Size</th>
                    <th>Filled</th>
                    <th>Avg price</th>
                    <th>Age</th>
                    <th>Strategy</th>
                    <th>Executor</th>
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
                          {o.strategy_id || "—"}
                        </td>
                        <td className="font-mono text-ink-400 truncate max-w-[120px]">
                          {o.executor_id || "—"}
                        </td>
                        <td className="flex gap-1">
                          <button
                            onClick={() => setSelected(o)}
                            className="btn-ghost text-[11px] py-0.5"
                          >
                            inspect
                          </button>
                          {(o.state === "open" ||
                            o.state === "submitted" ||
                            o.state === "partially_filled") && (
                            <button
                              onClick={() => cancelOrder(o)}
                              disabled={busy === o.order_id}
                              className="btn-ghost text-[11px] py-0.5 text-[#ef4560]"
                            >
                              {busy === o.order_id ? "…" : "cancel"}
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

        <Card
          title={`Executors (${executors.length})`}
          description="Durable executor orchestrator. Cancellation flushes through risk/protection logic."
        >
          {executors.length === 0 ? (
            <Empty label="No executors." />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>State</th>
                    <th>Kind</th>
                    <th>Account</th>
                    <th>Strategy</th>
                    <th>Market</th>
                    <th>Created</th>
                    <th>Last heartbeat</th>
                    <th>Orders</th>
                    <th>Executor id</th>
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
                            className="btn-ghost text-[11px] py-0.5 text-[#ef4560]"
                          >
                            {busy === e.executor_id ? "…" : "cancel"}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {selected ? (
          <Card
            title={`Order ${selected.order_id}`}
            description={`${selected.market} · ${selected.side} ${selected.order_type}`}
            actions={
              <button
                onClick={() => setSelected(null)}
                className="btn-ghost text-xs"
              >
                Close
              </button>
            }
          >
            <Json value={selected} />
          </Card>
        ) : null}
      </PageBody>
    </div>
  );
}

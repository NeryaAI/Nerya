"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useState } from "react";
import { useLocale } from "next-intl";
import { Card, Empty } from "../Page";
import { JsonView } from "../JsonView";
import { ChevronDownIcon, ChevronRightIcon } from "../icons";
import { financeNumber, finiteNumber } from "../../lib/financeDisplay";

type BacktestTable = { id: string; columns: string[]; rows: unknown[][] };
const numericColumns = new Set(["qty", "size", "price", "ideal_price", "notional", "fee", "slippage_bps", "slippage_usd", "count", "open", "high", "low", "close", "volume", "order_attempts", "orders_submitted", "queued"]);

const labels: Record<string, string> = {
  trades: "copy.components_backtest_BacktestTables.label_trades", decisions: "copy.components_backtest_BacktestTables.label_decisions", rejected_signals: "copy.components_backtest_BacktestTables.label_rejected_signals", analysis_by_reason: "copy.components_backtest_BacktestTables.label_analysis_by_reason",
  ts: "copy.components_backtest_BacktestTables.label_ts", signal_ts: "copy.components_backtest_BacktestTables.label_signal_ts", market: "copy.components_backtest_BacktestTables.label_market", status: "copy.components_backtest_BacktestTables.label_status", reason: "copy.components_backtest_BacktestTables.label_reason", side: "copy.components_backtest_BacktestTables.label_side",
  input_sources: "copy.components_backtest_BacktestTables.label_input_sources", selected_roles: "copy.components_backtest_BacktestTables.label_selected_roles", agent_execution: "copy.components_backtest_BacktestTables.label_agent_execution", qty: "copy.components_backtest_BacktestTables.label_qty", size: "copy.components_backtest_BacktestTables.label_size",
  price: "copy.components_backtest_BacktestTables.label_price", ideal_price: "copy.components_backtest_BacktestTables.label_ideal_price", notional: "copy.components_backtest_BacktestTables.label_notional", fee: "copy.components_backtest_BacktestTables.label_fee", slippage_bps: "copy.components_backtest_BacktestTables.label_slippage_bps",
  slippage_usd: "copy.components_backtest_BacktestTables.label_slippage_usd", intent_id: "copy.components_backtest_BacktestTables.label_intent_id", forced_close: "copy.components_backtest_BacktestTables.label_forced_close", reject_reason: "copy.components_backtest_BacktestTables.label_reject_reason", count: "copy.components_backtest_BacktestTables.label_count",
  order_events: "copy.components_backtest_BacktestTables.label_order_events", method: "copy.components_backtest_BacktestTables.label_method", phase: "copy.components_backtest_BacktestTables.label_phase", trigger_market: "copy.components_backtest_BacktestTables.label_trigger_market",
  order_attempts: "copy.components_backtest_BacktestTables.label_order_attempts", orders_submitted: "copy.components_backtest_BacktestTables.label_orders_submitted", queued: "copy.components_backtest_BacktestTables.label_queued", action_counts: "copy.components_backtest_BacktestTables.label_action_counts",
  error: "copy.components_backtest_BacktestTables.label_error", error_kind: "copy.components_backtest_BacktestTables.label_error_kind", order_id: "copy.components_backtest_BacktestTables.label_order_id",
};
const states: Record<string, string> = { dispatch: "copy.components_backtest_BacktestTables.label_dispatch", skip: "copy.components_backtest_BacktestTables.label_skip", hold: "copy.components_backtest_BacktestTables.label_hold", ok: "copy.components_backtest_BacktestTables.label_ok", attempted: "copy.components_backtest_BacktestTables.label_attempted", submitted: "copy.components_backtest_BacktestTables.label_submitted", filled: "copy.components_backtest_BacktestTables.label_filled", rejected: "copy.components_backtest_BacktestTables.label_rejected", error: "copy.components_backtest_BacktestTables.label_error", not_run: "copy.components_backtest_BacktestTables.label_not_run", buy: "copy.components_backtest_BacktestTables.label_buy", sell: "copy.components_backtest_BacktestTables.label_sell", forced_close: "copy.components_backtest_BacktestTables.label_forced_close", "trend gate not met": "copy.components_backtest_BacktestTables.label_trend_gate_not_met", "duplicate candle": "copy.components_backtest_BacktestTables.label_duplicate_candle", warmup: "copy.components_backtest_BacktestTables.label_warmup", skip_no_signal: "copy.components_backtest_BacktestTables.label_skip_no_signal", skip_holding: "copy.components_backtest_BacktestTables.label_skip_holding", skip_warmup: "copy.components_backtest_BacktestTables.label_skip_warmup", skip_duplicate_candle: "copy.components_backtest_BacktestTables.label_skip_duplicate_candle" };

export function BacktestTables({
  tables,
  compact = false,
  maxHeightClass = "max-h-[420px]",
}: {
  tables: BacktestTable[];
  compact?: boolean;
  maxHeightClass?: string;
}) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  return (
    <div className={compact ? "grid grid-cols-1 gap-3" : "grid grid-cols-1 gap-4"}>
      {tables.map((table) => (
        <Card key={table.id} title={formatTableTitle(table.id, zh)} padded={false}>
          <div className={compact ? "px-3 py-2.5" : "px-4 py-3.5"}>
            {table.rows.length === 0 ? (
              <Empty label={i18nCopy(zh, "copy.components_backtest_BacktestTables.001")} />
            ) : (
              <div className={`embedded-table-scroll ${maxHeightClass} rounded-md border border-[color:var(--line)]`}>
                <table className={`min-w-[720px] w-full ${compact ? "text-[11.5px]" : "text-xs"}`}>
                  <thead className="sticky top-0 bg-[color:var(--card-hi)]">
                    <tr>
                      {table.columns.map((col) => (
                        <th
                          key={col}
                          className={`whitespace-nowrap text-left font-medium text-[color:var(--text-muted)] ${
                            compact ? "px-2.5 py-1.5" : "px-3 py-2"
                          }`}
                        >
                          {labels[col] ? i18nCopy(zh, labels[col]) : col.replace(/_/g, " ")}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {table.rows.map((row, idx) => (
                      <tr key={idx} className="border-t border-brand-500/10">
                        {row.map((cell, cellIdx) => (
                          <td
                            key={cellIdx}
                            className={`align-top font-mono text-[color:var(--text-base)] ${
                              compact ? "px-2.5 py-1.5" : "px-3 py-2"
                            }`}
                          >
                            <Cell value={cell} column={table.columns[cellIdx]} locale={locale} />
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </Card>
      ))}
    </div>
  );
}

function formatTableTitle(id: string, zh: boolean): string {
  const key = id.replace(/_top\d+$/i, "");
  return labels[key] ? i18nCopy(zh, labels[key]) : key.replace(/_/g, " ");
}

function Cell({ value, column, locale }: { value: unknown; column: string; locale: string }) {
  const [open, setOpen] = useState(false);
  const zh = locale.startsWith("zh");
  if (value === null || value === undefined || value === "") {
    return <span className="text-ink-500">-</span>;
  }
  if (["ts", "signal_ts"].includes(column) && (typeof value === "number" || typeof value === "string")) {
    const n = Number(value), date = new Date(n < 1e12 ? n * 1000 : n);
    if (Number.isFinite(n) && Number.isFinite(date.getTime())) return <time className="whitespace-nowrap" dateTime={date.toISOString()} title={String(value)}>{date.toISOString().replace("T", " ").slice(0, 19)}</time>;
  }
  if (typeof value === "string" && ["status", "phase", "agent_execution", "side", "reason"].includes(column) && states[value]) {
    return <span title={value}>{i18nCopy(zh, states[value])}</span>;
  }
  // CSV cells are strings. Format known numeric columns only; do not turn
  // order ids or zero-prefixed identifiers into numbers. Keep exact raw data.
  if (typeof value === "string" && numericColumns.has(column)) {
    const number = finiteNumber(value);
    if (number !== null) return <span className="whitespace-nowrap" title={value}>{formatNumber(number, locale)}</span>;
  }
  if (typeof value === "number") {
    return (
      <span className="whitespace-nowrap">
        {Number.isFinite(value) ? formatNumber(value, locale) : "—"}
      </span>
    );
  }
  if (typeof value === "boolean") {
    return (
      <span className={value ? "text-accent-300" : "text-danger"}>
        {(value ? i18nCopy(zh, "copy.components_backtest_BacktestTables.002") : i18nCopy(zh, "copy.components_backtest_BacktestTables.003"))}
      </span>
    );
  }
  if (typeof value !== "object") {
    return <span className="block max-w-[24rem] whitespace-normal break-words">{String(value)}</span>;
  }
  const summary = Array.isArray(value)
    ? `[${value.length} item${value.length === 1 ? "" : "s"}]`
    : `{${Object.keys(value as Record<string, unknown>).length} field${
        Object.keys(value as Record<string, unknown>).length === 1 ? "" : "s"
      }}`;
  return (
    <div className="min-w-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="cursor-pointer inline-flex items-center gap-1 text-[11px] text-brand-200 hover:text-brand-100"
      >
        {open ? <ChevronDownIcon className="h-3 w-3" /> : <ChevronRightIcon className="h-3 w-3" />}
        {summary}
      </button>
      {open ? (
        <div className="mt-1 min-w-[18rem] normal-case">
          <JsonView value={value} showRawToggle={false} className="bg-ink-950/40" />
        </div>
      ) : null}
    </div>
  );
}

function formatNumber(value: number, locale: string): string {
  if (!Number.isFinite(value)) return "—";
  if (Math.abs(value) >= 100) return value.toLocaleString(locale, { maximumFractionDigits: 2 });
  if (Math.abs(value) >= 1) return value.toLocaleString(locale, { maximumFractionDigits: 4 });
  return financeNumber(value, locale);
}

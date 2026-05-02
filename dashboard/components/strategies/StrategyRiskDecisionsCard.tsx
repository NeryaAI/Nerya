"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, Empty, Pill } from "../Page";
import { clientApi, type RiskEvaluationRow } from "../../lib/clientApi";

interface Props {
  strategyId: string;
}

function fmtTs(ts: number): string {
  if (!ts) return "—";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return String(ts);
  }
}

/**
 * Strategy risk-decision panel (04-29 §11 P9).
 *
 * Reads ``/risk/evaluations`` and renders the most recent
 * rejected/escalated decisions for this strategy with operator
 * remediation hints. Each hint maps to a deep link (account driver,
 * limits page, kill-switch panel, etc) so the operator goes straight
 * from "why was I rejected?" to "click here to fix it".
 */
export function StrategyRiskDecisionsCard({ strategyId }: Props) {
  const t = useTranslations("strategyRisk");
  const [rows, setRows] = useState<RiskEvaluationRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const res = await clientApi.riskEvaluations({
        strategy_id: strategyId,
        decisions: ["reject", "escalate"],
        limit: 25,
        since_seconds: 86400,
      });
      setRows(res.evaluations ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId]);

  return (
    <Card
      title={t("title")}
      description={t("description")}
      actions={
        <button
          onClick={() => void load()}
          disabled={loading}
          className="btn-ghost text-xs"
        >
          {loading ? "…" : t("refresh")}
        </button>
      }
    >
      {error && (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {error}
        </div>
      )}
      {!loading && rows.length === 0 ? (
        <Empty label={t("noRejections")} />
      ) : null}
      <div className="embedded-list-scroll-lg grid gap-3">
        {rows.map((row) => (
          <div
            key={row.risk_evaluation_id}
            className="rounded-xl border border-white/8 bg-bg-card p-3 text-xs"
          >
            <div className="flex flex-wrap items-center gap-2">
              <Pill tone={row.decision === "reject" ? "danger" : "warn"}>
                {row.decision}
              </Pill>
              <span className="font-mono text-ink-300">
                {t("acct")} {row.account_id}
              </span>
              <span className="font-mono text-ink-400">
                ${(row.notional_usd ?? 0).toFixed(2)}
              </span>
              <span className="ml-auto text-ink-500">{fmtTs(row.ts)}</span>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {row.reasons.map((r, idx) => (
                <span
                  key={`${row.risk_evaluation_id}:${idx}`}
                  className="font-mono text-[11px] text-ink-300 bg-white/[0.04] px-1.5 py-0.5 rounded"
                >
                  {r}
                </span>
              ))}
            </div>
            {row.fix_hints && row.fix_hints.length > 0 ? (
              <div className="mt-2 grid gap-1.5">
                {row.fix_hints.map((hint, idx) => (
                  <div
                    key={`${row.risk_evaluation_id}:hint:${idx}`}
                    className="flex items-start justify-between gap-3 rounded-lg border border-amber-400/20 bg-amber-400/5 px-3 py-2"
                  >
                    <div>
                      <div className="text-amber-200 font-medium">
                        {hint.title}
                      </div>
                      <div className="text-ink-300 mt-0.5">{hint.detail}</div>
                      <div className="mt-0.5 font-mono text-[10px] text-ink-500">
                        {t("reason")} {hint.reason}
                      </div>
                    </div>
                    {hint.href ? (
                      <Link
                        href={hint.href}
                        className="btn-primary text-[11px] py-1 px-2 whitespace-nowrap shrink-0"
                      >
                        {t("fix")}
                      </Link>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </Card>
  );
}

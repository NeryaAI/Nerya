"use client";

import { useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { Card, Empty, Json, Pill } from "../Page";
import type { StrategyHistoryEnvelope } from "../../lib/strategyTypes";

interface Props {
  strategyId: string;
  history: StrategyHistoryEnvelope | null;
}

/**
 * Strategy-history ledger viewer. Replaces the legacy "history" tab
 * that used to call the now-removed ``strategy.history`` skill —
 * everything ships through the ``/strategies/runtime/workspace``
 * envelope so the dashboard never touches the kernel directly.
 */
export function StrategyHistoryCard({ strategyId, history }: Props) {
  const t = useTranslations("strategyHistory");
  const ledgers = history?.ledgers ?? {};
  const ledgerNames = useMemo(() => Object.keys(ledgers).sort(), [ledgers]);
  const [active, setActive] = useState<string | null>(null);

  if (!history || ledgerNames.length === 0) {
    return (
      <Card
        title={t("title")}
        description={t("description", { id: strategyId })}
      >
        <Empty label={t("noLedgers")} />
      </Card>
    );
  }

  const current = active ?? ledgerNames[0];
  const ledger = ledgers[current];

  return (
    <Card
      title={t("title")}
      description={t("description", { id: strategyId })}
    >
      <div className="flex flex-wrap gap-1.5 mb-3">
        {ledgerNames.map((name) => (
          <button
            key={name}
            onClick={() => setActive(name)}
            className={`text-[11px] px-2 py-1 rounded border ${
              current === name
                ? "bg-brand-500/20 text-white border-brand-500/40"
                : "bg-ink-900/40 text-ink-300 border-brand-500/10 hover:border-brand-500/30"
            }`}
          >
            {name}
            <span className="ml-1.5 text-ink-500">
              ({ledgers[name].count ?? 0})
            </span>
          </button>
        ))}
      </div>
      {ledger ? (
        <div className="space-y-2">
          <Pill tone="brand">{t("eventsCount", { n: ledger.count })}</Pill>
          <Json value={ledger.tail} />
        </div>
      ) : (
        <Empty label={t("ledgerEmpty")} />
      )}
    </Card>
  );
}

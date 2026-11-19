"use client";

import { useTranslations } from "next-intl";
import type { PositionInstrument } from "../../lib/api";

export function instrumentLabel(market: string | undefined, instrument?: PositionInstrument): string {
  return instrument?.label || market || "—";
}

export function InstrumentIdentity({ market, instrument, secondary }: {
  market?: string; instrument?: PositionInstrument; secondary?: string;
}) {
  const t = useTranslations("predictionPosition");
  const prediction = instrument?.type === "prediction_outcome";
  const state = instrument?.settlement_status;
  return <span className="block min-w-0 break-words" data-testid={prediction ? "prediction-identity" : undefined}>
    <strong>{instrumentLabel(market, instrument)}</strong>
    <span className="mt-1 flex flex-wrap gap-x-2 gap-y-1 text-xs font-normal text-[color:var(--text-muted)]">
      <span>{prediction ? instrument.outcome || "—" : secondary}</span>
      {prediction && state ? <span>{t(["open", "closed", "resolved"].includes(state) ? state : "unknown")}</span> : null}
      {prediction && instrument.data_status !== "available" ? <span role="status" className="text-warn">
        {t(instrument.data_status === "stale" ? "stale" : "dataUnavailable")}
      </span> : null}
    </span>
  </span>;
}

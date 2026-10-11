"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useRouter } from "next/navigation";
import { useLocale } from "next-intl";
import { setWorkspaceComposeDraft } from "../../lib/workspaceComposeDraft";
import { toast } from "../../lib/dialogs";
import type { Factor, FactorSource } from "../../lib/factorLibrary";
import { SparkIcon } from "../icons";

export function FactorResearchButton({ source, factor, className = "btn btn-secondary" }: { source?: FactorSource; factor?: Factor; className?: string }) {
  const zh = useLocale().startsWith("zh"), router = useRouter();
  const label = factor ? (i18nCopy(zh, "copy.components_factors_FactorResearchButton.001")) : (i18nCopy(zh, "copy.components_factors_FactorResearchButton.002"));
  return <button type="button" className={className} onClick={() => {
    const identity = source ? { strategy_id: source.strategy_id, ts: source.ts, ...(source.proposal_id ? { proposal_id: source.proposal_id } : {}) } : undefined;
    const text = factor
      ? (i18nCopy(zh, "copy.components_factors_FactorResearchButton.003", { value0: factor.factor_id, value1: factor.version, value2: factor.definition_hash }))
      : i18nCopy(zh, "copy.components_factors_FactorResearchButton.004", {request: identity
          ? i18nCopy(zh, "copy.components_factors_FactorResearchButton.frozenSource", {identity: JSON.stringify(identity)})
          : i18nCopy(zh, "copy.components_factors_FactorResearchButton.hypothesis")});
    if (!setWorkspaceComposeDraft({ text, attachments: [], autoSend: false })) {
      toast({ tone: "warn", message: i18nCopy(zh, "copy.components_factors_FactorResearchButton.005") }); return;
    }
    router.push(`/chat?draft=factor-${Date.now()}`);
  }}><SparkIcon size={15}/>{label}</button>;
}

"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import * as Dialog from "@radix-ui/react-dialog";
import { useLocale } from "next-intl";
import { useId, useState } from "react";
import { factorRequest, type Factor, type FactorDefinition, type FactorSource } from "../../lib/factorLibrary";
import { ErrorBanner } from "../Page";
import { XIcon } from "../icons";
import { toast } from "../../lib/dialogs";
import styles from "./Factors.module.css";

export const categories = ["momentum", "trend", "volatility", "volume", "mean_reversion", "custom"];
const categoryKeys:Record<string,string> = {momentum:"copy.components_factors_FactorEditor.label_momentum",trend:"copy.components_factors_FactorEditor.label_trend",volatility:"copy.components_factors_FactorEditor.label_volatility",volume:"copy.components_factors_FactorEditor.label_volume",mean_reversion:"copy.components_factors_FactorEditor.label_mean_reversion",custom:"copy.components_factors_FactorEditor.label_custom"};
export const categoryLabel = (category: string, zh: boolean) => categoryKeys[category] ? i18nCopy(zh,categoryKeys[category]) : category.replaceAll("_", " ");

export function FactorEditor({ factor, source, onClose, onSaved }: { factor?: Factor; source?: FactorSource; onClose: () => void; onSaved: (factor: Factor) => void }) {
  const zh = useLocale().startsWith("zh"), id = useId();
  const [form, setForm] = useState<FactorDefinition>({
    factor_id: factor?.factor_id || "", name: factor?.name || "", category: factor?.category || "momentum",
    expression: factor?.expression || "", parameters: factor?.parameters || {}, description: factor?.description || "",
    hypothesis: factor?.hypothesis || "", direction: factor?.direction || "higher_is_bullish",
    markets: factor?.markets || [], timeframes: factor?.timeframes || [], tags: factor?.tags || [], status: factor?.status || "candidate",
  });
  const [parameters, setParameters] = useState(JSON.stringify(form.parameters, null, 2));
  const [markets, setMarkets] = useState(form.markets.join(", ")), [timeframes, setTimeframes] = useState(form.timeframes.join(", "));
  const [reason, setReason] = useState(""), [saving, setSaving] = useState(false), [error, setError] = useState("");
  const patch = (key: keyof FactorDefinition, value: string) => setForm(old => ({ ...old, [key]: value }));
  const list = (value: string) => value.split(/[,，\n]/).map(s => s.trim()).filter(Boolean);
  return <Dialog.Root open onOpenChange={open => { if (!open && !saving) onClose(); }}><Dialog.Portal>
    <Dialog.Overlay className={styles.overlay}/><Dialog.Content className={styles.modal} onEscapeKeyDown={e => { if (saving) e.preventDefault(); }} onPointerDownOutside={e => e.preventDefault()}>
      <div className={styles.modalHeader}><Dialog.Title className={styles.modalTitle}>{factor ? (i18nCopy(zh, "copy.components_factors_FactorEditor.001")) : (i18nCopy(zh, "copy.components_factors_FactorEditor.002"))}</Dialog.Title><Dialog.Close className="ui-icon-button" disabled={saving} aria-label={i18nCopy(zh, "copy.components_factors_FactorEditor.003")}><XIcon size={18}/></Dialog.Close></div>
      <Dialog.Description className={`${styles.muted} mb-5`}>{i18nCopy(zh, "copy.components_factors_FactorEditor.004")}</Dialog.Description>
      <form className={styles.form} onSubmit={async event => {
        event.preventDefault(); if (saving) return; setError(""); setSaving(true);
        try {
          const parsed = JSON.parse(parameters);
          if (!parsed || Array.isArray(parsed) || typeof parsed !== "object" || Object.values(parsed).some(v => typeof v !== "number" || !Number.isFinite(v))) throw new Error(i18nCopy(zh, "copy.components_factors_FactorEditor.005"));
          const result = await factorRequest<{ factor: Factor; duplicates: string[] }>("save", { definition: { ...form, factor_id: form.factor_id.trim(), name: form.name.trim(), parameters: parsed, markets: list(markets), timeframes: list(timeframes) }, expected_version: factor?.version || 0, reason, ...(source ? { source_backtest: { strategy_id: source.strategy_id, ts: source.ts, proposal_id: source.proposal_id || null } } : {}) });
          if (result.duplicates.length) toast({ tone: "warn", message: `${i18nCopy(zh, "copy.components_factors_FactorEditor.006")}${result.duplicates.join(", ")}` });
          onSaved(result.factor);
        } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setSaving(false); }
      }}>
        <div className={styles.fields}>
          <label className={styles.field} htmlFor={`${id}-name`}>{i18nCopy(zh, "copy.components_factors_FactorEditor.007")}<input id={`${id}-name`} required maxLength={160} value={form.name} onChange={e => patch("name", e.target.value)}/></label>
          <label className={styles.field} htmlFor={`${id}-key`}>{i18nCopy(zh, "copy.components_factors_FactorEditor.008")}<input id={`${id}-key`} required disabled={!!factor} pattern="[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,79}" placeholder="momentum.return20" value={form.factor_id} onChange={e => patch("factor_id", e.target.value)}/></label>
        </div>
        <div className={styles.fields}>
          <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorEditor.009")}<select value={form.category} onChange={e => patch("category", e.target.value)}>{categories.map(c => <option key={c} value={c}>{categoryLabel(c,zh)}</option>)}</select></label>
          <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorEditor.010")}<select value={form.direction} onChange={e => patch("direction", e.target.value)}><option value="higher_is_bullish">{i18nCopy(zh, "copy.components_factors_FactorEditor.011")}</option><option value="lower_is_bullish">{i18nCopy(zh, "copy.components_factors_FactorEditor.012")}</option></select></label>
        </div>
        <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorEditor.013")}<textarea required maxLength={2000} value={form.expression} placeholder="close / delay(close, n) - 1" onChange={e => patch("expression", e.target.value)} spellCheck={false}/><small>{i18nCopy(zh, "copy.components_factors_FactorEditor.014")}</small></label>
        <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorEditor.015")}<textarea value={parameters} onChange={e => setParameters(e.target.value)} spellCheck={false}/></label>
        <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorEditor.016")}<textarea value={form.hypothesis} maxLength={4000} onChange={e => patch("hypothesis", e.target.value)} placeholder={i18nCopy(zh, "copy.components_factors_FactorEditor.017")}/></label>
        <div className={styles.fields}>
          <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorEditor.018")}<input value={markets} onChange={e => setMarkets(e.target.value)} placeholder="BINANCE:ETH/USDT:USDT"/></label>
          <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorEditor.019")}<input value={timeframes} onChange={e => setTimeframes(e.target.value)} placeholder="15m, 1h, 4h"/></label>
        </div>
        <p className={styles.muted}>{i18nCopy(zh, "copy.components_factors_FactorEditor.020")}</p>
        <div className={styles.fields}><label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorEditor.021")}<select value={form.status} onChange={e => patch("status", e.target.value)}><option value="candidate">{i18nCopy(zh, "copy.components_factors_FactorEditor.022")}</option><option value="rejected">{i18nCopy(zh, "copy.components_factors_FactorEditor.023")}</option><option value="retired">{i18nCopy(zh, "copy.components_factors_FactorEditor.024")}</option></select></label><label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorEditor.025")}<input required maxLength={2000} value={reason} onChange={e => setReason(e.target.value)}/></label></div>
        {error && <ErrorBanner error={error}/>}
        <div className={styles.actions}><button className="btn btn-primary" type="submit" disabled={saving}>{saving ? (i18nCopy(zh, "copy.components_factors_FactorEditor.026")) : (i18nCopy(zh, "copy.components_factors_FactorEditor.027"))}</button><button type="button" className="btn btn-ghost" disabled={saving} onClick={onClose}>{i18nCopy(zh, "copy.components_factors_FactorEditor.028")}</button></div>
      </form>
    </Dialog.Content></Dialog.Portal></Dialog.Root>;
}

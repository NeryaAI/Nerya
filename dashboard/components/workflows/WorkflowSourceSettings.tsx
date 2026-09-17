"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import { useId, useState, type ReactNode } from "react";
import { sourceDimension, sourceTypeLabel, updateSourceDimension, type SourceConfig } from "../../lib/workflowSources";
import { WorkflowHelp } from "./WorkflowNative";
import { useWorkflowText } from "./WorkflowCanvas";
import styles from "./WorkflowStudio.module.css";
import ui from "./WorkflowSourceSettings.module.css";

function Values({ label, values, onChange, disabled, options = [], placeholder }: { label: string; values: string[]; onChange: (values: string[]) => void; disabled: boolean; options?: string[]; placeholder: string }) {
  const t = useWorkflowText(), id = useId();
  const [draft, setDraft] = useState("");
  const commit = () => { const tokens = draft.split(/[\s,，;；]+/).filter(Boolean); if (tokens.length) onChange([...new Set([...values, ...tokens])]); setDraft(""); };
  return <fieldset className={ui.values}><legend>{label}</legend><div className={ui.tokenInput}>
    {values.map((value) => <span className={ui.token} key={value}>{value}<button type="button" aria-label={`${t("移除", "Remove")} ${value}`} disabled={disabled} onClick={() => onChange(values.filter((v) => v !== value))}>×</button></span>)}
    <input aria-label={label} value={draft} disabled={disabled} placeholder={placeholder} list={options.length ? id : undefined} onChange={(event) => setDraft(event.target.value)} onBlur={commit} onKeyDown={(event) => { if (event.key === "Enter" && !event.nativeEvent.isComposing) { event.preventDefault(); commit(); } }} />
    {options.length > 0 && <datalist id={id}>{options.filter((option) => !values.includes(option)).map((option) => <option value={option} key={option} />)}</datalist>}
  </div>{options.length > 0 && <div className={ui.presets}>{options.filter((option) => !values.includes(option)).slice(0, 7).map((option) => <button key={option} type="button" disabled={disabled} onClick={() => onChange([...values, option])}>+ {option}</button>)}</div>}</fieldset>;
}

export function WorkflowSourceSettings({ config, disabled, onChange, markets = [], parameters }: { config: SourceConfig; disabled: boolean; onChange: (config: SourceConfig) => void; markets?: string[]; parameters?: ReactNode }) {
  const t = useWorkflowText();
  const provider = String(config.provider || "runtime.market"), capability = String(config.capability || "candles");
  const news = provider === "runtime.news";
  const selected = sourceDimension(config, "markets", "market", markets);
  const frames = sourceDimension(config, "timeframes", "timeframe", ["1m"]);
  const inheritMarkets = config.markets === undefined && config.market === undefined;
  const matrix = "markets" in config || "timeframes" in config;
  const choices = news ? ["news"] : ["candles", "features", "ticker"];
  function setType(type: string) { const next: SourceConfig = { ...config, capability: type }; if (type === "ticker") { delete next.timeframe; delete next.timeframes; } onChange(next); }
  return <div className={styles.settingsStack} data-testid="workflow-source-settings">
    <div className={ui.topline}><span>{provider === "runtime.market" ? t("行情接口", "Market reader") : news ? t("新闻接口", "News reader") : provider}</span><WorkflowHelp label={t("数据源说明", "Data-source help")}><p>{t("一个来源可以读取多个品种和多个周期。每个组合分别请求并保留标识，不会把不同周期混成一组。", "A source may read multiple markets and timeframes. Every combination retains its identity.")}</p><p>{t("多值配置返回分组数据；原单值配置保持旧格式。现有脚本需要按品种和周期取值，或让 Agent 协助调整。", "Plural configuration returns grouped data. Legacy scalar settings retain their format. Update consuming code or ask the Agent to select series explicitly.")}</p></WorkflowHelp></div>
    <label className={styles.field}>{t("数据类型", "Data type")}<ChoiceSelect aria-label={t("数据类型", "Data type")} disabled={disabled} value={capability} onValueChange={setType}>{!choices.includes(capability) && <option value={capability}>{capability}</option>}{choices.map((v) => <option key={v} value={v}>{sourceTypeLabel(v, t)}</option>)}</ChoiceSelect></label>
    {news ? <Values label={t("新闻订阅", "News subscriptions")} values={sourceDimension(config, "sources", "source")} onChange={(sources) => onChange({ ...config, sources })} disabled={disabled} placeholder={t("添加订阅标识，回车确认", "Add a subscription ID")} /> : <>
      <Values label={t("品种", "Markets")} values={selected} options={markets} disabled={disabled} placeholder={t("输入品种，可粘贴多个", "Add markets, or paste several")} onChange={(values) => onChange(updateSourceDimension(config, "markets", "market", values))} />
      <label className={ui.inherit}><input type="checkbox" disabled={disabled} checked={inheritMarkets} onChange={(event) => { const next = { ...config }; if (event.target.checked) { delete next.market; delete next.markets; } else next.markets = [...selected]; onChange(next); }} />{t("跟随策略品种", "Use strategy markets")}</label>
      {capability !== "ticker" && <Values label={t("周期", "Timeframes")} values={frames} disabled={disabled} options={["1m", "5m", "15m", "1h", "4h", "1d"]} placeholder={t("添加周期，例如 2h", "Add a timeframe, e.g. 2h")} onChange={(values) => onChange(updateSourceDimension(config, "timeframes", "timeframe", values))} />}
    </>}
    {capability !== "ticker" && <label className={styles.field}>{t("每组读取条数", "Rows per series")}<input aria-label={t("每组读取条数", "Rows per series")} type="number" min={1} step={1} disabled={disabled} value={typeof config.limit === "number" ? config.limit : config.limit === "" ? "" : news ? 50 : 100} onChange={(event) => onChange({ ...config, limit: event.target.value === "" ? "" : Number(event.target.value) })} /></label>}
    {!news && <p className={ui.summary}>{t(`${selected.length} 个品种 × ${capability === "ticker" ? "1 个快照" : `${frames.length} 个周期`}`, `${selected.length} markets × ${capability === "ticker" ? "1 snapshot" : `${frames.length} timeframes`}`)}<span>{matrix ? t("分组传给脚本 / Agent", "Grouped inputs for scripts / Agents") : t("兼容原单周期读取", "Legacy-compatible input")}</span></p>}
    {parameters}
    <details className={ui.disclosure}><summary>{t("连接与使用方", "Connection & consumers")}</summary><div className={styles.settingsStack}><label className={styles.field}>{t("数据提供方", "Provider")}<input disabled={disabled} value={provider} onChange={(event) => onChange({ ...config, provider: event.target.value })} /></label><label className={styles.field}>{t("自定义数据类型", "Custom data type")}<input disabled={disabled} value={capability} onChange={(event) => setType(event.target.value)} /></label><label className={styles.field}>{t("调用脚本（每行一个）", "Consumer scripts (one per line)")}<textarea rows={2} disabled={disabled} value={Array.isArray(config.consumers) ? config.consumers.join("\n") : ""} onChange={(event) => onChange({ ...config, consumers: event.target.value.split("\n").filter(Boolean) })} /></label></div></details>
  </div>;
}

"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import { useId, useState } from "react";
import type { WorkflowNode } from "../../lib/workflowTypes";
import { extraSourceFields } from "../../lib/workflowGuidance";
import { asObject, at, scheduleSummary, tierLabel, withValue, type WorkflowText } from "../../lib/workflowPresentation";
import { useWorkflowText } from "./WorkflowCanvas";
import { WorkflowHelp } from "./WorkflowNative";
import { sourceErrors } from "../../lib/workflowSources";
import { WorkflowSourceSettings } from "./WorkflowSourceSettings";
import styles from "./WorkflowStudio.module.css";

type FieldSpec = { path: string; zh: string; en: string; type?: "number" | "boolean" | "lines" | "text" | "long"; min?: number; max?: number; options?: string[] };
const FIELDS: Record<string, FieldSpec[]> = {
  strategy: [{ path: "title", zh: "策略名称", en: "Strategy name" }, { path: "description", zh: "希望这个策略做什么？", en: "What should this strategy do?", type: "long" }],
  source: [{ path: "provider", zh: "数据提供方", en: "Data provider", options: ["runtime.market"] }, { path: "capability", zh: "数据类型", en: "Data capability", options: ["candles", "features", "news"] }, { path: "timeframe", zh: "数据周期", en: "Timeframe", options: ["1m", "5m", "15m", "1h", "4h", "1d"] }, { path: "limit", zh: "每次读取条数", en: "Rows per request", type: "number", min: 1 }, { path: "consumers", zh: "交给哪些脚本（每行一个文件）", en: "Consumer scripts (one file per line)", type: "lines" }],
  agent: [{ path: "agent_profile.role", zh: "让 Agent 做什么？", en: "What should this Agent do?", type: "long" }, { path: "llm_policy.default_tier", zh: "思考深度", en: "Reasoning tier", options: ["light", "medium", "high"] }, { path: "llm_policy.max_calls_per_run", zh: "每次最多调用模型次数", en: "Model call limit per run", type: "number", min: 1 }, { path: "agent_session.include_prior_messages", zh: "延续之前的对话", en: "Include previous messages", type: "boolean" }, { path: "agent_profile.allowed_tools", zh: "可用工具（每行一个）", en: "Allowed tools (one per line)", type: "lines" }],
  risk: [{ path: "allow_direct_order", zh: "允许脚本直接提交订单", en: "Allow direct script orders", type: "boolean" }, { path: "max_single_order_usd", zh: "单笔金额上限（美元）", en: "Maximum order amount (USD)", type: "number", min: 0 }, { path: "max_daily_notional_usd", zh: "每日交易金额上限（美元）", en: "Daily notional limit (USD)", type: "number", min: 0 }, { path: "max_open_positions", zh: "最多同时持仓数", en: "Maximum open positions", type: "number", min: 0 }, { path: "require_subagent_before_order", zh: "下单前需要 Agent 复核", en: "Require Agent review before an order", type: "boolean" }],
  evidence: [{ path: "runs", zh: "回看最近多少次运行", en: "Recent runs to review", type: "number", min: 1 }, { path: "max_age_hours", zh: "只看最近多少小时", en: "Maximum age (hours)", type: "number", min: 1 }, { path: "min_closed_trades", zh: "至少需要多少笔已结束交易", en: "Minimum closed trades", type: "number", min: 0 }],
  proposal: [{ path: "tuning_prompt", zh: "补充要求", en: "Additional instructions", type: "long" }, { path: "proposal_policy.allowed_targets", zh: "允许修改的文件（每行一个）", en: "Editable files (one per line)", type: "lines" }],
  validation: [{ path: "require_backtest", zh: "必须先通过回测", en: "Require a backtest", type: "boolean" }, { path: "require_shadow_run", zh: "必须先进行影子运行", en: "Require a shadow run", type: "boolean" }, { path: "max_patch_files", zh: "每次最多修改文件数", en: "Maximum changed files", type: "number", min: 1 }, { path: "max_position_size_change_pct", zh: "仓位调整幅度上限（%）", en: "Position-size change limit (%)", type: "number", min: 0 }],
};
export function configurationErrors(node: WorkflowNode, value: unknown, t: WorkflowText): string[] {
  if (node.binding.file || !node.editable) return [];
  const errors: string[] = node.kind === "source" && typeof value === "object" ? sourceErrors(asObject(value), t) : [];
  for (const spec of FIELDS[node.kind] || []) {
    const v = at(value, spec.path.split("."));
    if (v === undefined) continue;
    if (spec.type === "number" && (typeof v !== "number" || !Number.isFinite(v) || (spec.min !== undefined && v < spec.min) || (spec.max !== undefined && v > spec.max))) errors.push(t(spec.zh, spec.en) + t("：请输入有效范围内的数字", ": enter a valid number"));
    if (spec.type === "boolean" && typeof v !== "boolean") errors.push(t(spec.zh, spec.en) + t("：必须是开关值", ": must be a boolean"));
    if (spec.type === "lines" && (!Array.isArray(v) || v.some((x) => typeof x !== "string"))) errors.push(t(spec.zh, spec.en) + t("：必须是文本列表", ": must be a text list"));
  }
  if (node.kind === "proposal") {
    const objectives = asObject(value).objectives;
    if (objectives !== undefined && objectives !== null) {
      if (!Array.isArray(objectives) && typeof objectives !== "object") errors.push(t("请选择优化指标，自定义要求请写在补充要求中。", "Choose objective metrics. Put custom instructions in the additional requirements."));
      else if (objectiveValues(objectives).some((id) => !Object.hasOwn(OBJECTIVES, id))) errors.push(t("包含不支持的优化指标，请选择已有指标；自定义要求写在补充要求中。", "Unsupported objective. Choose a supported metric and put custom requirements in the instructions."));
    }
  }
  if (node.kind === "scheduler" && asObject(value).type === "interval") {
    const n = asObject(value).every_seconds;
    if (typeof n !== "number" || !Number.isInteger(n) || n <= 0) errors.push(t("运行间隔必须是正整数秒", "Interval must be a positive whole number of seconds"));
  }
  if (node.id === "agent:runtime") {
    const execution = asObject(asObject(value).agent_execution);
    for (const key of ["max_iterations", "max_tool_calls", "max_wall_seconds"]) {
      const n = execution[key];
      if (n != null && (typeof n !== "number" || !Number.isFinite(n) || n <= 0 || key !== "max_wall_seconds" && !Number.isInteger(n))) errors.push(t("Agent 预算必须为正数，或留空继承。", "Agent budgets must be positive or blank to inherit."));
    }
  }
  return errors;
}
const FIELD_HELP: Record<string, [string, string]> = {
  timeframe: ["一根数据代表的时间。例如 15m 是 15 分钟，不是策略运行间隔。", "Time represented by one bar. 15m means 15 minutes, not the strategy schedule."],
  limit: ["每次请求读取多少条；计算指标需要足够的历史数据。", "Rows fetched per request. Indicators need sufficient history."],
  "llm_policy.max_calls_per_run": ["仅控制脚本 ctx.llm 调用，不是策略 Agent 的决策轮数。", "Controls script ctx.llm calls, not strategy Agent decision rounds."],
  "agent_profile.role": ["写清分析任务、判断依据和期望输出；无需编写内部工具调用代码。", "Describe the task, evidence and output. No internal tool-call code is needed."],
  "agent_profile.allowed_tools": ["填写已存在的工具名称。留空会采用运行时默认工具，不代表无权限。", "Use actual tool names. Empty uses runtime defaults; it does not mean no permissions."],
  provider: ["可输入已接入的自定义提供方标识；填写名称不会自动安装服务。", "Enter an integrated custom provider ID. A name does not install a service."],
  capability: ["所选数据能力必须由提供方实际支持。", "The selected capability must be supported by the provider."],
};
const OBJECTIVES: Record<string, [string, string]> = {
  risk_adjusted_return: ["平衡收益与风险", "Balance return and risk"], drawdown: ["减少回撤", "Reduce drawdown"],
  return: ["提高收益", "Improve returns"], execution_quality: ["改善执行质量", "Improve execution quality"],
  win_rate: ["提高胜率", "Improve win rate"], slippage: ["减少滑点", "Reduce slippage"],
  sharpe: ["夏普比率", "Sharpe ratio"], sortino: ["索提诺比率", "Sortino ratio"],
};
function objectiveValues(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  const object = asObject(value);
  return [object.primary ? String(object.primary) : "", ...(Array.isArray(object.secondary) ? object.secondary.map(String) : [])].filter(Boolean);
}
function ObjectiveSettings({ value, disabled, onChange }: { value: unknown; disabled: boolean; onChange: (value: unknown) => void }) {
  const t = useWorkflowText();
  const selected = objectiveValues(value);
  function toggle(id: string, checked: boolean) {
    const next = checked ? [...selected, id] : selected.filter((item) => item !== id);
    onChange(value && !Array.isArray(value) && typeof value === "object" ? { ...asObject(value), primary: next[0] || "", secondary: next.slice(1) } : next);
  }
  const control = (id: string) => <label key={id} className={styles.toggleRow}><span>{OBJECTIVES[id] ? t(...OBJECTIVES[id]) : id}</span><input type="checkbox" checked={selected.includes(id)} disabled={disabled} onChange={(event) => toggle(id, event.target.checked)} /></label>;
  return <fieldset className={styles.settingsStack}><legend className={styles.field}>{t("重点改善什么？（可多选）", "What should improve? (choose several)")}</legend><div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", columnGap: 14 }}>{Object.keys(OBJECTIVES).slice(0, 4).map(control)}</div><details className={styles.advanced}><summary>{t("更多优化指标", "More objective metrics")}</summary>{Object.keys(OBJECTIVES).slice(4).map(control)}{selected.filter((id) => !Object.hasOwn(OBJECTIVES, id)).map(control)}</details></fieldset>;
}
function Field({ spec, value, onChange, disabled }: { spec: FieldSpec; value: unknown; onChange: (value: unknown) => void; disabled: boolean }) {
  const t = useWorkflowText();
  const id = useId();
  const label = t(spec.zh, spec.en);
  if (spec.type === "boolean") return <label className={styles.toggleRow} htmlFor={id}><span>{label}</span><input id={id} role="switch" type="checkbox" checked={value === true} disabled={disabled} onChange={(event) => onChange(event.target.checked)} /></label>;
  const string = value === undefined || value === null ? "" : Array.isArray(value) ? value.join("\n") : String(value);
  const choices: Record<string, string> | undefined = !spec.options ? undefined : spec.path.endsWith("default_tier") || spec.path === "tier" ? Object.fromEntries(["light", "medium", "high"].map((tier) => [tier, tierLabel(tier, t)])) : undefined;
  return <div className={styles.field}><div style={{ display: "flex", alignItems: "center", gap: 7 }}><label htmlFor={id}>{label}</label>{FIELD_HELP[spec.path] && <WorkflowHelp label={`${label} · ${t("帮助", "Help")}`}><p>{t(...FIELD_HELP[spec.path])}</p></WorkflowHelp>}</div>
    {choices ? <ChoiceSelect id={id} aria-label={label} value={string} disabled={disabled} onValueChange={(choiceValue) => onChange(choiceValue)}>{!Object.hasOwn(choices, string) && <option value={string}>{string ? t(`自定义：${string}`, `Custom: ${string}`) : t("未设置", "Not set")}</option>}{Object.entries(choices).map(([key, text]) => <option key={key} value={key}>{text}</option>)}</ChoiceSelect> : spec.options ? <input id={id} aria-label={label} list={`${id}-options`} value={string} disabled={disabled} placeholder={t("选择或输入自定义值", "Choose or enter a value")} onChange={(event) => onChange(event.target.value)} /> : spec.type === "long" || spec.type === "lines" ? <textarea id={id} rows={spec.type === "long" ? 4 : 3} value={string} disabled={disabled} onChange={(event) => onChange(spec.type === "lines" ? event.target.value.split("\n") : event.target.value)} /> : <input id={id} type={spec.type === "number" ? "number" : "text"} min={spec.min} max={spec.max} value={string} disabled={disabled} onChange={(event) => onChange(spec.type === "number" && event.target.value !== "" ? Number(event.target.value) : event.target.value)} />}
    {!choices && spec.options && <datalist id={`${id}-options`}>{spec.options.map((option) => <option key={option} value={option}>{spec.path.endsWith("default_tier") ? tierLabel(option, t) : option}</option>)}</datalist>}
  </div>;
}
export function CommonSettings({ node, config, disabled, onChange, markets = [] }: { node: WorkflowNode; config: Record<string, unknown>; disabled: boolean; onChange: (value: Record<string, unknown>) => void; markets?: string[] }) {
  const t = useWorkflowText();
  if (node.kind === "source") return <WorkflowSourceSettings config={config} markets={markets} disabled={disabled} onChange={onChange} parameters={<details className={styles.advanced}><summary>{t("指标与自定义参数", "Indicator & custom parameters")}</summary><ConfigTree value={extraSourceFields(config)} disabled={disabled} onChange={(next) => onChange({ ...config, ...asObject(next) })} /></details>} />;
  if (node.kind === "scheduler") return <ScheduleSettings config={config} disabled={disabled} onChange={onChange} />;
  const specs = node.kind === "agent" && node.id !== "agent:runtime" ? [{ path: "name", zh: "Agent 名称", en: "Agent name" }, { path: "tier", zh: "模型档位", en: "Model tier", options: ["light", "medium", "high"] }] : FIELDS[node.kind] || [];
  return <div className={styles.settingsStack}>{node.kind === "proposal" && <ObjectiveSettings value={config.objectives} disabled={disabled} onChange={(value) => onChange({ ...config, objectives: value })} />}{specs.filter((spec) => !["consumers", "provider", "capability", "agent_session.include_prior_messages", "agent_profile.allowed_tools", "proposal_policy.allowed_targets"].includes(spec.path)).map((spec) => <Field key={spec.path} spec={spec} value={at(config, spec.path.split("."))} disabled={disabled} onChange={(value) => onChange(withValue(config, spec.path.split("."), value))} />)}
    {specs.filter((spec) => ["consumers", "provider", "capability", "agent_session.include_prior_messages", "agent_profile.allowed_tools", "proposal_policy.allowed_targets"].includes(spec.path)).map((spec) => <details className={styles.advanced} key={spec.path}><summary>{t(spec.zh, spec.en)}</summary><Field spec={spec} value={at(config, spec.path.split("."))} disabled={disabled} onChange={(value) => onChange(withValue(config, spec.path.split("."), value))} /></details>)}
    {node.kind === "validation" && <p className={styles.helper}>{t("人工确认始终保留，不能在这里关闭。", "Operator approval remains mandatory.")}</p>}
    {node.kind === "risk" && <p className={styles.helper}>{t("脚本权限；Agent 工具与账户权限独立配置。", "Script permissions. Agent tools and account permissions are separate.")}</p>}
  </div>;
}
function ScheduleSettings({ config, disabled, onChange }: { config: Record<string, unknown>; disabled: boolean; onChange: (value: Record<string, unknown>) => void }) {
  const t = useWorkflowText();
  const cron = String(config.cron || "");
  const daily = cron.match(/^(\d{1,2}) (\d{1,2}) \* \* \*$/);
  const hourlyMatch = cron.match(/^0 \*\/(1|2|3|4|6|8|12) \* \* \*$/);
  const [custom, setCustom] = useState(config.type === "cron" && !daily && !hourlyMatch);
  const mode = config.type === "interval" ? "interval" : custom ? "custom" : hourlyMatch ? "hourly" : daily ? "daily" : "custom";
  return <div className={styles.settingsStack}>
    <label className={styles.field}>{t("什么时候运行", "When to run")}<ChoiceSelect aria-label={t("什么时候运行", "When to run")} value={mode} disabled={disabled} onValueChange={(choiceValue) => {
      const value = choiceValue; setCustom(value === "custom");
      onChange(value === "interval" ? { ...config, type: "interval", every_seconds: typeof config.every_seconds === "number" ? config.every_seconds : 300 } : { ...config, type: "cron", cron: value === "daily" ? "0 9 * * *" : value === "hourly" ? "0 */6 * * *" : cron || "0 9 * * *" });
    }}><option value="interval">{t("固定间隔", "Fixed interval")}</option><option value="daily">{t("每天定时", "Daily")}</option><option value="hourly">{t("按整点间隔", "Every few hours")}</option><option value="custom">{t("自定义规则（Cron）", "Custom rule (Cron)")}</option></ChoiceSelect></label>
    {mode === "hourly" ? <label className={styles.field}>{t("每隔多久复查一次", "Hours between checks")}<ChoiceSelect aria-label={t("每隔多久复查一次", "Hours between checks")} disabled={disabled} value={hourlyMatch?.[1] || "6"} onValueChange={(choiceValue) => onChange({ ...config, cron: `0 */${choiceValue} * * *` })}>{[1, 2, 3, 4, 6, 8, 12].map((hour) => <option key={hour} value={hour}>{t(`${hour} 小时`, `${hour} hours`)}</option>)}</ChoiceSelect></label> : mode === "interval" ? <><div className={styles.quickChoices}>{[60, 300, 900, 3600].map((n) => <button type="button" key={n} disabled={disabled} aria-pressed={config.every_seconds === n} onClick={() => onChange({ ...config, every_seconds: n })}>{n < 3600 ? t(`${n / 60} 分钟`, `${n / 60} min`) : t("1 小时", "1 hour")}</button>)}</div><Field spec={{ path: "every_seconds", zh: "自定义间隔（秒）", en: "Custom interval (seconds)", type: "number", min: 1 }} value={config.every_seconds} disabled={disabled} onChange={(value) => onChange({ ...config, every_seconds: value })} /></> : mode === "daily" ? <label className={styles.field}>{t("每天几点", "Time of day")}<input type="time" disabled={disabled} value={daily ? `${daily[2].padStart(2, "0")}:${daily[1].padStart(2, "0")}` : "09:00"} onChange={(event) => { const [hour, minute] = event.target.value.split(":"); if (hour && minute) onChange({ ...config, cron: `${Number(minute)} ${Number(hour)} * * *` }); }} /></label> : <Field spec={{ path: "cron", zh: "Cron 时间规则", en: "Cron expression" }} value={cron} disabled={disabled} onChange={(value) => onChange({ ...config, cron: value })} />}
    {mode !== "interval" && <Field spec={{ path: "timezone", zh: "时间所在时区", en: "Schedule timezone", options: ["UTC", "Asia/Shanghai", "Asia/Hong_Kong", "America/New_York", "Europe/London"] }} value={config.timezone || "UTC"} disabled={disabled} onChange={(value) => onChange({ ...config, timezone: value })} />}
    <Field spec={{ path: "enabled", zh: "应用后启用此调度", en: "Enable when applied", type: "boolean" }} value={config.enabled} disabled={disabled} onChange={(value) => onChange({ ...config, enabled: value })} />
    <p className={styles.helper}>{t("保存为提案，审批应用后生效。", "Takes effect after proposal approval and application.")}</p>
  </div>;
}

/** Advanced structured editing preserves unknown keys and nested extension data. */
export function ConfigTree({ value, onChange, disabled, depth = 0, allowAdd = true, lockedKeys = [] }: { value: unknown; onChange: (value: unknown) => void; disabled: boolean; depth?: number; allowAdd?: boolean; lockedKeys?: string[] }) {
  const t = useWorkflowText();
  const [key, setKey] = useState("");
  const [kind, setKind] = useState("text");
  const [error, setError] = useState("");
  if (!value || typeof value !== "object") return <Field spec={{ path: "value", zh: "值", en: "Value", type: typeof value === "boolean" ? "boolean" : typeof value === "number" ? "number" : "text" }} value={value} disabled={disabled} onChange={onChange} />;
  if (depth > 5) return <p className={styles.helper}>{t("更深层的结构请在完整配置中编辑。", "Edit deeper structures in the complete JSON configuration.")}</p>;
  const array = Array.isArray(value);
  const entries = Object.entries(value);
  function update(name: string, next: unknown) { onChange(array ? (value as unknown[]).map((item, i) => i === Number(name) ? next : item) : { ...asObject(value), [name]: next }); }
  return <div className={styles.configTree}>{entries.map(([name, item]) => item && typeof item === "object" ? <details key={name} className={styles.advanced}><summary>{array ? Number(name) + 1 : name} <span>· {Array.isArray(item) ? t("列表", "List") : t("分组", "Group")}</span></summary><ConfigTree value={item} depth={depth + 1} disabled={disabled} onChange={(next) => update(name, next)} /></details> : <Field key={name} spec={{ path: name, zh: name, en: name, type: typeof item === "boolean" ? "boolean" : typeof item === "number" ? "number" : "text" }} value={item} disabled={disabled || lockedKeys.includes(name)} onChange={(next) => update(name, next)} />)}
    {!disabled && allowAdd && <details className={styles.advanced}><summary>+ {array ? t("添加一项", "Add item") : t("添加自定义参数", "Add a custom parameter")}</summary><div className={styles.settingsStack}>{!array && <label className={styles.field}>{t("参数名称", "Parameter name")}<input value={key} onChange={(event) => setKey(event.target.value)} /></label>}<label className={styles.field}>{t("参数类型", "Parameter type")}<ChoiceSelect aria-label={t("参数类型", "Parameter type")} value={kind} onValueChange={(choiceValue) => setKind(choiceValue)}><option value="text">{t("文本", "Text")}</option><option value="number">{t("数字", "Number")}</option><option value="boolean">{t("开关", "Switch")}</option><option value="object">{t("分组", "Object")}</option><option value="array">{t("列表", "List")}</option></ChoiceSelect></label><button type="button" className={styles.secondaryButton} onClick={() => {
      const name = key.trim();
      if (!array && (!name || ["__proto__", "constructor", "prototype"].includes(name) || Object.hasOwn(value, name))) { setError(t("请使用不重复的有效参数名。", "Use a valid, unique parameter name.")); return; }
      const next = ({ text: "", number: 0, boolean: false, object: {}, array: [] } as Record<string, unknown>)[kind];
      onChange(array ? [...value as unknown[], next] : { ...asObject(value), [name]: next }); setKey(""); setError("");
    }}>{t("添加参数", "Add parameter")}</button>{error && <p className={styles.error} role="alert">{error}</p>}</div></details>}
  </div>;
}

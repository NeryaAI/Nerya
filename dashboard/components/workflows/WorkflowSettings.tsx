"use client";
import { Icon as NeryaGlyph } from "../icons";

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
import { ScheduleFields } from "./ScheduleFields";

type FieldSpec = { path: string; label: string; type?: "number" | "boolean" | "lines" | "text" | "long"; min?: number; max?: number; options?: string[] };
const FIELDS: Record<string, FieldSpec[]> = {
  strategy: [{ path: "title", label: "copy.workflowSettings.fields.strategy.title" }, { path: "description", label: "copy.workflowSettings.fields.strategy.description", type: "long" }],
  source: [{ path: "provider", label: "copy.workflowSettings.fields.source.provider", options: ["runtime.market"] }, { path: "capability", label: "copy.workflowSettings.fields.source.capability", options: ["candles", "features", "news"] }, { path: "timeframe", label: "copy.workflowSettings.fields.source.timeframe", options: ["1m", "5m", "15m", "1h", "4h", "1d"] }, { path: "limit", label: "copy.workflowSettings.fields.source.limit", type: "number", min: 1 }, { path: "consumers", label: "copy.workflowSettings.fields.source.consumers", type: "lines" }],
  agent: [{ path: "agent_profile.role", label: "copy.workflowSettings.fields.agent.agent_profile.role", type: "long" }, { path: "llm_policy.default_tier", label: "copy.workflowSettings.fields.agent.llm_policy.default_tier", options: ["light", "medium", "high"] }, { path: "llm_policy.max_calls_per_run", label: "copy.workflowSettings.fields.agent.llm_policy.max_calls_per_run", type: "number", min: 1 }, { path: "agent_session.include_prior_messages", label: "copy.workflowSettings.fields.agent.agent_session.include_prior_messages", type: "boolean" }, { path: "agent_profile.allowed_tools", label: "copy.workflowSettings.fields.agent.agent_profile.allowed_tools", type: "lines" }],
  risk: [{ path: "allow_direct_order", label: "copy.workflowSettings.fields.risk.allow_direct_order", type: "boolean" }, { path: "max_single_order_usd", label: "copy.workflowSettings.fields.risk.max_single_order_usd", type: "number", min: 0 }, { path: "max_daily_notional_usd", label: "copy.workflowSettings.fields.risk.max_daily_notional_usd", type: "number", min: 0 }, { path: "max_open_positions", label: "copy.workflowSettings.fields.risk.max_open_positions", type: "number", min: 0 }, { path: "require_subagent_before_order", label: "copy.workflowSettings.fields.risk.require_subagent_before_order", type: "boolean" }],
  evidence: [{ path: "runs", label: "copy.workflowSettings.fields.evidence.runs", type: "number", min: 1 }, { path: "max_age_hours", label: "copy.workflowSettings.fields.evidence.max_age_hours", type: "number", min: 1 }, { path: "min_closed_trades", label: "copy.workflowSettings.fields.evidence.min_closed_trades", type: "number", min: 0 }],
  proposal: [{ path: "tuning_prompt", label: "copy.workflowSettings.fields.proposal.tuning_prompt", type: "long" }, { path: "proposal_policy.allowed_targets", label: "copy.workflowSettings.fields.proposal.proposal_policy.allowed_targets", type: "lines" }],
  validation: [{ path: "require_backtest", label: "copy.workflowSettings.fields.validation.require_backtest", type: "boolean" }, { path: "require_shadow_run", label: "copy.workflowSettings.fields.validation.require_shadow_run", type: "boolean" }, { path: "max_patch_files", label: "copy.workflowSettings.fields.validation.max_patch_files", type: "number", min: 1 }, { path: "max_position_size_change_pct", label: "copy.workflowSettings.fields.validation.max_position_size_change_pct", type: "number", min: 0 }],
};
export function configurationErrors(node: WorkflowNode, value: unknown, t: WorkflowText): string[] {
  if (node.binding.file || !node.editable) return [];
  const errors: string[] = node.kind === "source" && typeof value === "object" ? sourceErrors(asObject(value), t) : [];
  for (const spec of FIELDS[node.id === "evidence:review" ? "evidence" : node.kind] || []) {
    const v = at(value, spec.path.split("."));
    if (v === undefined) continue;
    if (spec.type === "number" && (typeof v !== "number" || !Number.isFinite(v) || (spec.min !== undefined && v < spec.min) || (spec.max !== undefined && v > spec.max))) errors.push(t(spec.label) + t("copy.components_workflows_WorkflowSettings.001"));
    if (spec.type === "boolean" && typeof v !== "boolean") errors.push(t(spec.label) + t("copy.components_workflows_WorkflowSettings.002"));
    if (spec.type === "lines" && (!Array.isArray(v) || v.some((x) => typeof x !== "string"))) errors.push(t(spec.label) + t("copy.components_workflows_WorkflowSettings.003"));
  }
  if (node.kind === "proposal") {
    const objectives = asObject(value).objectives;
    if (objectives !== undefined && objectives !== null) {
      if (!Array.isArray(objectives) && typeof objectives !== "object") errors.push(t("copy.components_workflows_WorkflowSettings.004"));
      else if (objectiveValues(objectives).some((id) => !Object.hasOwn(OBJECTIVES, id))) errors.push(t("copy.components_workflows_WorkflowSettings.005"));
    }
  }
  if (node.id === "evidence:review") {
    for (const spec of FIELDS.evidence || []) {
      const n = at(value, spec.path.split("."));
      if (spec.type === "number" && n !== undefined && typeof n === "number" && !Number.isInteger(n)) errors.push(t(spec.label) + t("copy.components_workflows_WorkflowSettings.001"));
    }
  }
  if (node.kind === "scheduler" && asObject(value).type === "interval") {
    const n = asObject(value).every_seconds;
    if (typeof n !== "number" || !Number.isInteger(n) || n <= 0) errors.push(t("copy.components_workflows_WorkflowSettings.006"));
  }
  if (node.id === "agent:runtime") {
    const execution = asObject(asObject(value).agent_execution);
    for (const key of ["max_iterations", "max_tool_calls", "max_wall_seconds"]) {
      const n = execution[key];
      if (n != null && (typeof n !== "number" || !Number.isFinite(n) || n <= 0 || key !== "max_wall_seconds" && !Number.isInteger(n))) errors.push(t("copy.components_workflows_WorkflowSettings.007"));
    }
  }
  return errors;
}
const FIELD_HELP: Record<string, string> = {
  timeframe: "copy.workflowSettings.help.timeframe",
  limit: "copy.workflowSettings.help.limit",
  "llm_policy.max_calls_per_run": "copy.workflowSettings.help.llm_policy.max_calls_per_run",
  "agent_profile.role": "copy.workflowSettings.help.agent_profile.role",
  "agent_profile.allowed_tools": "copy.workflowSettings.help.agent_profile.allowed_tools",
  provider: "copy.workflowSettings.help.provider",
  capability: "copy.workflowSettings.help.capability",
};
const OBJECTIVES: Record<string, string> = {
  risk_adjusted_return: "copy.workflowSettings.objectives.risk_adjusted_return", drawdown: "copy.workflowSettings.objectives.drawdown",
  return: "copy.workflowSettings.objectives.return", execution_quality: "copy.workflowSettings.objectives.execution_quality",
  win_rate: "copy.workflowSettings.objectives.win_rate", slippage: "copy.workflowSettings.objectives.slippage",
  sharpe: "copy.workflowSettings.objectives.sharpe", sortino: "copy.workflowSettings.objectives.sortino",
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
  const control = (id: string) => <label key={id} className={styles.toggleRow}><span>{OBJECTIVES[id] ? t(OBJECTIVES[id]) : id}</span><input type="checkbox" checked={selected.includes(id)} disabled={disabled} onChange={(event) => toggle(id, event.target.checked)} /></label>;
  return <fieldset className={styles.settingsStack}><legend className={styles.field}>{t("copy.components_workflows_WorkflowSettings.008")}</legend><div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", columnGap: 14 }}>{Object.keys(OBJECTIVES).slice(0, 4).map(control)}</div><details className={styles.advanced}><summary>{t("copy.components_workflows_WorkflowSettings.009")}</summary>{Object.keys(OBJECTIVES).slice(4).map(control)}{selected.filter((id) => !Object.hasOwn(OBJECTIVES, id)).map(control)}</details></fieldset>;
}
function Field({ spec, value, onChange, disabled }: { spec: FieldSpec; value: unknown; onChange: (value: unknown) => void; disabled: boolean }) {
  const t = useWorkflowText();
  const id = useId();
  const label = t(spec.label);
  if (spec.type === "boolean") return <label className={styles.toggleRow} htmlFor={id}><span>{label}</span><input id={id} role="switch" type="checkbox" checked={value === true} disabled={disabled} onChange={(event) => onChange(event.target.checked)} /></label>;
  const string = value === undefined || value === null ? "" : Array.isArray(value) ? value.join("\n") : String(value);
  const choices: Record<string, string> | undefined = !spec.options ? undefined : spec.path.endsWith("default_tier") || spec.path === "tier" ? Object.fromEntries(["light", "medium", "high"].map((tier) => [tier, tierLabel(tier, t)])) : undefined;
  return <div className={styles.field}><div style={{ display: "flex", alignItems: "center", gap: 7 }}><label htmlFor={id}>{label}</label>{FIELD_HELP[spec.path] && <WorkflowHelp label={`${label} · ${t("copy.components_workflows_WorkflowSettings.010")}`}><p>{t(FIELD_HELP[spec.path])}</p></WorkflowHelp>}</div>
    {choices ? <ChoiceSelect id={id} aria-label={label} value={string} disabled={disabled} onValueChange={(choiceValue) => onChange(choiceValue)}>{!Object.hasOwn(choices, string) && <option value={string}>{string ? t("copy.components_workflows_WorkflowSettings.011", { value0: string }) : t("copy.components_workflows_WorkflowSettings.012")}</option>}{Object.entries(choices).map(([key, text]) => <option key={key} value={key}>{text}</option>)}</ChoiceSelect> : spec.options ? <input id={id} aria-label={label} list={`${id}-options`} value={string} disabled={disabled} placeholder={t("copy.components_workflows_WorkflowSettings.013")} onChange={(event) => onChange(event.target.value)} /> : spec.type === "long" || spec.type === "lines" ? <textarea id={id} rows={spec.type === "long" ? 4 : 3} value={string} disabled={disabled} onChange={(event) => onChange(spec.type === "lines" ? event.target.value.split("\n") : event.target.value)} /> : <input id={id} type={spec.type === "number" ? "number" : "text"} min={spec.min} max={spec.max} value={string} disabled={disabled} onChange={(event) => onChange(spec.type === "number" && event.target.value !== "" ? Number(event.target.value) : event.target.value)} />}
    {!choices && spec.options && <datalist id={`${id}-options`}>{spec.options.map((option) => <option key={option} value={option}>{spec.path.endsWith("default_tier") ? tierLabel(option, t) : option}</option>)}</datalist>}
  </div>;
}
export function CommonSettings({ node, config, disabled, onChange, markets = [] }: { node: WorkflowNode; config: Record<string, unknown>; disabled: boolean; onChange: (value: Record<string, unknown>) => void; markets?: string[] }) {
  const t = useWorkflowText();
  if (node.kind === "source") return <WorkflowSourceSettings config={config} markets={markets} disabled={disabled} onChange={onChange} parameters={<details className={styles.advanced}><summary>{t("copy.components_workflows_WorkflowSettings.014")}</summary><ConfigTree value={extraSourceFields(config)} disabled={disabled} onChange={(next) => onChange({ ...config, ...asObject(next) })} /></details>} />;
  if (node.kind === "scheduler") return <ScheduleSettings config={config} disabled={disabled} onChange={onChange} />;
  const specs = node.kind === "agent" && node.id !== "agent:runtime" ? [{ path: "name", label: "copy.workflowSettings.fields.runtimeAgent.name" }, { path: "tier", label: "copy.workflowSettings.fields.runtimeAgent.tier", options: ["light", "medium", "high"] }] : FIELDS[node.id === "evidence:review" ? "evidence" : node.kind] || [];
  return <div className={styles.settingsStack}>{node.kind === "proposal" && <ObjectiveSettings value={config.objectives} disabled={disabled} onChange={(value) => onChange({ ...config, objectives: value })} />}{specs.filter((spec) => !["consumers", "provider", "capability", "agent_session.include_prior_messages", "agent_profile.allowed_tools", "proposal_policy.allowed_targets"].includes(spec.path)).map((spec) => <Field key={spec.path} spec={spec} value={at(config, spec.path.split("."))} disabled={disabled} onChange={(value) => onChange(withValue(config, spec.path.split("."), value))} />)}
    {specs.filter((spec) => ["consumers", "provider", "capability", "agent_session.include_prior_messages", "agent_profile.allowed_tools", "proposal_policy.allowed_targets"].includes(spec.path)).map((spec) => <details className={styles.advanced} key={spec.path}><summary>{t(spec.label)}</summary><Field spec={spec} value={at(config, spec.path.split("."))} disabled={disabled} onChange={(value) => onChange(withValue(config, spec.path.split("."), value))} /></details>)}
    {node.kind === "validation" && <p className={styles.helper}>{t("copy.components_workflows_WorkflowSettings.015")}</p>}
    {node.kind === "risk" && <p className={styles.helper}>{t("copy.components_workflows_WorkflowSettings.016")}</p>}
  </div>;
}
function ScheduleSettings({ config, disabled, onChange }: { config: Record<string, unknown>; disabled: boolean; onChange: (value: Record<string, unknown>) => void }) {
  const t = useWorkflowText();
  const value = {
    cadence: config.type === "interval" ? "interval" as const : "cron" as const,
    cron: String(config.cron || "0 9 * * *"), everySeconds: String(config.every_seconds ?? 300),
    timezone: String(config.timezone || "UTC"), runAt: "",
    startsAt: typeof config.starts_at === "string" ? config.starts_at : null,
    endsAt: typeof config.ends_at === "string" ? config.ends_at : null,
  };
  return <div className={styles.settingsStack}>
    <ScheduleFields value={value} disabled={disabled} allowOnce={false} showOverlap={false} onChange={patch => {
      const next = { ...value, ...patch };
      const updated: Record<string, unknown> = { ...config, type: next.cadence, timezone: next.timezone };
      if (next.cadence === "cron") { updated.cron = next.cron; delete updated.every_seconds; }
      else { updated.every_seconds = Number(next.everySeconds); delete updated.cron; }
      onChange(updated);
    }}/>
    <Field spec={{ path: "enabled", label: "copy.workflowSettings.fields.schedule.enabled", type: "boolean" }} value={config.enabled} disabled={disabled} onChange={enabled => onChange({ ...config, enabled })} />
    <p className={styles.helper}>{t("copy.components_workflows_WorkflowSettings.029")}</p>
  </div>;
}

/** Advanced structured editing preserves unknown keys and nested extension data. */
export function ConfigTree({ value, onChange, disabled, depth = 0, allowAdd = true, lockedKeys = [] }: { value: unknown; onChange: (value: unknown) => void; disabled: boolean; depth?: number; allowAdd?: boolean; lockedKeys?: string[] }) {
  const t = useWorkflowText();
  const [key, setKey] = useState("");
  const [kind, setKind] = useState("text");
  const [error, setError] = useState("");
  if (!value || typeof value !== "object") return <Field spec={{ path: "value", label: "copy.workflowSettings.fields.value.value", type: typeof value === "boolean" ? "boolean" : typeof value === "number" ? "number" : "text" }} value={value} disabled={disabled} onChange={onChange} />;
  if (depth > 5) return <p className={styles.helper}>{t("copy.components_workflows_WorkflowSettings.030")}</p>;
  const array = Array.isArray(value);
  const entries = Object.entries(value);
  function update(name: string, next: unknown) { onChange(array ? (value as unknown[]).map((item, i) => i === Number(name) ? next : item) : { ...asObject(value), [name]: next }); }
  return <div className={styles.configTree}>{entries.map(([name, item]) => item && typeof item === "object" ? <details key={name} className={styles.advanced}><summary>{array ? Number(name) + 1 : name} <span>· {Array.isArray(item) ? t("copy.components_workflows_WorkflowSettings.031") : t("copy.components_workflows_WorkflowSettings.032")}</span></summary><ConfigTree value={item} depth={depth + 1} disabled={disabled} onChange={(next) => update(name, next)} /></details> : <Field key={name} spec={{ path: name, label: name, type: typeof item === "boolean" ? "boolean" : typeof item === "number" ? "number" : "text" }} value={item} disabled={disabled || lockedKeys.includes(name)} onChange={(next) => update(name, next)} />)}
    {!disabled && allowAdd && <details className={styles.advanced}><summary><NeryaGlyph name="plus" size={14} /> {array ? t("copy.components_workflows_WorkflowSettings.033") : t("copy.components_workflows_WorkflowSettings.034")}</summary><div className={styles.settingsStack}>{!array && <label className={styles.field}>{t("copy.components_workflows_WorkflowSettings.035")}<input value={key} onChange={(event) => setKey(event.target.value)} /></label>}<label className={styles.field}>{t("copy.components_workflows_WorkflowSettings.036")}<ChoiceSelect aria-label={t("copy.components_workflows_WorkflowSettings.037")} value={kind} onValueChange={(choiceValue) => setKind(choiceValue)}><option value="text">{t("copy.components_workflows_WorkflowSettings.038")}</option><option value="number">{t("copy.components_workflows_WorkflowSettings.039")}</option><option value="boolean">{t("copy.components_workflows_WorkflowSettings.040")}</option><option value="object">{t("copy.components_workflows_WorkflowSettings.041")}</option><option value="array">{t("copy.components_workflows_WorkflowSettings.042")}</option></ChoiceSelect></label><button type="button" className={styles.secondaryButton} onClick={() => {
      const name = key.trim();
      if (!array && (!name || ["__proto__", "constructor", "prototype"].includes(name) || Object.hasOwn(value, name))) { setError(t("copy.components_workflows_WorkflowSettings.043")); return; }
      const next = ({ text: "", number: 0, boolean: false, object: {}, array: [] } as Record<string, unknown>)[kind];
      onChange(array ? [...value as unknown[], next] : { ...asObject(value), [name]: next }); setKey(""); setError("");
    }}>{t("copy.components_workflows_WorkflowSettings.044")}</button>{error && <p className={styles.error} role="alert">{error}</p>}</div></details>}
  </div>;
}

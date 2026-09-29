"use client";
import { Icon as NeryaGlyph } from "../icons";

import { ChoiceSelect } from "../ChoiceSelect";
import { useTranslations } from "next-intl";
import { ScriptExplanation } from "./ScriptExplanation";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { WorkflowNode } from "../../lib/workflowTypes";
import { asObject, cardFacts, cardPurpose, cardTitle } from "../../lib/workflowPresentation";
import { clientApi } from "../../lib/clientApi";
import { cardGuide, editableSource } from "../../lib/workflowGuidance";
import { WorkflowIcon, useWorkflowText } from "./WorkflowCanvas";
import { WorkflowHelp } from "./WorkflowNative";
import { WorkflowAgentSettings, type AgentDefaults } from "./WorkflowAgentSettings";
import { WorkflowCodeEditor } from "./WorkflowCodeEditor";
import { CommonSettings, ConfigTree, configurationErrors } from "./WorkflowSettings";
import styles from "./WorkflowStudio.module.css";
import ui from "./WorkflowNative.module.css";

export function AccountChoice({ value, disabled, onChange }: { value: string; disabled: boolean; onChange: (value: string) => void }) {
  const t = useWorkflowText();
  const [options, setOptions] = useState<Array<{ id: string; mode: string; venue: string }>>([]);
  const [error, setError] = useState(false);
  useEffect(() => { let active = true; void clientApi.accountsList().then((out) => {
    if (active) setOptions(out.accounts.map(({ profile }) => ({ id: profile.id, mode: profile.mode, venue: profile.venue })));
  }).catch(() => { if (active) setError(true); }); return () => { active = false; }; }, []);
  return <><label className={styles.field}>{t("copy.components_workflows_WorkflowInspector.001")}<ChoiceSelect aria-label={t("copy.components_workflows_WorkflowInspector.002")} value={value} disabled={disabled} onValueChange={onChange}>
    {!options.some((item) => item.id === value) && <option value={value}>{value || t("copy.components_workflows_WorkflowInspector.003")}</option>}
    {options.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.mode.toUpperCase()}</option>)}
  </ChoiceSelect></label>{error && <p role="alert" className={styles.error}>{t("copy.components_workflows_WorkflowInspector.004")}</p>}</>;
}

export function NodeInspector({ node, raw, writable, onRaw, onMetadata, onClose, related, onSelectRelated, onDuplicate, onSave, onReset, onRuns, dirty, busy, nodes = [], defaults, displayTitle }: {
  node: WorkflowNode; raw: string; writable: boolean; onRaw: (value: string) => void;
  onMetadata: (value: { title?: string; description?: string }) => void; onClose: () => void;
  related: WorkflowNode[]; onSelectRelated: (id: string) => void;
  onDuplicate?: (node: WorkflowNode) => void; onAskAgent?: (request?: string) => void;
  onSave?: () => void; onReset?: () => void; onRuns?: () => void;
  dirty?: boolean; busy?: boolean; changeCount?: number;
  nodes?: WorkflowNode[]; defaults?: AgentDefaults; displayTitle?: string;
}) {
  const t = useWorkflowText();
  const wx = useTranslations("workflowExperience");
  const guide = cardGuide(node, t);
  const canEdit = writable && node.editable;
  let value: unknown = null;
  let parseError = "";
  if (!node.binding.file) { try { value = JSON.parse(raw); } catch { parseError = t("copy.components_workflows_WorkflowInspector.005"); } }
  const errors = parseError ? [parseError] : configurationErrors(node, value, t);
  const isObject = value !== null && typeof value === "object" && !Array.isArray(value);
  const canDuplicate = (node.kind === "script" || node.kind === "agent") && !!node.binding.file && node.id !== "agent:tuner" || editableSource(node);
  const source = node.binding.file || (node.binding.path ? `strategy.yml · ${node.binding.path.join(".") || "title"}` : node.resource);
  const code = <label className={styles.field}>{node.id === "agent:tuner" ? t("copy.simpleReview.planLabel") : node.kind === "agent" ? t("copy.components_workflows_WorkflowInspector.006") : "Python"}<textarea className={node.kind === "agent" ? styles.promptEditor : styles.codeEditor} aria-label={node.id === "agent:tuner" ? t("copy.simpleReview.planLabel") : t("copy.components_workflows_WorkflowInspector.007")} spellCheck={false} rows={node.kind === "agent" ? 7 : 14} value={raw} readOnly={!canEdit} onChange={(event) => onRaw(event.target.value)} /></label>;
  return <section className={ui.inspector} aria-label={t("copy.components_workflows_WorkflowInspector.008")} data-testid="workflow-inspector" onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") { event.preventDefault(); event.stopPropagation(); if (dirty && !busy) onSave?.(); } }}>
    <header className={ui.inspectorHeader}><h3>{displayTitle || cardTitle(node, t)}</h3><WorkflowHelp label={t("copy.components_workflows_WorkflowInspector.009")}><p>{cardPurpose(node, t)}</p><p>{guide.how}</p><p>{guide.impact}</p></WorkflowHelp><button className={ui.iconButton} type="button" onClick={onClose} aria-label={t("copy.components_workflows_WorkflowInspector.010")}><NeryaGlyph name="x" size={18} /></button></header>
    <div className={ui.inspectorBody}>
      {node.id === "evidence:review" && <p className={ui.inspectorDescription}>{t("copy.simpleReview.scriptHow")}</p>}
      {node.binding.file ? node.kind === "agent" ? code : <>
        <ScriptExplanation source={raw} fallback={node.description} />
        <details className={ui.disclosure}>
          <summary>{wx("code")}</summary>
          <p className={ui.muted}>{node.binding.file}</p>
          <WorkflowCodeEditor value={raw} onChange={onRaw} readOnly={!canEdit} label={t("copy.components_workflows_WorkflowInspector.011")} />
        </details>
      </> : !node.editable ? <p className={ui.inspectorDescription}>{cardFacts(node, t)}</p> : node.kind === "account" && typeof value === "string" && node.binding.path?.[0] !== "wallet_id" ? <AccountChoice value={value} disabled={!canEdit} onChange={(next) => onRaw(JSON.stringify(next))} /> : typeof value === "string" ? <label className={styles.field}>{node.kind === "source" ? t("copy.components_workflows_WorkflowInspector.012") : t("copy.components_workflows_WorkflowInspector.013")}<input value={value} disabled={!canEdit} onChange={(event) => onRaw(JSON.stringify(event.target.value))} /></label> : isObject && node.id === "agent:runtime" ? <WorkflowAgentSettings config={asObject(value)} nodes={nodes} defaults={defaults} disabled={!canEdit} onChange={(next) => onRaw(JSON.stringify(next, null, 2))} /> : isObject ? <CommonSettings node={node} config={asObject(value)} markets={nodes.filter((n) => n.binding.path?.[0] === "markets").map((n) => String(n.config))} disabled={!canEdit} onChange={(next) => onRaw(JSON.stringify(next, null, 2))} /> : null}
      {errors.length > 0 && <p role="alert" className={styles.error}>{errors.join("\n")}</p>}
      {node.href && <div className={ui.inlineActions}><Link href={node.href}>{node.kind === "account" ? t("copy.components_workflows_WorkflowInspector.014") : t("copy.components_workflows_WorkflowInspector.015")} <NeryaGlyph name="arrowUpRight" size={16} /></Link></div>}
      <details className={ui.disclosure} open={!node.editable}><summary>{t("copy.components_workflows_WorkflowInspector.016")}</summary><div className={styles.settingsStack}>
        {node.kind !== "strategy" && <label className={styles.field}>{t("copy.components_workflows_WorkflowInspector.017")}<input value={cardTitle(node, t)} disabled={!writable} maxLength={200} onChange={(event) => onMetadata({ title: event.target.value })} /></label>}
        <label className={styles.field}>{t("copy.components_workflows_WorkflowInspector.018")}<textarea rows={2} value={node.description || ""} disabled={!writable} maxLength={2000} onChange={(event) => onMetadata({ description: event.target.value })} /></label>
      </div></details>
      {!node.binding.file && node.editable && <details className={ui.disclosure} open={!!parseError}><summary>{t("copy.components_workflows_WorkflowInspector.019")}</summary><div className={styles.settingsStack}>
        {!parseError && <ConfigTree value={value} disabled={!canEdit} allowAdd={node.kind !== "strategy" && node.id !== "agent:runtime" && node.id !== "proposal:tuning"} lockedKeys={node.kind === "validation" ? ["require_operator_approval"] : []} onChange={(next) => onRaw(JSON.stringify(next, null, 2))} />}
        <details className={ui.disclosure} open={!!parseError}><summary>{t("copy.components_workflows_WorkflowInspector.020")}</summary><textarea className={styles.codeEditor} aria-label={t("copy.components_workflows_WorkflowInspector.021")} spellCheck={false} rows={12} value={raw} readOnly={!canEdit} onChange={(event) => onRaw(event.target.value)} /></details><p className={styles.resourcePath}>{source}</p>
      </div></details>}
      {(() => {
        const steps = related.filter((item) => item.kind === "scheduler" || item.kind === "script" || item.kind === "agent");
        return steps.length > 0 && <details className={ui.disclosure}><summary>{t("copy.components_workflows_WorkflowInspector.022")} · {steps.length}</summary>{steps.map((item) => <button key={item.id} className={ui.resource} type="button" onClick={() => onSelectRelated(item.id)}><WorkflowIcon kind={item.kind} size={15} /><span className={ui.resourceContent}><strong>{cardTitle(item, t)}</strong></span><NeryaGlyph name="chevronRight" size={16} /></button>)}</details>;
      })()}
      <div className={ui.inlineActions}>{onRuns && <button type="button" onClick={onRuns}>{t("copy.components_workflows_WorkflowInspector.023")}</button>}{canDuplicate && canEdit && onDuplicate && <button type="button" onClick={() => onDuplicate(node)}>{t("copy.components_workflows_WorkflowInspector.024")}</button>}{onReset && <button type="button" disabled={busy} onClick={onReset}>{t("copy.components_workflows_WorkflowInspector.025")}</button>}</div>
    </div>
  </section>;
}

"use client";

import { ChoiceSelect } from "../ChoiceSelect";

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
  return <><label className={styles.field}>{t("使用账户", "Account")}<ChoiceSelect aria-label={t("使用哪个账户", "Account to use")} value={value} disabled={disabled} onValueChange={onChange}>
    {!options.some((item) => item.id === value) && <option value={value}>{value || t("选择账户", "Choose an account")}</option>}
    {options.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.mode.toUpperCase()}</option>)}
  </ChoiceSelect></label>{error && <p role="alert" className={styles.error}>{t("账户列表未加载，原绑定已保留。", "Account list unavailable. Existing binding preserved.")}</p>}</>;
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
  const guide = cardGuide(node, t);
  const canEdit = writable && node.editable;
  let value: unknown = null;
  let parseError = "";
  if (!node.binding.file) { try { value = JSON.parse(raw); } catch { parseError = t("配置格式错误，请修正 JSON。", "Invalid configuration. Fix the JSON."); } }
  const errors = parseError ? [parseError] : configurationErrors(node, value, t);
  const isObject = value !== null && typeof value === "object" && !Array.isArray(value);
  const canDuplicate = node.kind === "script" || node.kind === "agent" && !!node.binding.file || editableSource(node);
  const source = node.binding.file || (node.binding.path ? `strategy.yml · ${node.binding.path.join(".") || "title"}` : node.resource);
  const code = <label className={styles.field}>{node.kind === "agent" ? t("任务指令", "Instructions") : "Python"}<textarea className={node.kind === "agent" ? styles.promptEditor : styles.codeEditor} aria-label={t("编辑文件内容", "Edit file content")} spellCheck={false} rows={node.kind === "agent" ? 7 : 14} value={raw} readOnly={!canEdit} onChange={(event) => onRaw(event.target.value)} /></label>;
  return <section className={ui.inspector} aria-label={t("节点详情编辑器", "Node details editor")} data-testid="workflow-inspector" onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") { event.preventDefault(); event.stopPropagation(); if (dirty && !busy) onSave?.(); } }}>
    <header className={ui.inspectorHeader}><h3>{displayTitle || cardTitle(node, t)}</h3><WorkflowHelp label={t("卡片帮助", "Card help")}><p>{cardPurpose(node, t)}</p><p>{guide.how}</p><p>{guide.impact}</p></WorkflowHelp><button className={ui.iconButton} type="button" onClick={onClose} aria-label={t("关闭详情", "Close details")}>×</button></header>
    <div className={ui.inspectorBody}>
      {node.binding.file ? node.kind === "agent" ? code : <>
        <p className={ui.muted}>{node.binding.file}</p>
        {related.filter(editableSource).map((item) => <button key={item.id} className={ui.resource} type="button" onClick={() => onSelectRelated(item.id)}><WorkflowIcon kind="source" size={16} /><span className={ui.resourceContent}><strong>{cardTitle(item, t)}</strong><small>{t("编辑输入参数", "Edit input parameters")}</small></span><span aria-hidden="true">›</span></button>)}
        <WorkflowCodeEditor value={raw} onChange={onRaw} readOnly={!canEdit} label={t("编辑 Python 脚本", "Edit Python script")} />
      </> : !node.editable ? <p className={ui.inspectorDescription}>{cardFacts(node, t)}</p> : node.kind === "account" && typeof value === "string" && node.binding.path?.[0] !== "wallet_id" ? <AccountChoice value={value} disabled={!canEdit} onChange={(next) => onRaw(JSON.stringify(next))} /> : typeof value === "string" ? <label className={styles.field}>{node.kind === "source" ? t("市场标识", "Market identifier") : t("绑定标识", "Binding")}<input value={value} disabled={!canEdit} onChange={(event) => onRaw(JSON.stringify(event.target.value))} /></label> : isObject && node.id === "agent:runtime" ? <WorkflowAgentSettings config={asObject(value)} nodes={nodes} defaults={defaults} disabled={!canEdit} onChange={(next) => onRaw(JSON.stringify(next, null, 2))} /> : isObject ? <CommonSettings node={node} config={asObject(value)} markets={nodes.filter((n) => n.binding.path?.[0] === "markets").map((n) => String(n.config))} disabled={!canEdit} onChange={(next) => onRaw(JSON.stringify(next, null, 2))} /> : null}
      {errors.length > 0 && <p role="alert" className={styles.error}>{errors.join("\n")}</p>}
      {node.href && <div className={ui.inlineActions}><Link href={node.href}>{node.kind === "account" ? t("管理账户", "Manage account") : t("查看与管理", "Open manager")} ↗</Link></div>}
      <details className={ui.disclosure} open={!node.editable}><summary>{t("名称与备注", "Name & notes")}</summary><div className={styles.settingsStack}>
        {node.kind !== "strategy" && <label className={styles.field}>{t("卡片名称", "Card name")}<input value={cardTitle(node, t)} disabled={!writable} maxLength={200} onChange={(event) => onMetadata({ title: event.target.value })} /></label>}
        <label className={styles.field}>{t("备注（仅展示）", "Notes (display only)")}<textarea rows={2} value={node.description || ""} disabled={!writable} maxLength={2000} onChange={(event) => onMetadata({ description: event.target.value })} /></label>
      </div></details>
      {!node.binding.file && node.editable && <details className={ui.disclosure} open={!!parseError}><summary>{t("高级设置", "Advanced settings")}</summary><div className={styles.settingsStack}>
        {!parseError && <ConfigTree value={value} disabled={!canEdit} allowAdd={node.kind !== "strategy" && node.id !== "agent:runtime" && node.id !== "proposal:tuning"} lockedKeys={node.kind === "validation" ? ["require_operator_approval"] : []} onChange={(next) => onRaw(JSON.stringify(next, null, 2))} />}
        <details className={ui.disclosure} open={!!parseError}><summary>{t("完整配置 JSON", "Full JSON")}</summary><textarea className={styles.codeEditor} aria-label={t("完整配置 JSON", "Complete configuration JSON")} spellCheck={false} rows={12} value={raw} readOnly={!canEdit} onChange={(event) => onRaw(event.target.value)} /></details><p className={styles.resourcePath}>{source}</p>
      </div></details>}
      {related.length > 0 && <details className={ui.disclosure}><summary>{t("关联资源", "Related resources")} · {related.length}</summary>{related.map((item) => <button key={item.id} className={ui.resource} type="button" onClick={() => onSelectRelated(item.id)}><WorkflowIcon kind={item.kind} size={15} /><span className={ui.resourceContent}><strong>{cardTitle(item, t)}</strong></span><span>›</span></button>)}</details>}
      <div className={ui.inlineActions}>{onRuns && <button type="button" onClick={onRuns}>{t("运行记录", "Run history")}</button>}{canDuplicate && canEdit && onDuplicate && <button type="button" onClick={() => onDuplicate(node)}>{t("复制资源", "Duplicate resource")}</button>}{onReset && <button type="button" disabled={busy} onClick={onReset}>{t("还原这张卡片", "Reset this card")}</button>}</div>
    </div>
  </section>;
}

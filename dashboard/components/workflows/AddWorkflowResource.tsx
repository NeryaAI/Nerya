"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { WorkflowAddition, WorkflowNode } from "../../lib/workflowTypes";
import { asObject } from "../../lib/workflowPresentation";
import { CommonSettings, configurationErrors, ConfigTree } from "./WorkflowSettings";
import { AccountChoice } from "./WorkflowInspector";
import { WorkflowIcon, kindName, useWorkflowText } from "./WorkflowCanvas";
import styles from "./WorkflowStudio.module.css";

const sourceDefault = { provider: "runtime.market", capability: "candles", timeframe: "1h", limit: 40, consumers: [] };
export function AddResource({ onAdd, onClose, seed, onDirtyChange }: { onAdd: (resource: WorkflowAddition) => void; onClose: () => void; seed?: WorkflowAddition | null; onDirtyChange?: (dirty: boolean) => void }) {
  const t = useWorkflowText();
  const [kind, setKind] = useState<WorkflowAddition["kind"] | null>(seed?.kind || null);
  const [name, setName] = useState(seed?.name || "");
  const [text, setText] = useState(seed?.content || "def transform(value):\n    return value\n");
  const [config, setConfig] = useState<Record<string, unknown>>(seed?.config ? asObject(seed.config) : sourceDefault);
  const [error, setError] = useState("");
  const changed = !!kind && (!!name.trim() || !!seed || JSON.stringify(config) !== JSON.stringify(sourceDefault) || text !== "def transform(value):\n    return value\n");
  useEffect(() => { onDirtyChange?.(changed); }, [changed, onDirtyChange]);
  const fakeNode: WorkflowNode = { id: "source:new", title: name, kind: "source", subtitle: "", resource: name, position: { x: 0, y: 0 }, config, editable: true, binding: { file: null, path: ["data_sources"] } };
  function choose(next: WorkflowAddition["kind"]) { setKind(next); setName(""); setError(""); setConfig(sourceDefault); setText(next === "agent" ? t("# 任务\n分析提供的数据，明确标注缺失信息。\n\n# 输出\n给出结论、依据和风险，不自动下单。\n", "# Task\nAnalyze the supplied data and identify missing evidence.\n\n# Output\nReturn findings, evidence and risks. Do not place orders.\n") : "def transform(value):\n    return value\n"); }
  return <aside className={styles.inspector} aria-label={t("添加资源", "Add resource")} data-testid="workflow-add-resource"><header className={styles.inspectorHeader}><h3>{seed ? t("复制卡片", "Duplicate card") : t("添加卡片", "Add a card")}</h3><button type="button" onClick={onClose} aria-label={t("关闭", "Close")}>×</button></header>
    {!kind ? <div className={styles.inspectorBody}><p className={styles.inspectorIntro}>{t("这一步需要做什么？", "What should this step do?")}</p>{(["source", "script", "agent", "account"] as const).map((item) => <button className={styles.addChoice} key={item} type="button" onClick={() => choose(item)}><WorkflowIcon kind={item} /><span><strong>{kindName(item, t)}</strong><small>{({ source: t("提供行情、新闻或自定义数据", "Supply market, news or custom data"), script: t("用代码计算和处理数据", "Calculate and process data in code"), agent: t("用任务说明定义分析角色", "Define an analysis role with instructions"), account: t("绑定一个已有账户", "Bind an existing account") })[item]}</small></span><span>→</span></button>)}<Link className={styles.ownerLink} href="/workflows">{t("管理独立调度器", "Manage independent schedules")} ↗</Link></div> : <form className={styles.inspectorBody} onSubmit={(event) => { event.preventDefault(); const trimmed = name.trim(); if (!trimmed) { setError(t("请填写资源名称或选择账户。", "Enter a name or select an account.")); return; } const errors = kind === "source" ? configurationErrors(fakeNode, config, t) : []; if (errors.length) { setError(errors.join("\n")); return; } onAdd({ kind, name: trimmed, ...(kind === "source" ? { config } : kind === "account" ? {} : { content: text }) }); }}>
      {!seed && <button type="button" className={styles.textButton} onClick={() => setKind(null)}>← {t("更换类型", "Choose another type")}</button>}
      {kind === "account" ? <AccountChoice value={name} disabled={false} onChange={setName} /> : <label className={styles.field}>{kind === "script" ? t("脚本文件名", "Script filename") : t("资源标识", "Resource identifier")}<input required maxLength={128} value={name} placeholder={kind === "script" ? "helpers/my_signal.py" : "my_custom_resource"} onChange={(event) => setName(event.target.value)} /></label>}
      {kind === "source" ? <><CommonSettings node={fakeNode} config={config} disabled={false} onChange={setConfig} /><details className={styles.advanced}><summary>{t("自定义参数", "Custom parameters")}</summary><ConfigTree value={config} disabled={false} onChange={(value) => setConfig(asObject(value))} /></details></> : kind === "agent" ? <label className={styles.field}>{t("让 Agent 做什么？", "What should this Agent do?")}<textarea rows={8} value={text} onChange={(event) => setText(event.target.value)} /></label> : kind === "script" ? <details className={styles.advanced}><summary>{t("查看或编辑代码", "View or edit code")}</summary><label className={styles.field}>Python<textarea className={styles.codeEditor} rows={14} value={text} onChange={(event) => setText(event.target.value)} /></label></details> : null}
      <p className={styles.helper}>{t("先加入当前修改，再统一保存提案。新卡片不会自动运行；脚本与 Agent 需要实际调用。", "Stage this resource, then save the proposal. New cards do not run automatically; scripts and Agents need executable calls.")}</p>
      {error && <p role="alert" className={styles.error}>{error}</p>}<button className={styles.primaryButton} type="submit">{t("加入当前变更", "Stage resource")}</button>
    </form>}
  </aside>;
}

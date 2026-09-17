"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { EvolutionTimelineEnvelope, EvolutionTimelineItem } from "../../lib/evolutionTypes";
import type { WorkflowGraph, WorkflowKind, WorkflowNode, WorkflowSummary, WorkflowView } from "../../lib/workflowTypes";
import { workflowApi } from "../../lib/workflowApi";
import { invalidateReadCache } from "../../lib/clientApi";
import { stateLabel } from "../../lib/workflowPresentation";
import { WorkflowCanvas, WorkflowIcon, useWorkflowText } from "./WorkflowCanvas";
import { StrategyWorkflowPanel, confirmDiscard } from "./StrategyWorkflowPanel";
import styles from "./WorkflowStudio.module.css";

function traceGraph(item: EvolutionTimelineItem | null, t: (zh: string, en: string) => string): WorkflowGraph {
  const sections = item?.process?.sections || [];
  if (sections.length) {
    const nodes: WorkflowNode[] = sections.map((section, index) => {
      const kind: WorkflowKind = /prompt|agent|reflect/.test(section.id) ? "agent" : /valid/.test(section.id) ? "validation" : /output|document/.test(section.id) ? "proposal" : /change|apply/.test(section.id) ? "apply" : "evidence";
      return { id: `trace:${section.id}`, kind, title: section.title, subtitle: section.summary || `${section.artifacts.length} ${t("份证据", "artifacts")}`, resource: section.id,
        position: { x: (index % 4) * 320 + 30, y: Math.floor(index / 4) * 230 + 80 },
        binding: { file: null, path: null }, editable: false, config: section,
        status: section.artifacts.length ? t("有记录", "Recorded") : t("无可用证据", "No available evidence") };
    });
    return { id: `trace:${item?.id}`, nodes, edges: nodes.slice(1).map((node, index) => ({ id: `trace-edge:${index}`, source: nodes[index].id, target: node.id, relation: "record_order", origin: "manifest", label: t("记录顺序", "Record order") })) };
  }
  const stages: Array<[WorkflowKind, string, string, string]> = [
    ["evidence", "signal", "信号与运行证据", "Signals & run evidence"], ["agent", "reflection", "Agent 复盘", "Agent reflection"],
    ["proposal", "proposal", "变更候选", "Change candidate"], ["validation", "validation", "验证与回放", "Validation & replay"],
    ["approval", "approval", "人工审批", "Operator approval"], ["apply", "outcome", "应用与回滚", "Apply & rollback"],
    ["observation", "asset", "观察与知识沉淀", "Observe & learn"],
  ];
  const nodes: WorkflowNode[] = stages.map(([kind, stage, zh, en], index) => ({ id: `stage:${stage}`, kind, title: t(zh, en), resource: stage,
    subtitle: item?.stage === stage ? item.summary || item.title : t("阶段定义 · 无本次执行证据", "Process stage · no execution evidence"),
    status: item?.stage === stage ? item.status : t("未观测", "Not observed"),
    position: { x: (index < 4 ? index : 6 - index) * 320 + 30, y: index < 4 ? 70 : 320 },
    config: item?.stage === stage ? item : { description: t("此卡片表示流程阶段，不代表已运行或已通过。", "This card describes a process stage; it does not claim execution or success.") },
    editable: false, binding: { file: null, path: null },
  }));
  return { id: `evolution:${item?.id || "definition"}`, nodes, edges: nodes.slice(1).map((node, index) => ({ id: `stage-edge:${index}`, source: nodes[index].id, target: node.id, origin: "manifest", relation: "process", label: t("流程定义", "Process definition") })) };
}

export function EvolutionWorkflow({ envelope, onOpenRecord, onOpenSettings, onDirtyChange }: {
  envelope: EvolutionTimelineEnvelope | null; onOpenRecord: (id: string) => void; onOpenSettings: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const t = useWorkflowText();
  const [mode, setMode] = useState<"strategy" | "records">("strategy");
  const [rows, setRows] = useState<WorkflowSummary[]>([]);
  const [selectedKey, setSelectedKey] = useState("");
  const [recordId, setRecordId] = useState("");
  const [node, setNode] = useState<WorkflowNode | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [dirty, setDirty] = useState(false);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  function saved(value: WorkflowView) {
    const key = `${value.strategy_id}:${value.source.proposal_id || "published"}`;
    const previous = rows.find((row) => row.key === selectedKey);
    if (previous) setRows((current) => [...current.filter((row) => row.key !== key), { ...previous, key, proposal_id: value.source.proposal_id, state: value.source.state, title: String(value.manifest.title || value.strategy_id) }]);
    setSelectedKey(key); setDirty(false);
  }
  useEffect(() => {
    let active = true;
    setLoading(true); setError("");
    void workflowApi.list().then((result) => {
      if (!active) return;
      const valid = result.workflows.filter((item) => !item.error);
      setRows(valid);
      setSelectedKey((current) => valid.some((item) => item.key === current) ? current : valid[0]?.key || "");
    }).catch((reason) => { if (active) setError(String(reason)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [reload]);
  const selected = rows.find((row) => row.key === selectedKey);
  const items = envelope?.timeline || [];
  const record = items.find((item) => item.id === recordId) || items.find((item) => (item.process?.sections.length || 0) > 0) || items[0] || null;
  const graph = useMemo(() => traceGraph(record, t), [record, t]);
  async function switchMode(next: "strategy" | "records") {
    if (mode === next) return;
    if (dirty && !await confirmDiscard(t("丢弃未保存的复盘配置修改？", "Discard unsaved review configuration edits?"))) return;
    setDirty(false); setMode(next); setNode(null);
  }
  return <section className={styles.evolution}>
    <div className={styles.evolutionTop}><div className={styles.tabs} role="tablist" aria-label={t("复盘工作流范围", "Review workflow scope")}><button role="tab" aria-selected={mode === "strategy"} onClick={() => void switchMode("strategy")}><WorkflowIcon kind="observation" size={16} />{t("策略复盘工作流", "Strategy review workflows")}</button><button role="tab" aria-selected={mode === "records"} onClick={() => void switchMode("records")}><WorkflowIcon kind="evidence" size={16} />{t("系统进化记录", "System evolution records")}</button></div><span className={styles.toolbarSpacer} /><button className={styles.secondaryButton} onClick={onOpenSettings}>{t("系统复盘配置", "System reflection settings")}</button></div>
    {error && <div role="alert" className={styles.errorBanner}><span>{t("策略暂未加载成功，已有内容不会丢失。", "Strategies could not be loaded. Existing content is preserved.")}</span><button type="button" disabled={loading} onClick={() => { invalidateReadCache(); setReload((value) => value + 1); }}>{t("重新加载策略", "Retry loading strategies")}</button><details><summary>{t("错误详情", "Error details")}</summary>{error}</details></div>}
    {mode === "strategy" ? <>{loading ? <div className={styles.loading} role="status" data-testid="evolution-workflows-loading">{t("正在加载策略与复盘卡片…", "Loading strategies and review cards…")}</div> : error ? null : rows.length ? <><label className={styles.field}>{t("选择策略与版本", "Choose a strategy and version")}<ChoiceSelect value={selectedKey} onValueChange={async (choiceValue) => { const value = choiceValue; if (!dirty || await confirmDiscard(t("丢弃未保存修改并切换策略？", "Discard edits and switch strategies?"))) { setDirty(false); setSelectedKey(value); } }}>{rows.map((row) => <option key={row.key} value={row.key}>{row.title} · {stateLabel(row.state, t)} · {row.proposal_id || t("当前版本", "current")}</option>)}</ChoiceSelect></label><div style={{ height: 16 }} />{selected && <StrategyWorkflowPanel key={selected.key} strategyId={selected.strategy_id} proposalId={selected.proposal_id} defaultView="evolution" onDirtyChange={setDirty} onSaved={saved} />}</> : <div className={styles.empty}>{t("还没有可展示的策略复盘配置。先创建一个策略工作流。", "No strategy review configuration is available yet. Create a strategy workflow first.")} <Link className={styles.ownerLink} href="/strategies">{t("策略工作流", "Strategy workflows")} ↗</Link></div>}</> : <>
      <div className={styles.evolutionTop}><ChoiceSelect aria-label={t("选择进化记录", "Choose evolution record")} value={record?.id || ""} onValueChange={(choiceValue) => { setRecordId(choiceValue); setNode(null); }}>{items.length ? items.map((item) => <option key={item.id} value={item.id}>{item.ts} · {item.title} · {item.status}</option>) : <option value="">{t("暂无执行记录 · 流程定义", "No execution records · process definition")}</option>}</ChoiceSelect><span className={styles.toolbarSpacer} />{record && <button className={styles.secondaryButton} onClick={() => onOpenRecord(record.id)}>{t("查看完整证据与审计", "Open full evidence & audit")} ↗</button>}</div>
      <div className={styles.notice}>{t("实线用于代码关系；这里的虚线展示记录顺序或流程定义。只展示服务端已有的证据，不把未执行的阶段标为成功。历史证据不可改写。", "Dashed edges here describe record order or process definitions. Only server-recorded evidence is shown; unobserved stages are not marked successful. Historical evidence cannot be overwritten.")}</div><div style={{ height: 12 }} />
      <div className={`${styles.studio} ${styles.canvasLayout} ${node ? styles.withInspector : ""}`}><WorkflowCanvas graph={graph} selectedId={node?.id} onSelect={setNode} />{node && <aside className={styles.inspector}><header className={styles.inspectorHeader}><WorkflowIcon kind={node.kind} /><h3>{node.title}</h3><button aria-label={t("关闭详情", "Close details")} onClick={() => setNode(null)}>×</button></header><div className={styles.inspectorBody}><p className={styles.notice}>{node.subtitle}</p><span className={styles.stateTag}>{node.status}</span><pre className={styles.resourcePath} style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(node.config, null, 2)}</pre>{record && <button className={styles.secondaryButton} onClick={() => onOpenRecord(record.id)}>{t("打开原始记录", "Open original record")}</button>}</div></aside>}</div>
    </>}
  </section>;
}

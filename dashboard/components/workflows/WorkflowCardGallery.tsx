"use client";

import { useState } from "react";
import type { WorkflowGraph, WorkflowKind, WorkflowNode } from "../../lib/workflowTypes";
import { cardTitle, cardFacts } from "../../lib/workflowPresentation";
import { WorkflowIcon, kindName, useWorkflowText } from "./WorkflowCanvas";
import ui from "./WorkflowNative.module.css";

export function WorkflowCardGallery({ graph, selectedId, onSelect }: { graph: WorkflowGraph; selectedId: string | null; onSelect: (node: WorkflowNode) => void }) {
  const t = useWorkflowText();
  const [query, setQuery] = useState("");
  const nodes = graph.nodes.filter((node) => `${node.title} ${node.resource} ${cardTitle(node, t)} ${kindName(node.kind, t)}`.toLowerCase().includes(query.trim().toLowerCase()));
  const groups: Array<{ title: string; kinds: WorkflowKind[] }> = graph.id.endsWith(":evolution") ? [
    { title: t("复盘与改进", "Review & improve"), kinds: ["evidence", "scheduler", "agent", "proposal"] },
    { title: t("验证与应用", "Validate & apply"), kinds: ["validation", "approval", "apply", "observation"] },
  ] : [
    { title: t("执行", "Execution"), kinds: ["scheduler", "script", "agent"] },
    { title: t("数据与边界", "Data & safeguards"), kinds: ["source", "risk", "account", "strategy"] },
  ];
  return <div className={ui.resourceList} data-testid="workflow-card-gallery">
    {(graph.nodes.length > 12 || query) && <input className={ui.sidebarSearch} type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("查找资源…", "Find a resource…")} aria-label={t("搜索卡片", "Search cards")} />}
    {groups.map((group) => { const members = nodes.filter((node) => group.kinds.includes(node.kind)); return members.length > 0 && <section className={ui.group} key={group.title}><h3>{group.title}</h3>{members.map((node) => <button key={node.id} type="button" className={ui.resource} data-workflow-node={node.id} data-kind={node.kind} aria-pressed={selectedId === node.id} aria-label={`${t("编辑详情", "View details")}: ${cardTitle(node, t)}`} onClick={() => onSelect(node)}>
      <span className={ui.resourceIcon} title={kindName(node.kind, t)}><WorkflowIcon kind={node.kind} size={18} /></span><span className={ui.resourceContent}><strong>{node.kind === "strategy" ? t("目标与说明", "Objective & notes") : cardTitle(node, t)}</strong>{node.kind !== "strategy" && cardFacts(node, t) !== cardTitle(node, t) && <small>{cardFacts(node, t)}</small>}</span><span className={ui.resourceArrow} aria-hidden="true">›</span>
    </button>)}</section>; })}
    {!nodes.length && <p className={ui.muted}>{t("没有匹配的资源", "No matching resources")}</p>}
  </div>;
}

"use client";
import { Icon as NeryaGlyph } from "../icons";

import { useState } from "react";
import type { WorkflowGraph, WorkflowKind, WorkflowNode } from "../../lib/workflowTypes";
import { cardTitle, cardFacts, cardPurpose } from "../../lib/workflowPresentation";
import { WorkflowIcon, kindName, useWorkflowText } from "./WorkflowCanvas";
import ui from "./WorkflowNative.module.css";
import { RoleAvatar } from "../RoleAvatar";
import { resolveWorkflowRole } from "../../lib/roleAvatars";

export function WorkflowCardGallery({ graph, selectedId, onSelect }: { graph: WorkflowGraph; selectedId: string | null; onSelect: (node: WorkflowNode) => void }) {
  const t = useWorkflowText();
  const [query, setQuery] = useState("");
  const nodes = graph.nodes.filter((node) => `${node.title} ${node.resource} ${cardTitle(node, t)} ${kindName(node.kind, t)}`.toLowerCase().includes(query.trim().toLowerCase()));
  const groups: Array<{ title: string; kinds: WorkflowKind[] }> = graph.id === "evolution" || graph.id.endsWith(":evolution") ? [
    { title: t("copy.components_workflows_WorkflowCardGallery.001"), kinds: ["script", "evidence", "scheduler", "agent", "proposal"] },
    { title: t("copy.components_workflows_WorkflowCardGallery.002"), kinds: ["validation", "approval", "apply", "observation"] },
  ] : [
    { title: t("copy.components_workflows_WorkflowCardGallery.003"), kinds: ["scheduler", "script", "agent"] },
    { title: t("copy.components_workflows_WorkflowCardGallery.004"), kinds: ["source", "risk", "account", "strategy"] },
  ];
  return <div className={ui.resourceList} data-testid="workflow-card-gallery">
    {(graph.nodes.length > 12 || query) && <input className={ui.sidebarSearch} type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("copy.components_workflows_WorkflowCardGallery.005")} aria-label={t("copy.components_workflows_WorkflowCardGallery.006")} />}
    {groups.map((group) => { const members = nodes.filter((node) => group.kinds.includes(node.kind)); return members.length > 0 && <section className={ui.group} key={group.title}><h3>{group.title}</h3>{members.map((node) => <button key={node.id} type="button" className={ui.resource} data-workflow-node={node.id} data-kind={node.kind} aria-pressed={selectedId === node.id} aria-label={`${t("copy.components_workflows_WorkflowCardGallery.007")}: ${cardTitle(node, t)}`} onClick={() => onSelect(node)}>
      <span className={ui.resourceIcon} title={kindName(node.kind, t)}>{node.kind === "agent" ? <RoleAvatar role={resolveWorkflowRole(node)} size={28} alt="" /> : <WorkflowIcon kind={node.kind} size={18} />}</span><span className={ui.resourceContent}><strong>{node.kind === "strategy" ? t("copy.components_workflows_WorkflowCardGallery.008") : cardTitle(node, t)}</strong>{node.kind !== "strategy" && cardFacts(node, t) !== cardTitle(node, t) && <small>{node.kind === "script" ? cardPurpose(node, t) : cardFacts(node, t)}</small>}</span><NeryaGlyph name="chevronRight" size={16} className={ui.resourceArrow} />
    </button>)}</section>; })}
    {!nodes.length && <p className={ui.muted}>{t("copy.components_workflows_WorkflowCardGallery.009")}</p>}
  </div>;
}

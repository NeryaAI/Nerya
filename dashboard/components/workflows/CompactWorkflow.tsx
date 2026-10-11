"use client";

import { useTranslations } from "next-intl";
import type { WorkflowGraph, WorkflowNode } from "../../lib/workflowTypes";
import { cardTitle, cardPurpose, cardFacts } from "../../lib/workflowPresentation";
import { useWorkflowText, WorkflowIcon, kindName } from "./WorkflowCanvas";
import { RoleAvatar } from "../RoleAvatar";
import { resolveWorkflowRole } from "../../lib/roleAvatars";
import styles from "./CompactWorkflow.module.css";

export function CompactWorkflow({ graph, nodes, selectedId, onSelect }: {
  graph: WorkflowGraph; nodes: WorkflowNode[]; selectedId?: string | null; onSelect: (node: WorkflowNode) => void;
}) {
  const t = useWorkflowText(), w = useTranslations("workflowUpgrade");
  return <section className={styles.flow} data-testid="compact-workflow" aria-label={w("compactFlow")}>
    <p className={styles.note}>{w("staticDefinition")}</p>
    <ol className={styles.steps}>{nodes.map((node, index) => {
      const link = index ? graph.edges.find(edge => edge.source === nodes[index - 1].id && edge.target === node.id) : undefined;
      return <li key={node.id} data-workflow-node={node.id}>
        {link && <div className={styles.connector} aria-hidden="true"><span>↓</span></div>}
        <button type="button" className={styles.step} aria-pressed={selectedId === node.id} onClick={() => onSelect(node)}>
          <span className={styles.number}>{String(index + 1).padStart(2, "0")}</span>
          <span className={styles.icon}>{node.kind === "agent" ? <RoleAvatar role={resolveWorkflowRole(node)} size={30} alt="" /> : <WorkflowIcon kind={node.kind} size={24} />}</span>
          <span className={styles.body}><span className={styles.top}><strong>{cardTitle(node, t)}</strong><small>{kindName(node.kind, t)}</small></span><span className={styles.purpose}>{cardPurpose(node, t)}</span><span className={styles.facts}>{cardFacts(node, t)}</span></span>
          <span className={styles.open} aria-hidden="true">›</span>
        </button>
      </li>;
    })}</ol>
  </section>;
}

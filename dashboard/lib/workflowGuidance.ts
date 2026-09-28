import type { WorkflowKind, WorkflowNode } from "./workflowTypes";
import { asObject, type WorkflowText } from "./workflowPresentation";

// Guidance is about resource contracts, never strategy-specific code generation.
const GUIDES: Record<WorkflowKind, { how: string; impact: string }> = {
  strategy: { how: "copy.workflowGuidance.strategy.how", impact: "copy.workflowGuidance.strategy.impact" },
  source: { how: "copy.workflowGuidance.source.how", impact: "copy.workflowGuidance.source.impact" },
  script: { how: "copy.workflowGuidance.script.how", impact: "copy.workflowGuidance.script.impact" },
  agent: { how: "copy.workflowGuidance.agent.how", impact: "copy.workflowGuidance.agent.impact" },
  scheduler: { how: "copy.workflowGuidance.scheduler.how", impact: "copy.workflowGuidance.scheduler.impact" },
  account: { how: "copy.workflowGuidance.account.how", impact: "copy.workflowGuidance.account.impact" },
  risk: { how: "copy.workflowGuidance.risk.how", impact: "copy.workflowGuidance.risk.impact" },
  evidence: { how: "copy.workflowGuidance.evidence.how", impact: "copy.workflowGuidance.evidence.impact" },
  proposal: { how: "copy.workflowGuidance.proposal.how", impact: "copy.workflowGuidance.proposal.impact" },
  validation: { how: "copy.workflowGuidance.validation.how", impact: "copy.workflowGuidance.validation.impact" },
  approval: { how: "copy.workflowGuidance.approval.how", impact: "copy.workflowGuidance.approval.impact" },
  apply: { how: "copy.workflowGuidance.apply.how", impact: "copy.workflowGuidance.apply.impact" },
  observation: { how: "copy.workflowGuidance.observation.how", impact: "copy.workflowGuidance.observation.impact" },
};
export function cardGuide(node: WorkflowNode, t: WorkflowText) {
  if (node.id === "evidence:review") return { how: t("copy.simpleReview.scriptHow"), impact: t("copy.workflowGuidance.evidence.impact") };
  const guide = GUIDES[node.kind];
  return { how: t(guide.how), impact: t(guide.impact) };
}
export function extraSourceFields(config: Record<string, unknown>) {
  const standard = new Set(["id", "name", "title", "description", "provider", "capability", "timeframe", "timeframes", "market", "markets", "sources", "source", "limit", "consumers", "account", "connection_id", "endpoint", "venue"]);
  return Object.fromEntries(Object.entries(config).filter(([key]) => !standard.has(key)));
}
export function editableSource(node: WorkflowNode) {
  return node.kind === "source" && node.binding.path?.[0] === "data_sources" && Object.keys(asObject(node.config)).length > 0;
}

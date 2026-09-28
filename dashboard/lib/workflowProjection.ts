import type { WorkflowGraph, WorkflowMetadata, WorkflowNode } from "./workflowTypes";
import type { WorkflowText } from "./workflowPresentation";
export type ResourceGroup = { id: string; title: string; members: WorkflowNode[] };
export type WorkflowProjection = { graph: WorkflowGraph; groups: ResourceGroup[]; supporting: ResourceGroup[]; aliases: Map<string, string> };

/** Keep triggers and executable steps together; supporting configuration stays below the canvas. */
export function compactWorkflow(graph: WorkflowGraph, t: WorkflowText, _metadata?: WorkflowMetadata): WorkflowProjection {
  const aliases = new Map<string, string>();
  if (graph.id.endsWith(":evolution") || graph.id === "evolution") {
    // Compact only the built-in review template, never a historical/custom trace.
    // Keep resource IDs, bindings and authored metadata intact for the inspector.
    if (!graph.nodes.some((node) => node.id === "evidence:review") || !graph.nodes.some((node) => node.id === "agent:tuner")) return { graph, groups: [], supporting: [], aliases };
    const steps = new Set(["evidence:review", "agent:tuner"]);
    const lifecycleIds = new Set(["scheduler:tuning", "proposal:tuning", "validation:tuning", "approval:operator", "apply:version", "observation:feedback"]);
    if (graph.nodes.some((node) => !steps.has(node.id) && !lifecycleIds.has(node.id))) return { graph, groups: [], supporting: [], aliases };
    const nodes = graph.nodes.filter((node) => steps.has(node.id)).map((node) => node.id === "evidence:review" ? { ...node, kind: "script" as const } : node);
    const settings = graph.nodes.filter((node) => node.id === "scheduler:tuning");
    const lifecycle = graph.nodes.filter((node) => !steps.has(node.id) && node.id !== "scheduler:tuning");
    const supporting: ResourceGroup[] = [];
    if (settings.length) supporting.push({ id: settings[0].id, title: t("copy.simpleReview.schedule"), members: settings });
    if (lifecycle.length) supporting.push({ id: lifecycle[0].id, title: t("copy.simpleReview.proposalSettings"), members: lifecycle });
    return { graph: { ...graph, nodes, edges: graph.edges.filter((edge) => steps.has(edge.source) && steps.has(edge.target)) }, groups: [], supporting, aliases };
  }
  const nodes = graph.nodes.filter((node) => node.kind === "scheduler" || node.kind === "script" || node.kind === "agent");
  const ids = new Set(nodes.map((node) => node.id));
  const supporting: ResourceGroup[] = [];
  for (const [kind, title] of [["strategy", t("copy.lib_workflowProjection.001")], ["risk", t("copy.lib_workflowProjection.003")], ["account", t("copy.lib_workflowProjection.004")], ["source", t("copy.lib_workflowProjection.005")]]) {
    const members = graph.nodes.filter((node) => node.kind === kind);
    if (kind === "strategy") members.push(...graph.nodes.filter((node) => node.kind === "source" && node.binding.path?.[0] !== "data_sources"));
    const visible = kind === "source" ? members.filter((node) => node.binding.path?.[0] === "data_sources") : members;
    if (visible.length) supporting.push({ id: visible[0].id, title, members: visible });
  }
  return { graph: { ...graph, nodes, edges: graph.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target)) }, groups: [], supporting, aliases };
}

import type { WorkflowGraph, WorkflowNode, WorkflowPosition } from "./workflowTypes";

/** Only a real small chain can be rendered as a linear sequence. Branches,
 * cycles, disconnected resources and authored annotations retain the canvas.
 */
export function linearWorkflow(graph: WorkflowGraph): WorkflowNode[] | null {
  if (!graph.nodes.length || graph.nodes.length > 6) return null;
  const nodes = new Map(graph.nodes.map(node => [node.id, node]));
  const outgoing = new Map<string, Set<string>>(), incoming = new Map<string, Set<string>>();
  for (const edge of graph.edges) {
    if (!nodes.has(edge.source) || !nodes.has(edge.target)) continue;
    if (edge.origin === "annotation" || edge.source === edge.target) return null;
    if (!outgoing.has(edge.source)) outgoing.set(edge.source, new Set());
    if (!incoming.has(edge.target)) incoming.set(edge.target, new Set());
    outgoing.get(edge.source)!.add(edge.target);
    incoming.get(edge.target)!.add(edge.source);
  }
  if ([...outgoing.values(), ...incoming.values()].some(edges => edges.size > 1)) return null;
  const roots = graph.nodes.filter(node => !incoming.has(node.id));
  if (roots.length !== 1) return null;
  const ordered: WorkflowNode[] = [], seen = new Set<string>();
  let current: WorkflowNode | undefined = roots[0];
  while (current && !seen.has(current.id)) {
    ordered.push(current); seen.add(current.id);
    const target: string | undefined = outgoing.get(current.id)?.values().next().value;
    current = target ? nodes.get(target) : undefined;
  }
  return ordered.length === graph.nodes.length && !current ? ordered : null;
}

export function validPositions(value: unknown): Record<string, WorkflowPosition> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).filter(([, position]) => {
    if (!position || typeof position !== "object") return false;
    const { x, y } = position as WorkflowPosition;
    return typeof x === "number" && typeof y === "number" && Number.isFinite(x) && Number.isFinite(y) && Math.abs(x) < 100_000 && Math.abs(y) < 100_000;
  }));
}

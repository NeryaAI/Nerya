import type { WorkflowGraph, WorkflowMetadata, WorkflowNode } from "./workflowTypes";
import { asObject, cardTitle, type WorkflowText } from "./workflowPresentation";
import { sourceDimension } from "./workflowSources";
export type ResourceGroup = { id: string; title: string; members: WorkflowNode[] };
export type WorkflowProjection = { graph: WorkflowGraph; groups: ResourceGroup[]; supporting: ResourceGroup[]; aliases: Map<string, string> };

/** Display-only folding. Every input/output relation retains its evidence
 * origin, and all edits still address canonical resource IDs. */
export function compactWorkflow(graph: WorkflowGraph, t: WorkflowText, metadata?: WorkflowMetadata): WorkflowProjection {
  const aliases = new Map<string, string>();
  if (graph.id.endsWith(":evolution") || graph.id === "evolution") return { graph, groups: [], supporting: [], aliases };
  const sourceNodes = graph.nodes.filter((node) => node.kind === "source");
  const definitions = sourceNodes.filter((node) => node.binding.path?.[0] === "data_sources");
  const scopes = sourceNodes.filter((node) => node.binding.path?.[0] !== "data_sources");
  const defaultMarkets = scopes.filter((node) => node.binding.path?.[0] === "markets").map((node) => String(node.config));
  const folded = new Map<string, WorkflowNode[]>();
  for (const node of definitions.length ? definitions : sourceNodes) {
    const config = asObject(node.config);
    const provider = String(config.provider || (node.binding.path?.[0] === "news_sources" ? "runtime.news" : "runtime.market"));
    // Different connections/accounts/endpoints must not silently become one.
    const key = JSON.stringify([provider, config.account ?? null, config.connection_id ?? null, config.venue ?? null, config.endpoint ?? null]);
    const group = folded.get(key) || []; group.push(node); folded.set(key, group);
  }
  // A legacy raw reader may coexist with a different explicit source.
  // Preserve that data edge rather than hiding it in strategy metadata.
  if (definitions.length) for (const scope of scopes) {
    const provider = scope.binding.path?.[0] === "news_sources" ? "runtime.news" : "runtime.market";
    const key = JSON.stringify([provider, null, null, null, null]);
    const matching = folded.get(key);
    if (matching) aliases.set(scope.id, matching[0].id);
    else if (graph.edges.some((edge) => edge.source === scope.id && edge.relation === "data" && edge.origin === "static")) {
      const previous = folded.get(key) || []; previous.push(scope); folded.set(key, previous);
    }
  }
  const groups: ResourceGroup[] = [], nodes: WorkflowNode[] = [];
  for (const members of folded.values()) {
    const first = members[0], provider = String(asObject(first.config).provider || (first.binding.path?.[0] === "news_sources" ? "runtime.news" : "runtime.market"));
    const markets = [...new Set(members.flatMap((node) => typeof node.config === "string" ? node.binding.path?.[0] === "markets" ? [node.config] : [] : sourceDimension(asObject(node.config), "markets", "market", provider === "runtime.news" ? [] : defaultMarkets)))];
    const frames = [...new Set(members.flatMap((node) => sourceDimension(asObject(node.config), "timeframes", "timeframe")))];
    const providerTitle = provider === "runtime.market" ? t("行情数据", "Market data") : provider === "runtime.news" ? t("新闻数据", "News data") : provider;
    const title = members.length === 1 && first.binding.path?.[0] === "data_sources" ? cardTitle(first, t) : providerTitle;
    const summary = [markets.length ? t(`${markets.length} 个品种`, `${markets.length} markets`) : "", frames.length ? t(`${frames.length} 个周期`, `${frames.length} timeframes`) : "", members.length > 1 ? t(`${members.length} 项读取配置`, `${members.length} subscriptions`) : ""].filter(Boolean).join(" · ");
    groups.push({ id: first.id, title, members });
    for (const node of members) aliases.set(node.id, first.id);
    nodes.push({ ...first, title, description: [markets.map((market) => market.includes(":") ? market.split(":").slice(1).join(":") : market).slice(0, 3).join(" · "), markets.length > 3 ? `+${markets.length - 3}` : "", frames.join(" / ")].filter(Boolean).join("  "),
      presentation: { summary: summary || providerTitle }, position: metadata?.nodes[first.id]?.position || { x: 30, y: 200 + (groups.length - 1) * 190 } });
  }
  const supporting: ResourceGroup[] = [];
  for (const [kind, title] of [["strategy", t("策略范围", "Strategy scope")], ["risk", t("风控", "Safeguards")], ["account", t("账户", "Accounts")]]) {
    const members = graph.nodes.filter((node) => node.kind === kind);
    if (kind === "strategy" && definitions.length) members.push(...scopes.filter((scope) => !groups.some((group) => group.members.some((member) => member.id === scope.id))));
    if (members.length) supporting.push({ id: members[0].id, title, members });
  }
  const supported = new Set(supporting.flatMap((group) => group.members.map((node) => node.id)));
  nodes.push(...graph.nodes.filter((node) => node.kind !== "source" && !supported.has(node.id)).map((node) => node.kind === "scheduler" && node.id === "scheduler:trading" && !metadata?.nodes[node.id]?.position ? { ...node, position: { x: 30, y: 10 } } : node));
  const ids = new Set(nodes.map((node) => node.id));
  const edges = graph.edges.map((edge) => ({ ...edge, source: aliases.get(edge.source) || edge.source, target: aliases.get(edge.target) || edge.target })).filter((edge) => edge.source !== edge.target && ids.has(edge.source) && ids.has(edge.target));
  return { graph: { ...graph, id: `${graph.id}:compact`, nodes, edges }, groups, supporting, aliases };
}

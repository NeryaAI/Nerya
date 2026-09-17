"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { CSSProperties, PointerEvent } from "react";
import { useLocale } from "next-intl";
import type { WorkflowEdge, WorkflowGraph, WorkflowKind, WorkflowNode, WorkflowPosition } from "../../lib/workflowTypes";
import styles from "./WorkflowStudio.module.css";
import { cardTitle, cardPurpose, cardFacts } from "../../lib/workflowPresentation";

export function useWorkflowText() {
  const locale = useLocale();
  return useCallback((zh: string, en: string) => locale.toLowerCase().startsWith("zh") ? zh : en, [locale]);
}
const KIND_NAMES: Record<WorkflowKind, [string, string]> = {
  strategy: ["策略", "Strategy"], source: ["数据源", "Data source"], script: ["脚本", "Script"],
  agent: ["Agent", "Agent"], scheduler: ["调度器", "Scheduler"], account: ["账户", "Account"],
  risk: ["风控", "Risk gate"], evidence: ["复盘证据", "Evidence"], proposal: ["变更提案", "Proposal"],
  validation: ["验证", "Validation"], approval: ["人工审批", "Approval"], apply: ["版本应用", "Apply"], observation: ["持续观察", "Observation"],
};
export function kindName(kind: WorkflowKind, t: (zh: string, en: string) => string) {
  return t(...KIND_NAMES[kind]);
}
export function WorkflowIcon({ kind, size = 18 }: { kind: WorkflowKind; size?: number }) {
  const paths: Record<string, string> = {
    strategy: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
    source: "M4 6c0-4 16-4 16 0s-16 4-16 0v12c0 4 16 4 16 0V6 M4 12c0 4 16 4 16 0",
    script: "m8 6-6 6 6 6 M16 6l6 6-6 6 M14 3l-4 18",
    agent: "M5 7h14v13H5z M12 3v4 M9 12h.01 M15 12h.01 M9 16h6 M2 11v5 M22 11v5",
    scheduler: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2",
    account: "M3 5h16v4 M3 5v15h18V9H3 M16 13h5v4h-5z",
    risk: "m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6z m-4 10 3 3 5-6",
    evidence: "M5 3h14v18H5z M8 7h8 M8 11h8 M8 15h5",
    proposal: "M4 4h10v16H4z M17 3v9 M13 7h8 M7 9h4 M7 13h4",
    validation: "m3 12 5 5 12-12 M4 3h5 M3 3v5 M21 16v5h-5",
    approval: "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M2 21v-3a7 7 0 0 1 11-5 M14 18l3 3 5-7",
    apply: "M3 3h18v18H3z M7 3v6h10V3 M7 21v-8h10v8",
    observation: "M3 12a9 9 0 0 1 16-6 M21 3v5h-5 M21 12A9 9 0 0 1 5 18 M3 21v-5h5",
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[kind]} /></svg>;
}
export function nodeSummary(node: WorkflowNode) {
  if (node.description) return node.description;
  const config = node.config;
  if (typeof config === "string") return config;
  if (config && typeof config === "object" && !Array.isArray(config)) {
    const value = config as Record<string, unknown>;
    const useful = ["cron", "every_seconds", "timeframe", "provider", "max_single_order_usd", "enabled", "model", "required"];
    const bits = useful.filter((key) => key in value).slice(0, 2).map((key) => `${key}: ${String(value[key])}`);
    if (bits.length) return bits.join(" · ");
  }
  return node.subtitle;
}
function displayEdges(edges: WorkflowEdge[]): WorkflowEdge[] {
  const result = new Map<string, WorkflowEdge>();
  const priorities: Record<string, number> = { conditional_dispatch: 5, branch_call: 4, parallel_dispatch: 4, aggregate: 3, context: 2 };
  for (const edge of edges) {
    const key = edge.origin === "annotation" ? edge.id : `${edge.source}\u0000${edge.target}`;
    const previous = result.get(key);
    if (previous?.relation === "conditional_dispatch" && edge.relation === "conditional_dispatch") result.set(key, { ...previous, label: [...new Set([previous.label, edge.label])].join(" / ") });
    else if (!previous || (priorities[edge.relation] || 1) > (priorities[previous.relation] || 1)) result.set(key, edge);
  }
  return [...result.values()];
}
const WIDTH = 264;
const HEIGHT = 154;
type Camera = { x: number; y: number; zoom: number };
type Drag = { startX: number; startY: number; origin: WorkflowPosition; nodeId?: string; moved: boolean };

export function WorkflowCanvas({ graph, selectedId, onSelect, onMove, onConnect, onEdgeSelect }: {
  graph: WorkflowGraph; selectedId?: string | null;
  onSelect: (node: WorkflowNode) => void;
  onMove?: (nodeId: string, position: WorkflowPosition) => void;
  onConnect?: (source: string, target: string) => void;
  onEdgeSelect?: (edge: WorkflowEdge) => void;
}) {
  const t = useWorkflowText();
  const viewport = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const [camera, setCamera] = useState<Camera>({ x: 24, y: 24, zoom: .8 });
  const [size, setSize] = useState({ width: 1000, height: 620 });
  const [preview, setPreview] = useState<{ id: string; position: WorkflowPosition } | null>(null);
  const [connecting, setConnecting] = useState<string | null>(null);
  const [nodeFilter, setNodeFilter] = useState("");
  const markerId = useId().replace(/:/g, "");
  const signature = graph.id + graph.nodes.map((n) => n.id).join("|");
  const nodes = graph.nodes.map((node) => preview?.id === node.id ? { ...node, position: preview.position } : node);
  const lookup = new Map(nodes.map((node) => [node.id, node]));
  const bounds = useMemo(() => {
    if (!graph.nodes.length) return { x: 0, y: 0, width: 1000, height: 620 };
    const left = Math.min(...graph.nodes.map((n) => n.position.x));
    const top = Math.min(...graph.nodes.map((n) => n.position.y));
    return { x: left, y: top, width: Math.max(...graph.nodes.map((n) => n.position.x + WIDTH)) - left,
      height: Math.max(...graph.nodes.map((n) => n.position.y + HEIGHT)) - top };
  }, [graph.nodes]);
  const fit = useCallback(() => {
    const zoom = Math.max(.18, Math.min(1.05, (size.width - 64) / bounds.width, (size.height - 94) / bounds.height));
    setCamera({ zoom, x: (size.width - bounds.width * zoom) / 2 - bounds.x * zoom, y: 42 + (size.height - 84 - bounds.height * zoom) / 2 - bounds.y * zoom });
  }, [bounds, size]);
  useEffect(() => {
    const target = viewport.current;
    if (!target) return;
    const observer = new ResizeObserver(([entry]) => setSize({ width: entry.contentRect.width, height: entry.contentRect.height }));
    observer.observe(target);
    return () => observer.disconnect();
  }, []);
  useEffect(() => { fit(); setConnecting(null); setNodeFilter(""); }, [signature, size.width, size.height]); // eslint-disable-line react-hooks/exhaustive-deps

  function zoomBy(factor: number) {
    setCamera((current) => {
      const zoom = Math.max(.18, Math.min(2, current.zoom * factor));
      const ratio = zoom / current.zoom;
      return { zoom, x: size.width / 2 - (size.width / 2 - current.x) * ratio,
        y: size.height / 2 - (size.height / 2 - current.y) * ratio };
    });
  }
  function begin(event: PointerEvent<HTMLDivElement>, nodeId?: string) {
    if (event.button !== 0 || (!nodeId && (event.target as Element).closest("button, a, input, select, [data-workflow-node], [data-edge]"))) return;
    if (nodeId && !onMove) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    const origin = nodeId ? lookup.get(nodeId)!.position : camera;
    drag.current = { nodeId, origin, startX: event.clientX, startY: event.clientY, moved: false };
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    const active = drag.current;
    if (!active) return;
    const dx = event.clientX - active.startX, dy = event.clientY - active.startY;
    if (Math.abs(dx) + Math.abs(dy) > 3) active.moved = true;
    if (active.nodeId) setPreview({ id: active.nodeId, position: { x: active.origin.x + dx / camera.zoom, y: active.origin.y + dy / camera.zoom } });
    else setCamera((current) => ({ ...current, x: active.origin.x + dx, y: active.origin.y + dy }));
  }
  function finishDrag() {
    if (preview && drag.current?.moved) onMove?.(preview.id, { x: Math.round(preview.position.x), y: Math.round(preview.position.y) });
    drag.current = null;
    setPreview(null);
  }
  function connect(id: string) {
    if (connecting && connecting !== id) { onConnect?.(connecting, id); setConnecting(null); }
    else setConnecting((current) => current === id ? null : id);
  }
  function focusNode(id: string) {
    const node = lookup.get(id);
    if (!node) return;
    onSelect(node);
    setCamera((current) => ({ ...current, zoom: Math.max(.8, current.zoom), x: size.width / 2 - (node.position.x + WIDTH / 2) * Math.max(.8, current.zoom), y: size.height / 2 - (node.position.y + HEIGHT / 2) * Math.max(.8, current.zoom) }));
  }
  return <div className={styles.canvasWrap}>
    <div className={styles.canvasToolbar}>
      <ChoiceSelect aria-label={t("定位节点", "Find a node")} value={nodeFilter} onValueChange={(value) => { setNodeFilter(value); focusNode(value); }}>
        <option value="">{t("定位节点…", "Find a node…")}</option>
        {graph.nodes.map((node) => <option key={node.id} value={node.id}>{kindName(node.kind, t)} · {cardTitle(node, t)}</option>)}
      </ChoiceSelect>
      <span className={styles.toolbarSpacer} />
      <button type="button" onClick={() => zoomBy(1 / 1.2)} aria-label={t("缩小", "Zoom out")}>−</button>
      <span className={styles.zoomLabel}>{Math.round(camera.zoom * 100)}%</span>
      <button type="button" onClick={() => zoomBy(1.2)} aria-label={t("放大", "Zoom in")}>+</button>
      <button type="button" onClick={fit}>{t("适应画布", "Fit view")}</button>
    </div>
    <div ref={viewport} className={styles.canvas} data-testid="workflow-canvas" role="region" aria-label={t("工作流画布，可拖动背景平移", "Workflow canvas. Drag the background to pan.")}
      onPointerDown={(event) => begin(event, (event.target as Element).closest<HTMLElement>("[data-drag-node]")?.dataset.dragNode)}
      onPointerMove={move} onPointerUp={finishDrag} onPointerCancel={() => { drag.current = null; setPreview(null); }}
      onKeyDown={(event) => { if (event.key === "Escape") setConnecting(null); }}>
      <div className={styles.world} style={{ transform: `translate(${camera.x}px, ${camera.y}px) scale(${camera.zoom})` }}>
        <svg className={styles.edges} width="1" height="1" aria-hidden="true">
          <defs><marker id={markerId} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="currentColor" /></marker></defs>
          {displayEdges(graph.edges).map((edge) => {
            const a = lookup.get(edge.source), b = lookup.get(edge.target);
            if (!a || !b) return null;
            const vertical = Math.abs(a.position.x - b.position.x) < WIDTH / 2;
            const backwards = b.position.x < a.position.x;
            const x1 = a.position.x + (vertical ? WIDTH : backwards ? 0 : WIDTH), y1 = a.position.y + HEIGHT / 2;
            const x2 = b.position.x + (vertical ? WIDTH : backwards ? WIDTH : 0), y2 = b.position.y + HEIGHT / 2;
            const bend = Math.max(48, Math.abs(x2 - x1) * .5);
            // Same-column siblings are independent dependencies, not a chain.
            // Route around card edges so a long link never passes through an
            // intermediate script/Agent and visually implies a false call.
            const siblings = graph.edges.filter((candidate) => candidate.source === edge.source && Math.abs((lookup.get(candidate.target)?.position.x ?? Infinity) - a.position.x) < WIDTH / 2);
            const lane = Math.max(x1, x2) + 24 + Math.max(0, siblings.findIndex((candidate) => candidate.id === edge.id)) * 10;
            const direction = y2 >= y1 ? 1 : -1;
            const corner = Math.min(12, Math.abs(y2 - y1) / 4);
            const d = vertical
              ? `M${x1},${y1} H${lane - corner} Q${lane},${y1} ${lane},${y1 + direction * corner} V${y2 - direction * corner} Q${lane},${y2} ${lane - corner},${y2} H${x2}`
              : `M${x1},${y1} C${x1 + (backwards ? -bend : bend)},${y1} ${x2 + (backwards ? bend : -bend)},${y2} ${x2},${y2}`;
            const active = selectedId === edge.source || selectedId === edge.target;
            return <g key={edge.id} data-edge={edge.id} data-routing={vertical ? "side" : "horizontal"} className={`${styles.edge} ${active ? styles.activeEdge : ""}`} data-origin={edge.origin}>
              <path d={d} fill="none" markerEnd={`url(#${markerId})`} />
              {edge.origin === "annotation" && <path d={d} className={styles.edgeHit} onClick={() => onEdgeSelect?.(edge)} />}
              {(active || ["parallel_dispatch", "conditional_dispatch", "aggregate"].includes(edge.relation)) && <text x={vertical ? lane : (x1 + x2) / 2} y={(y1 + y2) / 2 - 9} textAnchor="middle">{edge.label}</text>}
            </g>;
          })}
        </svg>
        {nodes.map((node) => <article key={node.id} data-workflow-node={node.id} data-kind={node.kind}
          className={`${styles.node} ${selectedId === node.id ? styles.selectedNode : ""} ${connecting === node.id ? styles.connectingNode : ""}`}
          style={{ left: node.position.x, top: node.position.y, width: WIDTH, height: HEIGHT } as CSSProperties}>
          <div className={styles.nodeTop}><span className={styles.nodeIcon}><WorkflowIcon kind={node.kind} /></span><span>{kindName(node.kind, t)}</span>
            {onMove && <button type="button" data-drag-node={node.id} className={styles.grip} aria-label={`${t("移动", "Move")} ${node.title}`} title={t("拖动，或用方向键移动", "Drag, or use arrow keys to move")}
              onKeyDown={(event) => { const directions: Record<string, WorkflowPosition> = { ArrowLeft: { x: -20, y: 0 }, ArrowRight: { x: 20, y: 0 }, ArrowUp: { x: 0, y: -20 }, ArrowDown: { x: 0, y: 20 } }; const delta = directions[event.key]; if (delta) { event.preventDefault(); onMove(node.id, { x: node.position.x + delta.x, y: node.position.y + delta.y }); } }}>⠿</button>}
          </div>
          <button className={styles.nodeBody} type="button" onClick={() => onSelect(node)} aria-label={`${t("编辑详情", "View details")}: ${cardTitle(node, t)}`} aria-pressed={selectedId === node.id}>
            <strong title={cardTitle(node, t)}>{cardTitle(node, t)}</strong><span title={cardPurpose(node, t)}>{cardPurpose(node, t)}</span>
          </button>
          <div className={styles.nodeBottom}><span className={styles.nodeRef}>{cardFacts(node, t)}</span>
            {onConnect && !node.id.startsWith("scheduler:installed/") && <button type="button" className={styles.port} aria-label={`${t("说明连线", "Annotate connection")}: ${node.title}`} title={t("选择两个卡片添加说明连线（不改变执行）", "Select two cards to annotate a relationship (does not change execution)")} onClick={() => connect(node.id)} />}
          </div>
        </article>)}
      </div>
      {connecting && <div className={styles.connectHint} role="status">{t("选择另一张卡片的圆形端口。说明连线不会改变执行逻辑。", "Choose another card’s circular port. Annotation edges do not change execution.")} <button onClick={() => setConnecting(null)}>{t("取消", "Cancel")}</button></div>}
    </div>
    <div className={styles.legend}><span><i />{t("代码调用 / 数据访问", "Code / data access")}</span><span><i className={styles.dashed} />{t("声明 / 配置绑定", "Declared / configured")}</span><span><i className={styles.annotation} />{t("手工说明", "Annotation")}</span><span className={styles.toolbarSpacer} />{graph.nodes.length} {t("个节点", "nodes")} · {graph.edges.length} {t("条关系", "edges")}</div>
  </div>;
}

"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { setComposeDraftPayload } from "../../lib/composeDraft";
import { asObject, cardTitle, cardFacts, stateLabel } from "../../lib/workflowPresentation";
import { NodeInspector } from "./WorkflowInspector";
import { WorkflowVerification } from "./WorkflowVerification";
import { WorkflowCommand, WorkflowHelp } from "./WorkflowNative";
import { WorkflowEditorDialog } from "./WorkflowEditorDialog";
import { compactWorkflow } from "../../lib/workflowProjection";
import { sourceTypeLabel } from "../../lib/workflowSources";
import sourceUi from "./WorkflowSourceSettings.module.css";
import { workflowDiff, diffValue } from "../../lib/workflowDiff";
import ui from "./WorkflowNative.module.css";
import { WorkflowActivity } from "./WorkflowActivity";
import { ContinuousStrategyStatus } from "./ContinuousStrategyStatus";
import { AddResource } from "./AddWorkflowResource";
import { WorkflowCardGallery } from "./WorkflowCardGallery";
import { configurationErrors } from "./WorkflowSettings";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { workflowApi } from "../../lib/workflowApi";
import { ModePill } from "../ModePill";
import { invalidateReadCache } from "../../lib/clientApi";
import { confirm as confirmDialog } from "../../lib/dialogs";
import type { WorkflowAddition, WorkflowChange, WorkflowEdge, WorkflowGraph, WorkflowMetadata, WorkflowNode, WorkflowPosition, WorkflowView } from "../../lib/workflowTypes";
import { WorkflowCanvas, WorkflowIcon, kindName, useWorkflowText } from "./WorkflowCanvas";
import styles from "./WorkflowStudio.module.css";

export async function confirmDiscard(message: string) {
  return confirmDialog({ message, tone: "warning" });
}
const rawValue = (node: WorkflowNode) => node.binding.file ? node.content || "" : JSON.stringify(node.config, null, 2);
// Editors use the same proposal-backed persistence as the canvas.

export function StrategyWorkflowPanel({ strategyId, proposalId, defaultView = "strategy", onDirtyChange, onSaved, renderTitle, headerActions }: {
  strategyId: string; proposalId?: string | null; defaultView?: "strategy" | "evolution";
  onDirtyChange?: (dirty: boolean) => void; onSaved?: (view: WorkflowView) => void;
  renderTitle?: (title: string) => ReactNode; headerActions?: ReactNode;
}) {
  const t = useWorkflowText();
  const router = useRouter();
  const [display, setDisplay] = useState<"canvas" | "all" | "cards">("canvas");
  const [additionSeed, setAdditionSeed] = useState<WorkflowAddition | null>(null);
  const [addDraftDirty, setAddDraftDirty] = useState(false);
  const [data, setData] = useState<WorkflowView | null>(null);
  const [view, setView] = useState<"strategy" | "evolution" | "runs">(defaultView);
  const [selected, setSelected] = useState<string | null>(null);
  const [inspecting, setInspecting] = useState(false);
  const focusComposer = useRef(false);
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null);
  const [rawDrafts, setRawDrafts] = useState<Record<string, string>>({});
  const [metadata, setMetadata] = useState<WorkflowMetadata>({ version: 1, nodes: {}, edges: [] });
  const [additions, setAdditions] = useState<WorkflowAddition[]>([]);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [commandDirty, setCommandDirty] = useState(false);
  const [checkOpen, setCheckOpen] = useState(false);
  const reviewing = useRef(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [savedMessage, setSavedMessage] = useState("");
  const request = useRef(0);
  const panelRef = useRef<HTMLElement>(null);
  function focusAfterEditor() {
    if (focusComposer.current) { focusComposer.current = false; return panelRef.current?.querySelector<HTMLElement>("[data-testid=workflow-command] textarea"); }
    const representative = selected && display === "canvas" ? projection?.aliases.get(selected) || selected : selected;
    return representative ? panelRef.current?.querySelector<HTMLElement>(`[data-workflow-node="${CSS.escape(representative)}"] button[aria-pressed]`) || panelRef.current?.querySelector<HTMLElement>(`[data-support-member="${CSS.escape(representative)}"]`) : undefined;
  }
  function reviewFromEditor() { setInspecting(false); setSelectedEdge(null); void reviewChanges(); }
  const dirty = addDraftDirty || Object.keys(rawDrafts).length > 0 || additions.length > 0 || (!!data && workflowDiff(data.metadata, metadata).length > 0);
  const hydrate = useCallback((value: WorkflowView) => {
    setData(value); setMetadata({ version: 1, nodes: value.metadata.nodes || {}, edges: value.metadata.edges || [] });
    setRawDrafts({}); setAdditions([]); setSelectedEdge(null); setAdding(false); setAddDraftDirty(false); setAdditionSeed(null);
  }, []);
  const load = useCallback(async () => {
    const version = ++request.current;
    setLoading(true); setError("");
    try { const out = await workflowApi.get(strategyId, proposalId); if (version === request.current) hydrate(out); }
    catch (reason) { if (version === request.current) setError(String(reason)); }
    finally { if (version === request.current) setLoading(false); }
  }, [strategyId, proposalId, hydrate]);
  useEffect(() => { void load(); return () => { request.current += 1; }; }, [load]);
  useEffect(() => { onDirtyChange?.(dirty || commandDirty); }, [dirty, commandDirty, onDirtyChange]);
  useEffect(() => {
    if (!dirty && !commandDirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    let confirming = false;
    const guardLink = (event: MouseEvent) => {
      const anchor = (event.target as Element).closest<HTMLAnchorElement>("a[href]");
      if (!anchor || anchor.target === "_blank" || anchor.hasAttribute("download") || anchor.href === window.location.href) return;
      event.preventDefault(); event.stopPropagation();
      if (confirming) return;
      confirming = true;
      void confirmDiscard(t("尚有未保存的工作流修改，离开将丢弃。是否继续？", "There are unsaved workflow edits. Leave and discard them?")).then((ok) => { confirming = false; if (ok) { window.removeEventListener("beforeunload", beforeUnload); window.location.assign(anchor.href); } });
    };
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", guardLink, true);
    return () => { window.removeEventListener("beforeunload", beforeUnload); document.removeEventListener("click", guardLink, true); };
  }, [dirty, commandDirty, t]);
  const graph = useMemo<WorkflowGraph | null>(() => {
    if (!data) return null;
    const original = data[view === "runs" ? "strategy" : view];
    const ids = new Set(original.nodes.map((node) => node.id));
    return { ...original, id: `${strategyId}:${data.source.proposal_id || "published"}:${view}`,
      nodes: original.nodes.map((node) => {
        const raw = rawDrafts[node.id];
        let change = {};
        if (raw !== undefined) { if (node.binding.file) change = { content: raw }; else { try { change = { config: JSON.parse(raw) as unknown }; } catch { /* Keep last canonical card while the editor shows invalid text. */ } } }
        const next = { ...node, ...change };
        if (node.kind === "strategy" && typeof asObject(next.config).title === "string") next.title = String(asObject(next.config).title);
        return { ...next, ...metadata.nodes[node.id] };
      }),
      edges: [...original.edges.filter((edge) => edge.origin !== "annotation"), ...metadata.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target))],
    };
  }, [data, metadata, rawDrafts, strategyId, view]);
  const projection = useMemo(() => graph ? compactWorkflow(graph, t, metadata) : null, [graph, t, metadata]);
  const shownGraph = display === "canvas" ? projection?.graph : graph;
  const group = projection && [...projection.groups, ...projection.supporting].find((g) => g.members.some((n) => n.id === selected));
  const node = graph?.nodes.find((item) => item.id === selected);
  const edge = graph?.edges.find((item) => item.id === selectedEdge);
  const related = node && graph ? graph.nodes.filter((item) => graph.edges.some((connection) => (connection.source === node.id && connection.target === item.id) || (connection.target === node.id && connection.source === item.id))) : [];
  const editable = !!data?.can_edit && !busy && !loading;
  function annotate(id: string, patch: { title?: string; description?: string; position?: WorkflowPosition }) {
    setMetadata((current) => ({ ...current, nodes: { ...current.nodes, [id]: { ...current.nodes[id], ...patch } } }));
  }
  async function chooseNode(item: WorkflowNode) {
    if (addDraftDirty && !await confirmDiscard(t("新卡片还未加入当前变更，放弃填写内容？", "Discard the unstaged new card?"))) return;
    setSelected(item.id); setInspecting(true); setSelectedEdge(null); setAdding(false); setAddDraftDirty(false);
  }
  async function closeAdd() {
    if (addDraftDirty && !await confirmDiscard(t("放弃正在填写的新卡片？", "Discard this unstaged card?"))) return false;
    setAdding(false); setAddDraftDirty(false); return true;
  }
  function duplicate(item: WorkflowNode) {
    const suffix = `_copy_${crypto.randomUUID().slice(0, 4)}`;
    const resource: WorkflowAddition = item.kind === "script" ? { kind: "script", name: String(item.binding.file).replace(/\.py$/, `${suffix}.py`), content: rawDrafts[item.id] ?? item.content ?? "" }
      : item.kind === "agent" ? { kind: "agent", name: `${String(item.binding.file).split("/").pop()?.replace(/\.agent\.md$/, "")}${suffix}`, content: rawDrafts[item.id] ?? item.content ?? "" }
      : { kind: "source", name: `${asObject(item.config).id || "source"}${suffix}`, config: { ...asObject(item.config), title: `${cardTitle(item, t)} ${t("副本", "copy")}` } };
    setAdditionSeed(resource); setAdding(true); setSelected(null); setSelectedEdge(null); setAddDraftDirty(false);
  }
  function resetCard(id: string) {
    setRawDrafts((current) => { const next = { ...current }; delete next[id]; return next; });
    setMetadata((current) => { const nodes = { ...current.nodes }; if (data?.metadata.nodes[id]) nodes[id] = data.metadata.nodes[id]; else delete nodes[id]; return { ...current, nodes }; });
    setError("");
  }
  const changedCards = new Set([...Object.keys(rawDrafts), ...Object.keys(metadata.nodes).filter((id) => workflowDiff(data?.metadata.nodes[id], metadata.nodes[id]).length > 0)]).size + additions.length;
  async function openRuns() { if (adding && !(await closeAdd())) return; setInspecting(false); setView("runs"); setSelected(null); setSelectedEdge(null); }
  function askAgent(item: WorkflowNode, instruction = "") {
    if (dirty) { setError(t("请先保存当前修改，再交给主 Agent，以免遗漏尚未保存的内容。", "Save your edits before handing this card to the main Agent.")); return; }
    setComposeDraftPayload({ autoSend: false, attachments: [], text: t(
      `请使用 strategy_author 帮我修改策略 ${strategyId} 的「${cardTitle(item, t)}」卡片。候选版本：${data?.source.proposal_id || "当前版本"}；节点：${item.id}；资源：${item.binding.file || JSON.stringify(item.binding.path)}。${instruction ? `我的修改要求：${instruction}。` : "请先问我希望怎样修改。"}读取真实配置和代码后修改，保存为待审核提案，不改变无关卡片，不启用交易或调度。`,
      `Use strategy_author to edit the card ${cardTitle(item, t)} in strategy ${strategyId}. Proposal: ${data?.source.proposal_id || "current"}; node: ${item.id}; resource: ${item.binding.file || JSON.stringify(item.binding.path)}. ${instruction ? `Requested change: ${instruction}.` : "Ask what I need changed."} Read the actual code/configuration and save a review proposal without changing unrelated cards or starting trades or schedules.`) });
    router.push("/chat");
  }
  async function switchView(next: "strategy" | "runs" | "evolution") {
    if (adding && !await closeAdd()) return;
    setInspecting(false); setView(next); setSelected(null); setSelectedEdge(null);
  }
  async function reviewChanges() {
    if (!data || busy || adding || !dirty || reviewing.current) return;
    reviewing.current = true;
    try {
      const allNodes = [...data.strategy.nodes, ...data.evolution.nodes];
      const changes = Object.entries(rawDrafts).flatMap(([id, raw]) => {
        const item = allNodes.find((n) => n.id === id);
        if (!item) throw new Error(t("资源已变化，请重新载入。", "The resource changed. Reload it."));
        const value: unknown = item.binding.file ? raw : JSON.parse(raw);
        const errors = configurationErrors(item, value, t);
        if (errors.length) throw new Error(`${cardTitle(item, t)}: ${errors.join("；")}`);
        return workflowDiff(item.binding.file ? item.content || "" : item.config, value).map((change) => ({ ...change, title: cardTitle(item, t) }));
      });
      for (const [id, meta] of Object.entries(metadata.nodes)) {
        const item = allNodes.find((n) => n.id === id);
        changes.push(...workflowDiff(data.metadata.nodes[id] || {}, meta).map((change) => ({ ...change, title: `${item ? cardTitle(item, t) : id} · ${t("显示设置", "Presentation")}` })));
      }
      for (const item of additions) changes.push({ title: t("新增资源", "New resource"), path: item.name, before: undefined, after: item.content ?? item.config });
      changes.push(...workflowDiff(data.metadata.edges, metadata.edges).map((change) => ({ ...change, title: t("说明连线（不改变执行）", "Annotations (no execution change)") })));
      const labels: Record<string, string> = { limit: t("读取条数", "Rows"), timeframe: t("数据周期", "Timeframe"), every_seconds: t("间隔（秒）", "Interval (seconds)"), title: t("名称", "Name"), description: t("说明", "Description"), enabled: t("启用状态", "Enabled"), "agent_profile.role": t("Agent 任务", "Agent instructions") };
      const approved = await confirmDialog({ title: t("检查修改", "Review changes"), okLabel: t("保存为提案", "Save proposal"), cancelLabel: t("继续编辑", "Keep editing"), message: <><p>{t("保存为待审提案，不会立即改变运行。", "Saves a review proposal without changing current execution.")}</p><div className={ui.changeList} data-testid="workflow-change-review">{changes.map((change, index) => <details key={index} className={ui.change} open={changes.length < 4}><summary>{change.title}{change.path ? ` · ${labels[change.path] || change.path}` : ""}</summary><div className={ui.changeColumns}><div><small>{t("修改前", "Before")}</small><pre>{diffValue(change.before, t("未设置", "Not set"))}</pre></div><div><small>{t("修改后", "After")}</small><pre className={ui.changeAfter}>{diffValue(change.after, t("移除", "Removed"))}</pre></div></div></details>)}</div></> });
      if (approved) await save();
    } catch (reason) { setError(String(reason)); }
    finally { reviewing.current = false; }
  }
  async function save() {
    if (!data || busy || adding) return;
    setBusy(true); setError(""); setSavedMessage("");
    try {
      const allNodes = [...data.strategy.nodes, ...data.evolution.nodes];
      const changes: WorkflowChange[] = Object.entries(rawDrafts).map(([id, value]) => {
        const canonical = allNodes.find((item) => item.id === id);
        if (!canonical) throw new Error(t("节点已变化，请重新载入。", "The node changed; reload the workflow."));
        if (canonical.binding.file) return { node_id: id, content: value };
        const config: unknown = JSON.parse(value);
        const errors = configurationErrors(canonical, config, t);
        if (errors.length) throw new Error(`${cardTitle(canonical, t)}: ${errors.join("；")}`);
        return { node_id: id, config };
      });
      const out = await workflowApi.propose({ strategy_id: strategyId, proposal_id: data.source.proposal_id, base_revision: data.revision, changes, additions, metadata });
      hydrate(out.workflow); onSaved?.(out.workflow);
      setSavedMessage(t("已保存为待审提案", "Saved for review"));
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  if (!graph || !data) return <section className={ui.panel} data-testid="strategy-workflow-panel" aria-busy={loading}><header className={ui.header}><div className={ui.title}><h2>{renderTitle ? renderTitle(strategyId) : strategyId}</h2></div><div className={ui.headerActions}>{headerActions}</div></header>{loading ? <div className={styles.loading} role="status">{t("正在读取工作流…", "Loading workflow…")}</div> : <div className={styles.empty} role="alert"><p>{error || t("工作流暂不可用", "Workflow unavailable")}</p><button className={styles.secondaryButton} onClick={() => void load()}>{t("重新加载", "Retry")}</button></div>}</section>;
  return <section ref={panelRef} className={ui.panel} data-testid="strategy-workflow-panel" aria-busy={busy || loading} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") { event.preventDefault(); void reviewChanges(); } }}>
    <header className={ui.header}><div className={ui.title}><h2>{renderTitle ? renderTitle(String(data.manifest.title || strategyId)) : String(data.manifest.title || strategyId)}</h2>{data.manifest.description ? <p>{String(data.manifest.description)}</p> : null}</div><div className={ui.headerActions}><ModePill mode={String(data.manifest.mode || "paper")} /><span className={ui.state}>{stateLabel(data.source.state, t)}</span>{headerActions}</div></header>
    {asObject(data.manifest.runtime).mode === "continuous" && <ContinuousStrategyStatus key={`${strategyId}:${data.source.proposal_id || "active"}`} strategyId={strategyId} proposalId={data.source.proposal_id} dirty={dirty} />}
    <div className={ui.toolbar}><div className={ui.tabs} role="tablist" aria-label={t("工作流视图", "Workflow view")}>
      <button type="button" role="tab" aria-selected={view === "strategy"} onClick={() => void switchView("strategy")}>{t("工作流", "Workflow")}</button>
      <button type="button" role="tab" aria-selected={view === "runs"} onClick={() => void switchView("runs")}>{t("运行", "Runs")}</button>
      <button type="button" role="tab" aria-selected={view === "evolution"} onClick={() => void switchView("evolution")}>{t("复盘", "Review")}</button>
    </div><span className={ui.spacer} />
      {view === "runs" && <button type="button" className={ui.quietButton} disabled={dirty || adding || busy || loading} onClick={() => setCheckOpen(true)}>{t("验证记录", "Verification records")}</button>}
      {view !== "runs" && <ChoiceSelect className={ui.quietButton} aria-label={t("显示内容", "Display mode")} value={display} style={{ background: "var(--bg)", minWidth: 0 }} onValueChange={(choiceValue) => { setDisplay(choiceValue as typeof display); setSelectedEdge(null); }}><option value="canvas">{t("精简工作流", "Workflow")}</option><option value="all">{t("全部关系", "All relationships")}</option><option value="cards">{t("资源列表", "Resources")}</option></ChoiceSelect>}
      {view !== "runs" && <button className={ui.iconButton} type="button" disabled={!editable} aria-label={t("添加资源", "Add resource")} title={t("添加资源", "Add resource")} onClick={() => { if (adding) { void closeAdd(); return; } setAdditionSeed(null); setAdding(true); setSelected(null); setSelectedEdge(null); }}>+</button>}
      {dirty ? <><button className={ui.quietButton} disabled={busy} type="button" onClick={async () => { if (await confirmDiscard(t("放弃手动修改？", "Discard manual edits?"))) { invalidateReadCache(); await load(); } }}>{t("撤销", "Discard")}</button><button className={ui.reviewButton} type="button" disabled={!editable || adding} onClick={() => void reviewChanges()}>{busy ? t("保存中…", "Saving…") : t("检查修改", "Review changes")} · {changedCards || 1}</button></> : <Link className={ui.quietButton} href={data.source.proposal_id ? `/self-evolution?tab=proposals&proposal_id=${encodeURIComponent(data.source.proposal_id)}` : `/strategies/${encodeURIComponent(strategyId)}`}>{t("版本", "Versions")} ↗</Link>}
    </div>
    {error && <div role="alert" className={styles.errorBanner}>{error}<button onClick={async () => { if (!dirty || await confirmDiscard(t("重新载入并放弃手动修改？", "Reload and discard manual edits?"))) { invalidateReadCache(); await load(); } }}>{t("重新载入", "Reload")}</button></div>}
    {savedMessage && <div role="status" className={styles.successBanner}>{savedMessage}</div>}
    {!data.can_edit && <p className={styles.notice}>{t("当前资源只读。", "This resource is read-only.")} <Link href={`/strategies/${encodeURIComponent(strategyId)}`}>{t("打开管理页", "Open manager")} ↗</Link></p>}
    {view === "evolution" && <div className={ui.inlineActions} style={{ padding: "12px 30px 0" }}><span className={ui.muted}>{t("复盘配置", "Review configuration")}</span><WorkflowHelp label={t("复盘帮助", "About review")}><p>{t("这里定义复盘规则，实际执行和审批结果在复盘记录中查看。", "These are review rules. Execution and approval outcomes are in review records.")}</p></WorkflowHelp><Link href="/self-evolution?tab=timeline">{t("查看记录", "View records")} ↗</Link></div>}
    {additions.length > 0 && <div className={styles.pendingAdditions}>{additions.map((item, index) => <span key={`${item.kind}:${item.name}:${index}`}>+ {item.name}<button disabled={busy} type="button" aria-label={`${t("撤销添加", "Undo addition")}: ${item.name}`} onClick={() => setAdditions((items) => items.filter((_, i) => i !== index))}>×</button></span>)}</div>}
    {view === "runs" ? <WorkflowActivity roleNames={Object.fromEntries(data.strategy.nodes.filter((n) => n.id.startsWith("agent:role/")).map((n) => [String(asObject(n.config).name), cardTitle(n, t)]))} key={`${strategyId}:${data.source.proposal_id || "active"}`} strategyId={strategyId} proposalId={data.source.proposal_id} onEdit={(kind) => { const target = data.strategy.nodes.find((n) => n.kind === kind && (kind !== "agent" || n.id === "agent:runtime")) || data.strategy.nodes.find((n) => n.kind === kind); setView("strategy"); setSelected(target?.id || null); setInspecting(!!target); }} /> : <div className={ui.body}>
      {display === "cards" ? <WorkflowCardGallery key={graph.id} graph={graph} selectedId={selected} onSelect={(item) => void chooseNode(item)} /> : <WorkflowCanvas graph={shownGraph || graph} selectedId={display === "canvas" && selected ? projection?.aliases.get(selected) || selected : selected} onSelect={(item) => void chooseNode(item)} onMove={editable ? (id, position) => annotate(id, { position }) : undefined}
        onConnect={editable && !adding ? (source, target) => { const id = `note:${crypto.randomUUID()}`; const next: WorkflowEdge = { id, source, target, origin: "annotation", relation: "annotation", label: t("说明关系", "Annotation") }; setMetadata((current) => ({ ...current, edges: [...current.edges, next] })); setSelectedEdge(id); setInspecting(true); setSelected(null); setAdding(false); } : undefined}
        onEdgeSelect={(item) => { if (adding) return; setSelectedEdge(item.id); setInspecting(true); setSelected(null); }} />}
      {display === "canvas" && !!projection?.supporting.length && <div className={sourceUi.supports} aria-label={t("运行配置", "Execution settings")}>{projection.supporting.map((item) => <button type="button" key={item.id} data-support-member={item.id} onClick={() => void chooseNode(item.members[0])}><WorkflowIcon kind={item.members[0].kind} size={15} />{item.title}{item.members[0].kind === "risk" ? ` · ${cardFacts(item.members[0], t)}` : item.members[0].kind === "account" ? ` · ${item.members.length}` : ""}</button>)}</div>}
      <WorkflowEditorDialog open={adding || inspecting && !!(node || edge)} title={adding ? t("添加资源", "Add resource") : node ? cardTitle(node, t) : t("说明连线", "Annotation")} onClose={() => { if (adding) void closeAdd(); else setInspecting(false); }} focusAfterClose={focusAfterEditor} footer={!adding ? <><button className={ui.quietButton} type="button" disabled={!editable || !node} onClick={() => { focusComposer.current = true; setInspecting(false); }}>{t("让 Agent 帮我改", "Edit with Agent")} ↗</button><span className={ui.spacer} />{dirty && <span className={ui.muted}>{t("有未保存修改", "Unsaved changes")}</span>}<button type="button" className={dirty ? ui.reviewButton : ui.quietButton} disabled={busy} onClick={() => dirty ? reviewFromEditor() : setInspecting(false)}>{dirty ? t("检查修改", "Review changes") : t("返回工作流", "Back to workflow")}</button></> : undefined}>
      {node && !adding && group && group.members.length > 1 && <div className={sourceUi.groupNav}><label htmlFor="workflow-group-resource">{node.kind === "source" && node.binding.path?.[0] === "data_sources" ? t("读取配置", "Subscription") : group.title}</label><ChoiceSelect id="workflow-group-resource" aria-label={t("节点内配置", "Configuration in this node")} value={selected || ""} onValueChange={(choiceValue) => setSelected(choiceValue)}>{group.members.map((item) => <option key={item.id} value={item.id}>{cardTitle(item, t)}{item.binding.path?.[0] === "data_sources" ? ` · ${sourceTypeLabel(String(asObject(item.config).capability || "candles"), t)}` : ""}</option>)}</ChoiceSelect></div>}
      {node && !adding && <NodeInspector key={node.id} node={node} displayTitle={node.kind === "source" && group?.members.length ? group.title : undefined} nodes={view === "strategy" ? graph.nodes : data.strategy.nodes} defaults={data.agent_defaults} raw={rawDrafts[node.id] ?? rawValue(node)} writable={editable} onRaw={(value) => setRawDrafts((current) => { const next = { ...current }; const canonical = [...data.strategy.nodes, ...data.evolution.nodes].find((item) => item.id === node.id); if (canonical && rawValue(canonical) === value) delete next[node.id]; else next[node.id] = value; return next; })} onMetadata={(patch) => annotate(node.id, patch)} onClose={() => setInspecting(false)} related={related} onSelectRelated={(id) => { setSelected(id); setInspecting(true); }} onDuplicate={duplicate} onSave={reviewFromEditor} onReset={rawDrafts[node.id] !== undefined || JSON.stringify(metadata.nodes[node.id]) !== JSON.stringify(data.metadata.nodes[node.id]) ? () => resetCard(node.id) : undefined} onRuns={openRuns} dirty={dirty && !adding} busy={busy} />}
      {adding && <AddResource key={additionSeed?.name || "new"} seed={additionSeed} onDirtyChange={setAddDraftDirty} onClose={() => void closeAdd()} onAdd={(resource) => { setAdditions((current) => [...current, resource]); setAdding(false); setInspecting(false); setAddDraftDirty(false); }} />}
      {edge && <section className={ui.inspector}><header className={ui.inspectorHeader}><h3>{t("说明连线", "Annotation")}</h3><button className={ui.iconButton} type="button" aria-label={t("关闭详情", "Close details")} onClick={() => setInspecting(false)}>×</button></header><div className={ui.inspectorBody}><p className={ui.muted}>{t("仅作说明，不改变执行。", "An annotation, not an execution link.")}</p><label className={styles.field}>{t("连线说明", "Edge label")}<input maxLength={160} disabled={!editable} value={edge.label} onChange={(event) => setMetadata((current) => ({ ...current, edges: current.edges.map((item) => item.id === edge.id ? { ...item, label: event.target.value } : item) }))} /></label><button className={ui.quietButton} disabled={!editable} onClick={() => { setMetadata((current) => ({ ...current, edges: current.edges.filter((item) => item.id !== edge.id) })); setSelectedEdge(null); setInspecting(false); }}>{t("移除连线", "Remove annotation")}</button></div></section>}
      </WorkflowEditorDialog>
    </div>}
    {checkOpen && <WorkflowVerification workflow={data} onClose={() => setCheckOpen(false)} onEdit={(where) => {
      const target = [...data.strategy.nodes, ...data.evolution.nodes].find((n) => n.binding.file && (where === n.binding.file || where.startsWith(n.binding.file + ":"))) || data.strategy.nodes.find((n) => n.kind === "strategy");
      setCheckOpen(false); setView("strategy"); setSelected(target?.id || null); setInspecting(!!target);
    }} />}
    <WorkflowCommand node={node} disabled={!editable || adding} dirty={dirty} onDraftChange={setCommandDirty} onSubmit={(text, nodeId) => { const target = [...data.strategy.nodes, ...data.evolution.nodes].find((item) => item.id === nodeId) || data.strategy.nodes.find((item) => item.kind === "strategy"); if (target) askAgent(target, text); }} />
  </section>;
}

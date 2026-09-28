"use client";
import { Icon as NeryaGlyph } from "../icons";

import { strategyChatUrl, strategyEditPrompt } from "../../lib/strategyChat";
import { ChoiceSelect } from "../ChoiceSelect";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { setComposeDraftPayload } from "../../lib/composeDraft";
import { asObject, cardTitle, cardFacts, stateLabel } from "../../lib/workflowPresentation";
import { NodeInspector } from "./WorkflowInspector";
import { WorkflowVerification } from "./WorkflowVerification";
import { WorkflowHelp } from "./WorkflowNative";
import { StrategyExportButton } from "./StrategyTransfer";
import { WorkflowEditorDialog } from "./WorkflowEditorDialog";
import { compactWorkflow } from "../../lib/workflowProjection";
import { sourceTypeLabel } from "../../lib/workflowSources";
import sourceUi from "./WorkflowSourceSettings.module.css";
import { workflowDiff, diffValue } from "../../lib/workflowDiff";
import ui from "./WorkflowNative.module.css";
import { WorkflowActivity } from "./WorkflowActivity";
import { WorkflowReviewActivity } from "./WorkflowReviewActivity";
import { useLocale, useTranslations } from "next-intl";
import { readEditDraft, writeEditDraft } from "../../lib/editDrafts";
import { StrategyVersionContext } from "./StrategyVersionContext";
import { rememberInvocation } from "../../lib/workflowReplay";
import { ContinuousStrategyStatus } from "./ContinuousStrategyStatus";
import { AddResource } from "./AddWorkflowResource";
import { WorkflowCardGallery } from "./WorkflowCardGallery";
import { configurationErrors } from "./WorkflowSettings";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { workflowApi } from "../../lib/workflowApi";
import { ModePill } from "../ModePill";
import { invalidateReadCache } from "../../lib/clientApi";
import { confirm as confirmDialog } from "../../lib/dialogs";
import type { WorkflowAddition, WorkflowChange, WorkflowGraph, WorkflowMetadata, WorkflowNode, WorkflowPosition, WorkflowView } from "../../lib/workflowTypes";
import { WorkflowCanvas, WorkflowIcon, useWorkflowText } from "./WorkflowCanvas";
import styles from "./WorkflowStudio.module.css";

export async function confirmDiscard(message: string) {
  return confirmDialog({ message, tone: "warning" });
}
const rawValue = (node: WorkflowNode) => node.binding.file ? node.content || "" : JSON.stringify(node.config, null, 2);
// Editors use the same proposal-backed persistence as the canvas.

export function StrategyWorkflowPanel({ strategyId, proposalId, defaultView = "strategy", onDirtyChange, onSaved, renderTitle, headerActions, embedded = false }: {
  strategyId: string; proposalId?: string | null; defaultView?: "strategy" | "evolution";
  onDirtyChange?: (dirty: boolean) => void; onSaved?: (view: WorkflowView) => void;
  renderTitle?: (title: string) => ReactNode; headerActions?: ReactNode; embedded?: boolean;
}) {
  const t = useWorkflowText();
  const router = useRouter();
  const wx = useTranslations("workflowExperience");
  const zh = useLocale().startsWith("zh");
  type SavedDraft = { revision:string; rawDrafts:Record<string,string>; metadata:WorkflowMetadata; additions:WorkflowAddition[] };
  const draftKey = (value:WorkflowView) => `workflow:${value.strategy_id}:${value.source.proposal_id || 'published'}`;
  const [conflictingDraft,setConflictingDraft] = useState<SavedDraft | null>(null);
  const [reviewLogsOpen, setReviewLogsOpen] = useState(false);
  useEffect(() => {
    const scope = new URLSearchParams(window.location.search).get("workflow_log");
    if (scope === "strategy") setView("runs");
    if (scope === "evolution") { setView("evolution"); setReviewLogsOpen(true); }
  }, [strategyId, proposalId]);
  const [display, setDisplay] = useState<"canvas" | "cards">("canvas");
  const [additionSeed, setAdditionSeed] = useState<WorkflowAddition | null>(null);
  const [addDraftDirty, setAddDraftDirty] = useState(false);
  const [data, setData] = useState<WorkflowView | null>(null);
  const [view, setView] = useState<"strategy" | "evolution" | "runs">(defaultView);
  const [selected, setSelected] = useState<string | null>(null);
  const [inspecting, setInspecting] = useState(false);
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null);
  const [rawDrafts, setRawDrafts] = useState<Record<string, string>>({});
  const [metadata, setMetadata] = useState<WorkflowMetadata>({ version: 1, nodes: {}, edges: [] });
  const [additions, setAdditions] = useState<WorkflowAddition[]>([]);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [checkOpen, setCheckOpen] = useState(false);
  const reviewing = useRef(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [savedMessage, setSavedMessage] = useState("");
  const request = useRef(0);
  const panelRef = useRef<HTMLElement>(null);
  function focusAfterEditor() {
    const representative = selected && display === "canvas" ? projection?.aliases.get(selected) || selected : selected;
    return representative ? panelRef.current?.querySelector<HTMLElement>(`[data-workflow-node="${CSS.escape(representative)}"] button[aria-pressed]`) || panelRef.current?.querySelector<HTMLElement>(`button[data-workflow-node="${CSS.escape(representative)}"]`) || panelRef.current?.querySelector<HTMLElement>(`[data-support-member="${CSS.escape(representative)}"]`) : undefined;
  }
  function reviewFromEditor() { setInspecting(false); setSelectedEdge(null); void reviewChanges(); }
  const dirty = addDraftDirty || Object.keys(rawDrafts).length > 0 || additions.length > 0 || (!!data && workflowDiff(data.metadata, metadata).length > 0);
  const hydrate = useCallback((value: WorkflowView, restore = true) => {
    const saved = restore ? readEditDraft<SavedDraft>(draftKey(value)) : null;
    const matched = saved?.revision === value.revision && saved.metadata && saved.rawDrafts && Array.isArray(saved.additions) ? saved : null;
    setConflictingDraft(saved && !matched ? saved : null);
    setData(value); setMetadata(matched?.metadata || { version: 1, nodes: value.metadata.nodes || {}, edges: value.metadata.edges || [] });
    setRawDrafts(matched?.rawDrafts || {}); setAdditions(matched?.additions || []); setSelectedEdge(null); setAdding(false); setAddDraftDirty(false); setAdditionSeed(null);
  }, []);
  useEffect(() => {
    if (data && data.strategy_id === strategyId && dirty && !conflictingDraft)
      writeEditDraft(draftKey(data),{ revision:data.revision, rawDrafts, metadata, additions });
  },[data,strategyId,rawDrafts,metadata,additions,dirty,conflictingDraft]);
  const load = useCallback(async () => {
    const version = ++request.current;
    setLoading(true); setError("");
    try { const out = await workflowApi.get(strategyId, proposalId); if (version === request.current) hydrate(out); }
    catch (reason) { if (version === request.current) setError(String(reason)); }
    finally { if (version === request.current) setLoading(false); }
  }, [strategyId, proposalId, hydrate]);
  useEffect(() => { void load(); return () => { request.current += 1; }; }, [load]);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    let confirming = false;
    const guardLink = (event: MouseEvent) => {
      const anchor = (event.target as Element).closest<HTMLAnchorElement>("a[href]");
      if (!anchor || anchor.target === "_blank" || anchor.hasAttribute("download") || anchor.href === window.location.href) return;
      event.preventDefault(); event.stopPropagation();
      if (confirming) return;
      confirming = true;
      void confirmDiscard(t("copy.components_workflows_StrategyWorkflowPanel.001")).then((ok) => { confirming = false; if (ok) { window.removeEventListener("beforeunload", beforeUnload); window.location.assign(anchor.href); } });
    };
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", guardLink, true);
    return () => { window.removeEventListener("beforeunload", beforeUnload); document.removeEventListener("click", guardLink, true); };
  }, [dirty, t]);
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
  const shownGraph = projection?.graph;
  const group = projection && [...projection.groups, ...projection.supporting].find((g) => g.members.some((n) => n.id === selected));
  const node = graph?.nodes.find((item) => item.id === selected);
  const edge = graph?.edges.find((item) => item.id === selectedEdge);
  const related = node && graph ? graph.nodes.filter((item) => graph.edges.some((connection) => (connection.source === node.id && connection.target === item.id) || (connection.target === node.id && connection.source === item.id))) : [];
  const editable = !!data?.can_edit && data.strategy_id === strategyId && !busy && !loading;
  function annotate(id: string, patch: { title?: string; description?: string; position?: WorkflowPosition }) {
    setMetadata((current) => ({ ...current, nodes: { ...current.nodes, [id]: { ...current.nodes[id], ...patch } } }));
  }
  async function chooseNode(item: WorkflowNode) {
    if (addDraftDirty && !await confirmDiscard(t("copy.components_workflows_StrategyWorkflowPanel.002"))) return;
    setSelected(item.id); setInspecting(true); setSelectedEdge(null); setAdding(false); setAddDraftDirty(false);
  }
  async function closeAdd() {
    if (addDraftDirty && !await confirmDiscard(t("copy.components_workflows_StrategyWorkflowPanel.003"))) return false;
    setAdding(false); setAddDraftDirty(false); return true;
  }
  function duplicate(item: WorkflowNode) {
    const suffix = `_copy_${crypto.randomUUID().slice(0, 4)}`;
    const resource: WorkflowAddition = item.kind === "script" ? { kind: "script", name: String(item.binding.file).replace(/\.py$/, `${suffix}.py`), content: rawDrafts[item.id] ?? item.content ?? "" }
      : item.kind === "agent" ? { kind: "agent", name: `${String(item.binding.file).split("/").pop()?.replace(/\.agent\.md$/, "")}${suffix}`, content: rawDrafts[item.id] ?? item.content ?? "" }
      : { kind: "source", name: `${asObject(item.config).id || "source"}${suffix}`, config: { ...asObject(item.config), title: `${cardTitle(item, t)} ${t("copy.components_workflows_StrategyWorkflowPanel.004")}` } };
    setAdditionSeed(resource); setAdding(true); setSelected(null); setSelectedEdge(null); setAddDraftDirty(false);
  }
  function resetCard(id: string) {
    setRawDrafts((current) => { const next = { ...current }; delete next[id]; return next; });
    setMetadata((current) => { const nodes = { ...current.nodes }; if (data?.metadata.nodes[id]) nodes[id] = data.metadata.nodes[id]; else delete nodes[id]; return { ...current, nodes }; });
    setError("");
  }
  const changedCards = new Set([...Object.keys(rawDrafts), ...Object.keys(metadata.nodes).filter((id) => workflowDiff(data?.metadata.nodes[id], metadata.nodes[id]).length > 0)]).size + additions.length;
  async function openRuns() {
    if (adding && !(await closeAdd())) return;
    setInspecting(false); setSelected(null); setSelectedEdge(null);
    if (view === "evolution") { setReviewLogsOpen(true); rememberInvocation("", "evolution"); }
    else { setView("runs"); rememberInvocation("", "strategy"); }
  }
  function askAgent(item: WorkflowNode | undefined, instruction = "") {
    if (dirty) { setError(t("copy.components_workflows_StrategyWorkflowPanel.005")); return; }
    if (!editable || !data) return;
    const target = item || data.strategy.nodes.find((n) => n.kind === "strategy");
    const requestText = target && target.kind !== "strategy"
      ? wx("editNodeRequest", { title: cardTitle(target, t) })
      : wx("editStrategyRequest", { title: String(data.manifest.title || strategyId) });
    setComposeDraftPayload({ autoSend: false, attachments: [], text: strategyEditPrompt(data, requestText + instruction, wx("editGuidance"), target) });
    setInspecting(false);
    // A fresh draft key also replaces a previous handoff on an already-open /chat page.
    router.push(strategyChatUrl(data.strategy_id, data.source.proposal_id, crypto.randomUUID()));
  }
  async function switchView(next: "strategy" | "runs" | "evolution") {
    if (adding && !await closeAdd()) return;
    setInspecting(false); setView(next); setSelected(null); setSelectedEdge(null); setReviewLogsOpen(false);
    const url = new URL(window.location.href);
    url.searchParams.delete("workflow_log"); url.searchParams.delete("workflow_run");
    window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
  }
  async function reviewChanges() {
    if (!data || busy || adding || !dirty || reviewing.current) return;
    reviewing.current = true;
    try {
      const allNodes = [...data.strategy.nodes, ...data.evolution.nodes];
      const changes = Object.entries(rawDrafts).flatMap(([id, raw]) => {
        const item = allNodes.find((n) => n.id === id);
        if (!item) throw new Error(t("copy.components_workflows_StrategyWorkflowPanel.009"));
        const value: unknown = item.binding.file ? raw : JSON.parse(raw);
        const errors = configurationErrors(item, value, t);
        if (errors.length) throw new Error(`${cardTitle(item, t)}: ${errors.join("；")}`);
        return workflowDiff(item.binding.file ? item.content || "" : item.config, value).map((change) => ({ ...change, title: cardTitle(item, t) }));
      });
      for (const [id, meta] of Object.entries(metadata.nodes)) {
        const item = allNodes.find((n) => n.id === id);
        changes.push(...workflowDiff(data.metadata.nodes[id] || {}, meta).map((change) => ({ ...change, title: `${item ? cardTitle(item, t) : id} · ${t("copy.components_workflows_StrategyWorkflowPanel.010")}` })));
      }
      for (const item of additions) changes.push({ title: t("copy.components_workflows_StrategyWorkflowPanel.011"), path: item.name, before: undefined, after: item.content ?? item.config });
      changes.push(...workflowDiff(data.metadata.edges, metadata.edges).map((change) => ({ ...change, title: t("copy.components_workflows_StrategyWorkflowPanel.012") })));
      const labels: Record<string, string> = { limit: t("copy.components_workflows_StrategyWorkflowPanel.013"), timeframe: t("copy.components_workflows_StrategyWorkflowPanel.014"), every_seconds: t("copy.components_workflows_StrategyWorkflowPanel.015"), title: t("copy.components_workflows_StrategyWorkflowPanel.016"), description: t("copy.components_workflows_StrategyWorkflowPanel.017"), enabled: t("copy.components_workflows_StrategyWorkflowPanel.018"), "agent_profile.role": t("copy.components_workflows_StrategyWorkflowPanel.019") };
      const approved = await confirmDialog({ title: t("copy.components_workflows_StrategyWorkflowPanel.020"), okLabel: t("copy.components_workflows_StrategyWorkflowPanel.021"), cancelLabel: t("copy.components_workflows_StrategyWorkflowPanel.022"), message: <><p>{t("copy.components_workflows_StrategyWorkflowPanel.023")}</p><div className={ui.changeList} data-testid="workflow-change-review">{changes.map((change, index) => <details key={index} className={ui.change} open={changes.length < 4}><summary>{change.title}{change.path ? ` · ${labels[change.path] || change.path}` : ""}</summary><div className={ui.changeColumns}><div><small>{t("copy.components_workflows_StrategyWorkflowPanel.024")}</small><pre>{diffValue(change.before, t("copy.components_workflows_StrategyWorkflowPanel.025"))}</pre></div><div><small>{t("copy.components_workflows_StrategyWorkflowPanel.026")}</small><pre className={ui.changeAfter}>{diffValue(change.after, t("copy.components_workflows_StrategyWorkflowPanel.027"))}</pre></div></div></details>)}</div></> });
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
        if (!canonical) throw new Error(t("copy.components_workflows_StrategyWorkflowPanel.028"));
        if (canonical.binding.file) return { node_id: id, content: value };
        const config: unknown = JSON.parse(value);
        const errors = configurationErrors(canonical, config, t);
        if (errors.length) throw new Error(`${cardTitle(canonical, t)}: ${errors.join("；")}`);
        return { node_id: id, config };
      });
      const out = await workflowApi.propose({ strategy_id: strategyId, proposal_id: data.source.proposal_id, base_revision: data.revision, changes, additions, metadata });
      writeEditDraft(draftKey(data),null);
      hydrate(out.workflow,false); onSaved?.(out.workflow);
      setSavedMessage(zh ? "候选变更已保存，尚未应用或启动。" : "Candidate saved. It has not been applied or started.");
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  if (!graph || !data) return <section className={ui.panel} data-testid="strategy-workflow-panel" aria-busy={loading}><header className={ui.header}><div className={ui.title}><h2>{renderTitle ? renderTitle(strategyId) : strategyId}</h2></div><div className={ui.headerActions}>{headerActions}</div></header>{loading ? <div className={styles.loading} role="status">{t("copy.components_workflows_StrategyWorkflowPanel.030")}</div> : <div className={styles.empty} role="alert"><p>{error || t("copy.components_workflows_StrategyWorkflowPanel.031")}</p><button className={styles.secondaryButton} onClick={() => void load()}>{t("copy.components_workflows_StrategyWorkflowPanel.032")}</button></div>}</section>;
  return <section ref={panelRef} className={embedded ? ui.embeddedPanel : ui.panel} data-testid="strategy-workflow-panel" aria-busy={busy || loading} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") { event.preventDefault(); void reviewChanges(); } }}>
    <header className={ui.header}><div className={ui.title}><h2>{renderTitle ? renderTitle(String(data.manifest.title || strategyId)) : String(data.manifest.title || strategyId)}</h2>{data.manifest.description ? <p>{String(data.manifest.description)}</p> : null}</div><div className={ui.headerActions}><ModePill mode={String(data.manifest.mode || "paper")} /><span className={ui.state}>{stateLabel(data.source.state, t)}</span>{headerActions}</div></header>
    <StrategyVersionContext workflow={data} dirty={dirty} />
    {conflictingDraft && <div role="alert" className="px-5 py-3 text-xs text-warn"><p>{zh ? "有基于旧版本的本地草稿，未自动覆盖最新配置。可展开查看后手动合并。" : "A local draft uses an older revision. It was not applied over the latest configuration; inspect it and merge deliberately."}</p><details><summary>{zh ? "查看旧草稿" : "Inspect old draft"}</summary><pre className="max-h-52 overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(conflictingDraft,null,2)}</pre></details><button type="button" onClick={() => { writeEditDraft(draftKey(data),null); setConflictingDraft(null); }}>{zh ? "放弃旧草稿" : "Discard old draft"}</button></div>}
    {asObject(data.manifest.runtime).mode === "continuous" && <ContinuousStrategyStatus key={`${strategyId}:${data.source.proposal_id || "active"}`} strategyId={strategyId} proposalId={data.source.proposal_id} dirty={dirty} />}
    <div className={ui.toolbar}><div className={ui.tabs} role="tablist" aria-label={t("copy.components_workflows_StrategyWorkflowPanel.033")}>
      <button type="button" role="tab" aria-selected={view === "strategy"} onClick={() => void switchView("strategy")}>{t("copy.components_workflows_StrategyWorkflowPanel.034")}</button>
      <button type="button" role="tab" aria-selected={view === "runs"} onClick={() => void switchView("runs")}>{t("copy.components_workflows_StrategyWorkflowPanel.035")}</button>
      <button type="button" role="tab" aria-selected={view === "evolution"} onClick={() => void switchView("evolution")}>{t("copy.components_workflows_StrategyWorkflowPanel.036")}</button>
    </div><span className={ui.spacer} />
      <div className={ui.actionGroup} role="group" aria-label={wx("strategyActions")}>
      <button type="button" className={ui.quietButton} disabled={!editable} data-testid="edit-strategy" onClick={async () => {
        if (adding && !await closeAdd()) return;
        const root = data.strategy.nodes.find((item) => item.kind === "strategy");
        if (root) { await switchView("strategy"); setSelected(root.id); setInspecting(true); setSelectedEdge(null); }
      }}><WorkflowIcon kind="strategy" size={15} />{wx("strategySettings")}</button>
      <StrategyExportButton strategyId={strategyId} proposalId={data.source.proposal_id} revision={data.revision} disabled={busy || loading || !data.can_edit} dirty={dirty} />
      <button type="button" className={`${ui.quietButton} ${ui.agentEditButton}`} disabled={!editable || dirty} title={dirty ? wx("saveBeforeAgent") : wx("editContextHint")} onClick={() => askAgent(undefined)} data-testid="edit-strategy-chat"><WorkflowIcon kind="agent" size={15} />{wx("editWithAgent")}</button>
      </div>
      {view === "runs" && <button type="button" className={ui.quietButton} disabled={dirty || adding || busy || loading} onClick={() => setCheckOpen(true)}>{t("copy.components_workflows_StrategyWorkflowPanel.038")}</button>}
      {view !== "runs" && !reviewLogsOpen && <ChoiceSelect className={ui.quietButton} aria-label={t("copy.components_workflows_StrategyWorkflowPanel.039")} value={display} style={{ background: "var(--bg)", minWidth: 0 }} onValueChange={(choiceValue) => { setDisplay(choiceValue as typeof display); setSelectedEdge(null); }}><option value="canvas">{t("copy.components_workflows_StrategyWorkflowPanel.040")}</option><option value="cards">{t("copy.components_workflows_StrategyWorkflowPanel.041")}</option></ChoiceSelect>}
      {view === "strategy" && <button className={ui.iconButton} type="button" disabled={!editable} aria-label={t("copy.components_workflows_StrategyWorkflowPanel.042")} title={t("copy.components_workflows_StrategyWorkflowPanel.043")} onClick={() => { if (adding) { void closeAdd(); return; } setAdditionSeed(null); setAdding(true); setSelected(null); setSelectedEdge(null); }}><NeryaGlyph name="plus" size={18} /></button>}
      {dirty ? <><button className={ui.quietButton} disabled={busy} type="button" onClick={async () => { if (await confirmDiscard(t("copy.components_workflows_StrategyWorkflowPanel.044"))) { writeEditDraft(draftKey(data),null); invalidateReadCache(); await load(); } }}>{t("copy.components_workflows_StrategyWorkflowPanel.045")}</button><button className={ui.reviewButton} type="button" disabled={!editable || adding} onClick={() => void reviewChanges()}>{busy ? t("copy.components_workflows_StrategyWorkflowPanel.046") : t("copy.components_workflows_StrategyWorkflowPanel.047")} · {changedCards || 1}</button></> : <Link className={ui.quietButton} href={data.source.proposal_id ? `/self-evolution?tab=proposals&proposal_id=${encodeURIComponent(data.source.proposal_id)}` : `/strategies/${encodeURIComponent(strategyId)}`}>{t("copy.components_workflows_StrategyWorkflowPanel.048")} <NeryaGlyph name="arrowUpRight" size={16} /></Link>}
    </div>
    {error && <div role="alert" className={styles.errorBanner}>{error}<button onClick={async () => { if (!dirty || await confirmDiscard(t("copy.components_workflows_StrategyWorkflowPanel.049"))) { writeEditDraft(draftKey(data),null); invalidateReadCache(); await load(); } }}>{t("copy.components_workflows_StrategyWorkflowPanel.050")}</button></div>}
    {savedMessage && <div role="status" className={styles.successBanner}>{savedMessage}</div>}
    {!data.can_edit && <p className={styles.notice}>{t("copy.components_workflows_StrategyWorkflowPanel.051")} <Link href={`/strategies/${encodeURIComponent(strategyId)}`}>{t("copy.components_workflows_StrategyWorkflowPanel.052")} <NeryaGlyph name="arrowUpRight" size={16} /></Link></p>}
    {view === "evolution" && <div className={ui.inlineActions} style={{ padding: "12px 30px 0" }}><button type="button" className={ui.quietButton} data-testid="workflow-review-log-toggle" onClick={async () => {
      if (adding && !await closeAdd()) return;
      setInspecting(false); setReviewLogsOpen(!reviewLogsOpen);
      if (!reviewLogsOpen) rememberInvocation("", "evolution");
      else { const url = new URL(window.location.href); url.searchParams.delete("workflow_log"); url.searchParams.delete("workflow_run"); window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash); }
    }}>{wx(reviewLogsOpen ? "reviewWorkflow" : "reviewLogs")}</button><span className={ui.muted}>{t("copy.simpleReview.summary")}</span><WorkflowHelp label={t("copy.components_workflows_StrategyWorkflowPanel.054")}><p>{t("copy.simpleReview.safety")}</p></WorkflowHelp><Link href="/self-evolution?tab=timeline">{t("copy.components_workflows_StrategyWorkflowPanel.056")} <NeryaGlyph name="arrowUpRight" size={16} /></Link></div>}
    {additions.length > 0 && <div className={styles.pendingAdditions}>{additions.map((item, index) => <span key={`${item.kind}:${item.name}:${index}`}><NeryaGlyph name="plus" size={14} /> {item.name}<button disabled={busy} type="button" aria-label={`${t("copy.components_workflows_StrategyWorkflowPanel.057")}: ${item.name}`} onClick={() => setAdditions((items) => items.filter((_, i) => i !== index))}><NeryaGlyph name="x" size={14} /></button></span>)}</div>}
    {view === "evolution" && reviewLogsOpen ? <WorkflowReviewActivity key={strategyId} strategyId={strategyId} /> : view === "runs" ? <WorkflowActivity roleNames={Object.fromEntries(data.strategy.nodes.filter((n) => n.id.startsWith("agent:role/")).map((n) => [String(asObject(n.config).name), cardTitle(n, t)]))} key={`${strategyId}:${data.source.proposal_id || "active"}`} strategyId={strategyId} proposalId={data.source.proposal_id} onEdit={(kind) => { const target = data.strategy.nodes.find((n) => n.kind === kind && (kind !== "agent" || n.id === "agent:runtime")) || data.strategy.nodes.find((n) => n.kind === kind); setView("strategy"); setSelected(target?.id || null); setInspecting(!!target); }} /> : <div className={ui.body}>
      {display === "cards" ? <WorkflowCardGallery key={graph.id} graph={shownGraph || graph} selectedId={selected} onSelect={(item) => void chooseNode(item)} /> : <WorkflowCanvas graph={shownGraph || graph} selectedId={display === "canvas" && selected ? projection?.aliases.get(selected) || selected : selected} onSelect={(item) => void chooseNode(item)} onMove={editable ? (id, position) => annotate(id, { position }) : undefined}
        onEdgeSelect={(item) => { if (adding) return; setSelectedEdge(item.id); setInspecting(true); setSelected(null); }} />}
      {!!projection?.supporting.length && <div className={sourceUi.supports} aria-label={t("copy.components_workflows_StrategyWorkflowPanel.058")}>{projection.supporting.map((item) => <button type="button" key={item.id} data-support-member={item.id} onClick={() => void chooseNode(item.members[0])}><WorkflowIcon kind={item.members[0].kind} size={15} />{item.title}{item.members[0].kind === "risk" ? ` · ${cardFacts(item.members[0], t)}` : item.members[0].kind === "account" ? ` · ${item.members.length}` : ""}</button>)}</div>}
      <WorkflowEditorDialog open={adding || inspecting && !!(node || edge)} title={adding ? t("copy.components_workflows_StrategyWorkflowPanel.059") : node ? cardTitle(node, t) : t("copy.components_workflows_StrategyWorkflowPanel.060")} onClose={() => { if (adding) void closeAdd(); else setInspecting(false); }} focusAfterClose={focusAfterEditor} footer={!adding ? <><button className={ui.quietButton} type="button" data-testid="edit-node-chat" disabled={!editable || !node || dirty} title={dirty ? wx("saveBeforeAgent") : wx("editContextHint")} onClick={() => askAgent(node)}><WorkflowIcon kind="agent" size={15} />{wx("editWithAgent")}</button><span className={ui.spacer} />{dirty && <span className={ui.muted}>{t("copy.components_workflows_StrategyWorkflowPanel.062")}</span>}<button type="button" className={dirty ? ui.reviewButton : ui.quietButton} disabled={busy} onClick={() => dirty ? reviewFromEditor() : setInspecting(false)}>{dirty ? t("copy.components_workflows_StrategyWorkflowPanel.063") : t("copy.components_workflows_StrategyWorkflowPanel.064")}</button></> : undefined}>
      {node && !adding && <div className={ui.editContext} data-testid="workflow-edit-context" aria-label={wx("editContext")} title={`${wx("editContextHint")} · ${data.revision}`}><WorkflowIcon kind="strategy" size={14} /><code>{data.strategy_id}</code><span>{stateLabel(data.source.state, t)}</span>{data.source.proposal_id && <code>{data.source.proposal_id}</code>}</div>}
      {node && !adding && group && group.members.length > 1 && <div className={sourceUi.groupNav}><label htmlFor="workflow-group-resource">{node.kind === "source" && node.binding.path?.[0] === "data_sources" ? t("copy.components_workflows_StrategyWorkflowPanel.065") : group.title}</label><ChoiceSelect id="workflow-group-resource" aria-label={t("copy.components_workflows_StrategyWorkflowPanel.066")} value={selected || ""} onValueChange={(choiceValue) => setSelected(choiceValue)}>{group.members.map((item) => <option key={item.id} value={item.id}>{cardTitle(item, t)}{item.binding.path?.[0] === "data_sources" ? ` · ${sourceTypeLabel(String(asObject(item.config).capability || "candles"), t)}` : ""}</option>)}</ChoiceSelect></div>}
      {node && !adding && <NodeInspector key={node.id} node={node} displayTitle={node.kind === "source" && group?.members.length ? group.title : undefined} nodes={view === "strategy" ? graph.nodes : data.strategy.nodes} defaults={data.agent_defaults} raw={rawDrafts[node.id] ?? rawValue(node)} writable={editable} onRaw={(value) => setRawDrafts((current) => { const next = { ...current }; const canonical = [...data.strategy.nodes, ...data.evolution.nodes].find((item) => item.id === node.id); if (canonical && rawValue(canonical) === value) delete next[node.id]; else next[node.id] = value; return next; })} onMetadata={(patch) => annotate(node.id, patch)} onClose={() => setInspecting(false)} related={related} onSelectRelated={(id) => { setSelected(id); setInspecting(true); }} onDuplicate={duplicate} onSave={reviewFromEditor} onReset={rawDrafts[node.id] !== undefined || JSON.stringify(metadata.nodes[node.id]) !== JSON.stringify(data.metadata.nodes[node.id]) ? () => resetCard(node.id) : undefined} onRuns={openRuns} dirty={dirty && !adding} busy={busy} />}
      {adding && <AddResource key={additionSeed?.name || "new"} draftScope={draftKey(data)+':addition:'+(additionSeed?.name || 'new')} seed={additionSeed} onDirtyChange={setAddDraftDirty} onClose={() => void closeAdd()} onAdd={(resource) => { setAdditions((current) => [...current, resource]); setAdding(false); setInspecting(false); setAddDraftDirty(false); }} />}
      {edge && <section className={ui.inspector}><header className={ui.inspectorHeader}><h3>{t("copy.components_workflows_StrategyWorkflowPanel.067")}</h3><button className={ui.iconButton} type="button" aria-label={t("copy.components_workflows_StrategyWorkflowPanel.068")} onClick={() => setInspecting(false)}><NeryaGlyph name="x" size={18} /></button></header><div className={ui.inspectorBody}><p className={ui.muted}>{t("copy.components_workflows_StrategyWorkflowPanel.069")}</p><label className={styles.field}>{t("copy.components_workflows_StrategyWorkflowPanel.070")}<input maxLength={160} disabled={!editable} value={edge.label} onChange={(event) => setMetadata((current) => ({ ...current, edges: current.edges.map((item) => item.id === edge.id ? { ...item, label: event.target.value } : item) }))} /></label><button className={ui.quietButton} disabled={!editable} onClick={() => { setMetadata((current) => ({ ...current, edges: current.edges.filter((item) => item.id !== edge.id) })); setSelectedEdge(null); setInspecting(false); }}>{t("copy.components_workflows_StrategyWorkflowPanel.071")}</button></div></section>}
      </WorkflowEditorDialog>
    </div>}
    {checkOpen && <WorkflowVerification workflow={data} onClose={() => setCheckOpen(false)} onEdit={(where) => {
      const target = [...data.strategy.nodes, ...data.evolution.nodes].find((n) => n.binding.file && (where === n.binding.file || where.startsWith(n.binding.file + ":"))) || data.strategy.nodes.find((n) => n.kind === "strategy");
      setCheckOpen(false); setView("strategy"); setSelected(target?.id || null); setInspecting(!!target);
    }} />}
  </section>;
}

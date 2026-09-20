"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useLocale } from "next-intl";
import type { ChatThread } from "../../lib/chat";
import type { ChatResult } from "../../lib/chatResults";
import { collectPortfolioArtifacts } from "../../lib/portfolioArtifacts";
import { PortfolioSnapshot } from "../finance/PortfolioSnapshot";
import { workbenchFiles, type WorkbenchResource } from "../../lib/canvasWorkbench";
import type { AgentWork } from "./useAgentWork";
import { WorkspaceTabs } from "./WorkspaceTabs";
import { CanvasOverview } from "./CanvasOverview";
import { CanvasFileReview } from "./CanvasFileReview";
import { ChatResultsPanel, type ResultFocusRequest } from "./ChatResultsPanel";
import { ChevronLeftIcon, ChevronRightIcon } from "../icons";
import reading from "./ChatReading.module.css";

const quiet = "flex min-h-9 items-center justify-center rounded px-2 text-xs text-[color:var(--text-muted)] hover:bg-ink-800/30 focus-visible:ring-2 focus-visible:ring-brand-400 disabled:opacity-40";
export function CanvasWorkbench({ thread, results, resources, agents, open, controls, focusRequest, onReveal, onAgent, renderResource, onResourceActive, error }: {
  thread: ChatThread | null; results: ChatResult[]; resources: WorkbenchResource[]; agents?: AgentWork[];
  open: boolean; controls?: ReactNode; focusRequest?: ResultFocusRequest;
  onReveal?: (id: string) => void; onAgent?: (id: string) => void;
  renderResource: (id: string) => ReactNode; onResourceActive: (active: boolean) => void; error?: string;
}) {
  const zh = useLocale().startsWith("zh");
  const [tab, setTab] = useState("overview");
  const [selection, setSelection] = useState("");
  const [resultRequest, setResultRequest] = useState<ResultFocusRequest>();
  const files = useMemo(() => workbenchFiles(resources), [resources]);
  const snapshots = useMemo(() => collectPortfolioArtifacts(thread), [thread]);
  const options = [...resources.map((r) => ({ key: `file:${r.id}`, id: r.id, result: false, label: r.path || r.title })),
    ...snapshots.map((r, index) => ({ key: r.id, id: r.id, result: false, label: `${zh ? "账户快照" : "Account snapshot"} ${index + 1} · ${r.accounts.map((a) => a.id).join(", ")}` })),
    ...results.map((r) => ({ key: `result:${r.id}`, id: r.id, result: true, label: `${r.agentId ? (zh ? `成员 · 第 ${r.attempt} 轮 · ` : `Agent · Run ${r.attempt} · `) : (zh ? "主对话 · " : "Main conversation · ")}${r.title}` }))];
  const selected = options.find((item) => item.key === selection) || options[options.length - 1];
  const index = options.findIndex((item) => item.key === selected?.key);
  const snapshot = snapshots.find((item) => item.id === selected?.key);
  const resource = resources.find((r) => r.id === selected?.id);
  useEffect(() => { onResourceActive(open && tab === "preview" && !selected?.result && resource?.kind === "browser"); }, [open, tab, selected?.key, resource?.kind, onResourceActive]);
  function focusPreview() { requestAnimationFrame(() => document.getElementById("canvas-content-tab-preview")?.focus({ preventScroll: true })); }
  function openResult(id: string) { setSelection(`result:${id}`); setTab("preview"); setResultRequest((old) => ({ id, count: (old?.count || 0) + 1 })); focusPreview(); }
  function openResource(id: string) { setSelection(`file:${id}`); setTab("preview"); focusPreview(); }
  useEffect(() => { if (focusRequest?.id && focusRequest.count) openResult(focusRequest.id); }, [focusRequest?.id, focusRequest?.count]);
  useEffect(() => { if ((tab === 'files' && !files.length) || (tab === 'preview' && !options.length)) setTab('overview'); }, [tab, files.length, options.length]);
  function browse(value: string) { if ((value === 'files' && !files.length) || (value === 'preview' && !options.length)) return; setTab(value); requestAnimationFrame(() => document.getElementById(`canvas-content-tab-${value}`)?.focus({ preventScroll: true })); }
  function select(key: string) { setSelection(key); const r = options.find((item) => item.key === key); if (r?.result) setResultRequest((old) => ({ id: r.id, count: (old?.count || 0) + 1 })); }
  const picker = selected ? <div data-preview-picker>
    <ChoiceSelect aria-label={zh ? "预览内容" : "Preview item"} title={selected.label} value={selected.key} onValueChange={select}>
      <optgroup label={zh ? "最终结果" : "Final results"}>{[...options].reverse().filter((o) => o.result).map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}</optgroup>
      {snapshots.length ? <optgroup label={zh ? "账户快照" : "Account snapshots"}>{options.filter((o) => o.key.startsWith("snapshot:")).map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}</optgroup> : null}
      <optgroup label={zh ? "文件与产物" : "Files and artifacts"}>{options.filter((o) => !o.result && !o.key.startsWith("snapshot:")).map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}</optgroup>
    </ChoiceSelect>
    <button type="button" className={quiet} title={zh ? "上一项" : "Previous item"} aria-label={zh ? "上一项" : "Previous item"} disabled={index <= 0} onClick={() => select(options[index - 1].key)}><ChevronLeftIcon size={14} /></button>
    <span data-preview-counter className="text-[11px] tabular-nums text-[color:var(--text-muted)]">{index + 1}/{options.length}</span>
    <button type="button" className={quiet} title={zh ? "下一项" : "Next item"} aria-label={zh ? "下一项" : "Next item"} disabled={index >= options.length - 1} onClick={() => select(options[index + 1].key)}><ChevronRightIcon size={14} /></button>
  </div> : null;
  return <div id="workspace-canvas" className="flex h-full min-h-0 min-w-0 flex-col" data-testid="canvas-workspace">
    <header className="flex shrink-0 items-center justify-between border-b border-[color:var(--line)] pr-1" data-testid="workbench-navigation">
      <h2 id="canvas-workspace-heading" tabIndex={-1} className="sr-only">{zh ? "任务工作区" : "Task workspace"}</h2>
      <div className="min-w-0 flex-1 [&>div]:border-b-0 [&_button]:px-2">
        <WorkspaceTabs id="canvas-content" label={zh ? "Canvas 内容" : "Canvas content"} value={tab} onChange={setTab}
          tabs={[{ id: "overview", label: zh ? "概览" : "Overview" }, ...(files.length ? [{ id: "files", label: zh ? "文件" : "Files", meta: <span className="text-[11px] text-[color:var(--text-muted)]">{files.length}</span> }] : []), ...(options.length ? [{ id: "preview", label: zh ? "预览" : "Preview" }] : [])]} />
      </div><div className="flex shrink-0 items-center">{controls}</div>
    </header>
    <section id="canvas-content-panel-overview" role="tabpanel" aria-labelledby="canvas-content-tab-overview" hidden={tab !== "overview"} className={tab === "overview" ? "flex min-h-0 flex-1 flex-col" : "hidden"}>
      {snapshots.length ? <div className="flex shrink-0 items-center justify-between gap-2 border-b border-[color:var(--line)] px-4 py-1 text-xs"><span className="text-[color:var(--text-muted)]">{zh ? `${snapshots.length} 份账户快照` : `${snapshots.length} account snapshots`}</span><button type="button" className={quiet} onClick={() => { select(snapshots[snapshots.length - 1].id); browse("preview"); }}>{zh ? "查看本次仓位" : "Review recorded positions"}</button></div> : null}
      <div className="min-h-0 flex-1"><CanvasOverview thread={thread} results={results} files={files} agents={agents} onResult={openResult} onResource={openResource} onAgent={onAgent} onBrowse={browse} onReveal={onReveal} /></div>
    </section>
    <section id="canvas-content-panel-files" role="tabpanel" aria-labelledby="canvas-content-tab-files" hidden={tab !== "files"} className={tab === "files" ? "min-h-0 flex-1" : "hidden"}>
      <CanvasFileReview files={files} onPreview={openResource} />
    </section>
    <section id="canvas-content-panel-preview" role="tabpanel" aria-labelledby="canvas-content-tab-preview" hidden={tab !== "preview"} className={tab === "preview" ? "flex min-h-0 flex-1 flex-col" : "hidden"}>
      <div hidden={Boolean(selected && !selected.result)} className={!selected || selected.result ? "min-h-0 flex-1" : "hidden"}>
        <ChatResultsPanel results={results} selectedId={selected?.result ? selected.id : undefined} hidePicker navigation={picker} focusRequest={resultRequest} onReveal={onReveal} />
      </div>
      {tab === "preview" && selected && !selected.result ? <div className="flex min-h-0 flex-1 flex-col">
        <div className={reading.toolbar} data-testid="resource-toolbar">{picker}{snapshot ? <button type="button" className={`${quiet} shrink-0`} disabled={!onReveal} onClick={() => onReveal?.(snapshot.turnId)}>{zh ? "返回对应对话" : "Show in conversation"}</button> : <button type="button" className={`${quiet} shrink-0`} onClick={() => browse("files")}>{zh ? "返回文件" : "Back to files"}</button>}</div>
        {error && !snapshot ? <p role="status" className="px-4 py-2 text-xs text-warn">{error}</p> : null}
        <div className="min-h-0 flex-1 overflow-auto" data-testid="workbench-resource-preview">{open ? snapshot ? <div className="mx-auto max-w-[1000px] px-4 py-2" data-testid="canvas-portfolio-snapshot"><PortfolioSnapshot key={snapshot.id} accounts={snapshot.accounts} /></div> : renderResource(selected.id) : null}</div>
      </div> : null}
    </section>
  </div>;
}

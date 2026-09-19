"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as Popover from "@radix-ui/react-popover";
import { invalidateReadCache } from "../../lib/clientApi";
import { workflowApi } from "../../lib/workflowApi";
import type { WorkflowSummary, WorkflowView } from "../../lib/workflowTypes";
import { StrategyWorkflowPanel, confirmDiscard } from "./StrategyWorkflowPanel";
import { useWorkflowText } from "./WorkflowCanvas";
import styles from "./WorkflowStudio.module.css";
import ui from "./WorkflowNative.module.css";
import { stateLabel } from "../../lib/workflowPresentation";

export function StrategyWorkflowHub({ onLegacyView }: { onLegacyView?: () => void }) {
  const t = useWorkflowText();
  const [rows, setRows] = useState<WorkflowSummary[]>([]);
  const [selection, setSelection] = useState<{ strategyId: string; proposalId: string | null } | null>(null);
  const [query, setQuery] = useState("");
  const [allVersions, setAllVersions] = useState(false);
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [manageOpen, setManageOpen] = useState(false);
  const [selectionEpoch, setSelectionEpoch] = useState(0);
  const switching = useRef(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [dirty, setDirty] = useState(false);
  const load = useCallback(async () => {
    setError(""); setLoading(true);
    try {
      const result = await workflowApi.list();
      setRows(result.workflows);
      setSelection((current) => {
        if (current) return current;
        const params = new URLSearchParams(window.location.search);
        const requested = params.get("strategy_id");
        if (requested) return { strategyId: requested, proposalId: params.get("proposal_id") };
        const first = result.workflows.find((row) => !row.error);
        return first ? { strategyId: first.strategy_id, proposalId: first.proposal_id } : null;
      });
    } catch (reason) { setError(String(reason)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const strategyId = params.get("strategy_id");
    if (strategyId) setSelection({ strategyId, proposalId: params.get("proposal_id") });
    void load();
  }, [load]);
  const filtered = useMemo(() => rows.filter((row) => `${row.title} ${row.strategy_id} ${row.state} ${row.mode} ${row.markets?.join(" ") || ""}`.toLowerCase().includes(query.toLowerCase())), [query, rows]);
  const visibleRows = allVersions ? filtered : [...new Set(filtered.map((row) => row.strategy_id))].map((id) => filtered.find((row) => row.strategy_id === id && selection?.strategyId === id && row.proposal_id === selection.proposalId) || filtered.find((row) => row.strategy_id === id && !row.proposal_id) || filtered.find((row) => row.strategy_id === id)!);
  const acceptLeave = useCallback(async () => !dirty || confirmDiscard(t("放弃未保存的修改与输入？", "Discard unsaved edits and input?")), [dirty, t]);
  function updateSelection(strategyId: string, proposalId: string | null) {
    setSelection({ strategyId, proposalId }); setDirty(false);
    const url = new URL(window.location.href);
    url.searchParams.set("strategy_id", strategyId);
    if (proposalId) url.searchParams.set("proposal_id", proposalId); else url.searchParams.delete("proposal_id");
    window.history.replaceState(window.history.state, "", url.toString());
  }
  async function select(row: WorkflowSummary) {
    if (switching.current) return;
    setSwitcherOpen(false);
    if (selection?.strategyId === row.strategy_id && selection.proposalId === row.proposal_id) return;
    switching.current = true;
    try { if (await acceptLeave()) { updateSelection(row.strategy_id, row.proposal_id); setSelectionEpoch((n) => n + 1); } }
    finally { switching.current = false; }
  }
  function onSaved(value: WorkflowView) { updateSelection(value.strategy_id, value.source.proposal_id); void load(); }
  const renderTitle = (title: string) => <Popover.Root open={switcherOpen} onOpenChange={(open) => { setSwitcherOpen(open); if (open) { setQuery(""); setManageOpen(false); } }}><Popover.Trigger asChild><button type="button" className={ui.strategyTrigger} aria-label={t("切换策略", "Switch strategy")}><span>{title}</span><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg></button></Popover.Trigger><Popover.Portal><Popover.Content className={ui.switcherMenu} align="start" sideOffset={12} collisionPadding={16} aria-label={t("选择策略", "Choose strategy")} onOpenAutoFocus={(event) => { event.preventDefault(); searchRef.current?.focus(); }}>
    <input ref={searchRef} className={ui.sidebarSearch} type="search" value={query} onChange={(event) => setQuery(event.target.value)} aria-label={t("搜索策略", "Search strategies")} placeholder={t("搜索策略…", "Search strategies…")} onKeyDown={(event) => { if (event.key === "ArrowDown") { event.preventDefault(); event.currentTarget.parentElement?.querySelector<HTMLButtonElement>("[data-strategy-option]")?.focus(); } }} />
    <div className={ui.switcherList} aria-label={t("可选策略", "Available strategies")} onKeyDown={(event) => { if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return; const options = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("[data-strategy-option]")]; const current = options.indexOf(document.activeElement as HTMLButtonElement); const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length; event.preventDefault(); options[next]?.focus(); }}>
      {visibleRows.map((row) => <button key={row.key} type="button" data-strategy-option={row.key} className={ui.strategyOption} title={row.error || row.title} aria-pressed={selection?.strategyId === row.strategy_id && selection.proposalId === row.proposal_id} onClick={() => void select(row)} data-testid={`strategy-card-${row.strategy_id}`}><span><strong>{row.title}</strong><small>{row.error ? t("无法加载", "Unavailable") : `${row.mode.toUpperCase()} · ${stateLabel(row.state, t)}`}{allVersions && row.proposal_id ? ` · ${row.proposal_id}` : ""}</small></span><span aria-hidden="true">{selection?.strategyId === row.strategy_id && selection.proposalId === row.proposal_id ? "✓" : ""}</span></button>)}
      {!visibleRows.length && <p className={ui.muted}>{loading ? t("加载中…", "Loading…") : t("没有匹配的策略", "No matching strategies")}</p>}
    </div><div className={ui.switcherFooter}><label><input type="checkbox" checked={allVersions} onChange={(event) => setAllVersions(event.target.checked)} />{t("包含候选版本", "Include candidates")}</label></div>
  </Popover.Content></Popover.Portal></Popover.Root>;
  const management = <Popover.Root open={manageOpen} onOpenChange={(open) => { setManageOpen(open); if (open) setSwitcherOpen(false); }}><Popover.Trigger asChild><button type="button" className={ui.iconButton} aria-label={t("更多策略操作", "More strategy actions")}>···</button></Popover.Trigger><Popover.Portal><Popover.Content className={ui.manageMenu} align="end" sideOffset={8} collisionPadding={16} aria-label={t("策略操作", "Strategy actions")}>
    <button type="button" disabled={loading || dirty} onClick={() => { setManageOpen(false); invalidateReadCache(); void load(); }}>{t("刷新策略", "Refresh strategies")}</button>{onLegacyView && <button type="button" onClick={async () => { setManageOpen(false); if (await acceptLeave()) onLegacyView(); }}>{t("高级管理", "Administration")}</button>}<Link href="/workflows" onClick={() => setManageOpen(false)}>{t("调度管理", "Schedules")} ↗</Link>
  </Popover.Content></Popover.Portal></Popover.Root>;
  return <div className={ui.hub} data-testid="workflow-native-hub">
    <div className={ui.workspace}>
      {error && <div className={styles.errorBanner} role="alert">{error}<button onClick={() => { invalidateReadCache(); void load(); }}>{t("重试", "Retry")}</button></div>}
      {selection ? <StrategyWorkflowPanel key={`${selection.strategyId}:${selectionEpoch}`} strategyId={selection.strategyId} proposalId={selection.proposalId} onDirtyChange={setDirty} onSaved={onSaved} renderTitle={renderTitle} headerActions={management} /> : <div className={ui.empty}><p>{loading ? t("正在读取策略…", "Loading strategies…") : t("还没有策略", "No strategies yet")}</p>{!loading && <p className={ui.muted}>{t("直接在主 Agent 对话中描述策略，创建后会显示在这里。", "Describe your strategy in the main Agent conversation. Created strategies appear here.")}</p>}</div>}
    </div>
  </div>;
}

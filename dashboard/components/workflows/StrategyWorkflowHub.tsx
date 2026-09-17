"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as Popover from "@radix-ui/react-popover";
import { clientApi, invalidateReadCache, type AccountSummary } from "../../lib/clientApi";
import { setComposeDraftPayload } from "../../lib/composeDraft";
import { workflowApi } from "../../lib/workflowApi";
import type { WorkflowSummary, WorkflowTemplate, WorkflowView } from "../../lib/workflowTypes";
import { StrategyWorkflowPanel, confirmDiscard } from "./StrategyWorkflowPanel";
import { WorkflowIcon, useWorkflowText } from "./WorkflowCanvas";
import styles from "./WorkflowStudio.module.css";
import ui from "./WorkflowNative.module.css";
import { stateLabel } from "../../lib/workflowPresentation";

const TEMPLATES: Array<{ id: WorkflowTemplate; zh: string; en: string; kind: "script" | "agent" | "scheduler" }> = [
  { id: "multi_script", zh: "多脚本", en: "Scripts", kind: "script" },
  { id: "script_agent", zh: "脚本触发 Agent", en: "Script → Agent", kind: "agent" },
  { id: "scheduler_agent", zh: "定时 Agent", en: "Scheduled Agent", kind: "scheduler" },
];

export function StrategyWorkflowHub({ onLegacyView }: { onLegacyView?: () => void }) {
  const t = useWorkflowText();
  const router = useRouter();
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
  const [showTemplates, setShowTemplates] = useState(false);
  const [template, setTemplate] = useState<WorkflowTemplate>("multi_script");
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [account, setAccount] = useState("");
  const [markets, setMarkets] = useState("BINANCE:BTCUSDT");
  const [creating, setCreating] = useState(false);
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
  useEffect(() => {
    if (!showTemplates) return;
    let cancelled = false;
    void clientApi.accountsList().then((out) => {
      if (cancelled) return;
      const paper = out.accounts.filter((item) => item.profile.mode === "paper" && item.profile.status !== "disabled");
      setAccounts(paper); setAccount((value) => value || paper[0]?.profile.id || "");
    }).catch((reason) => { if (!cancelled) setError(String(reason)); });
    return () => { cancelled = true; };
  }, [showTemplates]);
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
    try { if (await acceptLeave()) { updateSelection(row.strategy_id, row.proposal_id); setSelectionEpoch((n) => n + 1); setShowTemplates(false); } }
    finally { switching.current = false; }
  }
  async function create() {
    if (creating || !account.trim() || !markets.trim() || !await acceptLeave()) return;
    setCreating(true); setError("");
    try {
      const out = await workflowApi.template({ template, accounts: [account.trim()], markets: markets.split(/[\s,]+/).filter(Boolean) });
      updateSelection(out.strategy_id, out.proposal_id); setShowTemplates(false); await load();
    } catch (reason) { setError(String(reason)); }
    finally { setCreating(false); }
  }
  async function askAgent() {
    if (!await acceptLeave()) return;
    setComposeDraftPayload({ autoSend: false, attachments: [], text: t(
      "请使用 strategy_author 创建策略。先确认我的市场、账户与目标，再读取已有资源，建立经过校验的待审策略提案。保持 paper 模式，不启用调度或交易。",
      "Use strategy_author to create a strategy. Confirm my markets, accounts and objective, read existing resources, then create a validated review proposal. Keep paper mode; do not activate schedules or trading.") });
    router.push("/chat");
  }
  function onSaved(value: WorkflowView) { updateSelection(value.strategy_id, value.source.proposal_id); void load(); }
  const renderTitle = (title: string) => <Popover.Root open={switcherOpen} onOpenChange={(open) => { setSwitcherOpen(open); if (open) { setQuery(""); setManageOpen(false); } }}><Popover.Trigger asChild><button type="button" className={ui.strategyTrigger} aria-label={t("切换策略", "Switch strategy")}><span>{title}</span><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg></button></Popover.Trigger><Popover.Portal><Popover.Content className={ui.switcherMenu} align="start" sideOffset={12} collisionPadding={16} aria-label={t("选择策略", "Choose strategy")} onOpenAutoFocus={(event) => { event.preventDefault(); searchRef.current?.focus(); }}>
    <input ref={searchRef} className={ui.sidebarSearch} type="search" value={query} onChange={(event) => setQuery(event.target.value)} aria-label={t("搜索策略", "Search strategies")} placeholder={t("搜索策略…", "Search strategies…")} onKeyDown={(event) => { if (event.key === "ArrowDown") { event.preventDefault(); event.currentTarget.parentElement?.querySelector<HTMLButtonElement>("[data-strategy-option]")?.focus(); } }} />
    <div className={ui.switcherList} aria-label={t("可选策略", "Available strategies")} onKeyDown={(event) => { if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return; const options = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("[data-strategy-option]")]; const current = options.indexOf(document.activeElement as HTMLButtonElement); const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length; event.preventDefault(); options[next]?.focus(); }}>
      {visibleRows.map((row) => <button key={row.key} type="button" data-strategy-option={row.key} className={ui.strategyOption} title={row.error || row.title} aria-pressed={selection?.strategyId === row.strategy_id && selection.proposalId === row.proposal_id} onClick={() => void select(row)} data-testid={`strategy-card-${row.strategy_id}`}><span><strong>{row.title}</strong><small>{row.error ? t("无法加载", "Unavailable") : `${row.mode.toUpperCase()} · ${stateLabel(row.state, t)}`}{allVersions && row.proposal_id ? ` · ${row.proposal_id}` : ""}</small></span><span aria-hidden="true">{selection?.strategyId === row.strategy_id && selection.proposalId === row.proposal_id ? "✓" : ""}</span></button>)}
      {!visibleRows.length && <p className={ui.muted}>{loading ? t("加载中…", "Loading…") : t("没有匹配的策略", "No matching strategies")}</p>}
    </div><div className={ui.switcherFooter}><label><input type="checkbox" checked={allVersions} onChange={(event) => setAllVersions(event.target.checked)} />{t("包含候选版本", "Include candidates")}</label><button type="button" className={ui.quietButton} onClick={() => { setSwitcherOpen(false); void askAgent(); }}>+ {t("新建策略", "New strategy")}</button></div>
  </Popover.Content></Popover.Portal></Popover.Root>;
  const management = <Popover.Root open={manageOpen} onOpenChange={(open) => { setManageOpen(open); if (open) setSwitcherOpen(false); }}><Popover.Trigger asChild><button type="button" className={ui.iconButton} aria-label={t("更多策略操作", "More strategy actions")}>···</button></Popover.Trigger><Popover.Portal><Popover.Content className={ui.manageMenu} align="end" sideOffset={8} collisionPadding={16} aria-label={t("策略操作", "Strategy actions")}>
    <button type="button" onClick={() => { setManageOpen(false); void askAgent(); }}>{t("新建策略", "New strategy")}</button><button type="button" disabled={loading || dirty} onClick={() => { setManageOpen(false); invalidateReadCache(); void load(); }}>{t("刷新策略", "Refresh strategies")}</button><button type="button" onClick={() => { setManageOpen(false); setShowTemplates(true); }}>{t("从模板创建", "Start from a template")}</button>{onLegacyView && <button type="button" onClick={async () => { setManageOpen(false); if (await acceptLeave()) onLegacyView(); }}>{t("高级管理", "Administration")}</button>}<Link href="/workflows" onClick={() => setManageOpen(false)}>{t("调度管理", "Schedules")} ↗</Link>
  </Popover.Content></Popover.Portal></Popover.Root>;
  return <div className={ui.hub} data-testid="workflow-native-hub">
    <div className={ui.workspace}>
      {error && <div className={styles.errorBanner} role="alert">{error}<button onClick={() => { invalidateReadCache(); void load(); }}>{t("重试", "Retry")}</button></div>}
      {showTemplates && <section className={styles.templatePanel} aria-label={t("从模板创建", "Start from a template")}><div className={ui.header}><h2>{t("选择起点", "Choose a starting point")}</h2><span className={ui.spacer} /><button type="button" className={ui.iconButton} aria-label={t("关闭模板", "Close templates")} onClick={() => setShowTemplates(false)}>×</button></div><div className={styles.quickChoices}>{TEMPLATES.map((item) => <button type="button" key={item.id} aria-pressed={template === item.id} disabled={creating} onClick={() => setTemplate(item.id)}><WorkflowIcon kind={item.kind} size={16} />{t(item.zh, item.en)}</button>)}</div><form className={styles.templateForm} onSubmit={(event) => { event.preventDefault(); void create(); }}><label className={styles.field}>{t("模拟账户", "Paper account")}{accounts.length ? <ChoiceSelect value={account} onValueChange={(choiceValue) => setAccount(choiceValue)} required disabled={creating}>{accounts.map((item) => <option value={item.profile.id} key={item.profile.id}>{item.profile.id}</option>)}</ChoiceSelect> : <input value={account} onChange={(event) => setAccount(event.target.value)} required disabled={creating} />}</label><label className={styles.field}>{t("市场", "Markets")}<input value={markets} onChange={(event) => setMarkets(event.target.value)} required disabled={creating} /></label><button className={ui.reviewButton} disabled={creating || !account.trim() || !markets.trim()}>{creating ? t("创建中…", "Creating…") : t("创建草稿", "Create draft")}</button></form><p className={ui.muted}>{t("模拟模式；调度与下单默认关闭。", "Paper mode. Schedules and orders start disabled.")}</p></section>}
      {selection ? <StrategyWorkflowPanel key={`${selection.strategyId}:${selectionEpoch}`} strategyId={selection.strategyId} proposalId={selection.proposalId} onDirtyChange={setDirty} onSaved={onSaved} renderTitle={renderTitle} headerActions={management} /> : <div className={ui.empty}><p>{t("你想让策略做什么？", "What should your strategy do?")}</p><button type="button" className={ui.quietButton} onClick={() => void askAgent()}>{t("和 Agent 创建第一个策略", "Create your first strategy with Agent")} →</button></div>}
    </div>
  </div>;
}

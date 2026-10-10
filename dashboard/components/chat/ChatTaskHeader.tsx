"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useRef, useState, type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import {OPEN_CONVERSATION_FIND} from "./ConversationFind";
import {SearchIcon,XIcon} from "../icons";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import * as Menu from "@radix-ui/react-dropdown-menu";
import type { ChatThread } from "../../lib/chat";
import type { ChatResult } from "../../lib/chatResults";
import { toast } from "../../lib/dialogs";
import { AgentsIcon, ChevronDownIcon, ComposeIcon, CopyIcon, FileIcon, PanelLeftIcon, GlobeIcon } from "../icons";
import { ShellNavigationTrigger } from "../shell/ShellNavigationTrigger";
import { ShellNotifications } from "../shell/ShellNotifications";
import { isWorking, type AgentWork } from "./useAgentWork";
import { taskStatus, statusLabel, type TaskStatus, type Connection } from "../../lib/workbench";
import styles from "./ChatTaskHeader.module.css";
import { shortTaskStatus } from "./taskStatusCopy";

export function ChatTaskHeader({ thread, agents, results, sending, approvalCount, loading = false,
  showTabs, tab, canvasVisible, onSelect, onToggleCanvas, onOpenResult, browserVisible = false, onToggleBrowser, workspaceAvailable = false, workStatus, connection,diagnostics, onToggleReports }: {
  diagnostics?:ReactNode;
  workStatus?: TaskStatus; connection?: Connection;
  thread: ChatThread | null; agents: AgentWork[]; results: ChatResult[];
  sending: boolean; approvalCount: number; loading?: boolean; showTabs: boolean;
  tab: string; canvasVisible: boolean; onSelect: (tab: string) => void;
  onToggleCanvas: () => void; onOpenResult: (id: string) => void;
  browserVisible?: boolean; onToggleBrowser?: () => void; workspaceAvailable?: boolean;
  onToggleReports?: () => void;
}) {
  const zh = useLocale().startsWith("zh");
  const tAssistants = useTranslations("assistants");
  const navigating = useRef(false);
  const [showDiagnostics,setShowDiagnostics]=useState(false);
  const state = workStatus || taskStatus(thread);
  const effective = approvalCount ? {...state,waiting_for:"approval" as const} : state;
  const status = loading ? (zh ? "加载中" : "Loading") : effective.waiting_for === "configuration" ? shortTaskStatus(effective,zh) : statusLabel(effective,zh);
  const tone = effective.needs_attention || approvalCount ? "bg-warn" : effective.execution === "running" ? "bg-brand-400 motion-safe:animate-pulse" : "bg-ink-400";
  const title = thread?.title || (loading ? (i18nCopy(zh, "copy.components_chat_ChatTaskHeader.007")) : (i18nCopy(zh, "copy.components_chat_ChatTaskHeader.008")));
  const latest = results.findLast((result) => !result.agentId) || [...results].sort((a, b) => b.ts - a.ts)[0];
  const canNavigate = showTabs && Boolean(thread?.id) && !loading;
  const toggleLabel = canvasVisible ? (i18nCopy(zh, "copy.components_chat_ChatTaskHeader.009")) : (i18nCopy(zh, "copy.components_chat_ChatTaskHeader.010"));

  const compactStatus=loading?status:shortTaskStatus(effective,zh);

  async function copyLink() {
    if (!thread?.id) return;
    try {
      const url = new URL(`/chat/${encodeURIComponent(thread.id)}`, window.location.origin);
      await navigator.clipboard.writeText(url.href);
      toast({ message: i18nCopy(zh, "copy.components_chat_ChatTaskHeader.011"), tone: "ok" });
    } catch {
      toast({ message: i18nCopy(zh, "copy.components_chat_ChatTaskHeader.012"), tone: "error" });
    }
  }

  return <header className={styles.header} data-has-tabs={showTabs} data-testid="task-topbar">
    <div className={styles.bar}>
      <div className={styles.identity}>
        <ShellNavigationTrigger />
        <h1 className={styles.title}>
          <Menu.Root>
            <Menu.Trigger asChild>
              <button type="button" className={styles.titleButton} aria-label={`${i18nCopy(zh, "copy.components_chat_ChatTaskHeader.013")}: ${title}`} title={title} data-testid="task-title-menu">
                <span className={styles.titleGlyph}><FileIcon size={14} /></span><span className={`${styles.titleText} min-w-0 truncate`}>{title}</span><ChevronDownIcon size={14} className="shrink-0 text-[color:var(--text-muted)]" />
              </button>
            </Menu.Trigger>
            <Menu.Portal>
              <Menu.Content className="ui-select-menu w-80" align="start" sideOffset={6} collisionPadding={8} aria-label={i18nCopy(zh, "copy.components_chat_ChatTaskHeader.014")}
                onCloseAutoFocus={(event) => { if (navigating.current) { event.preventDefault(); navigating.current = false; } }}>
                <Menu.Label className="px-3 py-2">
                  <span className={`block text-sm font-medium ${styles.menuTitle}`}>{title}</span>
                  <span className="mt-2 flex items-center gap-2 text-xs text-[color:var(--text-muted)]"><span aria-hidden className={`h-1.5 w-1.5 rounded-full ${tone}`} />{status}</span>
                </Menu.Label>
                {state.external&&<Menu.Label className="px-3 py-2 text-xs leading-5 text-[color:var(--text-muted)]">{thread?.source?.toUpperCase()} · {zh?"外部会话。追加消息交给外部 Agent，不启动本地模型。":"External session. Follow-ups go to the external Agent without starting a local model."}</Menu.Label>}
                <Menu.Separator className="my-1 h-px bg-[color:var(--line)]" />
                <Menu.Item className="ui-select-option" disabled={!thread?.messages.length} onSelect={() => { navigating.current=true;window.dispatchEvent(new Event(OPEN_CONVERSATION_FIND)); }}><SearchIcon size={15}/><span className="flex-1">{zh?"在会话中查找":"Find in conversation"}</span><span className="text-xs text-[color:var(--text-muted)]">⌘F</span></Menu.Item>
                <Menu.Item className="ui-select-option" disabled={!canNavigate || !agents.length} onSelect={() => { navigating.current = true; onSelect("agents"); }}>
                  <AgentsIcon size={15} /><span className="flex-1">{i18nCopy(zh, "copy.components_chat_ChatTaskHeader.015")}</span><span className="text-xs text-[color:var(--text-muted)]">{agents.length}</span>
                </Menu.Item>
                <Menu.Item className="ui-select-option" disabled={!canNavigate || !latest} onSelect={() => { if (latest) { navigating.current = true; onOpenResult(latest.id); } }}>
                  <FileIcon size={15} />{i18nCopy(zh, "copy.components_chat_ChatTaskHeader.016")}
                </Menu.Item>
                {onToggleBrowser && <Menu.Item className="ui-select-option" disabled={!canNavigate} onSelect={() => { navigating.current = true; onToggleBrowser(); }}><GlobeIcon size={15}/>{i18nCopy(zh, "copy.components_chat_ChatTaskHeader.017")}</Menu.Item>}
                <Menu.Item className="ui-select-option" disabled={!canNavigate} onSelect={() => { void copyLink(); }}>
                  <CopyIcon size={15} />{i18nCopy(zh, "copy.components_chat_ChatTaskHeader.018")}
                </Menu.Item>
                <Menu.Separator className="my-1 h-px bg-[color:var(--line)]" />
                {diagnostics&&<Menu.Item className="ui-select-option" onSelect={()=>setShowDiagnostics(true)}>{zh?"运行版本与诊断":"Runtime diagnostics"}</Menu.Item>}
                <Menu.Item asChild className="ui-select-option"><Link href="/chat"><ComposeIcon size={15} />{i18nCopy(zh, "copy.components_chat_ChatTaskHeader.019")}</Link></Menu.Item>
              </Menu.Content>
            </Menu.Portal>
          </Menu.Root>
        </h1>
        {showTabs ? <span className={styles.status} role="status" aria-live="polite" data-testid="task-header-status" data-idle={!loading&&effective.execution==="idle"&&!effective.waiting_for&&connection!=="offline"} title={status}><span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone}`} /><span className={styles.statusLabel}>{compactStatus}{connection === "offline" ? (zh ? " · 状态可能过期" : " · May be out of date") : ""}</span></span> : null}
      </div>
      <div className={styles.actions}>
        {thread?.messages.length ? <button type="button" className={styles.action} aria-label={zh?"在会话中查找":"Find in conversation"} title={zh?"在会话中查找 · ⌘/Ctrl F":"Find in conversation · ⌘/Ctrl F"} onClick={()=>window.dispatchEvent(new Event(OPEN_CONVERSATION_FIND))}><SearchIcon size={15}/></button> : null}
        <Link href="/chat" className={`${styles.action} ${styles.newChat}`} aria-label={i18nCopy(zh, "copy.components_chat_ChatTaskHeader.020")} title={i18nCopy(zh, "copy.components_chat_ChatTaskHeader.021")}><ComposeIcon size={16} /></Link>
        {onToggleReports && <button type="button" id="assistant-results-toggle" data-testid="open-assistant-results" className={styles.action}
          aria-label={tAssistants("reportsAndResults")} title={tAssistants("reportsAndResults")} aria-controls="task-dock-panel-reports"
          aria-expanded={canvasVisible && tab === "reports"} onClick={onToggleReports}>
          <FileIcon size={16}/><span className={styles.actionLabel}>{tAssistants("results")}</span>
        </button>}
        {showTabs && workspaceAvailable ? <button type="button" id="task-workspace-toggle" className={styles.action} data-testid="open-workspace" aria-label={toggleLabel} title={toggleLabel}
          aria-controls="task-workspace" aria-expanded={canvasVisible} onClick={onToggleCanvas} disabled={loading}>
          <PanelLeftIcon size={16} className="rotate-180" /><span className={styles.actionLabel}>{i18nCopy(zh, "copy.components_chat_ChatTaskHeader.022")}</span>
        </button> : null}
        <ShellNotifications />
      </div>
    </div>
    <Dialog.Root open={showDiagnostics} onOpenChange={setShowDiagnostics}><Dialog.Portal><Dialog.Overlay className="ui-modal-overlay"/><Dialog.Content className="ui-dialog" aria-describedby={undefined}><div className="mb-4 flex items-center justify-between gap-4"><Dialog.Title className="text-sm font-semibold">{zh?"运行版本与诊断":"Runtime diagnostics"}</Dialog.Title><Dialog.Close className="ui-icon-button" aria-label={zh?"关闭":"Close"}><XIcon size={16}/></Dialog.Close></div>{diagnostics}</Dialog.Content></Dialog.Portal></Dialog.Root>
  </header>;
}

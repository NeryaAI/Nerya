"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useRef, useState, type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import {OPEN_CONVERSATION_FIND} from "./ConversationFind";
import {SearchIcon,XIcon} from "../icons";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { shortTaskStatus } from "./taskStatusCopy";
import type { ChatThread, TaskDelivery } from "../../lib/chat";
import type { ChatResult } from "../../lib/chatResults";
import type { Connection, TaskStatus } from "../../lib/workbench";
import { toast } from "../../lib/dialogs";
import { AgentsIcon, ChevronDownIcon, ComposeIcon, CopyIcon, FileIcon, PanelLeftIcon, GlobeIcon } from "../icons";
import { ShellNavigationTrigger } from "../shell/ShellNavigationTrigger";
import { ShellNotifications } from "../shell/ShellNotifications";
import type { AgentWork } from "./useAgentWork";
import styles from "./ChatTaskHeader.module.css";

export function ChatTaskHeader({ thread, agents, results, loading = false,
  showTabs, tab, canvasVisible, onSelect, onToggleCanvas, onOpenResult, onToggleBrowser, workspaceAvailable = false, diagnostics, onToggleReports, workStatus, delivery, connection, sending = false, approvalCount = 0 }: {
  diagnostics?:ReactNode;
  thread: ChatThread | null; agents: AgentWork[]; results: ChatResult[];
  loading?: boolean; showTabs: boolean;
  tab: string; canvasVisible: boolean; onSelect: (tab: string) => void;
  onToggleCanvas: () => void; onOpenResult: (id: string) => void;
  onToggleBrowser?: () => void; workspaceAvailable?: boolean;
  onToggleReports?: () => void;
  delivery?: TaskDelivery | null;
  workStatus?: TaskStatus; connection?: Connection;
  sending?: boolean; approvalCount?: number;
}) {
  const zh = useLocale().startsWith("zh");
  const tAssistants = useTranslations("assistants");
  const navigating = useRef(false);
  const [showDiagnostics,setShowDiagnostics]=useState(false);
  const title = thread?.title || (loading ? (i18nCopy(zh, "copy.components_chat_ChatTaskHeader.007")) : (i18nCopy(zh, "copy.components_chat_ChatTaskHeader.008")));
  const latest = results.findLast((result) => !result.agentId) || [...results].sort((a, b) => b.ts - a.ts)[0];
  const canNavigate = showTabs && Boolean(thread?.id) && !loading;
  const toggleLabel = canvasVisible ? (i18nCopy(zh, "copy.components_chat_ChatTaskHeader.009")) : (i18nCopy(zh, "copy.components_chat_ChatTaskHeader.010"));


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
                </Menu.Label>
                <Menu.Separator className="my-1 h-px bg-[color:var(--line)]" />
                <Menu.Item className="ui-select-option" disabled={!thread?.messages.length} onSelect={() => { navigating.current=true;window.dispatchEvent(new Event(OPEN_CONVERSATION_FIND)); }}><SearchIcon size={15}/><span className="flex-1">{i18nCopy(zh, "copy.components_chat_ChatTaskHeader.025")}</span><span className="text-xs text-[color:var(--text-muted)]">⌘F</span></Menu.Item>
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
                {diagnostics&&<Menu.Item className="ui-select-option" onSelect={()=>setShowDiagnostics(true)}>{i18nCopy(zh, "copy.components_chat_ChatTaskHeader.026")}</Menu.Item>}
                <Menu.Item asChild className="ui-select-option"><Link href="/chat"><ComposeIcon size={15} />{i18nCopy(zh, "copy.components_chat_ChatTaskHeader.019")}</Link></Menu.Item>
              </Menu.Content>
            </Menu.Portal>
          </Menu.Root>
        </h1>
      </div>
      <div className={styles.actions}>
        {thread?.messages.length ? <button type="button" className={styles.action} aria-label={i18nCopy(zh, "copy.components_chat_ChatTaskHeader.028")} title={i18nCopy(zh, "copy.components_chat_ChatTaskHeader.029")} onClick={()=>window.dispatchEvent(new Event(OPEN_CONVERSATION_FIND))}><SearchIcon size={15}/></button> : null}
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
    {workStatus?.needs_attention && <span className="px-3 text-xs text-warn">{shortTaskStatus(workStatus, zh)}</span>}
    {delivery && <details className="mx-3 mb-1 min-w-0 text-xs text-[color:var(--text-muted)]" data-testid="delivery-stage-rail">
      <summary className="cursor-pointer">{tAssistants(`deliveryStates.${delivery.completion?.allowed === true
        ? delivery.completion.status === "complete" ? "complete" : "partial" : "incomplete"}`)}</summary>
      <ol className="mt-1 flex flex-wrap gap-x-3 gap-y-1">{delivery.stages.map(stage => <li key={stage.id}
        className="max-w-full break-words" data-current={stage.state}>{stage.label || stage.id}</li>)}</ol>
    </details>}
    <Dialog.Root open={showDiagnostics} onOpenChange={setShowDiagnostics}><Dialog.Portal><Dialog.Overlay className="ui-modal-overlay"/><Dialog.Content className="ui-dialog" aria-describedby={undefined}><div className="mb-4 flex items-center justify-between gap-4"><Dialog.Title className="text-sm font-semibold">{i18nCopy(zh, "copy.components_chat_ChatTaskHeader.030")}</Dialog.Title><Dialog.Close className="ui-icon-button" aria-label={i18nCopy(zh, "copy.components_chat_ChatTaskHeader.031")}><XIcon size={16}/></Dialog.Close></div>{diagnostics}</Dialog.Content></Dialog.Portal></Dialog.Root>
  </header>;
}

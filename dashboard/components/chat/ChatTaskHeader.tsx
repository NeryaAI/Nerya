"use client";

import { useRef } from "react";
import Link from "next/link";
import { useLocale } from "next-intl";
import * as Menu from "@radix-ui/react-dropdown-menu";
import type { ChatThread } from "../../lib/chat";
import type { ChatResult } from "../../lib/chatResults";
import { toast } from "../../lib/dialogs";
import { AgentsIcon, ChevronDownIcon, ComposeIcon, CopyIcon, FileIcon, PanelLeftIcon, GlobeIcon } from "../icons";
import { ShellNavigationTrigger } from "../shell/ShellNavigationTrigger";
import { ShellNotifications } from "../shell/ShellNotifications";
import { isWorking, type AgentWork } from "./useAgentWork";
import styles from "./ChatTaskHeader.module.css";

export function ChatTaskHeader({ thread, agents, results, sending, approvalCount, loading = false,
  showTabs, tab, canvasVisible, onSelect, onToggleCanvas, onOpenResult, browserVisible = false, onToggleBrowser, workspaceAvailable = false }: {
  thread: ChatThread | null; agents: AgentWork[]; results: ChatResult[];
  sending: boolean; approvalCount: number; loading?: boolean; showTabs: boolean;
  tab: string; canvasVisible: boolean; onSelect: (tab: string) => void;
  onToggleCanvas: () => void; onOpenResult: (id: string) => void;
  browserVisible?: boolean; onToggleBrowser?: () => void; workspaceAvailable?: boolean;
}) {
  const zh = useLocale().startsWith("zh");
  const navigating = useRef(false);
  const current = thread?.messages.findLast((message) => message.role === "assistant");
  const running = sending || Boolean(current?.role === "assistant" && current.loading) || agents.some((agent) => isWorking(agent.state));
  const attention = Boolean(current?.role === "assistant" && current.error) || agents.some((agent) => ["failed", "blocked", "timeout", "interrupted"].includes(agent.state));
  const status = approvalCount ? (zh ? `${approvalCount} 项待审批` : `${approvalCount} awaiting approval`)
    : loading ? (zh ? "加载中" : "Loading") : attention ? (zh ? "需要处理" : "Needs attention")
    : running ? (zh ? "进行中" : "Working") : results.length ? (zh ? "结果可查看" : "Results ready") : (zh ? "就绪" : "Ready");
  const tone = approvalCount || attention ? "bg-warn" : running ? "bg-brand-400 motion-safe:animate-pulse" : "bg-ink-400";
  const title = thread?.title || (loading ? (zh ? "正在加载对话" : "Loading conversation") : (zh ? "新对话" : "New conversation"));
  const latest = results.findLast((result) => !result.agentId) || [...results].sort((a, b) => b.ts - a.ts)[0];
  const canNavigate = showTabs && Boolean(thread?.id) && !loading;
  const toggleLabel = canvasVisible ? (zh ? "收起工作区" : "Hide workspace") : (zh ? "打开工作区" : "Open workspace");

  async function copyLink() {
    if (!thread?.id) return;
    try {
      const url = new URL(`/chat/${encodeURIComponent(thread.id)}`, window.location.origin);
      await navigator.clipboard.writeText(url.href);
      toast({ message: zh ? "已复制对话链接，打开时仍需访问权限。" : "Conversation link copied. Access is still required.", tone: "ok" });
    } catch {
      toast({ message: zh ? "无法复制，请从浏览器地址栏复制对话链接。" : "Could not copy. Copy the conversation address from your browser.", tone: "error" });
    }
  }

  return <header className={styles.header} data-has-tabs={showTabs} data-testid="task-topbar">
    <div className={styles.bar}>
      <div className={styles.identity}>
        <ShellNavigationTrigger />
        <h1 className={styles.title}>
          <Menu.Root>
            <Menu.Trigger asChild>
              <button type="button" className={styles.titleButton} aria-label={`${zh ? "任务菜单" : "Task menu"}: ${title}`} title={title} data-testid="task-title-menu">
                <span className={styles.titleGlyph}><FileIcon size={14} /></span><span className={`${styles.titleText} min-w-0 truncate`}>{title}</span><ChevronDownIcon size={14} className="shrink-0 text-[color:var(--text-muted)]" />
              </button>
            </Menu.Trigger>
            <Menu.Portal>
              <Menu.Content className="ui-select-menu w-80" align="start" sideOffset={6} collisionPadding={8} aria-label={zh ? "当前任务" : "Current task"}
                onCloseAutoFocus={(event) => { if (navigating.current) { event.preventDefault(); navigating.current = false; } }}>
                <Menu.Label className="px-3 py-2">
                  <span className={`block text-sm font-medium ${styles.menuTitle}`}>{title}</span>
                  <span className="mt-2 flex items-center gap-2 text-xs text-[color:var(--text-muted)]"><span aria-hidden className={`h-1.5 w-1.5 rounded-full ${tone}`} />{status}</span>
                </Menu.Label>
                <Menu.Separator className="my-1 h-px bg-[color:var(--line)]" />
                <Menu.Item className="ui-select-option" disabled={!canNavigate || !agents.length} onSelect={() => { navigating.current = true; onSelect("agents"); }}>
                  <AgentsIcon size={15} /><span className="flex-1">{zh ? "查看协作成员" : "View collaborators"}</span><span className="text-xs text-[color:var(--text-muted)]">{agents.length}</span>
                </Menu.Item>
                <Menu.Item className="ui-select-option" disabled={!canNavigate || !latest} onSelect={() => { if (latest) { navigating.current = true; onOpenResult(latest.id); } }}>
                  <FileIcon size={15} />{zh ? "查看最新结果" : "View latest result"}
                </Menu.Item>
                {onToggleBrowser && <Menu.Item className="ui-select-option" disabled={!canNavigate} onSelect={() => { navigating.current = true; onToggleBrowser(); }}><GlobeIcon size={15}/>{zh ? '打开浏览器' : 'Open browser'}</Menu.Item>}
                <Menu.Item className="ui-select-option" disabled={!canNavigate} onSelect={() => { void copyLink(); }}>
                  <CopyIcon size={15} />{zh ? "复制对话链接" : "Copy conversation link"}
                </Menu.Item>
                <Menu.Separator className="my-1 h-px bg-[color:var(--line)]" />
                <Menu.Item asChild className="ui-select-option"><Link href="/chat"><ComposeIcon size={15} />{zh ? "新建对话" : "New conversation"}</Link></Menu.Item>
              </Menu.Content>
            </Menu.Portal>
          </Menu.Root>
        </h1>
        {showTabs ? <span className={styles.status} role="status" aria-live="polite" data-testid="task-header-status"><span aria-hidden className={`h-1.5 w-1.5 rounded-full ${tone}`} />{status}</span> : null}
      </div>
      <div className={styles.actions}>
        <Link href="/chat" className={`${styles.action} ${styles.newChat}`} aria-label={zh ? "新建对话" : "New conversation"} title={zh ? "新建对话" : "New conversation"}><ComposeIcon size={16} /></Link>
        {showTabs && workspaceAvailable ? <button type="button" id="task-workspace-toggle" className={styles.action} data-testid="open-workspace" aria-label={toggleLabel} title={toggleLabel}
          aria-controls="task-workspace" aria-expanded={canvasVisible} onClick={onToggleCanvas} disabled={loading}>
          <PanelLeftIcon size={16} className="rotate-180" /><span className={styles.actionLabel}>{zh ? "工作区" : "Workspace"}</span>
        </button> : null}
        <ShellNotifications />
      </div>
    </div>
  </header>;
}

"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { PlusIcon, MoreIcon, FileIcon } from "./icons";
import { ShellNavigationTrigger } from "./shell/ShellNavigationTrigger";
import { AssistantAvatar } from "./NeryaLogo";
import { AssistantSettingsDialog } from "./AssistantSettingsDialog";
import { AssistantOverviewPopover } from "./chat/AssistantOverviewPopover";
import type { useAssistantTasks } from "./chat/useAssistantTasks";
import { assistantsApi, ASSISTANTS_CHANGED, type AssistantProfile } from "../lib/assistants";

export function AssistantChatHeader({sessionId, preferredId, onIdentity, onToggleReports, reportsOpen = false, tasks, lastActiveAt, onOpenReports, onOpenResult}: {
  sessionId?: string; preferredId: string; onIdentity: (assistant: AssistantProfile, kind?: string, parentSessionId?: string) => void;
  onToggleReports?: () => void; reportsOpen?: boolean;
  tasks?: ReturnType<typeof useAssistantTasks>; lastActiveAt?: number; onOpenReports?: () => void; onOpenResult?: (commandId: string) => void;
}) {
  const t = useTranslations("assistants"), router = useRouter();
  const [rows, setRows] = useState<AssistantProfile[]>([]);
  const [current, setCurrent] = useState<AssistantProfile | null>(null);
  const [editor, setEditor] = useState<{profile?: AssistantProfile} | null>(null);
  const [error, setError] = useState("");
  const [taskConversation, setTaskConversation] = useState(false);
  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const [inventory, binding] = await Promise.all([assistantsApi.list(), sessionId ? assistantsApi.session(sessionId) : Promise.resolve(null)]);
        if (!active) return;
        const profile = binding?.assistant || inventory.assistants.find(row => row.id === preferredId) || inventory.assistants.find(row => row.id === "main") || inventory.assistants[0];
        setRows(inventory.assistants.filter(row => row.enabled)); setCurrent(profile); onIdentity(profile, binding?.conversation?.conversation_kind, binding?.conversation?.parent_session_id); setError("");
        setTaskConversation(binding?.conversation?.conversation_kind === "task");
        const edit = new URLSearchParams(window.location.search).get("editAssistant");
        if (edit) setEditor(edit === "new" ? {} : {profile});
      } catch (e) { if (active) setError(e instanceof Error ? e.message : String(e)); }
    }
    void load(); window.addEventListener(ASSISTANTS_CHANGED, load);
    return () => { active = false; window.removeEventListener(ASSISTANTS_CHANGED, load); };
  }, [sessionId, preferredId, onIdentity]);
  const first = rows.find(row => row.id === "main") || rows[0];
  const second = current && current.id !== first?.id ? current : rows.find(row => row.id !== first?.id);
  const visible = [first, second].filter((row): row is AssistantProfile => Boolean(row));
  async function select(profile: AssistantProfile) {
    if (current?.id === profile.id && !taskConversation) return;
    try {
      const home = await assistantsApi.home(profile.id);
      router.push("/chat/" + encodeURIComponent(home.session_id));
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }
  function close() {
    setEditor(null);
    const url = new URL(window.location.href); url.searchParams.delete("editAssistant");
    window.history.replaceState(window.history.state, "", url.pathname + url.search);
  }
  return <header className="shrink-0 border-b border-[color:var(--line)] px-4 py-2.5 sm:px-6" data-testid="assistant-chat-header">
    <div className="flex min-w-0 items-center gap-2">
      {onToggleReports && <ShellNavigationTrigger />}
      <div className="flex min-w-0 flex-1 gap-2" aria-label={t("title")}>
        {visible.map(profile => <button type="button" key={profile.id} onClick={() => void select(profile)} aria-pressed={current?.id === profile.id}
          data-testid="assistant-select" className={`${profile.id === current?.id ? "flex" : "hidden sm:flex"} min-h-11 min-w-0 items-center gap-2.5 rounded-lg px-2.5 text-left text-sm transition-colors ${current?.id === profile.id ? "bg-[color:var(--panel-bg)] text-[color:var(--text-base)]" : "text-[color:var(--text-muted)] hover:bg-[color:var(--panel-bg)]"}`}>
          <AssistantAvatar profile={profile} size={32} />
          <span className="min-w-0"><span className="block truncate font-medium">{profile.name}</span><span className="block truncate text-xs text-[color:var(--text-muted)]">{taskConversation && current?.id === profile.id ? t("backToAssistant") : profile.role || t("general")}</span></span>
        </button>)}
      </div>
      {onToggleReports && <button type="button" id="assistant-results-toggle" data-testid="open-assistant-results"
        className="flex min-h-11 shrink-0 items-center gap-2 rounded-lg px-3 text-sm text-[color:var(--text-muted)] hover:bg-[color:var(--panel-bg)]"
        aria-label={t("reportsAndResults")} title={t("reportsAndResults")} aria-controls="task-dock-panel-reports" aria-expanded={reportsOpen}
        onClick={onToggleReports}><FileIcon size={17}/><span className="hidden sm:inline">{t("results")}</span></button>}
      {current && tasks && onOpenReports && onOpenResult && <AssistantOverviewPopover assistant={current} tasks={tasks} lastActiveAt={lastActiveAt}
        onEdit={() => setEditor({profile: current})} onOpenReports={onOpenReports} onOpenResult={onOpenResult}/>}
      {rows.length < 2 && <button type="button" className="ui-icon-button hidden sm:inline-flex" aria-label={t("create")} onClick={() => setEditor({})}><PlusIcon size={17} /></button>}
      <button type="button" className="ui-icon-button" aria-label={t("edit")} disabled={!current} onClick={() => setEditor({profile: current!})}><MoreIcon size={17} /></button>
    </div>
    {error && <p role="alert" className="mt-2 text-xs text-danger">{error}</p>}
    {current && !current.enabled && <p role="status" className="mt-2 text-xs text-warn">{t("disabled")}</p>}
    {editor && <AssistantSettingsDialog current={editor.profile} onClose={close} onSaved={profile => { const updating = editor.profile?.id === profile.id; close(); if (!updating) router.push("/chat?assistant=" + encodeURIComponent(profile.id)); }} />}
  </header>;
}

"use client";
import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { TaskList } from "../chat/TaskList";
import { MoreIcon, PlusIcon } from "../icons";
import { AssistantAvatar } from "../NeryaLogo";
import { AssistantSettingsDialog } from "../AssistantSettingsDialog";
import { assistantsApi, ASSISTANTS_CHANGED, type AssistantProfile } from "../../lib/assistants";

export function AssistantSidebar() { return <Suspense><AssistantNavigation /></Suspense>; }

function AssistantNavigation() {
  const t = useTranslations("assistants"), pathname = usePathname(), query = useSearchParams();
  const [rows, setRows] = useState<AssistantProfile[]>([]);
  const [selected, setSelected] = useState("main");
  const [editor, setEditor] = useState<AssistantProfile | null>(null);
  const preferred = query?.get("assistant") || "main";
  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const sid = pathname?.startsWith("/chat/") ? decodeURIComponent(pathname.split("/")[2]) : "";
        const [data, binding] = await Promise.all([assistantsApi.list(), sid ? assistantsApi.session(sid) : Promise.resolve(null)]);
        if (!active) return;
        setRows(data.assistants.filter(row => row.enabled)); setSelected(binding?.assistant?.id || preferred);
      } catch { if (active) setRows([]); }
    }
    void load(); window.addEventListener(ASSISTANTS_CHANGED, load);
    return () => { active = false; window.removeEventListener(ASSISTANTS_CHANGED, load); };
  }, [pathname, preferred]);
  const first = rows.find(row => row.id === "main") || rows[0];
  const second = rows.find(row => row.id === selected && row.id !== first?.id) || rows.find(row => row.id !== first?.id);
  return <div data-testid="assistant-navigation">
    <section className="mb-6" aria-label={t("title")} data-testid="sidebar-assistants">
      <div className="flex items-center justify-between px-3 py-2 text-xs font-medium text-[color:var(--text-muted)]"><span>{t("title")}</span>{rows.length < 2 && <Link className="ui-icon-button" href="/chat?assistant=main&editAssistant=new" aria-label={t("create")}><PlusIcon size={14}/></Link>}</div>
      <div className="grid gap-[3px]">
      {[first, second].filter((row): row is AssistantProfile => Boolean(row)).map(row => <div key={row.id} className="group flex items-center gap-1">
        <Link href={"/chat?assistant=" + encodeURIComponent(row.id)} aria-current={selected === row.id ? "page" : undefined}
          className={`flex min-h-11 min-w-0 flex-1 items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm transition-colors ${selected === row.id ? "bg-[color:var(--panel-bg)] text-[color:var(--text-base)]" : "text-[color:var(--text-muted)] hover:bg-[color:var(--panel-bg)]"}`}>
          <AssistantAvatar profile={row}/><span className="truncate">{row.name}</span>
        </Link>
        <button type="button" onClick={()=>setEditor(row)} className="ui-icon-button opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100" aria-label={t("editNamed", {name: row.name})}><MoreIcon size={15}/></button>
      </div>)}
      </div>
    </section>
    <TaskList assistantId={selected}/>
    {editor && <AssistantSettingsDialog current={editor} onClose={()=>setEditor(null)} onSaved={()=>setEditor(null)}/>}
  </div>;
}

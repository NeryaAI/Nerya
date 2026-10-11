"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { ConversationActions } from "./ChatHistoryActions";
import Link from "next/link";
import { useEffect,useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { usePathname } from "next/navigation";
import styles from "./WorkbenchChrome.module.css";
import { callApi } from "../../lib/clientApi";
import { taskEntryTitle } from "../../lib/workbench";

type Entry={session_id:string;title?:string;meta?:{title?:string}};
export function TaskList({assistantId}: {assistantId?: string} = {}){
  const tAssistants=useTranslations("assistants");
  const zh=useLocale().startsWith("zh"),pathname=usePathname();
  const [expanded,setExpanded]=useState(false);
  const [rows,setRows]=useState<Entry[]>([]),[error,setError]=useState(false),[loading,setLoading]=useState(true);
  useEffect(()=>{
    let stopped=false;let timer:ReturnType<typeof setTimeout>;const controller=new AbortController();
    async function refresh(){try{
      const params=new URLSearchParams({limit:"50"});
      const result=await callApi<{sessions:Entry[]}>(assistantId ? "/assistants/conversations?kind=task&assistant_id="+encodeURIComponent(assistantId) : "/agent/sessions?"+params,{signal:controller.signal});
      if(!Array.isArray(result.sessions))throw new Error("invalid_sessions");
      if(!stopped){setRows(result.sessions.map(row=>({...row,title:taskEntryTitle(row)})));setError(false);}
    }catch{if(!stopped)setError(true);}finally{if(!stopped){setLoading(false);timer=setTimeout(refresh,document.hidden?15000:2000);}}}
    timer=setTimeout(refresh,0);return()=>{stopped=true;controller.abort();clearTimeout(timer);};
  },[assistantId]);
  return <section className={styles.tasks} aria-label={assistantId ? tAssistants("conversations") : i18nCopy(zh, "copy.components_chat_TaskList.005")} data-testid="workbench-tasks">
    <div className={styles.tasksHeader}><span className={styles.tasksHeading}>{assistantId ? tAssistants("taskConversations") : i18nCopy(zh, "copy.components_chat_TaskList.006")}</span>
      {assistantId && <Link href={"/chat?assistant="+encodeURIComponent(assistantId)+"&conversation=task"} className="ui-icon-button" aria-label={tAssistants("newConversation")}>+</Link>}
    </div>
    {error&&<p role="status" className={styles.listHint}>{i18nCopy(zh, "copy.components_chat_TaskList.008")}</p>}
    {!error&&loading&&<p className={styles.listHint}>{i18nCopy(zh, "copy.components_chat_TaskList.009")}</p>}
    {!error&&!loading&&!rows.length&&<p className={styles.listHint}>{i18nCopy(zh, "copy.components_chat_TaskList.010")}</p>}
    {(expanded ? rows : rows.slice(0,Math.max(6,rows.findIndex(row=>pathname==="/chat/"+encodeURIComponent(row.session_id))+1))).map(row=>{const title=row.title||(i18nCopy(zh, "copy.components_chat_TaskList.011"));return <div key={row.session_id} className={"group "+styles.taskRow} data-active={pathname==="/chat/"+row.session_id}>
      <Link href={"/chat/"+encodeURIComponent(row.session_id)} aria-current={pathname==="/chat/"+row.session_id?"page":undefined} className={styles.task} title={title}>
        <span className={styles.taskTitle}>{title}</span>
      </Link><div className={styles.taskActions}><ConversationActions id={row.session_id} title={title}/></div>
    </div>;})}
    {rows.length>6&&<button type="button" className={styles.showMore} aria-expanded={expanded} onClick={()=>setExpanded(value=>!value)}>{expanded?(i18nCopy(zh, "copy.components_chat_TaskList.012")):(i18nCopy(zh, "copy.components_chat_TaskList.013"))}</button>}
  </section>;
}

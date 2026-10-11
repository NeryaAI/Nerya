"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import Link from "next/link";
import {useLocale} from "next-intl";
import type {ChatThread} from "../../lib/chat";
import {useContext} from "react";
import {StrategyDetailContext} from "./StrategyDetailContext";
export function TaskProvenance({thread}:{thread:ChatThread|null}){
 const zh=useLocale().startsWith("zh");
 const details=useContext(StrategyDetailContext);
 if(!thread?.strategy_id)return null;
 const query=new URLSearchParams({strategy_id:thread.strategy_id,session_id:thread.id});
 if(thread.strategy_proposal_id)query.set("proposal_id",thread.strategy_proposal_id);
 return <nav className="mt-3 flex flex-wrap items-center gap-3 border-t border-[color:var(--line)] pt-3 text-xs text-[color:var(--text-muted)]" aria-label={i18nCopy(zh, "copy.components_chat_TaskProvenance.001")}>
  {details?<button type="button" className="min-h-10 py-3 underline" onClick={()=>details.open({kind:"strategy",strategyId:thread.strategy_id!,proposalId:thread.strategy_proposal_id})}>{thread.strategy_id} · {thread.strategy_proposal_id?(i18nCopy(zh, "copy.components_chat_TaskProvenance.002")):(i18nCopy(zh, "copy.components_chat_TaskProvenance.003"))}</button>:<Link className="min-h-10 py-3 underline" href={"/strategies?"+query}>{thread.strategy_id} · {thread.strategy_proposal_id?(i18nCopy(zh, "copy.components_chat_TaskProvenance.004")):(i18nCopy(zh, "copy.components_chat_TaskProvenance.005"))}</Link>}
  <Link className="min-h-10 py-3 underline" href={"/orders?strategy="+encodeURIComponent(thread.strategy_id)}>{i18nCopy(zh, "copy.components_chat_TaskProvenance.006")}</Link>
  <span>{i18nCopy(zh, "copy.components_chat_TaskProvenance.007")}</span>
 </nav>;
}

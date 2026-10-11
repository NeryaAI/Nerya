"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import {useLocale} from "next-intl";
import type {AgentWork} from "./useAgentWork";
export function CollaborationSummary({agents,onOpen}:{agents:AgentWork[];onOpen:()=>void}){
 const zh=useLocale().startsWith("zh");
 if(!agents.length)return null;
 const active=agents.filter(a=>["running","queued"].includes(a.state)),blocked=agents.filter(a=>["failed","blocked","interrupted"].includes(a.state));
 return <details className="mx-auto w-full max-w-[860px] px-4 py-2 text-xs" data-testid="collaboration-summary">
  <summary className="min-h-10 cursor-pointer py-3">{i18nCopy(zh, "copy.components_chat_CollaborationSummary.001")} · {active.length} {i18nCopy(zh, "copy.components_chat_CollaborationSummary.002")} · {blocked.length} {i18nCopy(zh, "copy.components_chat_CollaborationSummary.003")}</summary>
  <ul className="space-y-2">{agents.map(a=><li key={a.id} className="flex flex-wrap gap-2"><strong>{a.name}</strong><span className="min-w-0 flex-1">{a.title}</span><span>{a.state} · {i18nCopy(zh, "copy.components_chat_CollaborationSummary.004")}{a.attempt}{i18nCopy(zh, "copy.components_chat_CollaborationSummary.005")}</span></li>)}</ul>
  <button type="button" className="min-h-11 underline" onClick={onOpen}>{i18nCopy(zh, "copy.components_chat_CollaborationSummary.006")}</button>
  <p className="text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_CollaborationSummary.007")}</p>
 </details>;
}

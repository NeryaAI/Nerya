"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import {useCallback,useEffect,useMemo,useRef,useState,type RefObject} from "react";
import {createPortal} from "react-dom";
import {useLocale} from "next-intl";
import {Icon,SearchIcon,XIcon} from "../icons";
import styles from "./WorkbenchChrome.module.css";
import { conversationMatches, type FindEntry, type TimelineMatch } from "./timelineReveal";

export const OPEN_CONVERSATION_FIND="nerya:find-in-conversation";
export type { FindEntry } from "./timelineReveal";

/** Like ZCode's TaskFindDialog: an optional, nonmodal tool above the reading pane. */
export function ConversationFind({entries,scrollRef,onReveal,onCancelReveal,hasMore,onOlder,loadingOlder,session=""}:{
  entries:FindEntry[];scrollRef:RefObject<HTMLDivElement>;onReveal:(id:string,match:TimelineMatch,signal:AbortSignal)=>void|boolean|Promise<void|boolean>;
  session?:string;
  onCancelReveal?:()=>void;
  hasMore?:boolean;onOlder?:()=>void;loadingOlder?:boolean;
}){
  const zh=useLocale().startsWith("zh");
  const [open,setOpen]=useState(false),[query,setQuery]=useState(""),[index,setIndex]=useState(-1);
  const input=useRef<HTMLInputElement>(null),previousFocus=useRef<HTMLElement|null>(null);
  const hits=useMemo(()=>conversationMatches(entries,query),[entries,query]);
  const pending=useRef<AbortController|null>(null);
  const [locating,setLocating]=useState(false),[missed,setMissed]=useState(false);
  const cancel=useCallback(()=>{pending.current?.abort();pending.current=null;onCancelReveal?.();setLocating(false);setMissed(false);},[onCancelReveal]);
  useEffect(()=>{cancel();setIndex(-1);},[query,session,cancel]);
  useEffect(()=>()=>{pending.current?.abort();},[]);
  const close=useCallback(()=>{cancel();setOpen(false);if(previousFocus.current?.isConnected)previousFocus.current.focus({preventScroll:true});},[cancel]);
  useEffect(()=>{
    const show=()=>{previousFocus.current=document.activeElement instanceof HTMLElement?document.activeElement:null;setOpen(true);requestAnimationFrame(()=>{input.current?.focus();input.current?.select();});};
    const key=(event:KeyboardEvent)=>{
      if(event.isComposing)return;
      if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==="f"&&!document.querySelector('[aria-modal="true"]')){event.preventDefault();show();}
      if(event.key==="Escape"&&open){event.preventDefault();close();}
    };
    window.addEventListener(OPEN_CONVERSATION_FIND,show);window.addEventListener("keydown",key);
    return()=>{window.removeEventListener(OPEN_CONVERSATION_FIND,show);window.removeEventListener("keydown",key);};
  },[open,close]);
  const move=async(delta:number)=>{
    if(!hits.length)return;
    cancel();const controller=new AbortController();pending.current=controller;
    const next=index<0?(delta<0?hits.length-1:0):(index+delta+hits.length)%hits.length;
    setIndex(next);setLocating(true);
    try { const found=await onReveal(hits[next].id,hits[next],controller.signal);if(!controller.signal.aborted)setMissed(found===false); }
    catch { if(!controller.signal.aborted)setMissed(true); }
    finally { if(!controller.signal.aborted){setLocating(false);input.current?.focus({preventScroll:true});} }
  };
  const host=scrollRef.current?.parentElement;
  if(!open||!host)return null;
  return createPortal(<section className={styles.find} role="dialog" aria-modal="false" aria-label={i18nCopy(zh, "copy.components_chat_ConversationFind.001")} data-testid="conversation-find">
    <div className={styles.findRow}>
      <SearchIcon size={15}/><input ref={input} autoFocus type="search" aria-label={i18nCopy(zh, "copy.components_chat_ConversationFind.002")} placeholder={i18nCopy(zh, "copy.components_chat_ConversationFind.003")} value={query}
        onChange={event=>{cancel();setQuery(event.target.value);setIndex(-1);}}
        onKeyDown={event=>{if(event.nativeEvent.isComposing)return;if(event.key==="Enter"){event.preventDefault();if(event.shiftKey)move(-1);else move(1);}}}/>
      <span className={styles.findCount} aria-live="polite">{hits.length&&index>=0?Math.min(index+1,hits.length):0}/{hits.length}</span>
      <button type="button" disabled={!hits.length} onClick={()=>move(-1)} aria-label={i18nCopy(zh, "copy.components_chat_ConversationFind.004")} title={i18nCopy(zh, "copy.components_chat_ConversationFind.005")}><Icon name="arrowDown" className="rotate-180" size={14}/></button>
      <button type="button" disabled={!hits.length} onClick={()=>move(1)} aria-label={i18nCopy(zh, "copy.components_chat_ConversationFind.006")} title={i18nCopy(zh, "copy.components_chat_ConversationFind.007")}><Icon name="arrowDown" size={14}/></button>
      <button type="button" onClick={close} aria-label={i18nCopy(zh, "copy.components_chat_ConversationFind.008")} title="Esc"><XIcon size={15}/></button>
    </div>
    {(locating||missed)&&<p className={styles.findFooter} role="status">{locating?(i18nCopy(zh, "copy.components_chat_ConversationFind.009")):(i18nCopy(zh, "copy.components_chat_ConversationFind.010"))}</p>}
    {query&&hasMore&&<div className={styles.findFooter}><span>{i18nCopy(zh, "copy.components_chat_ConversationFind.011")}</span><button type="button" disabled={loadingOlder} onClick={onOlder}>{i18nCopy(zh, "copy.components_chat_ConversationFind.012")}</button></div>}
  </section>,host);
}

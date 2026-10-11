"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { useLocale } from "next-intl";
import { ConversationFind } from "./ConversationFind";
import type { ChatMessage } from "../../lib/chat";
import { liveEventsToBlocks } from "../../lib/chat";
import { finalReplyText, publicReplyText } from "../../lib/chatResults";
import { useTimelineReveal } from "./timelineReveal";

type Unit={id:string;messages:ChatMessage[];live:boolean};
const heights=new Map<string,number>();
const anchors=new Map<string,{id:string;offset:number}>();
export function hasReadingAnchor(session:string){return anchors.has(session);}
export function clearReadingAnchor(session:string){anchors.delete(session);}


/** Each stable turn keeps a measured placeholder; only nearby content mounts. */
export const TurnWindow=memo(function TurnWindow({unit,root,children,session,forced=false}:{unit:{id:string;live:boolean};root:RefObject<HTMLDivElement>;children:ReactNode;session:string;forced?:boolean}){
  const ref=useRef<HTMLDivElement>(null),[visible,setVisible]=useState(unit.live);
  const key=session+":"+unit.id;
  const [height,setHeight]=useState(heights.get(key)||280);
  useEffect(()=>{
    const el=ref.current;if(!el)return;
    if(typeof IntersectionObserver==="undefined"){setVisible(true);return;}
    const observer=new IntersectionObserver(entries=>setVisible(entries[0].isIntersecting),{root:root.current,rootMargin:"1200px 0px"});
    observer.observe(el);return()=>observer.disconnect();
  },[root,key]);
  useLayoutEffect(()=>{
    const el=ref.current;if(!el||(!visible&&!unit.live&&!forced)||typeof ResizeObserver==="undefined")return;
    const observer=new ResizeObserver(()=>{const size=el.getBoundingClientRect().height;if(size>0){heights.set(key,size);setHeight(size);}});
    observer.observe(el);return()=>observer.disconnect();
  },[visible,unit.live,forced,key]);
  const mounted=visible||unit.live||forced;
  return <div ref={ref} id={"turn-"+encodeURIComponent(unit.id)} data-timeline-turn={unit.id} data-timeline-mounted={mounted?"true":"false"} style={!mounted?{height}:undefined}>
    {mounted?children:null}
  </div>;
});

export function useReadingAnchor(session:string,scopeRef:RefObject<HTMLDivElement>,scrollRef:RefObject<HTMLDivElement>,count:number,reveal:ReturnType<typeof useTimelineReveal>["reveal"]){
  const initialized=useRef("");
  useEffect(()=>{
    const root=scrollRef.current;if(!root||!count)return;
    const controller=new AbortController();
    if(initialized.current!==session){
      initialized.current=session;
      const hashTarget=location.hash.startsWith("#turn-")?decodeURIComponent(location.hash.slice(6)):null;
      const saved=anchors.get(session),target=new URLSearchParams(location.search).get("message")||hashTarget;
      const id=target||saved?.id;
      if(id)void reveal({session,id,focus:Boolean(target)},controller.signal).then(found=>{
        if(!found||controller.signal.aborted||target||!saved)return;
        const node=[...(scopeRef.current?.querySelectorAll<HTMLElement>("[data-timeline-turn]")||[])].find(el=>el.dataset.timelineTurn===id);
        if(node)root.scrollTop+=node.getBoundingClientRect().top-root.getBoundingClientRect().top-saved.offset;
      });
    }
    const save=()=>{
      if(root.scrollHeight-root.scrollTop-root.clientHeight<80){anchors.delete(session);return;}
      const top=root.getBoundingClientRect().top;
      const node=[...(scopeRef.current?.querySelectorAll<HTMLElement>("[data-timeline-turn]")||[])].find(el=>el.getBoundingClientRect().bottom>top);
      if(node)anchors.set(session,{id:node.dataset.timelineTurn!,offset:node.getBoundingClientRect().top-top});
    };
    root.addEventListener("scroll",save,{passive:true});return()=>{controller.abort();root.removeEventListener("scroll",save);};
  },[session,Boolean(count),scopeRef,scrollRef,reveal]);
}

export const ConversationTimeline=memo(function ConversationTimeline({messages,session,scrollRef,renderMessage,hasMore,onOlder,loadingOlder,publicOnly=false}:{
  messages:ChatMessage[];session:string;scrollRef:RefObject<HTMLDivElement>;
  renderMessage:(message:ChatMessage,index:number)=>ReactNode;hasMore?:boolean;onOlder?:()=>void;loadingOlder?:boolean;publicOnly?:boolean;
}){
  const zh=useLocale().startsWith("zh");
  const units=useMemo(()=>{
    const result:Unit[]=[];
    for(const message of messages){
      const id=message.role==="assistant"?message.turn?.turn_id:undefined;
      if(message.role==="assistant"&&result.length&&result.at(-1)!.messages.at(-1)?.role==="user"){
        const unit=result.at(-1)!;unit.id=id||unit.id;unit.messages.push(message);unit.live=!!message.loading;
      }else result.push({id:id||message.backend_message_id||message.id,messages:[message],live:message.role==="assistant"&&!!message.loading});
    }
    return result;
  },[messages]);
  const indexes=useMemo(()=>new Map(messages.map((m,i)=>[m.id,i])),[messages]);
  const scopeRef=useRef<HTMLDivElement>(null);
  const {forced,reveal,cancel}=useTimelineReveal({session,scopeRef,scrollRef,hasMore,loadingOlder,onOlder,
    resolveUnit:id=>units.find(unit=>unit.id===id||unit.messages.some(m=>m.id===id||m.backend_message_id===id))?.id});
  useReadingAnchor(session,scopeRef,scrollRef,units.length,reveal);
  const findEntries=useMemo(()=>messages.map(message=>({id:message.backend_message_id||message.id,text:message.role==="user"?message.text:
    publicOnly ? finalReplyText(message) : publicReplyText(message,message.loading?liveEventsToBlocks(message.live_events||message.turn?.activity_events||[]):message.turn?.blocks||[])})),[messages,publicOnly]);
  return <div key={session} ref={scopeRef} data-timeline-session={session}>
    <ConversationFind key={session} session={session} entries={findEntries} scrollRef={scrollRef} onCancelReveal={cancel} onReveal={(id,match,signal)=>reveal({session,id,match,focus:false},signal)} hasMore={hasMore} onOlder={onOlder} loadingOlder={loadingOlder}/>
    {hasMore&&<div className="flex justify-center pb-4"><button type="button" className="min-h-9 px-3 text-xs text-[color:var(--text-muted)] hover:text-[color:var(--text-base)]" disabled={loadingOlder} onClick={onOlder}>{loadingOlder?(i18nCopy(zh, "copy.components_chat_ConversationTimeline.001")):(i18nCopy(zh, "copy.components_chat_ConversationTimeline.002"))}</button></div>}
    {units.map(unit=><TurnWindow key={unit.messages[0].id} unit={unit} root={scrollRef} session={session} forced={forced===unit.id}>{unit.messages.map(m=><div key={m.id} data-find-entry={m.backend_message_id||m.id}>{renderMessage(m,indexes.get(m.id)!)}</div>)}</TurnWindow>)}
  </div>;
});

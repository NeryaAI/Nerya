"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import {useLocale} from "next-intl";
import {useEffect,useMemo,useRef,useState} from "react";
import type {ToolStep} from "../../lib/agentConversation";
import {toolPresentation} from "../../lib/agentConversation";
import type {NativeBlockEnvelope} from "../../lib/chat";
import {executionEntries,type ExecutionEntry,type ExecutionMember} from "../../lib/executionTimeline";
import {ToolActivity} from "./ReadableExecution";
import {StreamedMarkdown} from "./TurnBlocks";
import {CheckIcon,ChevronRightIcon,SearchIcon,Icon} from "../icons";
import { interactionReceipt, reasoningPreview } from "../../lib/toolOutputPresentation";
import { InteractionReceipt } from "./InteractionReceipt";
import styles from "./ExecutionTimeline.module.css";

function ExploreGroup({steps,state,choices,onChoice}:{steps:ToolStep[];state:string;choices:Record<string,boolean>;onChoice:(key:string,value:boolean)=>void}) {
  const zh=useLocale().startsWith("zh");
  const [choice,setOpen]=useState<boolean|null>(null);
  const open=choice??steps.some(step=>choices[step.key]===true);
  return <details open={open} onToggle={e=>{if(e.currentTarget.open!==open)setOpen(e.currentTarget.open);}} className={styles.group} data-testid="execution-explore-group">
    <summary className={styles.summary}><ChevronRightIcon size={12}/><SearchIcon size={14}/><span>{i18nCopy(zh, "copy.components_chat_ExecutionTimeline.001")}{steps.length}{i18nCopy(zh, "copy.components_chat_ExecutionTimeline.002")}</span><CheckIcon size={13}/></summary>
    {open&&<div className={styles.groupBody}>{steps.map(step=><ToolActivity key={step.key} step={step} state={state} open={choices[step.key]} onOpenChange={value=>onChoice(step.key,value)}/>)}</div>}
  </details>;
}

function ReasoningActivity({entry}:{entry:Extract<ExecutionEntry,{kind:"thinking"|"text"}>}) {
  const zh=useLocale().startsWith("zh"),[open,setOpen]=useState(false);
  const previewRef=useRef<HTMLSpanElement>(null),previewTextRef=useRef<HTMLSpanElement>(null),bodyRef=useRef<HTMLDivElement>(null),follow=useRef(true);
  const [previewOverflow,setPreviewOverflow]=useState(false);
  const text=String(entry.block.text||"");
  const summary=entry.active?reasoningPreview(text):String(entry.block.summary||reasoningPreview(text));
  const duration=typeof entry.block.elapsed_ms==="number"&&entry.block.elapsed_ms>0?Math.max(1,Math.round(entry.block.elapsed_ms/1000)):null;
  const label=entry.active?(i18nCopy(zh, "copy.components_chat_ExecutionTimeline.003")):(i18nCopy(zh, "copy.components_chat_ExecutionTimeline.004"));
  useEffect(()=>{
    const node=previewRef.current;if(!node)return;
    const sync=()=>{
      const overflow=entry.active&&node.scrollWidth>node.clientWidth+1;
      setPreviewOverflow(previous=>previous===overflow?previous:overflow);
      if(entry.active)node.scrollLeft=node.scrollWidth;
    };
    sync();
    if(typeof ResizeObserver==="undefined")return;
    const observer=new ResizeObserver(sync);observer.observe(node);
    if(previewTextRef.current)observer.observe(previewTextRef.current);
    return()=>observer.disconnect();
  },[summary,entry.active,open]);
  useEffect(()=>{
    const node=bodyRef.current;if(!node)return;
    const sync=()=>{if(follow.current)node.scrollTop=node.scrollHeight;};
    sync();
    if(typeof ResizeObserver==="undefined")return;
    // 侧栏展开、字体加载或窗口缩放也会改变高度；不要只依赖 token 更新。
    const observer=new ResizeObserver(sync);observer.observe(node);
    if(node.firstElementChild)observer.observe(node.firstElementChild);
    return()=>observer.disconnect();
  },[open]);
  useEffect(()=>{
    const node=bodyRef.current;
    if(!node){follow.current=true;return;}
    // 只跟随当前思考面板；用户上滚阅读后，新 token 不得抢回滚动位置。
    if(follow.current)node.scrollTop=node.scrollHeight;
  },[text,open]);
  return <details open={open} onToggle={e=>{if(e.currentTarget.open!==open)setOpen(e.currentTarget.open);}}
    className={styles.reasoning} data-turn-section="reasoning" data-testid="reasoning-activity" data-streaming={entry.active} data-stream-id={entry.block.stream_id as string|undefined}>
    <summary className={styles.summary}><ChevronRightIcon size={12}/><Icon name="spark" size={14}/><span className={styles.reasoningLabel}>{label}</span>{duration&&!entry.active&&<span className={styles.duration}>{duration} s</span>}<span ref={previewRef} className={entry.active?styles.reasoningPreview:styles.subject} data-overflow={previewOverflow} data-testid="reasoning-preview"><span ref={previewTextRef} className={styles.reasoningPreviewText}>{open?"":summary}</span></span>{entry.active&&<span className={styles.activeDot} data-testid="reasoning-status-dot" aria-hidden="true"/>}</summary>
    {open&&<div ref={bodyRef} className={styles.reasoningBody} data-testid="reasoning-body" tabIndex={0} onScroll={e=>{const n=e.currentTarget;follow.current=n.scrollHeight-n.clientHeight-n.scrollTop<=4;}}><div className={styles.reasoningText}>{text}</div></div>}
  </details>;
}

function Narration({entry}:{entry:Extract<ExecutionEntry,{kind:"thinking"|"text"}>}) {
  const text=String(entry.block.text||""),receipt=interactionReceipt(text);
  return <div className={styles.narration} data-turn-section="commentary" data-find-text>{receipt?<InteractionReceipt receipt={receipt}/>:<StreamedMarkdown text={text} active={entry.active}/>}</div>;
}

export function ExecutionTimeline({steps,state,members=[],blocks=[]}:{steps:ToolStep[];state:string;members?:ExecutionMember[];blocks?:NativeBlockEnvelope[]}) {
  const zh=useLocale().startsWith("zh");
  const [choices,setChoices]=useState<Record<string,boolean>>({});
  const onChoice=(key:string,value:boolean)=>setChoices(previous=>({...previous,[key]:value}));
  const entries=useMemo(()=>executionEntries(blocks,steps,["running","stopping"].includes(state)),[blocks,steps,state]);
  const groups:ExecutionEntry[][]=[];
  for(const entry of entries){
    const previous=groups.at(-1);
    const explore=(item:ExecutionEntry)=>item.kind==="tool"&&Boolean(item.step.result)&&!toolPresentation(item.step,state,zh).failed&&["read","search"].includes(toolPresentation(item.step,state,zh).family);
    if(explore(entry)&&previous?.every(explore))previous.push(entry);else groups.push([entry]);
  }
  const labels:Record<string,string>={running:i18nCopy(zh, "copy.components_chat_ExecutionTimeline.005"),queued:i18nCopy(zh, "copy.components_chat_ExecutionTimeline.006"),pending:i18nCopy(zh, "copy.components_chat_ExecutionTimeline.007"),planned:i18nCopy(zh, "copy.components_chat_ExecutionTimeline.008"),blocked:i18nCopy(zh, "copy.components_chat_ExecutionTimeline.009"),completed:i18nCopy(zh, "copy.components_chat_ExecutionTimeline.010"),succeeded:i18nCopy(zh, "copy.components_chat_ExecutionTimeline.011"),failed:i18nCopy(zh, "copy.components_chat_ExecutionTimeline.012"),timeout:i18nCopy(zh, "copy.components_chat_ExecutionTimeline.013"),cancelled:i18nCopy(zh, "copy.components_chat_ExecutionTimeline.014"),interrupted:i18nCopy(zh, "copy.components_chat_ExecutionTimeline.015")};
  return <div className={styles.timeline} data-testid="execution-timeline">
    {groups.flatMap(group=>group.length>=3&&group.every(entry=>entry.kind==="tool")?[<ExploreGroup key={group[0].key} steps={group.map(entry=>(entry as Extract<ExecutionEntry,{kind:"tool"}>).step)} state={state} choices={choices} onChoice={onChoice}/>]:group.map(entry=>entry.kind==="tool"?<ToolActivity key={entry.key} step={entry.step} state={state} open={choices[entry.step.key]} onOpenChange={value=>onChoice(entry.step.key,value)}/>:entry.kind==="thinking"?<ReasoningActivity key={entry.key} entry={entry}/>:<Narration key={entry.key} entry={entry}/>))}
    {members.length>0&&<div className={styles.members} aria-label={i18nCopy(zh, "copy.components_chat_ExecutionTimeline.016")}>{members.map(member=><div key={member.id} className={styles.member} data-state={member.status}>
      <span className={styles.memberDot}/><span>{member.name}</span><span className={styles.subject} title={member.task}>{member.task}</span><span className={styles.state}>{labels[member.status]||member.status}</span>
    </div>)}</div>}
  </div>;
}

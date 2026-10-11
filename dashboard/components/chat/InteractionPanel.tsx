"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useRef, useState } from "react";
import { useLocale } from "next-intl";
import { callApi, ApiError } from "../../lib/clientApi";
import type { Interaction } from "../../lib/workbench";
import { readEditDraft, writeEditDraft } from "../../lib/editDrafts";

export function InteractionPanel({items,onResolved}:{items:Interaction[];onResolved:()=>void}) {
  return <div className="mx-auto w-full max-w-[860px] space-y-3 px-4">{items.map(item=><InteractionCard key={item.interaction_id} item={item} onResolved={onResolved}/>)}</div>;
}
type Answer = {text:string;selected:string[]};
type Draft = Answer & {response_id:string;answers?:Record<string,Answer>;index?:number};
function InteractionCard({item,onResolved}:{item:Interaction;onResolved:()=>void}) {
  const zh=useLocale().startsWith("zh");
  const key="interaction:"+item.interaction_id;
  const [draft,setDraft]=useState(()=>readEditDraft<Draft>(key) || {text:"",selected:[],response_id:crypto.randomUUID()});
  const questions=item.payload.questions || [];
  const index=Math.min(draft.index || 0, Math.max(0,questions.length-1));
  const question=questions[index];
  const answer=question ? draft.answers?.[question.id] || {text:"",selected:[]} : null;
  const structured=item.kind==="question" && questions.length>0;
  const [busy,setBusy]=useState(false),[error,setError]=useState("");
  const lock=useRef(false);
  const [expanded,setExpanded]=useState(item.state!=="deferred");
  function change(next:Partial<typeof draft>){const value={...draft,...next};setDraft(value);writeEditDraft(key,value);}
  async function respond(action:"answer"|"accept"|"revise"|"reject"|"defer"|"reconcile", value:Draft=draft) {
    if(lock.current)return;lock.current=true;setBusy(true);setError("");
    try {
      const body=action==="reconcile"
        ? {session_id:item.session_id,interaction_id:item.interaction_id,action}
        : {text:value.text,selected:value.selected,...(structured?{answers:Object.fromEntries(Object.entries(value.answers || {}).filter(([,a])=>a.text.trim()||a.selected.length))}:{}),response_id:value.response_id,session_id:item.session_id,interaction_id:item.interaction_id,expected_revision:item.revision,action};
      const result=await callApi<{ok:boolean;error?:string}>("/agent/interactions/respond",{method:"POST",body});
      if(!result.ok)throw new Error(result.error);
      if(action!=="defer")writeEditDraft(key,null);else {setExpanded(false);change({response_id:crypto.randomUUID()});}
      onResolved();
    } catch(reason) {
      const conflict=reason instanceof ApiError&&reason.status===409;
      setError(conflict?(i18nCopy(zh, "copy.components_chat_InteractionPanel.001")):(i18nCopy(zh, "copy.components_chat_InteractionPanel.002")));
      onResolved();
    } finally {lock.current=false;setBusy(false);}
  }
  function updateAnswer(next:Answer, advance=false) {
    if(!question)return;
    const value={...draft,answers:{...draft.answers,[question.id]:next}};
    setDraft(value);writeEditDraft(key,value);
    if(advance){
      if(index===questions.length-1)void respond("answer",value);
      else {const moved={...value,index:index+1};setDraft(moved);writeEditDraft(key,moved);}
    }
  }
  function continueAnswer(){if(structured&&index<questions.length-1)change({index:index+1});else void respond(item.kind==="plan"?"accept":"answer");}
  if(item.state==="answered")return <section className="rounded-lg border border-[color:var(--line)] bg-[color:var(--card)] p-4" data-testid="user-interaction" aria-labelledby={item.interaction_id}>
    <strong id={item.interaction_id}>{item.payload.title}</strong>
    <p className="mt-2 text-sm text-[color:var(--text-muted)]">{i18nCopy(zh,"copy.components_chat_InteractionPanel.023")}</p>
    <button type="button" className="btn btn-primary mt-3 min-h-11" disabled={busy} onClick={()=>void respond("reconcile")}>{busy?i18nCopy(zh,"copy.components_chat_InteractionPanel.015"):i18nCopy(zh,"copy.components_chat_InteractionPanel.024")}</button>
    {error&&<p role="alert" className="mt-2 text-sm text-danger">{error}</p>}
  </section>;
  return <section className="rounded-lg border border-[color:var(--line)] bg-[color:var(--card)] p-4" data-testid="user-interaction" aria-labelledby={item.interaction_id}>
    <button type="button" className="flex min-h-11 w-full items-center justify-between gap-3 text-left" onClick={()=>setExpanded(!expanded)} aria-expanded={expanded}>
      <strong id={item.interaction_id}>{item.payload.title}</strong><span className="text-xs text-[color:var(--text-muted)]">{item.kind==="plan"?(i18nCopy(zh, "copy.components_chat_InteractionPanel.003")):(i18nCopy(zh, "copy.components_chat_InteractionPanel.004"))}</span>
    </button>
    {expanded&&<form onSubmit={e=>{e.preventDefault();continueAnswer();}} className="space-y-3">
      {item.payload.message&&<p className="whitespace-pre-wrap text-sm">{item.payload.message}</p>}
      {item.payload.steps&&<ol className="list-decimal space-y-1 pl-5 text-sm">{item.payload.steps.map((step,i)=><li key={i}>{step}</li>)}</ol>}
      {item.payload.deliverables?.length ? <p className="text-sm">{i18nCopy(zh, "copy.components_chat_InteractionPanel.005")}{item.payload.deliverables.join(" · ")}</p>:null}
      {item.payload.constraints?.length ? <p className="text-sm">{i18nCopy(zh, "copy.components_chat_InteractionPanel.006")}{item.payload.constraints.join(" · ")}</p>:null}
      {structured&&question&&answer&&<fieldset disabled={busy} className="space-y-3" key={question.id}>
        <legend className="text-base font-medium">{question.question}</legend>
        <div className="flex items-center justify-between text-sm text-[color:var(--text-muted)]">
          <span aria-live="polite">{index+1} / {questions.length}</span>
          <div className="flex gap-2">
            <button type="button" className="btn btn-secondary" disabled={busy||index===0} onClick={()=>change({index:index-1})}>{i18nCopy(zh, "copy.components_chat_InteractionPanel.007")}</button>
            <button type="button" className="btn btn-secondary" disabled={busy||index===questions.length-1} onClick={()=>change({index:index+1})}>{i18nCopy(zh, "copy.components_chat_InteractionPanel.008")}</button>
          </div>
        </div>
        {question.options.map(option=><label key={option} className="flex min-h-11 cursor-pointer items-center gap-3 rounded border border-[color:var(--line)] p-3 hover:bg-[color:var(--panel-bg)]">
          <input type={question.multiple?"checkbox":"radio"} name={item.interaction_id+question.id} checked={answer.selected.includes(option)} onChange={e=>updateAnswer({text:question.multiple?answer.text:"",selected:question.multiple?(e.target.checked?[...answer.selected,option]:answer.selected.filter(v=>v!==option)):[option]},!question.multiple)}/>
          <span>{option}</span>
        </label>)}
        <label className="block text-sm">{question.options.length?(i18nCopy(zh, "copy.components_chat_InteractionPanel.009")):(i18nCopy(zh, "copy.components_chat_InteractionPanel.010"))}
          <textarea className="input-dark mt-1 min-h-20 w-full" value={answer.text} onChange={e=>updateAnswer({text:e.target.value,selected:question.multiple?answer.selected:[]})}/>
        </label>
        <p className="text-xs text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_InteractionPanel.011")}</p>
      </fieldset>}
      {!structured&&item.payload.choices?.map(choice=><label key={choice} className="flex min-h-11 items-center gap-3 rounded px-2 hover:bg-[color:var(--panel-bg)]"><input type={item.payload.multiple?"checkbox":"radio"} name={item.interaction_id} checked={draft.selected.includes(choice)} disabled={busy}
        onChange={e=>change({selected:item.payload.multiple ? e.target.checked?[...draft.selected,choice]:draft.selected.filter(v=>v!==choice):[choice]})}/><span>{choice}</span></label>)}
      {!structured&&<label className="block text-sm">{item.kind==="plan"?(i18nCopy(zh, "copy.components_chat_InteractionPanel.012")):(i18nCopy(zh, "copy.components_chat_InteractionPanel.013"))}<textarea className="input-dark mt-1 min-h-20 w-full" value={draft.text} disabled={busy} onChange={e=>change({text:e.target.value})}/></label>}
      {item.kind==="plan"&&<p className="text-xs text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_InteractionPanel.014")}</p>}
      <div className="flex flex-wrap gap-2"><button type="submit" disabled={busy||(!structured&&item.kind==="question"&&!draft.text.trim()&&!draft.selected.length)} className="btn btn-primary min-h-11">{busy?(i18nCopy(zh, "copy.components_chat_InteractionPanel.015")):item.kind==="plan"?(i18nCopy(zh, "copy.components_chat_InteractionPanel.016")):structured?(index<questions.length-1?(i18nCopy(zh, "copy.components_chat_InteractionPanel.017")):(i18nCopy(zh, "copy.components_chat_InteractionPanel.018"))):(i18nCopy(zh, "copy.components_chat_InteractionPanel.019"))}</button>
      {item.kind==="plan"&&<><button type="button" className="btn btn-secondary min-h-11" disabled={busy||!draft.text.trim()} onClick={()=>void respond("revise")}>{i18nCopy(zh, "copy.components_chat_InteractionPanel.020")}</button><button type="button" className="btn btn-secondary min-h-11" disabled={busy} onClick={()=>void respond("reject")}>{i18nCopy(zh, "copy.components_chat_InteractionPanel.021")}</button></>}
      <button type="button" className="btn btn-secondary min-h-11" disabled={busy} onClick={()=>void respond("defer")}>{i18nCopy(zh, "copy.components_chat_InteractionPanel.022")}</button></div>
    </form>}
    {error&&<p role="alert" className="mt-2 text-sm text-danger">{error}</p>}
  </section>;
}

"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useLocale } from "next-intl";
import { useState } from "react";
import { contentValue, fieldLabel, record, safeLink } from "../../lib/agentConversation";
import type { ToolFamily } from "../../lib/toolSemantics";
import { interactionReceipt, isUnifiedDiff } from "../../lib/toolOutputPresentation";
import { Markdown } from "./Markdown";
import { CodeOutput } from "./CodeOutput";
import { InteractionReceipt } from "./InteractionReceipt";
import { CheckIcon } from "../icons";
import styles from "./ExecutionTimeline.module.css";

/** Small, typed previews. Full transport remains available in the diagnostic disclosure. */
export function ToolResultContent({value,family,path="",depth=0}:{value:unknown;family:ToolFamily;path?:string;depth?:number}) {
  const zh=useLocale().startsWith("zh");
  if(depth>5)return <p className={styles.caption}>{i18nCopy(zh, "copy.components_chat_ToolResultContent.001")}</p>;
  const raw=record(value),structured=raw.structuredContent||raw.structured_content;
  const media=Array.isArray(raw.content)?raw.content.filter(part=>record(part).type!=="text"):[];
  if(structured&&typeof structured==="object"&&media.length) return <>
    <ToolResultContent value={structured} family={family} path={path} depth={depth+1}/>
    <ToolResultContent value={{content:media}} family={family} path={path} depth={depth+1}/>
  </>;
  const decoded=contentValue(value),data=record(decoded);
  if(Array.isArray(data.content)) return <div className={styles.contentParts} data-testid="tool-content-parts">{data.content.slice(0,40).map((part,index)=>{
    const item=record(part),resource=record(item.resource);
    if(item.type==="diff")return <CodeOutput key={index} diff text={String(item.text||item.diff||"")} path={String(record(item.metadata).path||item.path||path)}/>;
    if(item.type==="code")return <CodeOutput key={index} text={String(item.text||"")} path={String(record(item.metadata).path||item.path||path)}/>;
    if(item.type==="json"||item.type==="shell")return <ToolResultContent key={index} value={item.data??item.text} family={item.type==="shell"?"shell":family} path={path} depth={depth+1}/>;
    if(item.type==="text"&&typeof item.text==="string")return <ToolResultContent key={index} value={item.text} family={family} path={path} depth={depth+1}/>;
    const mime=String(item.mimeType||item.mime_type||item.media_type||"");
    if(item.type==="image"&&/^image\/(png|jpeg|webp|gif)$/.test(mime)&&typeof item.data==="string"&&item.data.length<7000000)
      // eslint-disable-next-line @next/next/no-img-element
      return <img key={index} src={`data:${mime};base64,${item.data}`} alt={i18nCopy(zh, "copy.components_chat_ToolResultContent.002")} className={styles.outputImage} loading="lazy"/>;
    if(item.type==="resource"&&typeof resource.text==="string")return <CodeOutput key={index} text={resource.text} path={String(resource.uri||"")}/>;
    return <p key={index} className={styles.caption}>{i18nCopy(zh, "copy.components_chat_ToolResultContent.003")}</p>;
  })}</div>;
  if(typeof data.diff==="string")return <CodeOutput diff text={data.diff} path={String(data.path||path)}/>;
  if(typeof decoded==="string") {
    const receipt=interactionReceipt(decoded);
    if(receipt)return <InteractionReceipt receipt={receipt}/>;
    if(isUnifiedDiff(decoded))return <CodeOutput diff text={decoded} path={path}/>;
    // Transport fragments are not prose. Never hand damaged/compacted JSON to Markdown.
    if(/^\s*(?:\{\s*"|\[\s*\{|"[\w/-]+"\s*:)/.test(decoded)||decoded.includes("[compacted_kept]"))return <p className={styles.caption}>{i18nCopy(zh, "copy.components_chat_ToolResultContent.004")}</p>;
    if(family==="read"&&/\.(?:py|tsx?|jsx?|json|ya?ml|toml|sh|rs|css|sql)(?:$|\?)/i.test(path))return <CodeOutput text={decoded} path={path}/>;
    return <div className={styles.resultBody}><Markdown className={styles.compactMarkdown}>{decoded.slice(0,24000)}</Markdown>{decoded.length>24000&&<p className={styles.caption}>{i18nCopy(zh, "copy.components_chat_ToolResultContent.005")}</p>}</div>;
  }
  if(typeof data.stdout==="string"||typeof data.stderr==="string")return <CodeOutput text={[data.stdout,data.stderr].filter(Boolean).join("\n")} label={typeof data.exit_code==="number"?`${i18nCopy(zh, "copy.components_chat_ToolResultContent.006")} ${data.exit_code}`:undefined}/>;
  const tasks=Array.isArray(data.todos)?data.todos:Array.isArray(data.tasks)?data.tasks:family==="plan"&&Array.isArray(decoded)?decoded:null;
  if(tasks) return <div className={styles.checklist} data-testid="tool-task-checklist">{tasks.slice(0,80).map((task,index)=>{
    const row=record(task),status=String(row.status||"pending"),done=["completed","done","succeeded"].includes(status);
    return <div key={String(row.id||index)} className={styles.checkItem} data-state={status}>
      {done?<CheckIcon size={14}/>:<span className={styles.checkCircle}/>}
      <span>{String(row.content||row.title||row.text||row.description||"")}</span>
      <span className={styles.state}>{done?(i18nCopy(zh, "copy.components_chat_ToolResultContent.007")):status==="in_progress"||status==="running"?(i18nCopy(zh, "copy.components_chat_ToolResultContent.008")):(i18nCopy(zh, "copy.components_chat_ToolResultContent.009"))}</span>
    </div>;
  })}{tasks.length>80&&<p>{i18nCopy(zh, "copy.components_chat_ToolResultContent.010")}</p>}</div>;
  const sources=Array.isArray(data.sources)?data.sources:family==="search"&&Array.isArray(data.results)?data.results:null;
  if(sources?.some(item=>safeLink(record(item).url||record(item).link))) return <div className={styles.sources} data-testid="tool-source-results">
    {sources.slice(0,12).map((item,index)=>{const row=record(item),url=safeLink(row.url||row.link);return <div key={url||index} className={styles.source}>
      {url?<a href={url} target="_blank" rel="noopener noreferrer">{String(row.title||row.name||url)}</a>:<span>{String(row.title||row.name||"")}</span>}
      {row.snippet||row.description?<p>{String(row.snippet||row.description).slice(0,400)}</p>:null}
    </div>;})}
    {sources.length>12&&<p>{i18nCopy(zh, "copy.components_chat_ToolResultContent.011", { value0: sources.length })}</p>}
  </div>;
  const candles=Array.isArray(data.candles)?data.candles:family==="market"&&Array.isArray(data.rows)?data.rows:null;
  if(candles?.length&&candles.every(row=>Array.isArray(row)||"close" in record(row)))return <div className={styles.tableWrap} data-testid="tool-market-preview">
    <p className={styles.caption}>{[data.market||data.symbol,data.timeframe].filter(Boolean).join(" · ")} · {i18nCopy(zh, "copy.components_chat_ToolResultContent.012", { value0: candles.length, value1: Math.min(8,candles.length) })}</p>
    <table><thead><tr>{([i18nCopy(zh, "copy.components_chat_ToolResultContent.013"), i18nCopy(zh, "copy.components_chat_ToolResultContent.014"), i18nCopy(zh, "copy.components_chat_ToolResultContent.015"), i18nCopy(zh, "copy.components_chat_ToolResultContent.016"), i18nCopy(zh, "copy.components_chat_ToolResultContent.017"), i18nCopy(zh, "copy.components_chat_ToolResultContent.018")]).map(label=><th key={label}>{label}</th>)}</tr></thead>
      <tbody>{candles.slice(-8).map((item,index)=>{const row=record(item),cells=Array.isArray(item)?item.slice(0,6):[row.time||row.timestamp||row.ts,row.open,row.high,row.low,row.close,row.volume];return <tr key={index}>{cells.map((cell,i)=><td key={i}>{cell==null?"—":String(cell)}</td>)}</tr>;})}</tbody>
    </table>
  </div>;
  if(decoded==null)return null;
  if(typeof decoded!=="object")return <p>{String(decoded)}</p>;
  return <StructuredResult value={decoded} family={family} depth={depth}/>;
}

const fieldNames:Record<string,string>={strategy_id:"copy.components_chat_ToolResultContent.019",status:"copy.components_chat_ToolResultContent.020",state:"copy.components_chat_ToolResultContent.021",name:"copy.components_chat_ToolResultContent.022",title:"copy.components_chat_ToolResultContent.023",path:"copy.components_chat_ToolResultContent.024",main_path:"copy.components_chat_ToolResultContent.025",strategy_yml_path:"copy.components_chat_ToolResultContent.026",strategy_md_path:"copy.components_chat_ToolResultContent.027",workflow_path:"copy.components_chat_ToolResultContent.028",tests_path:"copy.components_chat_ToolResultContent.029",proposal_id:"copy.components_chat_ToolResultContent.030",next_steps:"copy.components_chat_ToolResultContent.031",files:"copy.components_chat_ToolResultContent.032",bytes_after:"copy.components_chat_ToolResultContent.033",lines_after:"copy.components_chat_ToolResultContent.034",occurrences_replaced:"copy.components_chat_ToolResultContent.035",count:"copy.components_chat_ToolResultContent.036",market:"copy.components_chat_ToolResultContent.037",symbol:"copy.components_chat_ToolResultContent.038",timeframe:"copy.components_chat_ToolResultContent.039",validation:"copy.components_chat_ToolResultContent.040",metrics:"copy.components_chat_ToolResultContent.041"};
const internalFields=new Set(["kind","type","role","call_id","tool_use_id","content_hash","sha256","tokens","usage","next_steps"]);
function StructuredResult({value,family,depth}:{value:unknown;family:ToolFamily;depth:number}) {
  const zh=useLocale().startsWith("zh");
  const data=record(value);
  const proseKeys=["summary","message","answer","markdown","final_text"];
  const prose=proseKeys.filter(key=>typeof data[key]==="string"&&data[key]);
  const entries=Array.isArray(value)?value.slice(0,12).map((item,index)=>[String(index+1),item] as const):Object.entries(data).filter(([key,val])=>!internalFields.has(key)&&!prose.includes(key)&&val!=null&&val!=="");
  const count=Array.isArray(value)?value.length:entries.length;
  return <div className={styles.structuredResult} data-testid="tool-structured-result">
    {prose.map(key=><ToolResultContent key={key} value={data[key]} family={family} depth={depth+1}/>)}
    <dl className={styles.resultFields}>{entries.slice(0,12).map(([key,item])=>{
      const label=i18nCopy(zh, fieldNames[key] ?? "")||fieldLabel(key,zh);
      const simple=typeof item==="string"||typeof item==="number"||typeof item==="boolean";
      const file=simple&&typeof item==="string"&&(key.endsWith("_path")||key==="path");
      return <div key={key}><dt>{label}</dt><dd>{simple?<span className={file?styles.fileValue:undefined} title={file?String(item):undefined}>{file?String(item).split("/").pop():typeof item==="boolean"?(item?(i18nCopy(zh, "copy.components_chat_ToolResultContent.042")):(i18nCopy(zh, "copy.components_chat_ToolResultContent.043"))):String(item)}</span>:<NestedResult value={item} family={family} depth={depth+1}/>}</dd></div>;
    })}</dl>
    {count>12&&<p className={styles.caption}>{i18nCopy(zh, "copy.components_chat_ToolResultContent.044", { value0: count-12 })}</p>}
    {Array.isArray(data.next_steps)&&<details className={styles.nestedResult}><summary>{i18nCopy(zh, "copy.components_chat_ToolResultContent.045")}</summary><ul>{data.next_steps.slice(0,12).map((item,index)=><li key={index}>{typeof item==="string"?item:fieldLabel(String(index+1),zh)}</li>)}</ul></details>}
    {!entries.length&&!prose.length&&<p className={styles.caption}>{i18nCopy(zh, "copy.components_chat_ToolResultContent.046")}</p>}
  </div>;
}

function NestedResult({value,family,depth}:{value:unknown;family:ToolFamily;depth:number}) {
  const zh=useLocale().startsWith("zh"),[open,setOpen]=useState(false);
  return <details className={styles.nestedResult} onToggle={event=>setOpen(event.currentTarget.open)}><summary>{Array.isArray(value)?`${value.length} ${i18nCopy(zh, "copy.components_chat_ToolResultContent.047")}`:`${Object.keys(record(value)).length} ${i18nCopy(zh, "copy.components_chat_ToolResultContent.048")}`}</summary>{open&&<ToolResultContent value={value} family={family} depth={depth}/>}</details>;
}

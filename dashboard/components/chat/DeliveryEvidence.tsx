"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useLocale } from "next-intl";
import type { ChatResult } from "../../lib/chatResults";
import { recordOf } from "../../lib/externalCalls";

export function DeliveryEvidence({result,onOpenFile,onReveal}:{result:ChatResult;onOpenFile?:(path:string)=>void;onReveal?:()=>void}) {
  const zh=useLocale().startsWith("zh"), evidence=result.evidence;
  const files=[...new Set([...(evidence?.artifacts?.created||[]),...(evidence?.artifacts?.modified||[])])];
  const tests=evidence?.artifacts?.tests_run||[];
  const risks=evidence?.artifacts?.unverified_risks||[];
  const errors=evidence?.artifacts?.errors||[];
  const failed=tests.some(raw=>{const t=recordOf(raw);return t.ok===false||(typeof t.exit_code==="number"&&t.exit_code!==0);})||errors.length>0||evidence?.verifier?.hard_status==="failed"||evidence?.verifier?.hard_passed===false;
  return <section aria-label={i18nCopy(zh, "copy.components_chat_DeliveryEvidence.001")} data-testid="delivery-evidence" className="mb-6 space-y-3 border-b border-[color:var(--line)] pb-5 text-sm">
    <h2 className="text-base font-semibold">{i18nCopy(zh, "copy.components_chat_DeliveryEvidence.002")}</h2>
    <p>{failed?(i18nCopy(zh, "copy.components_chat_DeliveryEvidence.003")):evidence?.verifier?.hard_passed===true?(i18nCopy(zh, "copy.components_chat_DeliveryEvidence.004")):(i18nCopy(zh, "copy.components_chat_DeliveryEvidence.005"))}</p>
    {failed&&evidence?.verifier?.hard_passed!==true&&<p className="text-xs text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_DeliveryEvidence.006")}</p>}
    {files.length>0&&<div><h3 className="font-medium">{i18nCopy(zh, "copy.components_chat_DeliveryEvidence.007")}</h3><ul className="mt-1 space-y-1">{files.map(path=><li key={path} className="break-all">{onOpenFile?<button type="button" className="min-h-11 text-left underline" onClick={()=>onOpenFile(path)}>{path}</button>:<code>{path}</code>}</li>)}</ul></div>}
    {tests.length>0&&<div><h3 className="font-medium">{i18nCopy(zh, "copy.components_chat_DeliveryEvidence.008")}</h3><ul className="mt-1 space-y-2">{tests.map((raw,i)=>{const t=recordOf(raw);return <li key={i} className="break-words"><code>{String(t.command||t.cmd||t.action||t.tool||"Validation")}</code><span className="ml-2 text-xs">{typeof t.exit_code==="number"?"exit "+t.exit_code:t.ok===true?(i18nCopy(zh, "copy.components_chat_DeliveryEvidence.009")):t.ok===false?(i18nCopy(zh, "copy.components_chat_DeliveryEvidence.010")):(i18nCopy(zh, "copy.components_chat_DeliveryEvidence.011"))}</span></li>;})}</ul></div>}
    {(risks.length>0||errors.length>0)&&<div><h3 className="font-medium text-warn">{i18nCopy(zh, "copy.components_chat_DeliveryEvidence.012")}</h3><ul className="mt-1 list-disc space-y-1 pl-5">{[...risks,...errors].map((raw,i)=>{const r=recordOf(raw);return <li key={i}>{String(r.message||r.reason||r.kind||r.path||r.error||"Unverified")}</li>;})}</ul></div>}
    {result.turnId&&onReveal&&<button type="button" className="inline-flex min-h-11 items-center text-[color:var(--text-muted)] underline" onClick={onReveal}>{i18nCopy(zh, "copy.components_chat_DeliveryEvidence.013")}</button>}
  </section>;
}

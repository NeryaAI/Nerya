"use client";
import {useTranslations} from "next-intl";
import {runLabel} from '../../lib/taskRuns';
export type ScheduledReceipt={run_id?:string;turn_id?:string;session_id?:string;execution_status?:string;business_status?:string;delivery_status?:string;ttl_exceeded?:boolean;late_outcome?:boolean};
export function ScheduledExecutionReceipt({receipt}:{receipt?:ScheduledReceipt|null}){
 const t=useTranslations("taskRuns");if(!receipt)return null;
 return <div className="mt-4 rounded border border-[color:var(--line)] p-3 text-sm" data-testid="scheduled-execution-receipt">
   <p>{runLabel(receipt.execution_status||'not_started',t)}</p>
   {receipt.business_status&&<p className="mt-1 text-xs">{runLabel(receipt.business_status,t)}</p>}
   <p className="mt-1 text-xs text-[color:var(--text-muted)]">{t("notificationDelivery",{status:runLabel('delivery:'+(receipt.delivery_status||'not_recorded'),t)})}</p>
   {receipt.ttl_exceeded&&<p className="mt-1 text-xs text-warn">{t("deadlineExceeded")}</p>}
   {receipt.late_outcome&&<p className="mt-1 text-xs">{t("lateReceipt")}</p>}
   {receipt.session_id&&<a className="mt-2 inline-flex min-h-11 items-center underline" href={"/chat/"+encodeURIComponent(receipt.session_id)+(receipt.run_id?'?run='+encodeURIComponent(receipt.run_id):receipt.turn_id?"#turn-"+encodeURIComponent(receipt.turn_id):"")}>{t("viewConversation")}</a>}
 </div>;
}

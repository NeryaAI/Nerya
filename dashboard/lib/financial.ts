import {callApi} from './clientApi';
import type {RunOrigin} from './taskRuns';
export type TaskDescriptor={task_kind:RunOrigin;task_id:string;title:string;session_id:string;source_revision:string;security_revision:string;owner_actor_id:string;mode:string};
export type FinancialPolicy={actions:string[];resources:Record<string,string[]>;limits:{single_usd:string;rolling_24h_usd:string;total_usd:string;fee_usd:string;slippage_bps:number;asset_amounts:Record<string,string>;trade_actions?:string[];max_leverage?:string}};
export type FinancialGrant={grant_id:string;revision:number;state:string;expires_at:number;security_revision:string;approved_by?:string;policy:FinancialPolicy;usage?:{total_usd:string;rolling_24h_usd:string;reserved_usd:string;allowance_risk_usd?:string}};
export type FinancialCapability={resource_id:string;resource_type:'account'|'wallet';provider:string;kind:string;supported:boolean;permission_enabled:boolean;ready:boolean;reason?:string;live_verified:boolean};
export const financialApi={
  task:(kind:RunOrigin,id:string)=>callApi<{ok:boolean;task:TaskDescriptor}>('/agent/tasks/descriptor?'+new URLSearchParams({task_kind:kind,task_id:id})),
  grants:(kind:RunOrigin,id:string)=>callApi<{ok:boolean;grants:FinancialGrant[]}>('/financial/grants?'+new URLSearchParams({task_kind:kind,task_id:id})),
  create:(task:TaskDescriptor,policy:FinancialPolicy,hours:number)=>callApi<{ok:boolean;grant:FinancialGrant}>('/financial/grants',{method:'POST',body:{
    task_kind:task.task_kind,task_id:task.task_id,expected_security_revision:task.security_revision,policy,expires_at:Date.now()/1000+hours*3600}}),
  decision:(grant:FinancialGrant,action:'approve'|'revoke')=>callApi<{ok:boolean;grant:FinancialGrant}>(`/financial/grants/${encodeURIComponent(grant.grant_id)}/${action}`,{method:'POST',body:{expected_revision:grant.revision}}),
  capabilities:()=>callApi<{ok:boolean;enabled:boolean;capabilities:FinancialCapability[]}>('/financial/capabilities'),
  resource:(id:string)=>callApi<{ok:boolean;resource:string;revision:string;permissions:Record<string,boolean>;limits:Record<string,string>}>(`/financial/resources/${encodeURIComponent(id)}`),
  updateResource:(id:string,revision:string,permissions:Record<string,boolean>,limits:Record<string,string>)=>callApi(`/financial/resources/${encodeURIComponent(id)}`,{method:'POST',body:{expected_revision:revision,permissions,limits}}),
  reconcileAction:(id:string)=>callApi(`/financial/actions/${encodeURIComponent(id)}/reconcile`,{method:'POST',body:{}}),
};

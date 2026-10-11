import { callApi } from "./clientApi";
import type { TurnPayload } from "./chat";

export type RunOrigin = "strategy_agent" | "scheduled_agent";
export type TaskRun = {
  run_id: string; task_kind: RunOrigin; task_id: string; session_id: string;
  command_id?: string; turn_id?: string; command_revision?: number; source_revision: string;
  admission_status: string; execution_status: string; business_status: string; delivery_status: string;
  trigger_kind: string; scheduled_at?: number; created_at: number; updated_at: number; revision: number;
  reason?: string; elapsed_ms?: number; error?: {code?: string};
  retry_of_run_id?: string;
  queue?:{paused:boolean;pause_reason:string};
  snapshot: {title?: string; mode?: string; budget?: Record<string,unknown>; effective_permissions?: unknown; accepted_model?: unknown};
  result: {status?: string; final_text?: string; effects?: Array<Record<string,unknown>>; turn?: TurnPayload};
};
export type RunPage = {ok: boolean; runs: TaskRun[]; next_cursor?: string | null; error?: string};
export const pendingRun = (run: TaskRun) => ["queued","running","stopping","awaiting_input","awaiting_approval","unconfirmed"].includes(run.execution_status)
  || ["submitted","confirming","unconfirmed","needs_recovery"].includes(run.business_status)
  || ["pending","unconfirmed"].includes(run.delivery_status);
const runStates = new Set(['queued','running','stopping','awaiting_approval','awaiting_input','unconfirmed','succeeded','failed',
  'blocked','interrupted','skipped','reported','no_action','confirmed','submitted','not_started','needs_recovery',
  'historical_unverified','partial','confirming','prepared','submitting','rejected','stopped',
  'running_unconfirmed','completed','needs_approval','cancelled','returned']);
const deliveryStates = new Set(['not_requested','pending','delivered','failed','unconfirmed','not_recorded']);
export function runLabel(state: string, t: (key: string) => string): string {
  if (state.startsWith('delivery:')) {
    const delivery = state.slice('delivery:'.length);
    return deliveryStates.has(delivery) ? t('delivery.' + delivery) : delivery;
  }
  return runStates.has(state) ? t('states.' + state) : state;
}
export const taskRunApi = {
  list: (params: Record<string,string>,signal?:AbortSignal) => callApi<RunPage>("/agent/runs?"+new URLSearchParams(params),{signal}),
  get: (id:string,signal?:AbortSignal) => callApi<{ok:boolean;run:TaskRun}>("/agent/runs/"+encodeURIComponent(id),{signal}),
  control: (run:TaskRun,action:string,extra:Record<string,unknown>={}) => callApi<{ok:boolean;run_id?:string;session_id?:string}>("/agent/runs/"+encodeURIComponent(run.run_id)+"/control",{method:"POST",body:{
    action,expected_revision:run.command_revision||run.revision,client_request_id:crypto.randomUUID(),...extra}}),
  events:(id:string,after=0,signal?:AbortSignal)=>callApi<{ok:boolean;events:Array<Record<string,unknown>>;next_seq:number;has_more:boolean}>(`/agent/runs/${encodeURIComponent(id)}/events?after_seq=${after}`,{signal}),
  create: (kind:RunOrigin,id:string,revision:string,requestId:string) => callApi<{ok:boolean;run_id:string;session_id:string;duplicate?:boolean}>("/agent/runs",{
    method:"POST",body:{task_kind:kind,task_id:id,expected_task_revision:revision,client_request_id:requestId}}),
};

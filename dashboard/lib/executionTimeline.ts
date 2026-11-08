import type { NativeBlock, NativeBlockEnvelope, TurnPayload, LiveEvent } from "./chat";
import { operationIdentity, pairOperations, record, type ToolStep } from "./agentConversation";
import { normalizeTranscriptBlocks } from "./transcriptProjection";

export type ExecutionMember = { id:string; name:string; status:string; task:string };
export function executionSteps(blocks:NativeBlockEnvelope[], turn?:TurnPayload):ToolStep[] {
  const events = new Map<string,{seq:number;ts:number;kind:string;data:Record<string,unknown>}>();
  let seq=0;
  for (const [index,envelope] of blocks.entries()) {
    const block=record(envelope.block||envelope),kind=String(block.kind||envelope.kind||"");
    if(!["tool_use","tool_result"].includes(kind))continue;
    const id=operationIdentity(block),key=id?kind+":"+id:kind+":"+seq;
    const previous=events.get(key);
    events.set(key,{seq:previous?.seq??index,ts:0,kind,data:block});
    seq++;
  }
  // Old turns may only have a persisted tool trace. Do not duplicate modern blocks.
  if(!events.size)for(const [index,entry] of (turn?.tool_trace||[]).entries()) {
    events.set("legacy:"+index,{seq:seq++,ts:0,kind:"tool_result",data:{...entry,action:entry.action||entry.skill_id,call_id:"legacy:"+index}});
  }
  return pairOperations([...events.values()]);
}

export function executionMembers(events:LiveEvent[]):ExecutionMember[] {
  const members=new Map<string,ExecutionMember>();
  for(const e of events) {
    if(!e.kind.startsWith("team.")&&!e.kind.startsWith("subagent."))continue;
    const nested=record(e.event||e.data),name=String(e.subagent||e.role||nested.role||e.name||"");
    if(!name)continue;
    const id=String(e.agent_id||[e.team_run_id||e.group_id||"",e.team_task_id||name].join(":"));
    const previous=members.get(id);
    let status=String(e.status||nested.status||previous?.status||"queued");
    if(e.kind==="subagent.end")status=e.ok===false?"failed":"completed";
    else if(e.kind==="subagent.start")status="running";
    else if(e.kind.endsWith(".timeout"))status="timeout";
    members.set(id,{id,name,status,task:String(e.team_task_subject||e.task||previous?.task||"")});
  }
  return [...members.values()];
}

export function executionArtifacts(blocks:NativeBlockEnvelope[]):NativeBlockEnvelope[] {
  return blocks.filter(env=>["chart","attachment","approval_request"].includes(String((env.block||env as NativeBlock).kind||env.kind||"")));
}

export function executionDefaultOpen(loading:boolean, state:string, finalReply:string):boolean {
  return loading || !(["succeeded","completed"].includes(state) && Boolean(finalReply.trim()));
}

export type ExecutionEntry = { kind:"tool"; key:string; step:ToolStep } | { kind:"thinking"|"text"; key:string; block:NativeBlock; active:boolean };

/** Match results at the original call position, while preserving reasoning boundaries. */
export function executionEntries(blocks:NativeBlockEnvelope[], steps:ToolStep[], live:boolean):ExecutionEntry[] {
  const byId = new Map(steps.filter(step=>operationIdentity(step.data)).map(step => [operationIdentity(step.data), step]));
  const byBlock = new Map(steps.map(step => [step.event.data,step]));
  const used = new Set<string>(), entries:ExecutionEntry[] = [];
  let tail=-1;
  blocks.forEach((env,index)=>{if(["text","thinking","tool_use","tool_result"].includes(String((env.block||env).kind)))tail=index;});
  blocks.forEach((env,index) => {
    const b = (env.block || env) as NativeBlock, kind = String(b.kind || env.kind || "");
    if (kind === "tool_use" || kind === "tool_result") {
      const step = byId.get(operationIdentity(b)) || byBlock.get(b);
      if (step && !used.has(step.key)) { used.add(step.key); entries.push({ kind:"tool", key:`tool:${step.key}`, step }); }
    } else if ((kind === "thinking" || kind === "text") && typeof b.text === "string" && b.text.trim() && !b.retry) {
      const key = `${kind}:${b.stream_id || env.index || b.index || index}`;
      const active = live && b.completed !== true && (typeof b.presentation_active === "boolean" ? b.presentation_active : index===tail);
      entries.push({ kind, key, block:b, active });
    }
  });
  for (const step of steps) if (!used.has(step.key)) entries.push({kind:"tool",key:`tool:${step.key}`,step});
  return entries;
}

/** A single public body keeps its existing surface; intermediate multi-stage narration stays in order. */
export function partitionTranscript(blocks:NativeBlockEnvelope[], finalReply:string, fallbackReply:string) {
  const texts = blocks.filter(env => (env.block || env).kind === "text");
  const last = texts.at(-1);
  const reply = finalReply || (texts.length > 1 ? String((last?.block || last)?.text || "") : fallbackReply);
  const normalized = (value:unknown) => typeof value === "string" ? value.trim().replace(/\s+/g," ") : "";
  return { reply, work:blocks.filter(env => {
    if ((env.block || env).kind !== "text") return true;
    if (texts.length <= 1 || env === last) return false;
    return normalized((env.block || env).text) !== normalized(reply);
  }) };
}

/** Committed order wins; retained stream results only fill absent evidence, never replace a real result. */
export function enrichCommittedBlocks(committed:NativeBlockEnvelope[], streamed:NativeBlockEnvelope[], live = false) {
  const interrupted = [...committed, ...streamed].map(env => ({...env, ...record(env.block || env)}))
    .filter(b => b.kind === "model_transport" && b.status === "interrupted" && b.stream_id);
  // An interruption ends preview ownership even while loading/polling lags.
  // Normalize first: the same stream's completed replacement must survive.
  const normalize = (batch:NativeBlockEnvelope[]) => normalizeTranscriptBlocks(batch).filter(env => {
    const b = {...env, ...record(env.block || env)};
    return !(b.completed === false && ["text", "thinking"].includes(String(b.kind)) || b.phase === "input_streaming")
      || !interrupted.some(stop => stop.stream_id === b.stream_id && ["epoch", "session_id", "turn_id"].every(key =>
        !stop[key] || !b[key] || stop[key] === b[key]));
  });
  live = live && !interrupted.length;
  const discarded = [...committed, ...streamed].filter(env => {
    const b = record(env.block || env);
    return b.kind === "model_transport" && b.status === "discarded";
  });
  const rows = normalize([...committed, ...discarded]);
  const source = normalize([...streamed, ...discarded]);
  if (!rows.length) return source;
  const narrativeKey = (env: NativeBlockEnvelope) => {
    const b = record(env.block || env), turn = b.turn_id || env.turn_id;
    return ["text", "thinking"].includes(String(b.kind)) && turn && b.stream_id && !b.retry
      ? JSON.stringify([b.kind, turn, b.stream_id]) : "";
  };
  // Persisted transcripts may omit the event-bus epoch. Reconcile only a
  // one-to-one narrative identity with one known epoch across both inputs.
  // Multiple epochs/candidates are ambiguous and must remain separate.
  const domains = new Map<string, {epochs:Set<string>; sessions:Set<string>; saved:number; live:number}>();
  for (const [side, batch] of [["saved", rows], ["live", source]] as const) for (const env of batch) {
    const key = narrativeKey(env);
    if (!key) continue;
    const b = record(env.block || env);
    const domain = domains.get(key) || {epochs:new Set<string>(), sessions:new Set<string>(), saved:0, live:0};
    const epoch = b.epoch || env.epoch, session = b.session_id || env.session_id;
    if (epoch) domain.epochs.add(String(epoch));
    if (session) domain.sessions.add(String(session));
    domain[side]++;
    domains.set(key, domain);
  }
  const identity = (env: NativeBlockEnvelope) => {
    const b = record(env.block || env), kind = String(b.kind || "");
    if (["text", "thinking"].includes(kind) && b.stream_id) {
      const domain = domains.get(narrativeKey(env));
      const epoch = b.epoch || env.epoch || (domain?.saved === 1 && domain.live === 1
        && domain.epochs.size === 1 && domain.sessions.size <= 1 ? [...domain.epochs][0] : "");
      return JSON.stringify([kind, epoch, b.turn_id || env.turn_id || "", b.stream_id]);
    }
    const id = kind.startsWith("tool_") ? operationIdentity(b)
      : b.stream_id || b.chart_id || b.approval_id || b.attachment_id || b.block_id;
    return id ? kind + ":" + id : "";
  };
  const text = (b:Record<string,unknown>) => typeof b.text === "string" ? b.text.trim().replace(/\s+/g, " ") : "";
  const finalRow = !live ? rows.findLast(row => {
    const b = record(row.block || row);
    return b.kind === "text" && b.completed !== false;
  }) : undefined;
  const match = (env:NativeBlockEnvelope) => {
    const key = identity(env), b = record(env.block || env);
    const index = key ? rows.findIndex(row => identity(row) === key) : -1;
    if (index >= 0 || !["text", "thinking"].includes(String(b.kind)) || !text(b)) return index;
    // Legacy saved replies have no stream ID. Match their complete body only;
    // a prefix/substring can be a distinct progress update.
    return rows.findIndex(row => {
      const saved = record(row.block || row);
      // Text equality cannot resolve conflicting or ambiguous stream identity.
      if (saved.stream_id && b.stream_id) {
        const savedEpoch = saved.epoch || row.epoch, epoch = b.epoch || env.epoch;
        const savedTurn = saved.turn_id || row.turn_id, turn = b.turn_id || env.turn_id;
        if (saved.stream_id === b.stream_id || savedEpoch && epoch && savedEpoch !== epoch
          || savedTurn && turn && savedTurn !== turn) return false;
      }
      return saved.kind === b.kind && (!saved.stream_id || !b.stream_id || row === finalRow) && text(saved) === text(b);
    });
  };
  const anchors = source.map(env => rows[match(env)]);
  for (const [position, env] of source.entries()) {
    const b = record(env.block || env), narrative = ["text", "thinking"].includes(String(b.kind));
    const complete = narrative && b.completed === true && !b.retry && Boolean(text(b));
    const index = match(env);
    if (index >= 0) {
      const saved = record(rows[index].block || rows[index]);
      // A running snapshot can lag behind its stream. A completed source is
      // authoritative, including an intentionally empty/null tool result.
      if (narrative && (live ? saved.completed !== true : complete && saved.completed === false)) {
        rows[index] = {...rows[index], block:{...saved, ...b}};
      } else if (b.kind === "tool_result" && saved.result === undefined) {
        rows[index] = {...rows[index], block:{...b, ...Object.fromEntries(Object.entries(saved).filter(([,v])=>v!==undefined))}};
      }
    } else if (live || complete || (!narrative && b.phase !== "input_streaming" && ["tool_use", "tool_result", "chart", "attachment", "approval_request", "team_trace", "subagent_trace"].includes(String(b.kind)))) {
      if (!live) {
        // Fill gaps before the next durable anchor, keeping the terminal reply
        // last even when the saved turn contains only a failure fallback.
        const next = anchors.slice(position + 1).find(Boolean) || finalRow;
        const before = next ? rows.indexOf(next) : -1;
        rows.splice(before < 0 ? rows.length : before, 0, env);
        continue;
      }
      const call = operationIdentity(b);
      const after = b.kind === "tool_result" && call ? rows.findIndex(row => record(row.block || row).kind === "tool_use" && operationIdentity(record(row.block || row)) === call) : -1;
      rows.splice(after < 0 ? rows.length : after + 1, 0, env);
    }
  }
  return normalizeTranscriptBlocks(rows);
}

/** Keep approval/chart/attachment artifacts in the native block renderer. */
export function renderableExecutionBlocks(blocks: NativeBlockEnvelope[]): NativeBlockEnvelope[] {
  return blocks.filter((env) => {
    const kind = String((env.block || env).kind || env.kind || "");
    return !["approval_request", "chart", "attachment"].includes(kind);
  });
}

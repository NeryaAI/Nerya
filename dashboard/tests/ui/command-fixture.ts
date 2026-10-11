import type { Page, Route } from "@playwright/test";
import type { ConversationCommand, CommandState } from "../../lib/conversationCommands";

export async function installCommandFixture(page: Page, options: { complete?:boolean; loseAck?:boolean; responseText?:string; onSend?:(body:any)=>void } = {}) {
  const commands = new Map<string, ConversationCommand>();
  const requests: any[] = [];
  const controls: any[] = [];
  const queue = { paused:false, pause_reason:"", revision:1 };
  let lost = false;
  let checkpoint: { turn_id:string; resumable:boolean } | null = null;
  const setState = (id:string,state:CommandState) => { const command=commands.get(id)!; command.state=state; command.revision++; command.updated_at=Date.now()/1000; return command; };
  const handler = async (route:Route) => {
    const request=route.request(), url=new URL(request.url()), path=url.pathname.replace(/^\/api\/proxy/,"");
    const sid=url.searchParams.get("session_id") || "";
    let body:unknown;
    if (path === "/agent/commands/reference") body={ok:true,content:JSON.stringify({reference:{captured_at:"2026-09-25T00:00:00Z"},content:"Immutable evidence at selection time."}),artifact_sha256:"a".repeat(64)};
    else if (path === "/agent/commands/fork") { controls.push(request.postDataJSON()); body={ok:true,session_id:"branch_"+"a".repeat(32)}; }
    else if (path === "/agent/commands/events") body={ok:true,events:[],cursor:Number(url.searchParams.get("after_seq") || 0),has_more:false};
    else if (path === "/agent/commands/control") {
      const input=request.postDataJSON(); controls.push(input);
      const command=commands.get(input.command_id);
      if (input.action !== "reconcile" && input.expected_revision !== (command?.revision || queue.revision)) { await route.fulfill({status:409,json:{ok:false,error:"command_revision_conflict"}}); return; }
      if (input.action === "stop" && command) setState(command.command_id,"stopping");
      if (input.action === "pause" || input.action === "resume") { queue.paused=input.action === "pause"; queue.revision++; }
      if (input.action === "edit" && command) { command.input=input.text; command.revision++; }
      if (input.action === "remove" && command) setState(command.command_id,"removed");
      if (input.action === "move" && command) {
        const items=[...commands.values()].filter(item=>item.state === "queued" && item !== command).sort((a,b)=>a.position-b.position);
        const index=items.findIndex(item=>item.command_id === input.before_command_id);
        items.splice(index<0 ? items.length : index,0,command); items.forEach((item,index)=>{item.position=index+1;item.revision++;});
      }
      body={ok:true,queue,commands:[...commands.values()],checkpoint};
    } else if (request.method() === "POST") {
      const input=request.postDataJSON(); requests.push(input);
      let command=commands.get(input.command_id);
      const duplicate=Boolean(command);
      if (!command) {
        const running=[...commands.values()].some(item=>item.state === "running");
        const kind=input.command_type || "send";
        command={command_id:input.command_id,session_id:input.session_id,kind,turn_id:input.request.resume_turn_id || (kind === "guide" ? [...commands.values()].find(item=>item.kind!=="guide"&&item.state==="running")?.turn_id : undefined) || "turn-"+input.command_id,
          revision:1,position:commands.size+1,state:kind === "guide" ? "delivered" : options.complete ? "succeeded" : running || queue.paused&&!input.request.run_only&&kind!=="resume" ? "queued" : "running",
          created_at:Date.now()/1000,updated_at:Date.now()/1000,input:input.request.payload?.text || input.request.continuation_feedback || "",
          attachments:input.request.payload?.attachments || [], context:{input_text:input.request.payload?.text,requested_model:{model_context_window:1048576},accepted_model:{provider:"test",model:"fixture-model"}},
          has_result:!!options.complete,show_user:kind === "send"};
        if (options.complete) command.result={command_id:command.command_id,turn_id:command.turn_id,stopped_reason:"end_turn",final_text:options.responseText || "Verified fixture response.",blocks:[]};
        commands.set(command.command_id,command); options.onSend?.(input.request);
      }
      if (options.loseAck && !lost) { lost=true; await route.abort("connectionreset"); return; }
      body={ok:true,command,duplicate};
    } else if (url.searchParams.has("command_id")) {
      const command=commands.get(url.searchParams.get("command_id")!);
      if (!command) {await route.fulfill({status:404,json:{ok:false,error:"command_not_found"}});return;}
      body={ok:true,command};
    } else body={ok:true,queue,commands:[...commands.values()].filter(item=>item.session_id === sid).sort((a,b)=>a.position-b.position),checkpoint};
    await route.fulfill({json:body});
  };
  await page.route("**/api/proxy/agent/commands**",handler);
  return {commands,requests,controls,queue,setState,setCheckpoint:(value:typeof checkpoint)=>{checkpoint=value;}};
}

export async function parityFixture(page:Page,options:{language?:string;theme?:string;loseAck?:boolean;complete?:boolean;historyCount?:number}={}) {
  const errors:string[]=[];
  page.on("pageerror",error=>errors.push(error.message));
  await page.addInitScript(({language,theme})=>{localStorage.setItem("nerya.ui_settings.v1",JSON.stringify({language,darkMode:theme}));},{language:options.language || "en",theme:options.theme || "dark"});
  const now="2026-09-25T09:00:00Z";
  const history=Array.from({length:options.historyCount || 0},(_,index)=>({message_id:`history-${index}`,role:index%2 ? "assistant" : "user",content:index%2 ? `Verified historical result ${index}. Evidence is retained with the original conversation.` : `Historical request ${index}. Review the selected evidence.`,ts:1720000000+index,turn_id:`history-turn-${Math.floor(index/2)}`,meta:{}}));
  const sessions=[{session_id:"parity-session",source:"user_chat",title:"Agent parity review",meta:{title:"Agent parity review"},created_at:now,updated_at:now,message_count:0}];
  const tiers=["light","medium","high"].map(tier=>({tier,provider:"test",model:"fixture-model"}));
  await page.route("**/api/**",async route=>{
    const url=new URL(route.request().url()),path=url.pathname.replace(/^\/api\/proxy/,"");
    let body:unknown={ok:true,items:[],count:0,total:0};
    if(path === "/auth/status")body={ok:true,authenticated:true,local_access:true,enabled:true,password_set:true};
    else if(path === "/setup/readiness")body={status:"ok",data:{checks:[],blocking:[]}};
    else if(path === "/operator/overview")body={status:"ok",data:{attention:[],counts:{},accounts:[],strategies:[]}};
    else if(path === "/operator/nav")body={ok:true,data:{primary:[],advanced:[]},primary:[],advanced:[]};
    else if(path === "/runtime/info")body={ok:true,protocol_version:1,build_id:"isolated-test-build",workspace_id:"isolated-fixture",capabilities:["conversation_commands","session_view","user_interactions","plan_mode"]};
    else if(path === "/agent/sessions/view"){await route.fulfill({status:404,json:{ok:false,error:"session_not_found"}});return;}
    else if(path === "/health")body={status:"ok"};
    else if(path === "/workspace")body={root:"isolated-fixture",live_trading_enabled:false,kill_switch:false};
    else if(path === "/llm/config")body={ok:true,default_tier:"medium",intent_tier:"light",tiers,provider_profiles:[],reasoning_levels:[]};
    else if(path === "/llm/tiers")body={tiers,count:tiers.length};
    else if(path === "/llm/models")body={providers:{test:[{id:"fixture-model"}]}};
    else if(path === "/llm/providers")body={providers:[]};
    else if(path === "/llm/catalog")body={providers:[],reasoning_levels:[]};
    else if(path === "/agent/sessions")body={sessions,has_more:false};
    else if(path === "/agent/runs")body={ok:true,runs:[],next_cursor:null};
    else if(path === "/agent/session")body={...sessions[0],session_id:url.searchParams.get("session_id")};
    else if(path === "/agent/session/transcript")body={ok:true,...sessions[0],session_id:url.searchParams.get("session_id"),messages:history,count:history.length};
    else if(path === "/agent/open_turns")body={open_turns:[]};
    else if(path === "/agent/stream/events")body={events:[],cursor:0,latest_seq:0};
    else if(path.includes("approvals"))body={approvals:[]};
    else if(path.includes("strategy/list"))body={strategies:[]};
    else if(path === "/accounts/list")body={accounts:[]};
    else if(path === "/portfolio/summary")body={accounts:[],totals:{cash_usd:0,equity_usd:0}};
    else if(path === "/skills")body={skills:[]};
    else if(path === "/teams/roles")body={ok:true,roles:[]};
    else if(path === "/workspace/files")body={ok:true,entries:[]};
    await route.fulfill({json:body});
  });
  const engine=await installCommandFixture(page,options);
  return {...engine,errors};
}

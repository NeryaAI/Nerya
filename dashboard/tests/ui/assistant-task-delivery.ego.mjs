import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

function fixture() {
  const sid = "assistant_i18n_qa";
  const now = Date.UTC(2026,9,12)/1000;
  const profile = {id:"main",name:"Nerya",role:"Assistant",instructions:"",avatar:"mascot",enabled:true,
    model_provider:"",model_id:"",model_tier:"",tool_policy:{allow_groups:[],deny:[]},revision:1,
    research_allowed:true,memory_allowed:true,browser_allowed:true,files_allowed:true,shell_allowed:false,created_at:now,updated_at:now};
  const session = {session_id:sid,source:"assistant",title:"Task delivery QA",meta:{assistant_id:"main",conversation_kind:"assistant"},created_at:now,updated_at:now,message_count:5};
  const messages = ["succeeded","failed","interrupted","removed"].map((state,index)=>({
    message_id:"assistant_result:qa_"+state,role:"assistant",ts:now+index,
    content:state === "succeeded" ? "Full agent conclusion.\n\n" + "Evidence. ".repeat(180) + "END-OF-FULL-REPLY" : "Agent conclusion for " + state + ".",
    meta:{source:"assistant_task_result",assistant_task_id:"awork_"+state,task_title:"Task "+state,task_session_id:"taskchat_"+state,execution_status:state},
  }));
  messages.push({message_id:"assistant_result:qa_legacy",role:"assistant",ts:now+4,
    content:"**Legacy research** · 已完成 / Completed\n\nPreserved historical agent conclusion.\n\n[查看任务对话 / Open task conversation](/chat/taskchat_legacy)",
    meta:{assistant_task_id:"awork_legacy",task_session_id:"taskchat_legacy",execution_status:"succeeded"}});
  const qa = window.__taskDeliveryQA = {errors:[],requests:[]};
  localStorage.setItem("nerya.ui_settings.v1",JSON.stringify({language:"en",darkMode:"dark"}));
  window.addEventListener("error",event=>qa.errors.push(event.message));
  window.addEventListener("unhandledrejection",event=>qa.errors.push(String(event.reason)));
  const original = window.fetch.bind(window);
  window.fetch = async (input,init) => {
    const url = new URL(typeof input === "string" ? input : input.url,location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/")) return original(input,init);
    const path = url.pathname.replace(/^\/api\/proxy/,"");
    qa.requests.push(path);
    let result = {ok:true,items:[],events:[],accounts:[],approvals:[],strategies:[],skills:[],roles:[],count:0,total:0};
    if (path === "/auth/status") result={ok:true,authenticated:true,local_access:true,enabled:true,password_set:true};
    else if (path === "/runtime/info") result={ok:true,protocol_version:1,build_id:"isolated-i18n-qa",workspace_id:"isolated-i18n-qa",capabilities:["conversation_commands","session_view"]};
    else if (path === "/workspace") result={root:"isolated-i18n-qa",live_trading_enabled:false};
    else if (path === "/assistants") result={ok:true,assistants:[profile],workspace:"isolated-i18n-qa",channels:[]};
    else if (path === "/assistants/session") result={ok:true,assistant:profile,conversation:{conversation_kind:"assistant"}};
    else if (path === "/assistants/home") result={ok:true,assistant_id:"main",session_id:sid};
    else if (path === "/assistants/conversations") result={ok:true,sessions:[session]};
    else if (path === "/assistants/tasks") result={ok:true,tasks:[]};
    else if (path === "/assistants/results") result={ok:true,results:[]};
    else if (path === "/agent/sessions") result={sessions:[session],has_more:false};
    else if (path === "/agent/session") result=session;
    else if (path === "/agent/session/transcript") result={ok:true,...session,messages,count:messages.length,has_more:false};
    else if (path === "/agent/sessions/view") return Response.json({ok:false,error:"session_not_found"},{status:404});
    else if (path === "/agent/commands") result={ok:true,queue:{paused:false,pause_reason:"",revision:1},commands:[],checkpoint:null};
    else if (path === "/agent/commands/events") result={ok:true,events:[],cursor:0,has_more:false};
    else if (path === "/agent/open_turns") result={open_turns:[]};
    else if (path === "/agent/runs") result={ok:true,runs:[],next_cursor:null};
    else if (path === "/llm/tiers") result={tiers:[]};
    else if (path === "/llm/config") result={ok:true,tiers:[],provider_profiles:[]};
    else if (path === "/llm/models") result={providers:{}};
    else if (path === "/llm/providers") result={providers:[]};
    else if (path === "/operator/nav") result={ok:true,data:{primary:[],advanced:[]}};
    else if (path === "/setup/readiness") result={ok:true,status:"ok",data:{checks:[],blocking:[]}};
    return Response.json(result);
  };
}

export async function verifyTaskDelivery(page,baseUrl,artifactDir) {
  await mkdir(artifactDir,{recursive:true});
  // Repeated QA runs must not inherit this fixture's stale transcript metadata.
  await page.evaluate(()=>{
    for (const key of Object.keys(localStorage)) if (key.startsWith("nerya.chat.") && key.includes("isolated-i18n-qa")) localStorage.removeItem(key);
  });
  await page.cdp("Page.addScriptToEvaluateOnNewDocument",{source:`(${fixture.toString()})()`});
  await page.cdp("Emulation.setDeviceMetricsOverride",{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await page.goto(baseUrl+"/chat/assistant_i18n_qa");
  await page.waitForFunction(()=>document.querySelectorAll('[data-testid="assistant-task-receipt"]').length === 5,undefined,{timeout:20000});
  const read = () => page.evaluate(()=>({
    lang:document.documentElement.lang,
    receipts:[...document.querySelectorAll('[data-testid="assistant-task-receipt"]')].map(node=>({text:node.textContent,href:node.querySelector('a')?.getAttribute('href')})),
    reply:document.querySelector('[data-turn-id="assistant_result:qa_succeeded"]')?.textContent,
    legacy:document.querySelector('[data-turn-id="assistant_result:qa_legacy"]')?.textContent,
    errors:window.__taskDeliveryQA.errors,
  }));
  const english = await read();
  assert.equal(english.lang,"en");
  assert(english.receipts.every(row=>row.text.includes("Open task conversation") && !/\p{Script=Han}/u.test(row.text)));
  for (const label of ["Completed","Needs attention","Stopped","Cancelled"]) assert(english.receipts.some(row=>row.text.includes(label)));
  assert(english.reply.includes("END-OF-FULL-REPLY"));
  assert(english.legacy.includes("Preserved historical agent conclusion."));
  assert(!english.legacy.includes("已完成 / Completed"));
  assert.deepEqual(english.errors,[]);
  await page.screenshot({path:artifactDir+"/task-results-en.png",fullPage:true});
  await page.evaluate(()=>{
    const key="nerya.ui_settings.v1",settings=JSON.parse(localStorage.getItem(key)||"{}");
    localStorage.setItem(key,JSON.stringify({...settings,language:"zh"}));
    window.dispatchEvent(new StorageEvent("storage",{key}));
  });
  await page.waitForFunction(()=>document.documentElement.lang === "zh" && [...document.querySelectorAll('[data-testid="assistant-task-receipt"] a')].every(node=>node.textContent === "查看任务对话"));
  const chinese = await read();
  assert.deepEqual(chinese.receipts.map(row=>row.href),english.receipts.map(row=>row.href));
  assert(chinese.reply.includes("END-OF-FULL-REPLY"));
  assert.deepEqual(chinese.errors,[]);
  await page.cdp("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({path:artifactDir+"/task-results-zh-mobile.png",fullPage:true});
  await page.reload();
  await page.waitForFunction(()=>document.querySelectorAll('[data-testid="assistant-task-receipt"]').length === 5);
  const reloaded = await read();
  assert.equal(reloaded.lang,"en");
  assert.deepEqual(reloaded.receipts,english.receipts);
  assert(reloaded.reply.includes("END-OF-FULL-REPLY"));
  assert.deepEqual(reloaded.errors,[]);
  console.log({checks:"English and Chinese receipts, four terminal states, historical compatibility, full agent reply, unchanged links, mobile overflow and reload",errors:reloaded.errors});
}

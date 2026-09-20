const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {chromium,expect}=require('@playwright/test');
const runName=process.env.REVIEW_ROOT||'main-chat-real-0918-service';assert.ok(/^main-chat-real-[a-z0-9-]+$/.test(runName));
const root=path.resolve(__dirname,'../test-results',runName);
const info=JSON.parse(fs.readFileSync(path.join(root,'server-info.json')));
const ui=process.env.REVIEW_UI||info.ui||'http://127.0.0.1:18381';
assert.ok(/^http:\/\/127\.0\.0\.1:\d+$/.test(ui),'Review UI must be local');
const prompts={
 script:'帮我建一个比特币观察策略：每5分钟用脚本检查15分钟K线，收盘价站上20周期均线时记录提醒，不用AI分析，不要下单。创建后直接检查并用真实历史数据回放，有问题就修好，暂时别开启自动运行。',
 signal_agent:'帮我建一个比特币观察策略：脚本每5分钟检查15分钟K线，MACD金叉才请AI分析机会和风险，没金叉别调用AI，同一根K线别重复分析。建好后检查有信号、没信号和重复信号，再用真实历史数据回放，有问题就修好。不要下单，也别开启自动运行。',
 scheduled_agent:'帮我建一个定时观察策略，每天北京时间早上9点让AI看看比特币行情，给我一段机会和风险总结，不用脚本先筛选。建好后检查并用真实数据验证，不要只给方案。不要下单，暂时别开启自动运行。'
};
const caseName=process.env.REVIEW_CASE||'script';assert.ok(prompts[caseName]);
const attempt=process.env.REVIEW_ATTEMPT||'initial';assert.ok(/^[a-z0-9-]+$/.test(attempt));
const folder=path.join(root,caseName+'-'+attempt);assert.ok(!fs.existsSync(path.join(folder,'request.json')),'Existing request must be inspected, not resent');fs.mkdirSync(folder,{recursive:true});
let token='';
async function api(route,data){const r=await fetch(info.api+route,{method:data===undefined?'GET':'POST',headers:{'content-type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:data===undefined?undefined:JSON.stringify(data),signal:AbortSignal.timeout(30000)});const out=await r.json();assert.ok(r.ok&&out.ok!==false,JSON.stringify(out).slice(0,900));return out;}
(async()=>{
 assert.equal((await api('/workspace')).root,info.workspace);
 const pf=path.join(root,'.browser-password');const status=await api('/auth/status');
 if(!status.password_configured){const password=crypto.randomBytes(32).toString('base64url');fs.writeFileSync(pf,password,{mode:0o600});await api('/auth/admin/password',{new_password:password});}
 assert.ok(fs.existsSync(pf),'Existing authentication cannot be reset by acceptance');
 const auth=await api('/auth/login',{password:fs.readFileSync(pf,'utf8')});token=auth.token;
 const b=await chromium.launch({headless:true});const context=await b.newContext({viewport:{width:1600,height:1080},locale:'zh-CN'});
 await context.addInitScript(a=>{localStorage.setItem('nerya.admin_jwt.v1',a.token);localStorage.setItem('nerya.admin_jwt_expires_at.v1',String(a.expires_at));localStorage.setItem('nerya.ui_settings.v1',JSON.stringify({language:'zh',darkMode:'dark'}));},auth);
 const p=await context.newPage();p.setDefaultTimeout(30000);const errors=[],httpErrors=[];let sent=false,responseBody=null,requestBody=null;
 p.on('pageerror',e=>errors.push(e.message));p.on('response',r=>{if(r.status()>=500)httpErrors.push({url:new URL(r.url()).pathname,status:r.status()});});
 p.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.endsWith('/agent/run_turn_internal')){assert.ok(!sent,'The UI submitted this request twice');sent=true;requestBody=r.postDataJSON();fs.writeFileSync(path.join(folder,'request.json'),JSON.stringify({started_at:new Date().toISOString(),path:new URL(r.url()).pathname,body:requestBody},null,2));console.log(JSON.stringify({case:caseName,state:'submitted',session_id:requestBody.session_id,settings:{max_iterations:requestBody.max_iterations,max_total_tool_calls:requestBody.max_total_tool_calls,max_wall_seconds:requestBody.max_wall_seconds}}));}});
 try{
   const prior=process.env.REVIEW_CONTINUE?JSON.parse(fs.readFileSync(process.env.REVIEW_CONTINUE)):null;
   await p.goto(ui+(prior?'/chat/'+encodeURIComponent(prior.result.session_id):'/chat'),{waitUntil:'domcontentloaded',timeout:90000});
   const box=p.locator('textarea:visible').first();await expect(box).toBeEditable({timeout:90000});
   await box.fill(process.env.REVIEW_MESSAGE||prompts[caseName]);await p.screenshot({path:path.join(folder,'01-prompt.png'),fullPage:true});
   if(process.env.REVIEW_PREVIEW==='1'){console.log((await p.locator('body').innerText()).slice(0,4000));return;}
   const wait=p.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname.endsWith('/agent/run_turn_internal'),{timeout:1850000});
   const start=Date.now();await p.getByRole('button',{name:'发送',exact:true}).click();
   const response=await wait;responseBody=await response.json();
   fs.writeFileSync(path.join(folder,'result.json'),JSON.stringify({elapsed_s:(Date.now()-start)/1000,http_status:response.status(),result:responseBody},null,2),{mode:0o600});
   assert.equal(response.status(),200,JSON.stringify(responseBody));
   assert.ok(responseBody.turn_id&&!responseBody.error,JSON.stringify(responseBody).slice(0,1000));
   await expect(p.getByRole('button',{name:'发送',exact:true})).toBeVisible({timeout:30000});
   await p.evaluate(()=>document.fonts.ready);
   await p.screenshot({path:path.join(folder,'02-conversation-page.png'),fullPage:true});
   fs.writeFileSync(path.join(folder,'visible-conversation.txt'),await p.locator('body').innerText());
   const scroll=await p.locator('[data-testid="chat-scroll"]').count();
   // Capture actual DOM scroll containers, not a reconstructed conversation.
   const target=await p.evaluateHandle(()=>[...document.querySelectorAll('main div, main section')].filter(e=>e.scrollHeight>e.clientHeight+100&&/auto|scroll/.test(getComputedStyle(e).overflowY)).sort((a,b)=>b.scrollHeight-a.scrollHeight)[0]||document.scrollingElement);
   const extent=await target.evaluate(e=>({h:e.scrollHeight,view:e.clientHeight}));
   for(let y=0,i=0;y<extent.h&&i<30;y+=Math.max(400,extent.view-120),i++){await target.evaluate((e,pos)=>{e.scrollTop=pos},y);await p.screenshot({path:path.join(folder,`conversation-${String(i+1).padStart(2,'0')}.png`)});}
   fs.writeFileSync(path.join(folder,'capture.json'),JSON.stringify({errors,httpErrors,scope:info.scope,url:p.url(),scroll:extent,actual_route:'/agent/run_turn_internal'},null,2));
   console.log(JSON.stringify({case:caseName,elapsed_s:(Date.now()-start)/1000,status:response.status(),session_id:responseBody.session_id,turn_id:responseBody.turn_id,stop:responseBody.stopped_reason,budget:responseBody.budget,final_text:responseBody.final_text,errors,httpErrors}));
 }catch(e){fs.writeFileSync(path.join(folder,'failure.json'),JSON.stringify({error:String(e),sent,session_id:requestBody?.session_id,may_have_side_effects:sent&&!responseBody,errors,httpErrors},null,2));await p.screenshot({path:path.join(folder,'failure.png'),fullPage:true}).catch(()=>{});console.error(e);process.exitCode=1;}
 finally{await b.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {chromium,expect}=require('@playwright/test');
const name=process.env.REVIEW_ROOT||'main-chat-real-0918-r012';assert.ok(/^main-chat-real-[a-z0-9-]+$/.test(name));
const root=path.resolve(__dirname,'../test-results',name),out=path.join(root,'delivery');fs.mkdirSync(out,{recursive:true});
const server=JSON.parse(fs.readFileSync(path.join(root,'server-info.json'))), ui=server.ui;
function parsed(value){if(value&&typeof value==='object')return value;try{return JSON.parse(value);}catch{return null;}}
function put(file,value){const target=path.join(out,file);fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,JSON.stringify(value,null,2));}
const runs=[],candidates=[];
for(const folder of fs.readdirSync(root,{withFileTypes:true}).filter(d=>d.isDirectory()&&fs.existsSync(path.join(root,d.name,'result.json')))){
 const run=JSON.parse(fs.readFileSync(path.join(root,folder.name,'result.json'))), r=run.result||{},trace=r.tool_trace||[];
 const request=JSON.parse(fs.readFileSync(path.join(root,folder.name,'request.json')));
 const summary={case:folder.name,elapsed_s:run.elapsed_s,http_status:run.http_status,session_id:r.session_id,turn_id:r.turn_id,stop:r.stopped_reason,prompt:request.body?.payload?.text,budget:r.budget,final_text:r.final_text||'',tools:trace.map((t,index)=>({index,action:t.action,ok:t.ok,error:t.error||null,elapsed_ms:t.elapsed_ms,proposal_id:t.payload?.proposal_id||null,...(t.action==='run_shell'?{command:t.payload?.command,output:t.result}:{}),...(['strategy_validate','strategy_submit_proposal','strategy_backtest'].includes(t.action)?{receipt:t.result}:{} )}))};
 runs.push(summary);put(folder.name+'/public-turn.json',summary);
 const dest=path.join(out,folder.name);fs.mkdirSync(dest,{recursive:true});
 for(const f of fs.readdirSync(path.join(root,folder.name)).filter(n=>/^(conversation-\d+|0[12]-.+)\.png$/.test(n)))fs.copyFileSync(path.join(root,folder.name,f),path.join(dest,f));
 const submit=trace.filter(t=>t.action==='strategy_submit_proposal'&&t.ok===true&&parsed(t.result)?.validation?.ok!==false).at(-1);
 const proposal=submit?.payload?.proposal_id;
 if(proposal){assert.match(proposal,/^prp_[a-zA-Z0-9_]+$/);const dir=path.join(server.workspace,'evolution/proposals',proposal,'after/strategies');const ids=fs.readdirSync(dir,{withFileTypes:true}).filter(d=>d.isDirectory());assert.equal(ids.length,1);if(!candidates.some(c=>c.proposal===proposal))candidates.push({case:folder.name.split('-')[0],id:ids[0].name,proposal});}
}
(async()=>{
 const response=await fetch(server.api+'/auth/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({password:fs.readFileSync(path.join(root,'.browser-password'),'utf8')}),signal:AbortSignal.timeout(15000)});const auth=await response.json();assert.ok(response.ok&&auth.token);
 const browser=await chromium.launch({headless:true}),ctx=await browser.newContext({viewport:{width:1600,height:1120},locale:'zh-CN'});
 await ctx.addInitScript(a=>{localStorage.setItem('nerya.admin_jwt.v1',a.token);localStorage.setItem('nerya.admin_jwt_expires_at.v1',String(a.expires_at));localStorage.setItem('nerya.ui_settings.v1',JSON.stringify({language:'zh',darkMode:'dark'}));},auth);
 const p=await ctx.newPage();p.setDefaultTimeout(25000);const errors=[],httpErrors=[],captures=[],checks=[];
 p.on('pageerror',e=>errors.push(e.message));p.on('response',r=>{if(r.status()>=500)httpErrors.push({path:new URL(r.url()).pathname,status:r.status()});});
 async function get(route){const r=await fetch(server.api+route,{headers:{Authorization:'Bearer '+auth.token},signal:AbortSignal.timeout(20000)});const v=await r.json();assert.ok(r.ok&&v.ok!==false,JSON.stringify(v).slice(0,500));return v;}
 async function shot(file,target){const dest=path.join(out,file);fs.mkdirSync(path.dirname(dest),{recursive:true});await p.evaluate(()=>document.fonts.ready);if(target)await target.screenshot({path:dest,animations:'disabled'});else await p.screenshot({path:dest,fullPage:true,animations:'disabled'});captures.push(file);}
 try{
  await p.goto(ui+'/strategies',{waitUntil:'domcontentloaded',timeout:90000});
  await expect(p.getByTestId('strategy-workflow-panel')).toHaveAttribute('aria-busy','false',{timeout:90000});
  const visibleUI=await p.evaluate(()=>({url:location.href,buttons:[...document.querySelectorAll('button')].filter(el=>((el.getAttribute('aria-label')||'')+el.textContent).includes('新建策略')).map(el=>({html:el.outerHTML,ancestors:[el.parentElement,el.parentElement?.parentElement,el.parentElement?.parentElement?.parentElement].filter(Boolean).map(e=>({tag:e.tagName,className:e.className,test:e.dataset.testid}))})),body:document.body.innerText.slice(0,5500),scripts:[...document.scripts].map(s=>s.src)}));put('page-diagnostic.json',visibleUI);console.log(JSON.stringify({pageDiagnostic:visibleUI}));
  await expect(p.getByRole('button',{name:'新建策略',exact:false})).toHaveCount(0);
  await expect(p.getByTestId('workflow-create')).toHaveCount(0);checks.push('No creation wizard or strategy creation buttons in the actual page');
  for(const candidate of candidates){
   const q='?strategy_id='+encodeURIComponent(candidate.id)+'&proposal_id='+encodeURIComponent(candidate.proposal);
   const workflow=await get('/strategies/runtime/workflow'+q);
   const verification=await get('/strategies/runtime/workflow/check'+q+'&base_revision='+workflow.revision);
   put(candidate.case+'/workflow.json',workflow);put(candidate.case+'/verification.json',verification);
   candidate.facts={state:workflow.source.state,schedule:workflow.manifest.schedule,evaluation:workflow.manifest.evaluation,execution_mode:workflow.manifest.execution_mode,data_sources:workflow.manifest.data_sources,agent_profile:workflow.manifest.agent_profile,replay:verification.replay};
   assert.equal(workflow.source.proposal_id,candidate.proposal);assert.equal(workflow.manifest.schedule.enabled,false);
   await p.goto(ui+'/strategies'+q,{waitUntil:'domcontentloaded',timeout:90000});const panel=p.getByTestId('strategy-workflow-panel');
   await expect(panel).toHaveAttribute('aria-busy','false',{timeout:90000});await expect(panel.getByRole('button',{name:'检查与验证',exact:true})).toHaveCount(0);await expect(panel.getByTestId('workflow-next-step')).toHaveCount(0);
   await shot(candidate.case+'/workflow.png');
   const nodes=await panel.getByTestId('workflow-canvas').locator('[data-workflow-node]').evaluateAll(els=>els.map(el=>el.getAttribute('data-workflow-node')));
   for(const id of nodes){
    const node=panel.getByTestId('workflow-canvas').locator('[data-workflow-node="'+id.replaceAll('"','\\"')+'"]');const btn=node.getByRole('button',{name:/^编辑详情/});if(!await btn.count())continue;
    await btn.first().click();const modal=p.getByTestId('workflow-editor-dialog');await expect(modal).toBeVisible();await shot(candidate.case+'/card-'+id.replace(/[^a-zA-Z0-9_-]/g,'-')+'.png',modal);
    await p.getByTestId('workflow-inspector').getByRole('button',{name:'关闭详情',exact:true}).click();
   }
   const supports=await panel.locator('[data-support-member]').evaluateAll(els=>els.map(el=>el.getAttribute('data-support-member')));
   for(const id of supports){await panel.locator('[data-support-member="'+id+'"]' ).click();await expect(p.getByTestId('workflow-editor-dialog')).toBeVisible();await shot(candidate.case+'/settings-'+id.replace(/[^a-zA-Z0-9_-]/g,'-')+'.png',p.getByTestId('workflow-editor-dialog'));await p.getByTestId('workflow-inspector').getByRole('button',{name:'关闭详情',exact:true}).click();}
   await panel.getByRole('tab',{name:'复盘',exact:true}).click();await shot(candidate.case+'/review-workflow.png');
   await panel.getByRole('tab',{name:'运行',exact:true}).click();await panel.getByRole('button',{name:'验证记录',exact:true}).click();
   await expect(p.getByTestId('workflow-verification')).toHaveAttribute('aria-busy','false');await shot(candidate.case+'/verification.png',p.getByTestId('workflow-editor-dialog'));
   await p.getByRole('button',{name:'关闭验证',exact:true}).click();checks.push(candidate.case+': workflow, visible cards, review and read-only evidence');
  }
 }catch(e){errors.push(String(e));await shot('capture-error.png').catch(()=>{});}
 finally{await browser.close();}
 put('audit.json',{captured_at:new Date().toISOString(),snapshot_scope:'Workflow/card images show final candidate contents after recorded follow-ups; each original conversation remains separately preserved.',scope:server.scope,provider:server.provider,model:server.model,skill_sha256_at_service_start:server.skill_sha256,runs,candidates,checks,errors,httpErrors,captures});
 const manifest=[];function walk(dir){for(const e of fs.readdirSync(dir,{withFileTypes:true})){const f=path.join(dir,e.name);if(e.isDirectory())walk(f);else if(e.name!=='manifest.json')manifest.push({path:path.relative(out,f),bytes:fs.statSync(f).size,sha256:crypto.createHash('sha256').update(fs.readFileSync(f)).digest('hex')});}}walk(out);put('manifest.json',manifest);
 console.log(JSON.stringify({runs:runs.map(r=>({case:r.case,stop:r.stop,elapsed:r.elapsed_s})),candidates:candidates.map(c=>({case:c.case,id:c.id,proposal:c.proposal,status:c.facts?.replay?.status})),captures:captures.length,errors,httpErrors}));if(errors.length||httpErrors.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1});

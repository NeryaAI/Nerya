const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {chromium,expect}=require('@playwright/test');
const root=path.resolve(__dirname,'../test-results/main-chat-real-0918-service');
const out=path.join(root,'delivery');fs.mkdirSync(out,{recursive:true});
const server=JSON.parse(fs.readFileSync(path.join(root,'server-info.json')));
function parsed(v){if(v&&typeof v==='object')return v;try{return JSON.parse(v);}catch{return null;}}
const summaries=[];
for(const folder of fs.readdirSync(root,{withFileTypes:true}).filter(d=>d.isDirectory()&&d.name!=='workspace'&&d.name!=='delivery')){
 const file=path.join(root,folder.name,'result.json');if(!fs.existsSync(file))continue;
 const run=JSON.parse(fs.readFileSync(file));const r=run.result||{}, trace=r.tool_trace||[];
 const tools=trace.map(item=>{const result=parsed(item.result), payload=item.payload||{};const entry={action:item.action,ok:item.ok,error:item.error||null,elapsed_ms:item.elapsed_ms};
  if(['strategy_draft_proposal','strategy_validate','strategy_submit_proposal','strategy_backtest'].includes(item.action)){
   entry.proposal_id=payload.proposal_id||result?.proposal_id||null;
   entry.config_path=payload.config_path||null;entry.allow_mock=payload.allow_mock;
   if(result&&typeof result==='object')entry.result=Object.fromEntries(Object.entries(result).filter(([k])=>['ok','strategy_id','proposal_id','state','verdict','metrics','metrics_display','replay','validation','blockers','warnings','provenance','operator_summary','next_required_action','backtest_required','metrics_path','report_path'].includes(k)));
  }
  if(item.action==='run_shell')entry.command=String(payload.command||'').split('\n')[0];
  return entry;
 });
 const receipts={};for(const action of ['strategy_validate','strategy_submit_proposal','strategy_backtest']){const last=trace.filter(t=>t.action===action).at(-1), value=parsed(last?.result);receipts[action]=!!last&&last.ok===true&&value?.ok!==false&&value?.validation?.ok!==false;}
 const requestPath=path.join(root,folder.name,'request.json'),request=fs.existsSync(requestPath)?JSON.parse(fs.readFileSync(requestPath)):{};
 summaries.push({case:folder.name,elapsed_s:run.elapsed_s,http_status:run.http_status,session_id:r.session_id,turn_id:r.turn_id,stop:r.stopped_reason,budget:r.budget,receipts,tools,final_text:r.final_text||'',prompt:request.body?.payload?.text||request.body?.message||request.body?.input||null});
 for(const file of fs.readdirSync(path.join(root,folder.name)).filter(f=>/^conversation-\d+\.png$|^0[12]-.+\.png$/.test(f))){const dest=path.join(out,folder.name);fs.mkdirSync(dest,{recursive:true});fs.copyFileSync(path.join(root,folder.name,file),path.join(dest,file));}
}
fs.writeFileSync(path.join(out,'public-results.json'),JSON.stringify({scope:server.scope,provider:server.provider,model:server.model,runs:summaries},null,2));
const candidates=[
 {name:'script',id:'btc_sma20_observer',proposal:'prp_e67714835d40'},
 {name:'macd',id:'btc_macd_observer',proposal:'prp_320d83bde475'},
 {name:'scheduled',id:'btc_daily_observer',proposal:'prp_9b37f20a2d69'}
];
(async()=>{
 const response=await fetch(server.api+'/auth/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({password:fs.readFileSync(path.join(root,'.browser-password'),'utf8')})});const auth=await response.json();assert.ok(response.ok&&auth.token);
 const b=await chromium.launch({headless:true}),ctx=await b.newContext({viewport:{width:1600,height:1100},locale:'zh-CN'});
 await ctx.addInitScript(a=>{localStorage.setItem('nerya.admin_jwt.v1',a.token);localStorage.setItem('nerya.admin_jwt_expires_at.v1',String(a.expires_at));localStorage.setItem('nerya.ui_settings.v1',JSON.stringify({language:'zh',darkMode:'dark'}));},auth);
 const p=await ctx.newPage();p.setDefaultTimeout(30000);const errors=[],failures=[],captures=[];
 p.on('pageerror',e=>errors.push(e.message));p.on('response',r=>{if(r.status()>=500)failures.push({path:new URL(r.url()).pathname,status:r.status()});});
 async function get(route){const r=await fetch(server.api+route,{headers:{Authorization:'Bearer '+auth.token}});const v=await r.json();assert.ok(r.ok&&v.ok!==false,JSON.stringify(v).slice(0,800));return v;}
 async function capture(file,locator){const target=path.join(out,file);fs.mkdirSync(path.dirname(target),{recursive:true});if(locator)await locator.screenshot({path:target,animations:'disabled'});else await p.screenshot({path:target,fullPage:true,animations:'disabled'});captures.push(file);}
 try{
  for(const c of candidates){
   const q='?strategy_id='+encodeURIComponent(c.id)+'&proposal_id='+encodeURIComponent(c.proposal);
   const workflow=await get('/strategies/runtime/workflow'+q);
   const report=await get('/strategies/runtime/workflow/check'+q+'&base_revision='+workflow.revision);
   fs.writeFileSync(path.join(out,c.name+'-verification.json'),JSON.stringify(report,null,2));
   assert.equal(workflow.source.proposal_id,c.proposal);assert.equal(workflow.manifest.schedule.enabled,false);
   await p.goto('http://127.0.0.1:18381/strategies'+q,{waitUntil:'domcontentloaded',timeout:90000});
   const panel=p.getByTestId('strategy-workflow-panel');await expect(panel).toHaveAttribute('aria-busy','false',{timeout:90000});
   await expect(panel.getByRole('button',{name:'检查与验证',exact:true})).toHaveCount(0);
   await expect(p.getByRole('button',{name:'新建策略',exact:false})).toHaveCount(0);
   await expect(panel.getByTestId('workflow-next-step')).toHaveCount(0);
   await capture(c.name+'/workflow.png');
   const nodes=workflow.strategy.nodes.filter(n=>['script','source','agent','scheduler'].includes(n.kind));
   for(const n of nodes){
    const target=p.getByTestId('workflow-canvas').locator('[data-workflow-node="'+n.id.replaceAll('"','\\"')+'"]');
    if(!await target.count())continue;
    const btn=target.getByRole('button',{name:/^编辑详情/});if(!await btn.count())continue;
    await btn.first().click();const modal=p.getByTestId('workflow-editor-dialog');await expect(modal).toBeVisible();
    const file=c.name+'/card-'+n.id.replace(/[^a-zA-Z0-9_-]/g,'-')+'.png';await capture(file,modal);
    await p.getByTestId('workflow-inspector').getByRole('button',{name:'关闭详情',exact:true}).click();
   }
   await panel.getByRole('tab',{name:'运行',exact:true}).click();await panel.getByRole('button',{name:'验证记录',exact:true}).click();
   await expect(p.getByTestId('workflow-verification')).toHaveAttribute('aria-busy','false');await capture(c.name+'/verification.png',p.getByTestId('workflow-editor-dialog'));
   await p.getByRole('button',{name:'关闭验证',exact:true}).click();
  }
 }catch(e){errors.push(String(e));await p.screenshot({path:path.join(out,'capture-error.png'),fullPage:true}).catch(()=>{});}
 finally{await b.close();}
 fs.writeFileSync(path.join(out,'ui-audit.json'),JSON.stringify({errors,failures,captures,scope:'Read-only capture of actual main-Agent-created candidates; no mocked UI responses'},null,2));
 const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 let html='<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>真实主 Agent 创建验收</title><style>body{font:16px/1.7 system-ui;margin:30px auto;max-width:1250px;padding:0 22px;background:#15151b;color:#ededf2}img{max-width:100%;height:auto;border:1px solid #41414a;margin:12px 0}details{margin:22px 0;padding:12px;border:1px solid #41414a}a{color:#c1b2ff}pre{white-space:pre-wrap;font:13px/1.7 monospace}</style><h1>真实主 Agent 对话验收</h1><p>真实模型 '+esc(server.provider)+' / '+esc(server.model)+'。独立 Workspace；原 Workspace 数据库损坏未修复。初次 MACD 需要续聊，增强检查复测出现审批，不能称所有一次成功。原始回应中的完成表述以具体回执为准。</p>';
 for(const s of summaries){html+='<details><summary>'+esc(s.case)+' · '+esc(s.stop)+' · '+esc(s.elapsed_s)+' 秒</summary><pre>'+esc(JSON.stringify(s.receipts,null,2))+'</pre><p>'+esc(s.final_text||'此回合没有最终回复，详见停止原因')+'</p>';const dir=path.join(out,s.case);if(fs.existsSync(dir))for(const f of fs.readdirSync(dir).filter(f=>f.endsWith('.png')))html+='<a href="'+esc(s.case+'/'+f)+'"><img loading="lazy" src="'+esc(s.case+'/'+f)+'" alt="'+esc(s.case+' '+f)+'"></a>';html+='</details>';}
 for(const f of captures)html+='<h2>'+esc(f)+'</h2><a href="'+esc(f)+'"><img loading="lazy" src="'+esc(f)+'" alt="'+esc(f)+'"></a>';
 fs.writeFileSync(path.join(out,'index.html'),html+'</html>');
 const manifest=[];function walk(dir){for(const e of fs.readdirSync(dir,{withFileTypes:true})){const f=path.join(dir,e.name);if(e.isDirectory())walk(f);else if(e.name!=='manifest.json')manifest.push({path:path.relative(out,f),bytes:fs.statSync(f).size,sha256:crypto.createHash('sha256').update(fs.readFileSync(f)).digest('hex')});}}walk(out);fs.writeFileSync(path.join(out,'manifest.json'),JSON.stringify(manifest,null,2));
 console.log(JSON.stringify({runs:summaries.map(s=>({case:s.case,stop:s.stop,receipts:s.receipts})),captures:captures.length,errors,failures,delivery:out}));if(errors.length||failures.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1});

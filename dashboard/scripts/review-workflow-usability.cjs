const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { chromium, expect } = require('@playwright/test');
const runName = process.env.NERYA_USABILITY_RUN || 'workflow-usability';
assert.ok(/^workflow-usability[-a-z0-9]*$/.test(runName));
const ROOT = path.resolve(__dirname, '../test-results', runName);
const fixture = JSON.parse(fs.readFileSync(path.join(ROOT, 'fixture-index.json')));
const API = fixture.api, UI = 'http://127.0.0.1:18381';
const SHOTS = path.join(ROOT, 'screenshots'); fs.mkdirSync(SHOTS, {recursive:true});
const checks = [], images = [], failures = [], pageErrors = [];
let token = '', auth;
async function api(route, body) {
  const response = await fetch(API + route, {method:body===undefined?'GET':'POST', headers:{'Content-Type':'application/json', ...(token ? {Authorization:`Bearer ${token}`} : {})}, body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(30000)});
  const value = await response.json();
  assert.ok(response.ok && value.ok !== false, `${route}: ${JSON.stringify(value).slice(0,1000)}`);
  return value;
}
async function check(name, fn) { await fn(); checks.push(name); console.log('PASS '+name); }
async function main() {
  const workspace = await api('/workspace'); assert.equal(workspace.root, fixture.workspace);
  const status = await api('/auth/status');
  const passwordPath = path.join(ROOT, '.test-password');
  if (!status.password_configured) { const password=crypto.randomBytes(32).toString('base64url'); fs.writeFileSync(passwordPath,password,{mode:0o600}); await api('/auth/admin/password',{new_password:password}); }
  auth=await api('/auth/login',{password:fs.readFileSync(passwordPath,'utf8')}); token=auth.token;
  const browser = await chromium.launch({headless:true});
  const context = await browser.newContext({viewport:{width:1640,height:1180},locale:'zh-CN'});
  await context.addInitScript(a=>{if(!localStorage.getItem('nerya.ui_settings.v1')) localStorage.setItem('nerya.ui_settings.v1',JSON.stringify({language:'zh',darkMode:'dark'})); localStorage.setItem('nerya.admin_jwt.v1',a.token); localStorage.setItem('nerya.admin_jwt_expires_at.v1',String(a.expires_at));},auth);
  const page = await context.newPage();
  page.on('pageerror',e=>pageErrors.push(e.message));
  page.on('response',r=>{if(r.status()>=500) failures.push({type:'http',url:new URL(r.url()).pathname,status:r.status()});});
  const panel=page.getByTestId('strategy-workflow-panel');
  const inspector=page.getByTestId('workflow-inspector');
  async function open(item, proposalId) {
    await page.goto(`${UI}/strategies?strategy_id=${encodeURIComponent(item.strategy_id)}${proposalId?'&proposal_id='+encodeURIComponent(proposalId):''}`,{waitUntil:'domcontentloaded',timeout:90000});
    await expect(panel).toHaveAttribute('aria-busy','false',{timeout:90000});
    await expect(panel.locator('[data-workflow-node]').first()).toBeVisible({timeout:30000});
    await page.evaluate(()=>document.fonts.ready);
  }
  async function shot(name, target=panel, controlled=false) {
    await target.scrollIntoViewIfNeeded();
    if(controlled) await target.evaluate(el=>{ if(!el.querySelector('[data-acceptance-marker]')) {const label=document.createElement('p');label.dataset.acceptanceMarker='true';label.textContent='受控界面验收 · 模型输出为测试替身 · 非真实行情分析';label.style.cssText='font-size:13px;padding:12px 16px;margin:0 0 12px;border:1px solid #8a7441;border-radius:6px;color:#d1b578;background:#272319';el.prepend(label);}});
    const filename=name+'.png'; await target.screenshot({path:path.join(SHOTS,filename),animations:'disabled',timeout:45000});
    images.push({name,file:'screenshots/'+filename,controlled});
  }
  async function fullDetails(prefix) {
    const body=inspector.locator('[class*="inspectorBody"]');
    const size=await body.evaluate(el=>({height:el.clientHeight,max:Math.max(0,el.scrollHeight-el.clientHeight)}));
    const positions=[...new Set([0,...Array.from({length:Math.ceil(size.max/Math.max(1,size.height-60))},(_,i)=>Math.min(size.max,(i+1)*Math.max(1,size.height-60)))])];
    for(let i=0;i<positions.length;i++){await body.evaluate((el,top)=>{el.scrollTop=top;},positions[i]);await shot(prefix+'-scroll-'+(i+1),inspector);}
    await body.evaluate(el=>{el.scrollTop=0;});
  }
  try {
    // Verify the real file-backed view; no template endpoint is used.
    for(const item of fixture.cases) {
      await open(item);
      await shot(item.case+'-workflow');
      const graph=await api('/strategies/runtime/workflow?strategy_id='+encodeURIComponent(item.strategy_id));
      await panel.getByRole('button',{name:'卡片一览',exact:true}).click();
      await shot(item.case+'-all-cards');
      const nodes=item.case==='script'?graph.strategy.nodes.filter(n=>n.kind==='script'):item.case==='macd_agent'?graph.strategy.nodes:graph.strategy.nodes.filter(n=>['scheduler','agent'].includes(n.kind));
      for(const node of nodes) {
        await panel.locator(`[data-workflow-node="${node.id}"]`).getByRole('button',{name:/^编辑详情/}).click();
        await expect(inspector.getByText('怎么修改',{exact:true})).toBeVisible();
        await shot(item.case+'-'+node.id.replace(/[^a-zA-Z0-9_-]/g,'-')+'-details');
        await fullDetails(item.case+'-'+node.id.replace(/[^a-zA-Z0-9_-]/g,'-')+'-essential');
        if(node.kind==='script') {
          await inspector.getByText('查看或编辑代码',{exact:true}).click();
          await inspector.getByLabel('编辑文件内容',{exact:true}).scrollIntoViewIfNeeded();
          await shot(item.case+'-script-code');
        }
        if(node.kind==='agent' || node.kind==='source') {
          await inspector.getByRole('tab',{name:'高级设置',exact:true}).click();
          await shot(item.case+'-'+node.id.replace(/[^a-zA-Z0-9_-]/g,'-')+'-advanced');
          await fullDetails(item.case+'-'+node.id.replace(/[^a-zA-Z0-9_-]/g,'-')+'-advanced');
          await inspector.getByRole('tab',{name:'常用设置',exact:true}).click();
        }
        await inspector.getByRole('button',{name:'关闭详情',exact:true}).click();
      }
      checks.push(item.case+' card details render');
    }
    const macd=fixture.cases.find(x=>x.case==='macd_agent');
    await open(macd); await panel.getByRole('button',{name:'卡片一览',exact:true}).click();
    const graph=await api('/strategies/runtime/workflow?strategy_id='+macd.strategy_id);
    const source=graph.strategy.nodes.find(n=>n.kind==='source' && n.binding.path?.[0]==='data_sources');
    await panel.locator(`[data-workflow-node="${source.id}"]`).getByRole('button',{name:/^编辑详情/}).click();
    const count=inspector.locator('input[type=number]').first();
    await count.fill('180');
    await check('card-local save validates and persists with custom parameters',async()=>{
      const savedPromise=page.waitForResponse(r=>r.url().includes('/workflow/propose') && r.request().method()==='POST');
      await inspector.getByRole('button',{name:/保存全部修改/}).click();
      const result=await (await savedPromise).json(); assert.equal(result.ok,true,JSON.stringify(result));
      assert.equal(result.workflow.manifest.data_sources[0].limit,180);
      assert.deepEqual(result.workflow.manifest.data_sources[0].parameters,source.config.parameters);
      fs.writeFileSync(path.join(ROOT,'saved-proposal.json'),JSON.stringify(result,null,2));
      await expect(panel).toHaveAttribute('aria-busy','false'); await shot('saved-review-proposal');
      await open(macd,result.proposal_id); await panel.getByRole('button',{name:'卡片一览',exact:true}).click();
      await panel.locator(`[data-workflow-node="${source.id}"]`).getByRole('button',{name:/^编辑详情/}).click();
      await expect(inspector.locator('input[type=number]').first()).toHaveValue('180');
      await inspector.locator('input[type=number]').first().fill('181');
      await inspector.getByRole('button',{name:'还原这张卡片',exact:true}).click();
      await expect(inspector.locator('input[type=number]').first()).toHaveValue('180');
      checks.push('reset affects only this card');
      await inspector.getByRole('button',{name:'关闭详情',exact:true}).click();
      await panel.getByRole('tab',{name:'运行记录',exact:true}).click();
      await expect(page.getByTestId('workflow-activity')).toContainText('候选本身不会运行');
      await shot('candidate-has-no-live-run',page.getByTestId('workflow-activity'));
    });
    await open(macd); await panel.getByRole('tab',{name:'复盘进化',exact:true}).click();
    await panel.getByRole('button',{name:'卡片一览',exact:true}).click();
    await shot('review-evolution-all-cards');
    for(const node of graph.evolution.nodes) {
      await panel.locator(`[data-workflow-node="${node.id}"]`).getByRole('button',{name:/^编辑详情/}).click();
      await expect(inspector.getByText('怎么修改',{exact:true})).toBeVisible();
      await shot('evolution-'+node.id.replace(/[^a-zA-Z0-9_-]/g,'-')+'-details');
      await fullDetails('evolution-'+node.id.replace(/[^a-zA-Z0-9_-]/g,'-')+'-essential');
      await inspector.getByRole('button',{name:'关闭详情',exact:true}).click();
    }
    checks.push('all evolution cards explain editing and protected outcomes');
    // In-flight data comes from the real executor with an explicitly controlled model.
    const scheduled=fixture.cases.find(x=>x.case==='scheduled_agent');
    await open(scheduled); await panel.getByRole('tab',{name:'运行记录',exact:true}).click();
    fs.writeFileSync(path.join(ROOT,'start-run'),'browser ready');
    await check('running Agent appears automatically with its own tool events',async()=>{
      await expect(page.getByTestId('workflow-activity')).toContainText('等待执行结束',{timeout:60000});
      await expect(page.getByTestId('workflow-run-detail')).toContainText('market_data',{timeout:20000});
      await shot('agent-running',page.getByTestId('workflow-activity'),true);
      await page.getByTestId('workflow-run-detail').locator('details').filter({hasText:'market_data'}).first().locator('summary').first().click();
      await shot('agent-running-tool-input',page.getByTestId('workflow-activity'),true);
    });
    fs.writeFileSync(path.join(ROOT,'continue-run'),'capture complete');
    await check('completed and failed tasks remain distinct, with actual output',async()=>{
      await expect.poll(async()=>fs.existsSync(path.join(ROOT,'failed.json')),{timeout:60000}).toBe(true);
      const completed=JSON.parse(fs.readFileSync(path.join(ROOT,'finished.json')));
      const failed=JSON.parse(fs.readFileSync(path.join(ROOT,'failed.json')));
      await page.getByRole('button',{name:'刷新记录',exact:true}).click();
      await expect(page.locator(`[data-run-id="${completed.task_id}"]`)).toBeVisible({timeout:20000});
      await page.locator(`[data-run-id="${completed.task_id}"]`).click();
      await expect(page.getByTestId('workflow-run-detail')).toContainText('不是真实模型分析',{timeout:20000});
      await shot('agent-completed',page.getByTestId('workflow-activity'),true);
      await page.locator(`[data-run-id="${failed.task_id}"]`).click();
      await expect(page.getByTestId('workflow-run-detail')).toContainText('模拟数据源超时',{timeout:20000});
      await shot('agent-failed',page.getByTestId('workflow-activity'),true);
      const listed=await api('/strategies/runtime/agent_tasks?strategy_id='+scheduled.strategy_id);
      assert.equal(listed.count,2);assert.equal(listed.event_count,4);checks.push('two tasks, four lifecycle events, no duplicate run rows');
    });
    for(const item of fixture.cases.filter(x=>x.case!=='scheduled_agent')) {
      await open(item); await panel.getByRole('tab',{name:'运行记录',exact:true}).click();
      await expect(page.getByTestId('workflow-activity')).not.toContainText('正在读取已有运行记录',{timeout:20000});
      await shot(item.case+'-runtime',page.getByTestId('workflow-activity'),true);
    }
    await open(macd); await panel.getByRole('button',{name:'卡片一览',exact:true}).click();
    await panel.locator('[data-workflow-node="script:main.py"]').getByRole('button',{name:/^编辑详情/}).click();
    await inspector.getByLabel('也可以直接说想怎么改',{exact:true}).fill('同一根K线只提醒一次，不改交易权限');
    await shot('ask-agent-to-edit');
    await inspector.getByRole('button',{name:'让主 Agent 帮我修改',exact:true}).click();
    await expect(page).toHaveURL(/\/chat/,{timeout:30000});
    await expect(page.locator('textarea').filter({hasText:'同一根K线只提醒一次'}).first()).toBeVisible({timeout:30000});
    checks.push('natural-language handoff remains an unsent draft');
    await page.screenshot({path:path.join(SHOTS,'main-agent-edit-draft.png'),fullPage:true});images.push({name:'main-agent-edit-draft',file:'screenshots/main-agent-edit-draft.png'});
    await open(macd); await page.setViewportSize({width:430,height:900});
    await panel.getByRole('button',{name:'卡片一览',exact:true}).click();
    await panel.locator('[data-workflow-node="script:main.py"]').getByRole('button',{name:/^编辑详情/}).click();
    await inspector.scrollIntoViewIfNeeded();
    await shot('mobile-script-details',inspector);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+2));checks.push('mobile viewport has no document overflow');
    await page.setViewportSize({width:1640,height:1180});
    await page.evaluate(()=>localStorage.setItem('nerya.ui_settings.v1',JSON.stringify({language:'zh',darkMode:'light'})));
    await page.reload();await expect(panel).toHaveAttribute('aria-busy','false',{timeout:60000});
    await shot('light-theme-workflow');
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('nerya.ui_settings.v1')).darkMode),'light');
    assert.equal(await page.evaluate(()=>document.documentElement.classList.contains('dark')),false);
    checks.push('light theme persisted and dark class absent');
    assert.equal(pageErrors.length,0,JSON.stringify(pageErrors));
  } catch(error) {
    failures.push({type:'assertion',message:String(error),stack:error.stack});
    fs.writeFileSync(path.join(ROOT,'failure-page.txt'),await page.locator('body').innerText().catch(()=>''));
    await page.screenshot({path:path.join(SHOTS,'verification-failure.png'),fullPage:true}).catch(()=>{});
  } finally {
    fs.writeFileSync(path.join(ROOT,'browser-review.json'),JSON.stringify({checked_at:new Date().toISOString(),checks,images,failures,pageErrors,evidence_scope:fixture.evidence_scope},null,2));
    await browser.close();
  }
  console.log(JSON.stringify({checks:checks.length,images:images.length,failures,pageErrors}));
  if(failures.length||pageErrors.length) process.exitCode=1;
}
main().catch(error=>{console.error(error);process.exitCode=1;});

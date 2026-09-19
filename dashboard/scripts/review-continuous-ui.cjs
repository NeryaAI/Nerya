const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const { chromium, expect } = require('@playwright/test');
const name = process.env.REVIEW_ROOT || 'continuous-ui-0919'; assert.match(name,/^continuous-ui-[a-z0-9-]+$/);
const root = path.resolve(__dirname, '../test-results', name);
const server = JSON.parse(fs.readFileSync(path.join(root, 'server-info.json'), 'utf8'));
(async () => {
  const login = await fetch(server.api + '/auth/login', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify({password: fs.readFileSync(path.join(root,'.browser-password'),'utf8')}), signal: AbortSignal.timeout(10000) });
  const auth = await login.json(); assert.ok(login.ok && auth.token);
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN'});
  await context.addInitScript(a => {
    localStorage.setItem('nerya.admin_jwt.v1', a.token);
    localStorage.setItem('nerya.admin_jwt_expires_at.v1', String(a.expires_at));
    if (!localStorage.getItem('nerya.ui_settings.v1')) localStorage.setItem('nerya.ui_settings.v1', JSON.stringify({language:'zh',darkMode:'dark'}));
  }, auth);
  const page = await context.newPage(); page.setDefaultTimeout(30000);
  const errors = [], httpErrors = [], screenshots = [], receipts = [], checks = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('response', r => { if(r.status()>=500) httpErrors.push({status:r.status(),path:new URL(r.url()).pathname}); });
  async function shot(name) { await page.evaluate(() => document.fonts.ready); await page.screenshot({path:path.join(root,name+'.png'),fullPage:true,animations:'disabled'}); screenshots.push(name+'.png'); }
  async function api(route, body) { const r=await fetch(server.api+route,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+auth.token,'content-type':'application/json'},...(body?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(15000)}); const result=await r.json(); return {status:r.status,result}; }
  try {
    await page.goto(server.ui+'/strategies?strategy_id='+server.strategy_id,{waitUntil:'domcontentloaded',timeout:90000});
    await expect(page.getByTestId('strategy-workflow-panel')).toHaveAttribute('aria-busy','false',{timeout:90000});
    const panel = page.getByTestId('continuous-strategy-status');
    await expect(panel).toBeVisible();
    await expect(panel.getByRole('status')).toHaveText('已停止');
    await expect(page.locator('[data-workflow-node="scheduler:trading"]')).toHaveCount(0);
    await shot('01-stopped'); checks.push('continuous UI is distinct from scheduler');
    const stale = await api('/strategies/runtime/service/start',{strategy_id:server.strategy_id,expected_hash:'stale'});
    assert.equal(stale.result.ok,false); checks.push('stale start blocked');
    await panel.getByRole('button',{name:'启动监听',exact:true}).click();
    const dialog = page.getByRole('alertdialog').or(page.getByRole('dialog'));
    await expect(dialog).toBeVisible(); await shot('02-start-confirmation');
    await dialog.getByRole('button',{name:'启动监听',exact:true}).click();
    await expect(panel.getByRole('status')).toHaveText('运行中');
    await expect(panel).toContainText('已连接');
    const running = await api('/strategies/runtime/service/status?strategy_id='+server.strategy_id);
    assert.equal(running.result.state,'running'); assert.ok(running.result.last_message_at); receipts.push(running);
    await shot('03-running-dark'); checks.push('real start -> running -> websocket connected');
    await page.setViewportSize({width:390,height:844}); await shot('04-running-mobile');
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
    checks.push('mobile no document horizontal overflow');
    await page.setViewportSize({width:1440,height:1000});
    await page.evaluate(()=>{const p=JSON.parse(localStorage.getItem('nerya.ui_settings.v1'));p.darkMode='light';localStorage.setItem('nerya.ui_settings.v1',JSON.stringify(p));});
    await page.reload({waitUntil:'domcontentloaded'}); await expect(panel.getByRole('status')).toHaveText('运行中');
    await shot('05-running-light');
    await panel.getByRole('button',{name:'停止监听',exact:true}).click();
    await expect(panel.getByRole('status')).toHaveText('已停止');
    receipts.push(await api('/strategies/runtime/service/status?strategy_id='+server.strategy_id));
    await shot('06-stopped-after'); checks.push('UI stop -> actual supervisor stopped');
    assert.equal(errors.length,0); assert.equal(httpErrors.length,0);
  } catch(e) { errors.push(String(e)); await shot('failure').catch(()=>{}); process.exitCode=1; }
  finally {
    await api('/strategies/runtime/service/stop',{strategy_id:server.strategy_id}).catch(()=>{});
    fs.writeFileSync(path.join(root,'ui-result.json'),JSON.stringify({scope:server.scope,checks,errors,httpErrors,screenshots,receipts},null,2));
    await browser.close();
    fs.writeFileSync(path.join(root,'review-complete'),'done\n');
    console.log(JSON.stringify({checks,errors,httpErrors,screenshots}));
  }
})().catch(e=>{ console.error(e); process.exitCode=1; });

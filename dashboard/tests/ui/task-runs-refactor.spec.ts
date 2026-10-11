import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';
import path from 'node:path';

const state=JSON.parse(readFileSync(path.resolve(process.cwd(),'../../ui-review/task-run-refactor-20261003/qa-state.json'),'utf8')) as {
  session_id:string;oldest_run_id:string;latest_run_id:string;grant_id:string};
test.beforeEach(async({page})=>{
  await page.addInitScript(()=>{localStorage.setItem('nerya.ui_settings.v1',JSON.stringify({language:'zh',darkMode:'dark'}));
    localStorage.setItem('nerya.admin_jwt.v1','qa-fixture-operator-not-real');});
});

test('1000-run history pages through real API and opens the precise historical run',async({page},info)=>{
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  await page.setViewportSize({width:1440,height:980});
  await page.goto('/workflows');
  await page.getByRole('tab',{name:'历史',exact:true}).click();
  const history=page.getByTestId('task-run-history');await expect(history.locator('tbody tr')).toHaveCount(50);
  const first=await history.locator('tbody tr').first().innerText();
  await history.getByRole('button',{name:'加载更早运行'}).click();await expect(history.locator('tbody tr')).toHaveCount(100);
  expect(await history.locator('tbody tr').first().innerText()).toBe(first);
  await page.screenshot({path:info.outputPath('automation-history-desktop.png')});
  await page.goto(`/chat/${state.session_id}?run=${state.oldest_run_id}`);
  const detail=page.getByTestId('task-run-detail');await expect(detail).toHaveAttribute('data-run-id',state.oldest_run_id);
  await expect(detail).toContainText('QA report 0.');await expect(detail).not.toContainText('QA report 999.');
  await page.screenshot({path:info.outputPath('old-run-focus-desktop.png')});
  await detail.getByRole('button',{name:'返回连续会话'}).click();await expect(detail).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('task grant remains a draft until separate approval and never enables live funds',async({page,request},info)=>{
  const headers={Authorization:'Bearer qa-fixture-operator-not-real'};
  const previous=await (await request.get('/api/proxy/financial/grants?task_kind=scheduled_agent&task_id=qa_review',{headers})).json();
  const descriptor=await (await request.get('/api/proxy/agent/tasks/descriptor?task_kind=scheduled_agent&task_id=qa_review',{headers})).json();
  const fresh=await (await request.post('/api/proxy/financial/grants',{headers,data:{task_kind:'scheduled_agent',task_id:'qa_review',
    expected_security_revision:descriptor.task.security_revision,policy:previous.grants[0].policy}})).json();
  await page.goto('/workflows');await page.getByTestId('task-financial-grants').locator('summary').click();
  const grants=page.locator(`[data-grant-id="${fresh.grant.grant_id}"]`);await expect(grants).toContainText('草稿');
  await expect(grants).toContainText('qa-wallet');await expect(grants).toContainText('10 / 30 / 50');
  await page.screenshot({path:info.outputPath('finite-grant-review.png')});
  const capabilities=await request.get('/api/proxy/financial/capabilities',{headers});expect((await capabilities.json()).enabled).toBe(false);
  await grants.getByRole('button',{name:'批准授权',exact:true}).click();
  await page.getByRole('button',{name:'批准',exact:true}).click();await expect(grants).toContainText('已生效');
  expect((await (await request.get('/api/proxy/financial/capabilities',{headers})).json()).enabled).toBe(false);
  expect((await (await request.get('/api/proxy/financial/actions',{headers})).json()).actions).toHaveLength(0);
});

test('mobile run detail retains result and control access without horizontal overflow',async({page},info)=>{
  await page.setViewportSize({width:390,height:844});await page.goto(`/chat/${state.session_id}?run=${state.latest_run_id}`);
  const detail=page.getByTestId('task-run-detail');await expect(detail).toContainText('QA report 999.');
  await expect(detail.getByRole('button',{name:'创建新的运行'})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('task-run-mobile.png')});
});

test('readonly transport cannot execute or grant authority through body assertions',async({request})=>{
  const headers={Authorization:'Bearer qa-fixture-readonly-not-real'};
  const descriptor=await (await request.get('/api/proxy/agent/tasks/descriptor?task_kind=scheduled_agent&task_id=qa_review',{headers})).json();
  const run=await request.post('/api/proxy/agent/runs',{headers,data:{task_kind:'scheduled_agent',task_id:'qa_review',
    client_request_id:'readonly-spoof',expected_task_revision:descriptor.task.source_revision,_auth_actor_id:'local:loopback',_auth_scopes:['api:all']}});
  expect(run.status()).toBe(403);
  expect((await request.post('/api/proxy/triggers/schedules/tick',{headers,data:{_auth_scopes:['api:all']}})).status()).toBe(403);
  expect((await request.post(`/api/proxy/financial/grants/${state.grant_id}/approve`,{headers,data:{expected_revision:1,_auth_scopes:['api:all']}})).status()).toBe(403);
});

test('manual occurrence has its own identity and keeps schedule firing clock unchanged',async({request})=>{
  const headers={Authorization:'Bearer qa-fixture-operator-not-real'};
  const before=await (await request.get('/api/proxy/triggers/schedules/status?id=qa_review',{headers})).json();
  const descriptor=await (await request.get('/api/proxy/agent/tasks/descriptor?task_kind=scheduled_agent&task_id=qa_review',{headers})).json();
  const data={task_kind:'scheduled_agent',task_id:'qa_review',expected_task_revision:descriptor.task.source_revision,client_request_id:'manual-qa-identity'};
  const first=await (await request.post('/api/proxy/agent/runs',{headers,data})).json();
  const duplicate=await (await request.post('/api/proxy/agent/runs',{headers,data})).json();
  expect(first.run_id).toBe(duplicate.run_id);expect(duplicate.duplicate).toBe(true);
  const after=await (await request.get('/api/proxy/triggers/schedules/status?id=qa_review',{headers})).json();
  expect(after.schedules[0].last_fired_ts).toBe(before.schedules[0].last_fired_ts);
  const view=await (await request.get('/api/proxy/agent/runs/'+first.run_id,{headers})).json();
  expect(view.run.trigger_kind).toBe('manual');expect(view.run.task_id).toBe('qa_review');
});

test('incoming manual result preserves the historical Run without a duplicate chat history panel',async({page,request})=>{
  const headers={Authorization:'Bearer qa-fixture-operator-not-real'};
  await page.goto(`/chat/${state.session_id}?run=${state.oldest_run_id}`);
  const detail=page.getByTestId('task-run-detail');await expect(detail).toContainText('QA report 0.');
  await expect(page.getByTestId('task-run-history')).toHaveCount(0);
  const before=await page.getByTestId('transcript-scroll').evaluate(element=>element.scrollTop);
  const descriptor=await (await request.get('/api/proxy/agent/tasks/descriptor?task_kind=scheduled_agent&task_id=qa_review',{headers})).json();
  const receipt=await (await request.post('/api/proxy/agent/runs',{headers,data:{task_kind:'scheduled_agent',task_id:'qa_review',
    expected_task_revision:descriptor.task.source_revision,client_request_id:'reading-anchor-'+Date.now()}})).json();
  expect(receipt.ok).toBe(true);
  await expect(page.getByTestId('task-run-history')).toHaveCount(0);
  await expect(detail).toHaveAttribute('data-run-id',state.oldest_run_id);await expect(detail).toContainText('QA report 0.');
  expect(await page.getByTestId('transcript-scroll').evaluate(element=>element.scrollTop)).toBe(before);
});

test('task conversation accepts a follow-up and preserves its Run identity',async({page})=>{
  await page.goto(`/chat/${state.session_id}`);
  await expect(page.getByTestId('task-run-history')).toHaveCount(0);
  const send=page.getByTestId('native-command-send');
  await page.locator('[data-chat-composer] textarea').fill('Explain the latest QA report without executing external actions.');
  await expect(send).toBeEnabled();
  await send.click();
  await expect(page.getByTestId('transcript-scroll')).toContainText('Manual QA run completed using the fake execution adapter.');
});

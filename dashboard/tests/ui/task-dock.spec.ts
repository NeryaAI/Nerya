import { test, expect, type Page } from '@playwright/test';

const SESSION='task-dock-review';
const report='## Browser review\n\nThe report is ready to review. The working browser and network results stay in the side panel.\n\n### Checked\n\nPage content, the response status, and the saved report.\n\nThis is a synthetic UI acceptance task, not a production account.';
const browserPayload=(n:number)=>({skill_id:'browser',name:'browser_session.py',args:['--json',JSON.stringify({operation:'batch',session_id:'mb_dock_fixture',request_id:`dock-action-${n}`})]});
const browserBlocks=(n:number)=>[{block:{kind:'tool_use',action:'script_run',call_id:`browser-${n}`,payload:browserPayload(n)}},{block:{kind:'tool_result',action:'script_run',call_id:`browser-${n}`,ok:true,result:{ok:true}}}];

async function fixture(page:Page,{browser=true,answer=true,members=false,locale='en',theme='dark'}={}){
  const errors:string[]=[],requests:Record<string,any>[]=[];
  let running=false,seq=0,human=false;
  let finish:(()=>void)|undefined;
  const oldViewport=page.viewportSize()!;
  await page.setViewportSize({width:1280,height:800});
  await page.setContent('<!doctype html><meta charset="utf-8"><style>body{margin:0;background:#f5f6f8;color:#273343;font:18px system-ui}main{max-width:820px;margin:42px auto;padding:30px;background:white}small{color:#566172}h1{font-size:28px}label{display:block;margin:22px 0}input{padding:12px;font:inherit;border:1px solid #aeb6c3;width:90%}button{font:inherit;padding:10px 20px}p{line-height:1.6}</style><main><small>LOCAL ACCEPTANCE PAGE · SYNTHETIC DATA</small><h1>Research report</h1><p>Review the source register and save the checked report.</p><label>Report name<input value="Evidence review"></label><button>Save report</button><p>Saved. The response is available in Network.</p></main>');
  const actionBox=(await page.getByRole('button',{name:'Save report',exact:true}).boundingBox())!;
  const image='data:image/png;base64,'+(await page.screenshot()).toString('base64');
  await page.setViewportSize(oldViewport);
  await page.addInitScript(({locale,theme})=>{if(top===window)localStorage.setItem('nerya.ui_settings.v1',JSON.stringify({language:locale,darkMode:theme}));},{locale,theme});
  page.on('pageerror',e=>errors.push(e.message));
  const stamp=new Date().toISOString();
  const meta=(id:string)=>({session_id:id,title:id===SESSION?'Review the report in the work browser':'A separate conversation',created_at:stamp,updated_at:stamp,message_count:answer?2:1});
  const agent={id:'reviewer',name:'Reviewer',session_id:SESSION,group_id:'research',title:'Verify the report',state:'completed',attempt:1,updated_at:Date.now()/1000,output:{summary:'The source register is ready.'},context:{parent_session_id:SESSION,scope:'subagent',inherited_messages:2,saved_messages:4}};
  const networkRow={id:'net_fixture',seq:1,url:'https://research.test/api/save',method:'POST',type:'fetch',status:200,state:'finished',duration_ms:42,bytes:58};
  await page.route('**/api/**',async route=>{
    const u=new URL(route.request().url()),path=u.pathname.replace(/^\/api\/proxy/,'');
    const input=route.request().method()==='POST'?route.request().postDataJSON()||{}:{};
    const id=u.searchParams.get('session_id')||SESSION;
    let body:any={ok:true,items:[],count:0,total:0,events:[],approvals:[]};
    if(path==='/browsers/desktop'){
      requests.push(input);
      if(input.operation==='command'){human=input.command==='handoff'?true:input.command==='resume'?false:human;}
      const tabs=[{id:'1',url:'https://research.test/report',selected:true,protected:false}];
      const visual={action:'click',boxes:[actionBox],cursor:{x:actionBox.x+actionBox.width/2,y:actionBox.y+actionBox.height/2},url:tabs[0].url,ts:Date.now()/1000};
      if(input.operation==='network')body={ok:true,listening:true,requests:input.after?[]:[networkRow],cursor:1,generation:'fixture',retained:1};
      else if(input.operation==='network_detail')body={ok:true,request:{...networkRow,request_headers:{'content-type':'application/json'},response_headers:{'content-type':'application/json'},...(input.include_body?{body:'{"saved":true,"count":1}',body_state:'available'}:{})}};
      else if(input.operation==='trace')body={ok:true,status:running?'running':'completed',events:input.after?[]:[{seq:1,kind:'step',index:0,action:'click',phase:'completed',target:'Save report',frame_id:1}],cursor:1,controllable:true,session_id:'mb_dock_fixture',profile_id:'work',visual,frame_state:'available',frame:{image,frame_id:1,url:tabs[0].url,tabs,visual,ts:Date.now()/1000}};
      else body={ok:true,running:true,paused:human,human_control:human,control_id:human?'control_fixture':'',sensitive:false,tabs,image,agent_access:{enabled:true,occupied:true,executing:running,session_id:'mb_dock_fixture'},preferences:{automatic:true},config:{extensions:[]}};
    }else if(path==='/agent/sessions')body={sessions:[meta(SESSION),meta('task-dock-other')],has_more:false};
    else if(path==='/agent/session')body={...meta(id),id,messages:[]};
    else if(path==='/agent/session/transcript')body={ok:true,...meta(id),messages:[{message_id:'user-one',role:'user',content:meta(id).title,ts:stamp},...(answer&&id===SESSION?[{message_id:'answer-one',role:'assistant',content:report,ts:stamp,turn:{turn_id:'turn-one',reply_text:report,blocks:browser?browserBlocks(0):[]}}]:[])]};
    else if(path==='/agent/stream/events')body={events:running&&Number(u.searchParams.get('after_seq')||0)<seq?[{seq,event_id:`event-${seq}`,kind:'tool.start',skill:'script_run',action:'script_run',call_id:`browser-${seq}`,tool_call_id:`browser-${seq}`,skill_id:'native',session_id:SESSION,payload:browserPayload(seq)}]:[],cursor:seq,latest_seq:seq};
    else if(path==='/agent/run_turn_internal'){running=true;seq++;await new Promise<void>(r=>{finish=r;});running=false;body={turn_id:'live-turn',reply_text:report,blocks:browserBlocks(seq)};}
    else if(path==='/teams/agents')body={ok:true,agents:members&&id===SESSION?[agent]:[]};
    else if(path==='/teams/agents/get')body={ok:true,agent,events:[],messages:[],has_more:false};
    else if(path==='/auth/status')body={ok:true,authenticated:true,password_set:true,enabled:true};
    else if(path==='/operator/nav')body={ok:true,primary:[],advanced:[],data:{primary:[],advanced:[]}};
    else if(path==='/operator/overview')body={status:'ok',data:{attention:[],counts:{},accounts:[],strategies:[]}};
    else if(path==='/workspace')body={root:'task-dock-synthetic',live_trading_enabled:false,kill_switch:false};
    else if(path==='/llm/config')body={ok:true,tiers:[],provider_profiles:[],default_tier:'medium',reasoning_levels:['none','low','medium','high']};
    else if(path==='/llm/providers'||path==='/llm/catalog')body={providers:[]};
    else if(path==='/llm/tiers')body={tiers:[],count:0};
    else if(path==='/market/venues')body={venues:[]};
    else if(path==='/accounts/list')body={accounts:[],ts:0};
    else if(path.includes('strategy/list'))body={ok:true,strategies:[]};
    await route.fulfill({json:body});
  });
  await page.goto(`/chat/${SESSION}`);
  return {errors,requests,started:()=>running,advance:()=>{seq++;},finish:()=>finish?.()};
}
const tabs=(page:Page)=>page.getByTestId('task-dock-header').getByRole('tablist');
const close=(page:Page)=>page.getByTestId('task-dock-header').getByRole('button',{name:/Hide workspace|收起工作区/}).click();

for(const theme of ['dark','light'])test(`one content-driven panel, preserved source and keyboard navigation (${theme})`,async({page})=>{
  await page.setViewportSize({width:1440,height:1000});const state=await fixture(page,{members:true,theme});
  const nav=tabs(page);
  await expect(nav.getByRole('tab')).toHaveCount(3);
  await expect(page.getByTestId('task-topbar').getByRole('tablist')).toHaveCount(0);
  await expect(page.getByTestId('toggle-browser-panel')).toHaveCount(0);
  await expect(nav.getByRole('tab',{name:'Browser',exact:true})).toHaveAttribute('aria-selected','true');
  await expect(page.getByTestId('workspace-source')).toBeVisible();
  await expect(page.getByTestId('browser-input-shield')).toBeVisible();
  await page.screenshot({path:`test-results/task-dock-${theme}.png`});
  await nav.getByRole('tab',{name:'Browser',exact:true}).focus();await page.keyboard.press('ArrowRight');
  await expect(nav.getByRole('tab',{name:'Canvas',exact:true})).toBeFocused();
  await expect(page.getByTestId('canvas-workspace')).toBeVisible();
  await expect(page.getByRole('tab',{name:'Files',exact:true})).toHaveCount(0);
  await page.keyboard.press('End');
  await expect(nav.getByRole('tab',{name:/Agents/})).toHaveAttribute('aria-selected','true');
  await expect(page.getByTestId('agent-work-panel')).toBeVisible();await expect(page.getByTestId('workspace-source')).toBeVisible();
  await page.keyboard.press('Escape');await expect(page.locator('#task-workspace')).not.toBeVisible();
  await expect(page.getByTestId('open-workspace')).toBeFocused();
  await page.screenshot({path:`test-results/task-dock-collapsed-${theme}.png`});
  await page.getByTestId('open-workspace').click();await expect(nav.getByRole('tab',{name:/Agents/})).toHaveAttribute('aria-selected','true');
  await page.getByTestId('task-dock-header').getByRole('button',{name:'Workspace layout'}).click();await page.keyboard.press('Escape');
  await expect(page.locator('#task-workspace')).toBeVisible();
  await page.getByTestId('task-dock-header').getByRole('button',{name:'Workspace layout'}).click();
  await page.getByRole('menuitem',{name:'Expand workspace',exact:true}).click();
  await expect(page.getByTestId('workspace-source')).not.toBeVisible();
  await expect(nav.getByRole('tab',{name:/Agents/})).toHaveAttribute('aria-selected','true');
  await page.getByTestId('task-dock-header').getByRole('button',{name:'Workspace layout'}).click();
  await page.getByRole('menuitem',{name:'Restore side panel',exact:true}).click();
  await expect(page.getByTestId('workspace-source')).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('empty task has no empty panel tabs, manual browser remains discoverable',async({page})=>{
  const state=await fixture(page,{browser:false,answer:false});
  await expect(page.getByTestId('task-topbar')).toBeVisible();
  await expect(page.getByTestId('task-dock-header')).toHaveCount(0);await expect(page.getByTestId('open-workspace')).toHaveCount(0);
  await page.getByTestId('task-title-menu').click();await page.getByRole('menuitem',{name:'Open browser',exact:true}).click();
  await expect(tabs(page).getByRole('tab')).toHaveCount(1);await expect(tabs(page).getByRole('tab',{name:'Browser',exact:true})).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('a result-only conversation has only its Canvas tab',async({page})=>{
  const state=await fixture(page,{browser:false});
  await expect(tabs(page).getByRole('tab')).toHaveCount(1);
  await expect(tabs(page).getByRole('tab',{name:'Canvas',exact:true})).toBeVisible();
  await expect(page.getByTestId('browser-workspace')).toHaveCount(0);
  await expect(page.getByTestId('agent-work-panel')).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test('new browser calls do not steal selection or reopen a manually hidden panel',async({page})=>{
  await page.setViewportSize({width:1440,height:1000});const state=await fixture(page);
  await tabs(page).getByRole('tab',{name:'Canvas',exact:true}).click();
  const composer=page.locator('[data-chat-composer="docked"] textarea');await composer.fill('Continue reviewing the website');await composer.press('Enter');
  await expect.poll(state.started).toBe(true);
  try{
    await expect(page.getByLabel('Browser operation',{exact:true}).locator('option')).toHaveCount(2);
    expect(state.requests.some(r=>r.operation==='trace'&&r.call_id==='browser-1')).toBe(false);
    await expect(tabs(page).getByRole('tab',{name:'Canvas',exact:true})).toHaveAttribute('aria-selected','true');
    await close(page);state.advance();
    await expect(page.getByLabel('Browser operation',{exact:true}).locator('option')).toHaveCount(3);
    await expect(page.locator('#task-workspace')).not.toBeVisible();
    state.finish();await expect.poll(state.started).toBe(false);
    await page.reload();await expect(page.getByTestId('open-workspace')).toBeVisible();
    await expect(page.locator('#task-workspace')).not.toBeVisible();
    await page.getByTestId('open-workspace').click();await expect(tabs(page).getByRole('tab',{name:'Canvas',exact:true})).toHaveAttribute('aria-selected','true');
    expect(state.errors).toEqual([]);
  }finally{state.finish();}
});

test('switching keeps network state and pauses hidden observation without stopping the browser',async({page})=>{
  await page.setViewportSize({width:1440,height:1000});const state=await fixture(page);
  const browser=page.getByTestId('browser-workspace');await browser.getByRole('button',{name:'More browser tools'}).click();await page.getByRole('menuitem',{name:'Network',exact:true}).click();
  const network=page.getByTestId('browser-network');await network.getByLabel('Filter requests').fill('api/save');
  await network.getByRole('listitem').click();await network.getByRole('button',{name:'Response',exact:true}).click();await expect(network.locator('pre')).toContainText('"saved":true');
  await tabs(page).getByRole('tab',{name:'Canvas',exact:true}).click();await page.waitForTimeout(300);
  const readCount=state.requests.filter(r=>r.operation==='network').length;await page.waitForTimeout(1700);
  expect(state.requests.filter(r=>r.operation==='network')).toHaveLength(readCount);
  await tabs(page).getByRole('tab',{name:'Browser',exact:true}).click();
  await expect(network.getByLabel('Filter requests')).toHaveValue('api/save');await expect(network.locator('pre')).toContainText('"saved":true');
  await page.screenshot({path:'test-results/task-dock-network.png'});
  expect(state.requests.some(r=>['close','agent_revoke','open'].includes(r.operation))).toBe(false);
  expect(state.errors).toEqual([]);
});

test('narrow Chinese workspace fits and collapses back to the retained conversation draft',async({page})=>{
  await page.setViewportSize({width:390,height:844});const state=await fixture(page,{members:true,locale:'zh'});
  await expect(tabs(page).getByRole('tab')).toHaveCount(3);
  expect(await page.locator('#task-workspace').evaluate(e=>e.scrollWidth<=e.clientWidth+1)).toBe(true);
  await page.screenshot({path:'test-results/task-dock-mobile.png'});
  await close(page);const composer=page.locator('[data-chat-composer="docked"] textarea');await composer.fill('保留这段草稿');
  await page.getByTestId('open-workspace').click();await tabs(page).getByRole('tab',{name:'Canvas',exact:true}).click();await close(page);
  await expect(composer).toHaveValue('保留这段草稿');expect(state.errors).toEqual([]);
});

test('layout preference is scoped to the conversation',async({page})=>{
  await page.setViewportSize({width:1440,height:1000});await fixture(page);
  await close(page);await page.goto('/chat/task-dock-other');
  await expect(page.getByTestId('task-topbar')).toContainText('A separate conversation');await expect(page.locator('#task-workspace')).toHaveCount(0);
  await page.goto(`/chat/${SESSION}`);await expect(page.getByTestId('open-workspace')).toBeVisible();await expect(page.locator('#task-workspace')).not.toBeVisible();
});

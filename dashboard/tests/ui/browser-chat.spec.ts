import { test, expect, type Page } from '@playwright/test';

const session='browser-sidebar-acceptance', callId='browser-side-operation';
const payload={skill_id:'browser',name:'browser_session.py',args:['--json',JSON.stringify({operation:'batch',session_id:'mb_fixture'})]};
const use={block:{kind:'tool_use',action:'script_run',skill_id:'native',call_id:callId,payload}};
const result={block:{kind:'tool_result',action:'script_run',skill_id:'native',call_id:callId,ok:true,result:{ok:true,session_id:'mb_fixture'}}};

async function fixture(page:Page, {live=false,child=false,realApi=''}={}){
  if(realApi && (new URL(realApi).hostname!=='127.0.0.1'||new URL(realApi).protocol!=='http:'))throw new Error('Only an explicit loopback test API is allowed');
  const errors:string[]=[], requests:Record<string,any>[]=[];
  let finished=!live,started=false,human=false,fail=false,expired=false,automatic=true;
  let release:(()=>void)|undefined;
  let tabs=[{id:'1',url:'https://example.test/review',selected:true},{id:'2',url:'https://example.test/notes',selected:false}];
  let history=[{id:'history-1',url:'https://example.test/earlier',at:Date.now()/1000}];
  let extensions=[{name:'Review helper',version:'1.0',path:'/synthetic/review-helper',extension_id:'abcdefghijklmnopabcdefghijklmnop',digest:'synthetic',permissions:['storage'],host_permissions:[],content_script_matches:[],control_ui:true,enabled:true}];
  const viewport=page.viewportSize()!;
  await page.setViewportSize({width:1280,height:800});
  await page.setContent('<!doctype html><style>body{margin:0;background:#f3f5f7;font:18px system-ui;color:#243043}main{margin:48px auto;width:800px;background:white;padding:32px;border:1px solid #dce2e8;border-radius:12px}label{display:block;margin:24px 0}input,select{display:block;width:100%;box-sizing:border-box;padding:14px;margin-top:8px;border:1px solid #bbc7d4;border-radius:7px;font:18px system-ui}button{background:#223d63;color:white;padding:14px 25px;border:0;border-radius:6px}</style><main><small>LOCAL ACCEPTANCE SITE · SYNTHETIC DATA</small><h1>Review the report</h1><p>The Agent is preparing this form in the shared work browser.</p><label>Name<input value="Example"></label><label>Report type<select><option>Summary report</option></select></label><button>Preview report</button><p>Ready for review. No production account is connected.</p></main>');
  const box=(await page.getByLabel('Name', {exact:true}).boundingBox())!;
  const image='data:image/png;base64,'+(await page.screenshot()).toString('base64');
  await page.setViewportSize(viewport);
  await page.addInitScript(()=>localStorage.setItem('nerya.ui_settings.v1',JSON.stringify({language:'en',darkMode:'dark'})));
  page.on('pageerror',e=>errors.push(e.message));
  const stamp=new Date().toISOString(),meta={session_id:session,title:'Review the report in the work browser',created_at:stamp,updated_at:stamp,message_count:2};
  await page.route('**/api/**',async route=>{
    const url=new URL(route.request().url()),path=url.pathname.replace(/^\/api\/proxy/,'');
    const input=route.request().method()==='POST'?route.request().postDataJSON()||{}:{};
    let body:unknown={ok:true,items:[],count:0,total:0,events:[],approvals:[]};
    if(path==='/browsers/desktop'){
      requests.push(input);
      if(realApi){
        if(!['surface','network','network_detail','command','human_command','history','preferences'].includes(input.operation))throw new Error('Unexpected live test operation');
        const response=await page.request.post(realApi+'/browsers/desktop',{data:input});
        await route.fulfill({status:response.status(),body:await response.text(),contentType:'application/json'});return;
      }
      if(fail && ['surface','trace'].includes(input.operation)){await route.fulfill({status:503,json:{ok:false}});return;}
      if(input.operation==='command'){
        const command=input.command==='trace_control'?input.payload.control:input.command;
        if(command==='handoff')human=true;
        if(command==='resume')human=false;
      }
      if(input.operation==='preferences' && typeof input.automatic==='boolean')automatic=input.automatic;
      if(input.operation==='human_command'){
        if(!human || input.control_id!=='synthetic-control'){await route.fulfill({json:{ok:false,error:'take_over_before_manual_input'}});return;}
        if(input.command==='new_tab'){tabs=tabs.map(t=>({...t,selected:false}));tabs.push({id:String(tabs.length+1),url:'about:blank',selected:true});}
        if(input.command==='select_tab')tabs=tabs.map(t=>({...t,selected:t.id===input.payload.tab_id}));
        if(input.command==='close_tab')tabs=tabs.filter(t=>t.id!==input.payload.tab_id);
        if(input.command==='navigate')tabs=tabs.map(t=>t.selected?{...t,url:input.payload.url}:t);
      }
      if(input.operation==='history_clear')history=[];
      if(input.operation==='extension_apply')extensions=input.extensions;
      const visual={action:'dom',boxes:[box],cursor:{x:box.x+80,y:box.y+25},url:'https://example.test/review',ts:Date.now()/1000};
      if(input.operation==='trace'){
        if(input.conversation_id!==session||input.call_id!==callId)throw new Error('trace identity mismatch');
        const events=Array.from({length:finished?12:3},(_,index)=>{
          const base={ts:Date.now()/1000,kind:'step',index,action:index===0?'fill':index===1?'click':'wait_for',target:`Field ${index+1}`};
          return [{...base,seq:index*2+1,phase:'started',frame_id:0},...(!finished&&index===2?[]:[{...base,seq:index*2+2,phase:'completed',frame_id:index+1}])];
        }).flat();
        body={ok:true,call_id:callId,conversation_id:session,status:finished?'completed':'running',paused:human,controllable:!expired,cursor:events.length,
          events:events.filter(e=>e.seq>(input.after||0)),profile_id:'work',session_id:'mb_fixture',visual,
          frame_state:human?'paused':expired?'expired':'available',frame:human||expired?null:{image,frame_id:input.frame_id||20,ts:Date.now()/1000,url:input.frame_id?`https://example.test/step-${input.frame_id}`:'https://example.test/review',tabs,visual}};
      }else body={ok:true,running:true,paused:human,human_control:human,control_id:human?'synthetic-control':'',sensitive:false,tabs,
        preferences:{automatic},config:{id:'work',version:1,extensions},agent_access:{occupied:true,session_id:'mb_fixture',enabled:automatic},
        ...(human?{image}:{}),...(['history','history_clear'].includes(input.operation)?{history}:{})};
    }else if(path==='/agent/sessions')body={sessions:[meta],has_more:false};
    else if(path==='/agent/session')body={...meta,id:session,messages:[]};
    else if(path==='/agent/session/transcript')body={ok:true,...meta,messages:[
      {role:'user',message_id:'user-one',content:meta.title,ts:stamp},
      ...(live&&finished?[{role:'user',message_id:'browser-side-turn:user',content:'Fill the form and show the browser in the side panel',ts:stamp}]:[]),
      ...(!live||finished?[{role:'assistant',message_id:live?'browser-side-turn:assistant':'assistant-one',content:'The form is ready for review.',ts:stamp,
        turn:{turn_id:'browser-side-turn',reply_text:'The form is ready for review.',blocks:realApi?[]:[use,result]}}]:[])]};
    else if(path==='/agent/stream/events'){
      const after=Number(url.searchParams.get('after_seq')||0);
      const events=started&&after<1?[{seq:1,event_id:'browser-start',kind:child?'subagent.step':'tool.start',skill:'script_run',action:child?'(native)':'script_run',step_kind:'act',status:'started',subagent:'Browser helper',tool_call_id:callId,call_id:callId,skill_id:'native',payload,session_id:session}]:[];
      body={events,latest_seq:started?1:0,cursor:started?1:0,count:events.length};
    }else if(path==='/agent/run_turn_internal'){
      started=true;await new Promise<void>(resolve=>{release=resolve;});finished=true;
      body={turn_id:'browser-side-turn',reply_text:'The form is ready for review.',blocks:[use,result]};
    }else if(path==='/teams/agents')body={ok:true,agents:[]};
    else if(path==='/auth/status')body={ok:true,authenticated:true,password_set:true,enabled:true};
    else if(path==='/operator/nav')body={ok:true,data:{primary:[],advanced:[]},primary:[],advanced:[]};
    else if(path==='/operator/overview')body={status:'ok',data:{attention:[],counts:{},accounts:[],strategies:[]}};
    else if(path==='/workspace')body={root:'synthetic-browser-sidebar',live_trading_enabled:false,kill_switch:false};
    else if(path==='/llm/config')body={ok:true,tiers:[],provider_profiles:[],default_tier:'medium',reasoning_levels:['none','low','medium','high']};
    else if(path==='/llm/providers'||path==='/llm/catalog')body={providers:[]};
    else if(path==='/llm/tiers')body={tiers:[],count:0};
    else if(path==='/market/venues')body={venues:[]};
    else if(path==='/accounts/list')body={accounts:[],ts:0};
    else if(path.includes('strategy/list'))body={ok:true,strategies:[]};
    await route.fulfill({json:body}); // All API calls stay isolated from the user's runtime.
  });
  await page.goto(`/chat/${session}`);
  if(realApi){await page.getByTestId('task-title-menu').click();await page.getByRole('menuitem',{name:'Open browser',exact:true}).click();}
  return {errors,requests,started:()=>started,finish:()=>{finished=true;release?.();},fail:(v:boolean)=>{fail=v;},expire:()=>{expired=true;}};
}

async function showTool(page:Page, name:string){
  await page.getByTestId('browser-workspace').getByRole('button',{name:'More browser tools',exact:true}).click();
  await page.getByRole('menuitem',{name:name==='Tabs'?/^Tabs/:name,exact:name!=='Tabs'}).click();
}

for(const child of [false,true])test(`${child?'child':'main'} browser opens beside conversation while executing`,async({page})=>{
  await page.setViewportSize({width:1440,height:1050});
  const state=await fixture(page,{live:true,child});
  const input=page.locator('[data-chat-composer="docked"] textarea');
  await input.fill('Fill the form and show the browser in the side panel');await input.press('Enter');
  await expect.poll(state.started).toBe(true);
  try{
    const panel=page.getByTestId('browser-workspace');
    await expect(panel).toBeVisible();await expect(panel.getByAltText('Live browser viewport')).toBeVisible();
    await expect(page.locator('[data-turn-role="assistant"] [data-testid="browser-workspace"]')).toHaveCount(0);
    await expect(page.getByTestId('chat-browser-trace')).toHaveCount(0);
    const source=await page.getByTestId('workspace-source').boundingBox(),side=await panel.boundingBox();
    expect(side!.x).toBeGreaterThanOrEqual(source!.x+source!.width);
    await expect(panel.locator('[data-browser-step="2"]')).toContainText('Running');
    await expect(panel.getByTestId('browser-action-overlay')).toBeVisible();
    await expect(panel.getByTestId('browser-action-overlay').locator('rect')).toHaveCount(1);
    await expect(panel.getByTestId('browser-input-shield')).toBeVisible();
    await expect(panel.locator('header button')).toHaveCount(2);
    await expect(panel.getByTestId('browser-operation-details')).not.toHaveAttribute('open','');
    await expect(panel.getByRole('button',{name:'New tab',exact:true})).toHaveCount(0);
    await expect(panel.getByRole('tablist')).toHaveCount(0);
    state.finish();await expect(panel.locator('[data-browser-step]')).toHaveCount(12);
    await page.screenshot({path:'test-results/browser-compact-desktop.png'});
    await showTool(page,'Operation history');
    await expect(panel.locator('[data-browser-step="11"]')).toBeVisible();
    expect(state.errors).toEqual([]);
  }finally{state.finish();}
});

test('shield blocks input and takeover unlocks keyboard, tabs, navigation and history',async({page})=>{
  const state=await fixture(page);const panel=page.getByTestId('browser-workspace');
  await expect(panel.getByTestId('browser-input-shield')).toBeVisible();
  await panel.getByTestId('browser-input-shield').click({position:{x:30,y:30}});
  expect(state.requests.filter(r=>r.operation==='human_command')).toHaveLength(0);
  await showTool(page,'Tabs');
  await expect(panel.getByRole('button',{name:'New tab',exact:true})).toBeDisabled();
  await panel.getByRole('button',{name:'Back to page',exact:true}).click();
  await panel.getByRole('button',{name:'Take over',exact:true}).click();
  await expect(panel.getByTestId('browser-input-shield')).toHaveCount(0);
  await expect(panel.getByAltText('Live browser viewport')).toBeVisible();
  await showTool(page,'Tabs');
  await panel.getByRole('button',{name:'New tab',exact:true}).click();
  await expect(panel.getByRole('button',{name:'Close tab 3',exact:true})).toBeVisible();
  await panel.getByRole('button',{name:'Close tab 3',exact:true}).click();
  await expect(panel.getByRole('button',{name:'Close tab 3',exact:true})).toHaveCount(0);
  await panel.getByRole('button',{name:'example.test/review',exact:true}).click();
  await panel.getByTestId('browser-viewport').click({position:{x:50,y:50}});
  await page.keyboard.type('hello');
  await expect.poll(()=>state.requests.some(r=>r.operation==='human_command'&&r.command==='type')).toBe(true);
  await showTool(page,'History');
  await expect(panel).toContainText('example.test/earlier');
  await panel.getByRole('button').filter({hasText:'example.test/earlier'}).click();
  await expect(panel.getByLabel('Browser address')).toHaveValue('https://example.test/earlier');
  await panel.getByRole('button',{name:'Give to Agent',exact:true}).click();
  await expect(panel.getByTestId('browser-input-shield')).toBeVisible();
  await expect(panel.getByLabel('Browser address')).toBeDisabled();
  expect(state.errors).toEqual([]);
});

test('extensions open and can be disabled or removed from the side panel',async({page})=>{
  const state=await fixture(page);const panel=page.getByTestId('browser-workspace');
  await panel.getByRole('button',{name:'Take over',exact:true}).first().click();
  await expect(panel.getByRole('button',{name:'Give to Agent',exact:true})).toBeVisible();
  await showTool(page,'Extensions');
  await expect(panel).toContainText('Review helper');
  await panel.getByRole('button',{name:'Open',exact:true}).click();
  await expect.poll(()=>state.requests.some(r=>r.command==='extension_open')).toBe(true);
  await showTool(page,'Extensions');
  await panel.getByRole('button',{name:'Disable',exact:true}).click();
  await page.getByRole('dialog').getByRole('button',{name:/confirm/i}).click();
  await expect.poll(()=>state.requests.filter(r=>r.operation==='extension_apply').length).toBe(1);
  expect(state.requests.find(r=>r.operation==='extension_apply')!.extensions[0].enabled).toBe(false);
  await expect(panel.getByRole('button',{name:'Open',exact:true})).toBeDisabled();
  await expect(panel.getByRole('button',{name:'Enable',exact:true})).toBeVisible();
  await panel.getByRole('button',{name:'Remove',exact:true}).click();
  await page.getByRole('dialog').getByRole('button',{name:/confirm/i}).click();
  await expect(panel).not.toContainText('Review helper');
  expect(state.errors).toEqual([]);
});

test('replay and reconnect do not issue actions; close returns to chat on mobile',async({page})=>{
  await page.setViewportSize({width:390,height:844});const state=await fixture(page);const panel=page.getByTestId('browser-workspace');
  await expect(panel.getByAltText('Live browser viewport')).toBeVisible();
  expect(await panel.evaluate(e=>e.scrollWidth<=e.clientWidth+1)).toBe(true);
  await panel.getByTestId('browser-operation-details').locator('summary').click();
  await panel.getByRole('button',{name:'View frame',exact:true}).first().click();
  await expect(panel.getByRole('button',{name:'Back to live',exact:true})).toBeVisible();
  await panel.getByRole('button',{name:'Back to live',exact:true}).click();
  state.fail(true);await expect(panel.getByAltText('Live browser viewport')).toHaveCount(0);
  state.fail(false);await expect(panel.getByAltText('Live browser viewport')).toBeVisible();
  expect(state.requests.every(r=>['trace','surface'].includes(r.operation))).toBe(true);
  await panel.getByTestId('browser-operation-details').locator('summary').click();
  await page.screenshot({path:'test-results/browser-compact-mobile.png'});
  await page.getByTestId('task-dock-header').getByRole('button',{name:'Hide workspace',exact:true}).click();
  await expect(page.getByTestId('workspace-source')).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('real Chromium viewport and Network are live, inspectable and compact',async({page})=>{
  const realApi=process.env.NERYA_BROWSER_REVIEW_API||'';
  test.skip(!realApi,'Requires the isolated verify_browser_network.py --serve API');
  await page.setViewportSize({width:1440,height:1000});
  const state=await fixture(page,{realApi});const panel=page.getByTestId('browser-workspace');
  const image=panel.getByAltText('Live browser viewport');
  await expect(image).toBeVisible();await expect(panel.locator('header button')).toHaveCount(2);
  const first=await image.getAttribute('src');await expect.poll(()=>image.getAttribute('src')).not.toBe(first);
  await expect(panel.getByTestId('browser-input-shield')).toBeVisible();
  await page.screenshot({path:'test-results/browser-simple-live.png'});
  await showTool(page,'Network');const network=panel.getByTestId('browser-network');
  await expect(network.getByRole('status')).toContainText('Listening live');
  await network.getByLabel('Filter requests',{exact:true}).fill('/api/save');
  const row=network.getByRole('listitem').filter({hasText:'/api/save'}).first();await expect(row).toBeVisible();
  await row.click();await network.getByRole('button',{name:'Response',exact:true}).click();
  await expect(network.locator('pre')).toContainText('"saved":true');
  await expect(network.locator('pre')).toContainText('"count":1');
  expect(state.requests.filter(r=>r.operation==='network_detail'&&r.include_body===true)).toHaveLength(1);
  await page.screenshot({path:'test-results/browser-network-live.png'});
  await network.getByLabel('Filter requests',{exact:true}).fill('/api/data?');
  await network.getByRole('listitem').filter({hasText:'/api/data?'}).first().click();
  await network.getByRole('button',{name:'Response',exact:true}).click();
  await expect(network.locator('pre')).toContainText('Research notes');
  await expect(network.locator('pre')).not.toContainText('synthetic-hidden');
  await expect(network.locator('pre')).toContainText('[redacted]');
  await page.setViewportSize({width:390,height:844});
  expect(await panel.evaluate(e=>e.scrollWidth<=e.clientWidth+1)).toBe(true);
  await page.screenshot({path:'test-results/browser-network-mobile.png'});
  await panel.getByRole('button',{name:'Back to page',exact:true}).click();
  await panel.getByRole('button',{name:'Take over',exact:true}).click();
  await expect(panel.getByRole('button',{name:'Give to Agent',exact:true})).toBeVisible();
  await expect(panel.getByTestId('browser-input-shield')).toHaveCount(0);
  await panel.getByRole('button',{name:'Give to Agent',exact:true}).click();
  await expect(panel.getByTestId('browser-input-shield')).toBeVisible();
  expect(state.requests.every(r=>!['start_capture','replay','open'].includes(r.operation))).toBe(true);
  expect(state.errors).toEqual([]);
});

test('browser settings have one switch and no engine installer',async({page})=>{
  const state=await fixture(page);await page.goto('/browsers?tab=engines');
  const prefs=page.getByTestId('browser-preferences');await expect(prefs).toBeVisible();
  await expect(prefs.getByRole('switch')).toBeChecked();
  await prefs.getByRole('switch').uncheck();await expect(prefs.getByRole('switch')).not.toBeChecked();
  await expect(page.getByRole('button',{name:/install|select engine|probe/i})).toHaveCount(0);
  await expect(page.getByText(/camofox|cloakbrowser|lightpanda|obscura/i)).toHaveCount(0);
  await page.screenshot({path:'test-results/browser-simple-settings.png'});
  expect(state.requests.filter(r=>r.operation==='preferences'&&r.automatic===false)).toHaveLength(1);
  expect(state.errors).toEqual([]);
});

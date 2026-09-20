import { test, expect, type Page } from '@playwright/test';

async function setup(page:Page, initial=false){
  const requests:Record<string,unknown>[]=[],errors:string[]=[];
  let automatic=initial,fail=false;
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>localStorage.setItem('nerya.ui_settings.v1',JSON.stringify({language:'en',darkMode:'dark'})));
  await page.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname.replace(/^\/api\/proxy/,'');
    let body:unknown={ok:true,items:[],count:0,total:0,events:[],approvals:[]};
    if(path==='/browsers/desktop'){
      const input=route.request().postDataJSON();requests.push(input);
      if(fail && typeof input.automatic==='boolean'){await route.fulfill({json:{ok:false,error:'preference_save_failed'}});return;}
      if(typeof input.automatic==='boolean')automatic=input.automatic;
      body={ok:true,preferences:{automatic}};
    }else if(path==='/auth/status')body={ok:true,authenticated:true,password_set:true,enabled:true};
    else if(path==='/operator/nav')body={ok:true,data:{primary:[],advanced:[]},primary:[],advanced:[]};
    else if(path==='/operator/overview')body={status:'ok',data:{attention:[],counts:{},accounts:[],strategies:[]}};
    else if(path==='/workspace')body={root:'browser-preferences-fixture',live_trading_enabled:false,kill_switch:false};
    else if(path==='/agent/sessions')body={sessions:[],has_more:false};
    await route.fulfill({json:body});
  });
  await page.goto('/browsers');
  const panel=page.getByTestId('browser-preferences');await expect(panel.getByRole('switch')).toBeEnabled();
  return {panel,requests,errors,fail:()=>{fail=true;}};
}

test('automatic preference uses the server value and survives refresh without granting per-site access',async({page})=>{
  const {panel,requests,errors}=await setup(page,false);
  await expect(panel.getByRole('switch')).not.toBeChecked();
  await panel.getByRole('switch').check();
  await expect(panel.getByRole('switch')).toBeEnabled();
  await page.reload();await expect(panel.getByRole('switch')).toBeChecked();
  expect(requests.filter(r=>r.automatic===true)).toHaveLength(1);
  expect(requests.every(r=>r.operation==='preferences')).toBe(true);
  expect(errors).toEqual([]);
});

test('failed preference write is visible, rolled back and never automatically repeated',async({page})=>{
  const {panel,requests,errors,fail}=await setup(page,true);fail();
  await panel.getByRole('switch').click();
  await expect(panel.getByRole('alert')).toContainText('preference_save_failed');
  await expect(panel.getByRole('switch')).toBeChecked();
  await expect(panel.getByRole('switch')).toBeEnabled();
  expect(requests.filter(r=>typeof r.automatic==='boolean')).toHaveLength(1);
  expect(errors).toEqual([]);
});

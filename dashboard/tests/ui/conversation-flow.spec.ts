import {test,expect} from '@playwright/test';
import {parityFixture} from './command-fixture';
const input=(page:import('@playwright/test').Page)=>page.locator('[data-chat-composer] textarea');
async function start(page:import('@playwright/test').Page){await page.goto('/chat/parity-session');await expect(input(page)).toBeEnabled();}

test('quiet conversation has no duplicate collaboration, receipts or running strip',async({page})=>{
 const f=await parityFixture(page);await start(page);await input(page).fill('Inspect source');await page.getByTestId('native-command-send').click();
 await expect(page.getByTestId('task-header-status')).toContainText('Running');
 await expect(page.getByTestId('collaboration-summary')).toHaveCount(0);await expect(page.getByTestId('command-execution-state')).toHaveCount(0);
 await expect(page.getByTestId('stop-current-command')).toBeVisible();await expect(page.getByTestId('native-command-send')).toHaveCount(0);await expect(page.getByTestId('guide-current-turn')).toHaveCount(0);
 await input(page).fill('Keep the constraints');await page.getByTestId('guide-current-turn').click();await expect(page.getByTestId('guidance-receipt')).toBeVisible();
 f.setState([...f.commands.values()].find(c=>c.kind==='guide')!.command_id,'injected');await expect(page.getByTestId('guidance-receipt')).toHaveCount(0);expect(f.errors).toEqual([]);
});

test('failed turn has compact diagnostics and checkpoint continuation keeps queue paused',async({page},info)=>{
 const f=await parityFixture(page);await start(page);await input(page).fill('First task');await page.getByTestId('native-command-send').click();await expect.poll(()=>f.commands.size).toBe(1);
 await input(page).fill('Second task');await page.getByTestId('native-command-send').click();await expect.poll(()=>f.commands.size).toBe(2);
 const first=[...f.commands.values()][0];f.setState(first.command_id,'failed');first.error={code:'rate_limited',status_code:429,retrying:false};f.queue.paused=true;f.queue.pause_reason='failed';f.setCheckpoint({turn_id:first.turn_id,resumable:true});
 const error=page.locator('[data-turn-section="error"]');await expect(error).toContainText('rate-limited');await expect(page.getByTestId('task-header-status')).toContainText('Failed');await expect(error.getByRole('button',{name:'Continue this turn'})).toBeVisible();await expect(error.getByRole('button',{name:'Run again'})).toHaveCount(0);
 await expect(page.locator('[data-turn-section="error-raw"]')).toHaveCount(0);await error.getByRole('button',{name:'Details'}).click();await expect(page.getByRole('dialog')).toContainText('rate_limited');await page.keyboard.press('Escape');
 await page.screenshot({path:info.outputPath('failed-and-queue.png')});
 await error.getByRole('button',{name:'Continue this turn'}).click();await expect.poll(()=>f.requests.length).toBe(3);expect(f.requests[2].command_type).toBe('resume');expect(f.requests[2].request.resume_turn_id).toBe(first.turn_id);expect(f.queue.paused).toBe(true);expect(f.errors).toEqual([]);
});

test('rerun is explicit, preserves composer draft and sends run_only',async({page})=>{
 const f=await parityFixture(page);await start(page);await input(page).fill('Original task');await page.getByTestId('native-command-send').click();await expect.poll(()=>f.commands.size).toBe(1);
 const first=[...f.commands.values()][0];f.setState(first.command_id,'failed');first.error={code:'turn_failed',retrying:false};f.queue.paused=true;f.queue.pause_reason='failed';
 await expect(page.getByRole('button',{name:'Run again',exact:true})).toBeVisible();await input(page).fill('Unsent draft');await page.getByRole('button',{name:'Run again',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'Start a new turn'}).click();
 await expect.poll(()=>f.requests.length).toBe(2);expect(f.requests[1].request.run_only).toBe(true);expect(f.requests[1].request.payload.text).toBe('Original task');await expect(input(page)).toHaveValue('Unsent draft');expect(f.queue.paused).toBe(true);
});

test('a normal follow-up to an empty paused queue sends immediately without a confirmation modal',async({page})=>{
 const f=await parityFixture(page,{complete:true});
 f.queue.paused=true;f.queue.pause_reason='restarted';
 await start(page);
 await input(page).fill('请启用公共 Solana RPC 并继续');
 await page.getByTestId('native-command-send').click();
 await expect.poll(()=>f.requests.length).toBe(1);
 expect(f.requests[0].request.payload.text).toBe('请启用公共 Solana RPC 并继续');
 expect(f.requests[0].request.run_only).toBe(true);
 await expect(page.getByRole('dialog')).toHaveCount(0);
 await expect(input(page)).toHaveValue('');
 expect(f.queue.paused).toBe(true);
 expect(f.queue.pause_reason).toBe('restarted');
 expect(f.errors).toEqual([]);
});

test('follow-up during queued work does not bypass FIFO or ask to run only',async({page})=>{
 const f=await parityFixture(page);
 await start(page);
 await input(page).fill('First turn');await page.getByTestId('native-command-send').click();
 await expect.poll(()=>f.requests.length).toBe(1);
 f.queue.paused=true;f.queue.pause_reason='operator';
 await input(page).fill('Queued behind first');await page.getByTestId('native-command-send').click();
 await expect.poll(()=>f.requests.length).toBe(2);
 expect(f.requests[1].request.run_only).not.toBe(true);
 await expect(page.getByRole('dialog')).toHaveCount(0);
 expect(f.queue.paused).toBe(true);
 expect(f.errors).toEqual([]);
});

test('stale idle snapshot safely falls back to FIFO when the backend already owns a turn',async({page})=>{
 const f=await parityFixture(page);
 f.queue.paused=true;f.queue.pause_reason='restarted';
 const requests:any[]=[];
 await page.route('**/api/proxy/agent/commands',async route=>{
   if(route.request().method()!== 'POST'){await route.fallback();return;}
   const body=route.request().postDataJSON();requests.push(body);
   if(body.request.run_only===true){
     await route.fulfill({status:409,json:{ok:false,error:'session_turn_in_progress'}});
     return;
   }
   await route.fallback();
 });
 await start(page);
 await input(page).fill('Keep this follow-up queued safely');
 await page.getByTestId('native-command-send').click();
 await expect.poll(()=>requests.length).toBe(2);
 expect(requests[0].request.run_only).toBe(true);
 expect(requests[1].request.run_only).toBeUndefined();
 expect(requests[0].command_id).not.toBe(requests[1].command_id);
 await expect.poll(()=>f.requests.length).toBe(1);
 await expect(page.getByRole('dialog')).toHaveCount(0);
 await expect(input(page)).toHaveValue('');
 expect(f.queue.paused).toBe(true);
 expect(f.errors).toEqual([]);
});

test('waiting user decision disables queue resume and unknown outcome never reruns',async({page})=>{
 const f=await parityFixture(page);await start(page);await input(page).fill('Task requiring a decision');await page.getByTestId('native-command-send').click();await expect.poll(()=>f.commands.size).toBe(1);await input(page).fill('Later task');await page.getByTestId('native-command-send').click();
 const first=[...f.commands.values()][0];f.setState(first.command_id,'awaiting_input');f.queue.paused=true;f.queue.pause_reason='awaiting_input';
 const q=page.getByTestId('conversation-queue');await q.locator('summary').click();await expect(q.getByRole('button',{name:'Resume queue'})).toBeDisabled();
 f.setState(first.command_id,'unconfirmed');first.error={code:'execution_unconfirmed'};await expect(page.locator('[data-turn-section="error"]')).toBeVisible();await expect(page.getByRole('button',{name:'Run again',exact:true})).toHaveCount(0);
 await expect(page.getByRole('button',{name:'Reconcile execution'})).toBeVisible();expect(f.errors).toEqual([]);
});


test('ordinary replies do not auto-create result tabs or context rows',async({page})=>{
 const f=await parityFixture(page,{complete:true});await start(page);await input(page).fill('Brief answer');await page.getByTestId('native-command-send').click();
 await expect(page.getByText('Verified fixture response.',{exact:true})).toBeVisible();await expect(page.locator('details[data-testid="turn-context"]')).toHaveCount(0);
 await page.getByRole('button',{name:'Open workspace',exact:true}).click();await expect(page.getByTestId('task-dock-header').getByRole('tab')).toHaveCount(0);
 expect(f.errors).toEqual([]);
});


test('HTTP 408 preserves uncertain admission and original request id',async({page})=>{
 const f=await parityFixture(page);let original='';
 await page.route('**/api/proxy/agent/commands',async route=>{if(route.request().method()!=='POST'){await route.fallback();return;}original=route.request().postDataJSON().command_id;await route.fulfill({status:408,json:{ok:false,error:'timeout'}});});
 await start(page);await input(page).fill('Keep the original admission');await page.getByTestId('native-command-send').click();
 await expect(page.getByTestId('command-delivery-unconfirmed')).toBeVisible();await expect(input(page)).toHaveValue('Keep the original admission');
 expect(await page.evaluate(id=>localStorage.getItem('nerya.chat.pending-command.v2:'+id),original)).not.toBeNull();
});

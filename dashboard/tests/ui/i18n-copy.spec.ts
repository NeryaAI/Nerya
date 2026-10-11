import {test,expect,type Page} from '@playwright/test';
import {createTranslator} from 'next-intl';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {en,zh} from '../../messages';
import {runLabel} from '../../lib/taskRuns';
import {commandStateText} from '../../lib/commandCopy';

const state=JSON.parse(readFileSync(path.resolve(process.cwd(),'../../ui-review/task-run-refactor-20261003/qa-state.json'),'utf8')) as {
  session_id:string;latest_run_id:string};
function translationErrors(page:Page){
  const errors:string[]=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('console',message=>{
    if(message.type()==='error'&&/IntlError|MISSING_MESSAGE|INVALID_MESSAGE|INVALID_KEY|FORMATTING_ERROR/.test(message.text())) errors.push(message.text());
  });
  return errors;
}

test.beforeEach(async({page},info)=>{
  const language=info.title.startsWith('[en]')?'en':'zh';
  await page.addInitScript(language=>{
    localStorage.setItem('nerya.ui_settings.v1',JSON.stringify({language,darkMode:'dark'}));
    localStorage.setItem('nerya.admin_jwt.v1','qa-fixture-operator-not-real');
  },language);
});

test('state labels localize both execution and delivery while preserving unknown provider states',()=>{
  const english=createTranslator({locale:'en',messages:en,namespace:'taskRuns'});
  const chinese=createTranslator({locale:'zh',messages:zh,namespace:'taskRuns'});
  expect(runLabel('succeeded',english)).toBe('Agent finished');
  expect(runLabel('delivery:delivered',english)).toBe('Delivered');
  expect(runLabel('delivery:delivered',chinese)).toBe('已送达');
  expect(runLabel('provider_future_state',english)).toBe('provider_future_state');
  expect(commandStateText('awaiting_approval',false)).toBe('Awaiting approval');
  expect(commandStateText('awaiting_approval',true)).toBe('等待审批');
});

for(const locale of ['en','zh'] as const){
  const messages=locale==='en'?en:zh;
  test(`[${locale}] automation history, grant fields and settings use the selected resource bundle`,async({page},info)=>{
    const errors=translationErrors(page);
    await page.goto('/workflows');await expect(page.locator('html')).toHaveAttribute('lang',locale);
    await page.getByRole('tab',{name:messages.workflows.tabs.history,exact:true}).click();
    const history=page.getByTestId('task-run-history');await expect(history).toContainText(messages.taskRuns.history);
    await expect(history.locator('tbody tr')).toHaveCount(50);
    await expect(history.getByRole('combobox',{name:messages.taskRuns.executionStatus})).toBeVisible();
    await expect(history).not.toContainText('copy.');
    await page.getByRole('tab',{name:messages.workflows.tabs.settings,exact:true}).click();
    const grants=page.getByTestId('task-financial-grants');await grants.locator('summary').click();
    await expect(grants).toContainText(messages.financial.grantStates.draft);
    await expect(grants).toContainText(messages.financial.actions.wallet_transfer);
    await grants.getByRole('button',{name:messages.financial.createDraft,exact:true}).click();
    await expect(grants.getByPlaceholder(messages.financial.exactIds)).toHaveCount(10);
    await expect(grants.getByText(messages.financial.resources.wallets,{exact:true}).last()).toBeVisible();
    await expect(grants.getByText(messages.financial.limits.rolling_24h_usd,{exact:true})).toBeVisible();
    await expect(grants).not.toContainText('financial.');
    await page.screenshot({path:info.outputPath(`automation-grants-${locale}.png`)});
    await page.getByRole('button',{name:messages.workflows.kindAgent,exact:true}).click();
    const cadence=page.getByText(messages.workflows.fieldCadence,{exact:true}).locator('..').getByRole('button');
    await cadence.click();
    await page.getByRole('menuitemradio',{name:messages.workflows.cadenceOnce,exact:true}).click();
    await expect(page.getByLabel(messages.workflows.fieldRunAt,{exact:true})).toBeVisible();
    await page.getByRole('button',{name:messages.workflows.advancedTitle,exact:true}).last().click();
    await expect(page.getByText(messages.workflows.fieldBusyPolicy,{exact:true})).toBeVisible();
    await expect(page.getByLabel(messages.workflows.fieldRequiredFiles,{exact:true})).toBeVisible();
    await expect(page.getByText(messages.workflows.budgetHint,{exact:true})).toBeVisible();
    expect(errors).toEqual([]);
  });

  test(`[${locale}] run details translate full receipt sentences and switch language without losing the selected run`,async({page},info)=>{
    const errors=translationErrors(page);
    await page.route(`**/api/proxy/agent/runs/${state.latest_run_id}`,async route=>{
      const response=await route.fetch(),body=await response.json();
      body.run.delivery_status='delivered';body.run.result.effects=[{
        kind:'bridge_swap',state:'submitted',request:{recipient:'qa-recipient',asset:'NATIVE',amount:'0.01'},
        submission:{transaction_hash:'qa-receipt-identity'},receipt:{source_confirmed:true,destination_confirmed:false},
      }];
      await route.fulfill({response,json:body});
    });
    await page.goto(`/chat/${state.session_id}?run=${state.latest_run_id}`);
    const detail=page.getByTestId('task-run-detail');await expect(detail).toHaveAttribute('data-run-id',state.latest_run_id);
    await expect(detail).toContainText(locale==='en'?'Delivery: Delivered':'通知：已送达');
    await expect(detail).toContainText(messages.taskRuns.bridgePending);
    await expect(detail).toContainText(locale==='en'?'Recipient: qa-recipient':'接收方：qa-recipient');
    await expect(detail).toContainText(messages.financial.actions.bridge_swap);
    await expect(detail).not.toContainText('taskRuns.');await expect(detail).not.toContainText('financial.');
    await page.screenshot({path:info.outputPath(`run-receipts-${locale}.png`)});
    const next=locale==='en'?'zh':'en';
    await page.evaluate(language=>{
      const key='nerya.ui_settings.v1',settings=JSON.parse(localStorage.getItem(key)||'{}');
      localStorage.setItem(key,JSON.stringify({...settings,language}));
      window.dispatchEvent(new StorageEvent('storage',{key}));
    },next);
    await expect(page.locator('html')).toHaveAttribute('lang',next);
    await expect(detail).toContainText(next==='en'?'Delivery: Delivered':'通知：已送达');
    await expect(detail).toHaveAttribute('data-run-id',state.latest_run_id);
    await expect(detail).toContainText('qa-receipt-identity');
    expect(errors).toEqual([]);
  });

  test(`[${locale}] account funds permission form localizes labels and capability status`,async({page})=>{
    const errors=translationErrors(page);
    await page.route('**/api/proxy/accounts/get',route=>route.fulfill({json:{ok:true,account:{
      profile:{id:'qa-account',mode:'paper',venue:'qa',kind:'chain',provider_spec:'qa',base_currency:'USD',
        subaccount:'',status:'active',live_trading_enabled:false,initial_balance_usd:0,permissions:{},limits:{},credentials:{},wallet_id:'qa-wallet'},
      snapshot:null,reserved_usd:0,open_positions:[],open_position_count:0,protections:[],protection_count:0,active_executors:[],
    }}}));
    await page.goto('/accounts/qa-account');
    const panel=page.getByTestId('financial-resource-panel');
    await expect(panel.getByText(messages.financial.resourceTitle,{exact:true})).toBeVisible();
    await expect(panel).toContainText(messages.financial.moduleDisabled);
    await panel.getByRole('button',{name:messages.financial.configurePermissions,exact:true}).click();
    await expect(panel.getByRole('checkbox',{name:messages.financial.actions.wallet_transfer,exact:true})).toBeVisible();
    await expect(panel.getByLabel(messages.financial.accountLimits.single_usd,{exact:true})).toBeVisible();
    await expect(panel.getByRole('button',{name:messages.financial.savePermissions,exact:true})).toBeVisible();
    await expect(panel).not.toContainText('financial.');
    expect(errors).toEqual([]);
  });
}

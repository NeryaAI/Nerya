// Rendering-only regression: no browser, runtime, network, or model calls.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),ts=require('typescript');
for(const ext of ['.ts','.tsx'])require.extensions[ext]=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,f);
require.extensions['.css']=m=>{m.exports={};};
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),{NextIntlClientProvider}=require('next-intl');
const bundles=require('../messages/index.ts');
const {ToolActivity}=require('../components/chat/ReadableExecution.tsx');
const {executionSteps}=require('../lib/executionTimeline.ts');
const render=(step,locale='zh',props={})=>renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale,messages:bundles[locale],timeZone:'UTC'},React.createElement(ToolActivity,{step,state:'failed',...props})));
const stepFor=fields=>executionSteps([
  {block:{kind:'tool_use',call_id:'jupiter',action:'web_search',payload:{keyword:'Jupiter'}}},
  {block:{kind:'tool_result',call_id:'jupiter',...fields}},
])[0];

test('pending replay authorization waits while policy reads and replay failures stay distinct',()=>{
  const pending={ok:false,reason:'retrospective_replay_authorization_pending',
    interaction:{kind:'question',state:'pending'},message:'Waiting for an answer.'};
  const make=(fields,payload={policy_check_only:true})=>executionSteps([
    {block:{kind:'tool_use',call_id:'authorization',action:'strategy_backtest',payload}},
    {block:{kind:'tool_result',call_id:'authorization',action:'strategy_backtest',name:'strategy_backtest',protocol:null,
      metadata:{descriptor:{namespace:'native',result_kind:'json',risk:'write',tags:['strategy','backtest','write']}},...fields}},
  ])[0];
  const fields={ok:true,semantic_success:false,result_protocol:'user_interaction_pending',result:JSON.stringify(pending)};
  const {toolPresentation}=require('../lib/agentConversation.ts');
  for(const locale of ['zh','en']) {
    for(const receipt of [fields,{ok:true,result:JSON.stringify(pending)},
      {ok:false,error_kind:'deduped',elapsed_ms:0,result:null,error:'Repeated tool call suppressed'}]) {
      const step=make(receipt),original=JSON.stringify(step);
      const output=render(step,locale,{state:'awaiting_input',open:true});
      const presentation=toolPresentation(step,'awaiting_input',locale==='zh');
      assert.equal(presentation.title,locale==='zh'?'读取内容':'Read');
      assert.equal(presentation.subject,'strategy_backtest');
      assert.equal(presentation.name,'strategy_backtest');
      assert.equal(presentation.family,'read');
      assert.match(output,/strategy_backtest/);
      assert.match(output,/data-state="waiting"/);
      assert.match(output,locale==='zh'?/等待回答/:/Waiting for answer/);
      assert.doesNotMatch(output,/tool-failure-summary|data-state="failed"|运行回测|Run backtest|读取文件|Read file/);
      assert.equal(JSON.stringify(step),original);
    }
    for(const step of [make({ok:true,result:{ok:true}}),
      make({ok:true,result:{ok:true,result_type:'retrospective_authorization_policy',replay_started:false}}, {})]) {
      const original=JSON.stringify(step),read=toolPresentation(step,'succeeded',locale==='zh');
      assert.equal(read.family,'read');
      assert.equal(read.title,locale==='zh'?'读取内容':'Read');
      assert.equal(read.subject,'strategy_backtest');
      assert.equal(read.name,'strategy_backtest');
      assert.equal(read.failed,false);
      assert.equal(read.waiting,false);
      assert.doesNotMatch(render(step,locale,{state:'succeeded',open:false}),/读取文件|Read file|运行回测|Run backtest/);
      assert.equal(JSON.stringify(step),original);
    }
    const replay=toolPresentation(make({ok:true,result:{ok:true,result_type:'backtest_result'}}, {}),'succeeded',locale==='zh');
    assert.equal(replay.family,'backtest');
    assert.equal(replay.title,locale==='zh'?'运行回测':'Run backtest');
    for(const receipt of [{...fields,ok:false,error:'Provider unavailable'},
      {...fields,is_error:true},
      {ok:true,semantic_success:false,result:{ok:false,result_type:'backtest_result',backtest_status:'failed',message:'Replay failed.'}},
      {ok:true,result:{...pending,interaction:{kind:'question',state:'answered'}}}]) {
      const step=make(receipt);
      assert.equal(toolPresentation(step,'awaiting_input',locale==='zh').failed,true);
      assert.match(render(step,locale,{state:'awaiting_input'}),/data-state="failed"/);
    }
  }
});

test('a recorded error replaces the missing-output placeholder with a human summary',()=>{
  const step=stepFor({ok:false,error:'unexpected field: keyword'});
  const output=render(step);
  assert.match(output,/不支持的参数/);
  assert.match(output,/keyword/);
  assert.match(output,/data-state="failed"/);
  assert.doesNotMatch(output,/这条操作没有保存可读输出|unexpected field: keyword/);
  assert.match(output,/<details[^>]*data-testid="agent-debug"/);
  assert.doesNotMatch(output,/<details[^>]*open[^>]*data-testid="agent-debug"/);
  assert.doesNotMatch(output,/data-testid="agent-debug-json"/);
});

for (const locale of ['zh','en']) {
  test(locale+': scope verification remains visibly unresolved with diagnostics collapsed',()=>{
    const step=stepFor({ok:false,result:{error_code:'request_scope_unverified',reason:'request_scope_unverified',recovery_hint:{action:'bind_replay_resource_scope'}}});
    const before=JSON.stringify(step),output=render(step,locale);
    assert.match(output,locale==='zh'?/请求范围尚未验证/:/requested scope has not been verified/);
    assert.match(output,locale==='zh'?/资源和时间范围/:/resources and time range/);
    assert.match(output,/data-state="failed"/);
    assert.doesNotMatch(output,/request_scope_unverified|bind_replay_resource_scope|No readable output|没有保存可读输出/);
    const collapsed=render(step,locale,{open:false});
    assert.match(collapsed,locale==='zh'?/请求范围尚未验证/:/requested scope has not been verified/);
    assert.match(collapsed,/data-state="failed"/);
    assert.equal(JSON.stringify(step),before);
  });
  test(locale+': unsupported parameter summary is localized',()=>{
    const output=render(stepFor({ok:false,error:'unexpected field: keyword'}),locale);
    assert.match(output,locale==='zh'?/不支持的参数“keyword”/:/unsupported parameter, “keyword”/);
    assert.doesNotMatch(output,/unexpected field:/);
  });
}

test('meaningful human errors survive without the missing-output placeholder',()=>{
  const step=stepFor({ok:false,error:'The quote service is unavailable. Try again later.'});
  const output=render(step,'en');
  assert.ok(output.includes('The quote service is unavailable. Try again later.'));
  assert.doesNotMatch(output,/No readable output/);
});

test('unknown machine errors and empty failed receipts never appear as success or no result',()=>{
  for (const fields of [{ok:false,error:'unrecognized_failure_code'}, {ok:false},
    {ok:false,result:{summary:'SUCCESS_SENTINEL',error:{code:'internal_problem'}}}]) {
    const output=render(stepFor(fields),'en');
    assert.match(output,/This operation failed/);
    assert.match(output,/data-state="failed"/);
    assert.doesNotMatch(output,/unrecognized_failure_code|internal_problem|SUCCESS_SENTINEL|No readable output|data-nerya-icon="check"/);
  }
});

test('nested native and MCP errors receive the same summary',()=>{
  const results=[
    {ok:false,data:{message:'unexpected field: keyword'}},
    {isError:true,content:[{type:'text',text:'unexpected field: keyword'}]},
    {ok:false,content:[{type:'json',data:{reason:'request_scope_unverified'}}]},
    {isError:true,structuredContent:{error:{message:'unexpected field: keyword'}}},
  ];
  for (const result of results) {
    const output=render(stepFor({ok:false,result}),'en');
    assert.match(output,/unsupported parameter|requested scope has not been verified/);
    assert.doesNotMatch(output,/unexpected field:|request_scope_unverified|No readable output/);
  }
});

test('producer recovery failures render an actionable summary without machine diagnostics',()=>{
  const historical=render(stepFor({ok:false,error:'The historical read produced no verified historical state.',result:{recovery:{reason:'historical_rpc_evidence_failure'}}}),'zh');
  assert.match(historical,/历史数据读取未得到可验证状态/);
  assert.doesNotMatch(historical,/historical_rpc_evidence_failure/);
  const pool=render(stepFor({ok:false,error:'capability_gap: lp_pool_identity_required. The submitted manifest is missing a complete canonical pool declaration.'}),'zh');
  assert.match(pool,/候选缺少已验证的池身份绑定/);
  assert.doesNotMatch(pool,/lp_pool_identity_required/);
});

test('historical RPC deadline renders a bounded recovery summary',()=>{
  const output=render(stepFor({ok:false,error:'archive RPC deadline after 180 seconds',recovery:{reason:'historical_rpc_evidence_failure'}}),'zh');
  assert.match(output,/历史数据源在请求预算内未能完成读取/);
  assert.doesNotMatch(output,/archive RPC|historical_rpc_evidence_failure/);
});

test('failed shell details stay collapsed and cannot claim that output was missing',()=>{
  const step=stepFor({ok:false,action:'script_run',payload:{command:'PRIVATE_COMMAND'},result:{exit_code:1,stderr:'Traceback: PRIVATE_TRACE',stdout:'PARTIAL_OUTPUT'}});
  const output=render(step,'en');
  assert.match(output,/This operation failed/);
  assert.doesNotMatch(output,/PRIVATE_TRACE|PARTIAL_OUTPUT|No readable output/);
  assert.match(output,/data-testid="agent-debug"/);
});

test('actual empty output, pending work, and successful output remain distinct',()=>{
  const empty=render(stepFor({ok:true,result:null}),'en',{open:true});
  assert.match(empty,/No readable output was saved/);
  assert.doesNotMatch(empty,/tool-failure-summary/);
  const pending=executionSteps([{block:{kind:'tool_use',call_id:'pending',action:'web_search'}}])[0];
  const waiting=render(pending,'en',{state:'running',open:true});
  assert.match(waiting,/Waiting for the operation to return/);
  assert.doesNotMatch(waiting,/No readable output|tool-failure-summary/);
  const success=render(stepFor({ok:true,result:{summary:'Found 3 sources'}}),'en',{state:'succeeded',open:true});
  assert.match(success,/Found 3 sources/);
  assert.doesNotMatch(success,/tool-failure-summary/);
});

test('request content resembling an error cannot rewrite an unrelated failure',()=>{
  const step=stepFor({ok:false});
  step.data.payload={query:'request_scope_unverified'};
  const output=render(step,'en');
  assert.match(output,/This operation failed/);
  assert.doesNotMatch(output,/requested scope has not been verified/);
});

test('expanded debug rendering preserves exact error data, with no rewriting',()=>{
  const {AgentDebug}=require('../components/chat/ReadableExecution.tsx');
  const original=React.useState;
  const diagnostic={error:'unexpected field: keyword',result:{error_code:'request_scope_unverified'},stderr:'line 1\nline 2'};
  // Exercise the disclosure's expanded render state without a browser/runtime.
  React.useState=()=>[true,()=>{}];
  try {
    const output=renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale:'en',messages:bundles.en,timeZone:'UTC'},React.createElement(AgentDebug,{value:diagnostic})));
    const body=output.match(/<pre[^>]*>([\s\S]*?)<\/pre>/)[1].replace(/&quot;/g,'"').replace(/&amp;/g,'&').replace(/&#x27;/g,"'");
    assert.equal(body,JSON.stringify(diagnostic,null,2));
  } finally { React.useState=original; }
});

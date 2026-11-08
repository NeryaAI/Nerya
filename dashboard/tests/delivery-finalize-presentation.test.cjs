// Presentation-only: exercise the real projection and SSR without a runtime.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),ts=require('typescript');
for(const ext of ['.ts','.tsx'])require.extensions[ext]=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,f);
require.extensions['.css']=m=>{m.exports={};};
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),{NextIntlClientProvider}=require('next-intl');
const bundles=require('../messages/index.ts');
const {toolPresentation,localizeDeliveryReasons}=require('../lib/agentConversation.ts');
const {ChatResultsPanel}=require('../components/chat/ChatResultsPanel.tsx');
const {ToolActivity}=require('../components/chat/ReadableExecution.tsx');
const {executionSteps}=require('../lib/executionTimeline.ts');
const protocol='nerya.task_delivery.v1';
// Native handler emits transport ok:true, semantic_success:false, and JSON text.
const delivery=(status='partial',allowed=false,reasons=['check_missing:data_coverage','required_stage_pending'])=>({
  ok:allowed,status,protocol,delivery_revision:7,command_id:'cmd_delivery',skill_id:'strategy_author',workflow_id:'strategy_delivery',
  stages:[{id:'author',state:'completed'}],artifacts:[{artifact_id:'report',verified:true}],checks:[],
  limitations:['UNVERIFIED_SCOPE'],completion:{allowed,status,reasons,computed_at:1793174400},
});
const fields=(value=delivery())=>({action:'delivery_finalize',skill_id:'native',ok:true,semantic_success:value.completion?.allowed,result_protocol:protocol,result:JSON.stringify(value)});
const stepFor=response=>executionSteps([
  {block:{kind:'tool_use',call_id:'finalize',action:'delivery_finalize',skill_id:'native',payload:{expected_revision:6}}},
  {block:{kind:'tool_result',call_id:'finalize',...response}},
])[0];
const render=(step,locale='en',open=false)=>renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale,messages:bundles[locale],timeZone:'UTC'},React.createElement(ToolActivity,{step,state:'completed',open})));

for(const locale of ['en','zh']) {
  for(const status of ['partial','incomplete']) test(`${locale}: computed ${status} is a warning with localized reasons, expanded or collapsed`,()=>{
    const value=delivery(status),step=stepFor(fields(value)),before=JSON.stringify(step);
    const p=toolPresentation(step,'completed',locale==='zh');
    assert.equal(p.failed,false);
    assert.equal(p.warning,true);
    assert.equal(p.pending,false);
    assert.match(p.state,locale==='zh'?(status==='partial'?/部分交付/:/交付尚未完成/):(status==='partial'?/Partial delivery/:/Delivery is incomplete/));
    assert.match(p.summary,locale==='zh'?/历史数据覆盖不完整/:/Historical data coverage is incomplete/);
    assert.doesNotMatch(p.summary,/data_coverage|historical_replay/);
    assert.match(p.summary,locale==='zh'?/必需阶段尚未完成/:/A required stage is incomplete/);
    for(const open of [false,true]) {
      const html=render(step,locale,open);
      assert.match(html,/data-state="warning"/);
      assert.match(html,/text-warn/);
      assert.match(html,locale==='zh'?/历史数据覆盖不完整/:/Historical data coverage is incomplete/);
      assert.doesNotMatch(html,/data-nerya-icon="check"|tool-failure-summary|text-(?:ok|danger)|check_missing|data_coverage|historical_replay|UNVERIFIED_SCOPE|Done|Completed|已完成/);
      if(open) {
        assert.match(html,/data-testid="agent-debug"/);
        assert.doesNotMatch(html,/data-testid="agent-debug-json"/);
      }
    }
    assert.equal(JSON.stringify(step),before);
    assert.equal(step.data.semantic_success,false);
    assert.equal(JSON.parse(step.data.result).ok,false);
  });
  test(`${locale}: actual complete+allowed true retains success`,()=>{
    const step=stepFor(fields(delivery('complete',true,[]))),p=toolPresentation(step,'completed',locale==='zh');
    assert.equal(p.failed,false);
    assert.equal(Boolean(p.warning),false);
    assert.match(render(step,locale),/data-nerya-icon="check"/);
  });
}

test('compacted_kept uses the existing decoder, ignoring prose and display overrides',()=>{
  const response=fields();
  response.result='top_keys=[...]\n[compacted_kept]\n'+JSON.stringify({...delivery('partial',false,['check_missing:data_coverage']),delivery_revision:9,completion:{allowed:false,status:'partial',reasons:['check_missing:data_coverage'],computed_at:1793175780.189315}});
  response.display_result={summary:'MODEL_SUCCESS_SENTINEL'};
  const step=stepFor(response),p=toolPresentation(step,'completed',false);
  assert.equal(p.failed,false);
  assert.equal(p.warning,true);
  assert.doesNotMatch(render(step,'en',true),/claimed complete|MODEL_SUCCESS_SENTINEL|data-nerya-icon="check"/);
});

test('allowed partial scope still cannot display a completion check',()=>{
  const step=stepFor(fields(delivery('partial',true,[])));
  assert.equal(toolPresentation(step,'completed',false).warning,true);
  assert.doesNotMatch(render(step),/data-nerya-icon="check"/);
});

test('successful structured wrapper preserves the computed delivery warning',()=>{
  const step=stepFor({...fields(),result:{ok:true,structuredContent:delivery()}});
  const p=toolPresentation(step,'completed',false);
  assert.equal(p.failed,false);
  assert.equal(p.warning,true);
  assert.doesNotMatch(render(step),/data-nerya-icon="check"/);
});

test('finalizer without a result stays pending, never failed or complete',()=>{
  const step=executionSteps([{block:{kind:'tool_use',call_id:'finalize',action:'delivery_finalize',skill_id:'native'}}])[0];
  const p=toolPresentation(step,'running',false);
  assert.equal(p.failed,false);
  assert.equal(p.warning,false);
  assert.equal(p.pending,true);
  const html=renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale:'en',messages:bundles.en,timeZone:'UTC'},React.createElement(ToolActivity,{step,state:'running',open:true})));
  assert.match(html,/data-state="running"/);
  assert.doesNotMatch(html,/tool-failure-summary|data-nerya-icon="check"/);
});

test('public summaries omit raw reason subjects while diagnostics keep them',()=>{
  const step=stepFor(fields(delivery('partial',false,['check_missing:historical_replay','check_missing:data_coverage'])));
  for(const locale of ['en','zh']) {
    assert.doesNotMatch(render(step,locale,true),/historical_replay|data_coverage/);
    assert.match(toolPresentation(step,'completed',locale==='zh').summary,locale==='zh'?/历史回放未完成/:/Historical replay is incomplete/);
  }
  assert.match(step.data.result,/historical_replay|data_coverage/);
});

test('known delivery reason subjects use their specific user-facing meaning',()=>{
  const step=stepFor(fields(delivery('partial',false,['check_missing:data_coverage'])));
  assert.match(render(step,'en',true),/Historical data coverage is incomplete/);
  assert.match(render(step,'zh',true),/历史数据覆盖不完整/);
  assert.doesNotMatch(render(step,'en',true),/check_missing|data_coverage/);
});

test('localization accepts whole structured reasons, not Markdown, paths or URLs',()=>{
  assert.equal(localizeDeliveryReasons('UNVERIFIED_SCOPE',true),'请求范围尚未核验');
  assert.equal(localizeDeliveryReasons('check_missing:data_coverage',false),'Historical data coverage is incomplete');
  for(const text of ['See check_missing:data_coverage.', 'required_checks_missing.md',
    '/reports/check_missing:data_coverage', 'https://example.com/check_missing:data_coverage']) {
    assert.equal(localizeDeliveryReasons(text,true),text);
  }
  assert.equal(localizeDeliveryReasons('~~~json\n{"reason":"check_missing:data_coverage"}\n~~~',true),
    '~~~json\n{"reason":"check_missing:data_coverage"}\n~~~');
});

test('result preview, Markdown, copy and download retain the original body',async()=>{
  const text='check_missing:data_coverage\n\n~~~json\n{"reason":"check_missing:data_coverage"}\n~~~\n\n[required_checks_missing.md](https://example.com/check_missing:data_coverage)';
  const result={id:'raw-reason',title:'required_checks_missing',ts:1,text,delivery:delivery()};
  let tree;
  const Capture=()=>{tree=ChatResultsPanel({results:[result]});return tree;};
  const show=()=>renderToStaticMarkup(React.createElement(NextIntlClientProvider,
    {locale:'zh',messages:bundles.zh,timeZone:'UTC'},React.createElement(Capture)));
  const preview=show().split('data-testid="canvas-result-body">')[1].split('<footer')[0];
  assert.match(preview,/check_missing:data_coverage/);
  assert.match(preview,/required_checks_missing\.md/);
  assert.match(preview,/href="https:\/\/example.com\/check_missing:data_coverage"/);
  assert.doesNotMatch(preview,/历史数据覆盖不完整/);
  function nodes(node) {
    if(!node||typeof node!=='object')return [];
    return [node,...React.Children.toArray(node.props?.children).flatMap(nodes)];
  }
  assert.equal(nodes(tree).find(node=>node.type?.name==='CopyButton').props.text,text);
  const originalState=React.useState;
  try {
    React.useState=initial=>originalState(initial==='preview'?'markdown':initial);
    show();
    assert.equal(nodes(tree).find(node=>node.type==='textarea').props.value,text);
  }finally {React.useState=originalState;}
  const saved={document:global.document,window:global.window,create:URL.createObjectURL,revoke:URL.revokeObjectURL};
  let blob;
  try {
    URL.createObjectURL=value=>{blob=value;return 'blob:fixture';};
    URL.revokeObjectURL=()=>{};
    const anchor={click(){},remove(){}};
    global.document={createElement:()=>anchor,body:{appendChild(){}}};
    global.window={setTimeout:fn=>fn()};
    nodes(tree).find(node=>node.type==='button'&&node.props.onClick?.name==='download').props.onClick();
    assert.equal(await blob.text(),text);
    assert.equal(anchor.download,'required_checks_missing.md');
  }finally {
    global.document=saved.document;global.window=saved.window;
    URL.createObjectURL=saved.create;URL.revokeObjectURL=saved.revoke;
  }
});

test('non-delivery tools retain their original generic failure classification',()=>{
  for(const extra of [{is_error:true},{error_kind:'schema_validation'},{error_code:'schema_validation'},
    {workflow_contract_error:'delivery_contract_stale'},{errors:['failed']},{status:'cancelled'},{exit_code:2}]) {
    const step=stepFor({action:'web_search',skill_id:'native',ok:true,result:{summary:'Found sources'},...extra});
    const p=toolPresentation(step,'completed',false);
    assert.equal(p.failed,false,JSON.stringify(extra));
    assert.equal(Boolean(p.warning),false);
  }
});

test('unknown producer reason has a localized fallback without exposing machine codes',()=>{
  const step=stepFor(fields(delivery('incomplete',false,['future_reason:PRIVATE_ID'])));
  for(const locale of ['en','zh']) {
    const html=render(step,locale,true);
    assert.match(html,locale==='zh'?/完成条件尚未满足/:/Completion conditions remain unmet/);
    assert.doesNotMatch(html,/future_reason|PRIVATE_ID|copy\./);
  }
});

const negatives=[
  ['failed structured wrapper',response=>({...response,result:{ok:false,structuredContent:delivery()}})],
  ['transport error',response=>({...response,ok:false,error:'transport failed'})],
  ['transport false without prose',response=>({...response,ok:false})],
  ['MCP error',response=>({...response,isError:true})],
  ['native error',response=>({...response,is_error:true})],
  ['error kind',response=>({...response,error_kind:'schema_validation'})],
  ['outer failed status',response=>({...response,status:'failed'})],
  ['outer nonzero exit',response=>({...response,exit_code:2})],
  ['wrong producer',response=>({...response,skill_id:'mcp'})],
  ['wrong action',response=>({...response,action:'delivery_update'})],
  ['wrong protocol',response=>({...response,result_protocol:'other.v1'})],
  ['missing protocol',response=>{delete response.result_protocol;return response;}],
  ['missing semantic boolean',response=>{delete response.semantic_success;return response;}],
  ['contradictory semantic boolean',response=>({...response,semantic_success:true})],
];
const badValues=[
  ['invalid contract',{workflow_contract_error:'delivery_contract_stale'}],
  ['CAS',{error:'delivery_revision_conflict'}],
  ['schema',{error_code:'schema_validation'}],
  ['errors array',{errors:['failed']}],
  ['nonzero exit',{exit_code:1}],
  ['failed status',{status:'failed'}],
  ['failed success flag',{success:false}],
  ['missing completion',{completion:null}],
  ['missing reasons',{completion:{allowed:false,status:'partial'}}],
  ['non-array reasons',{completion:{allowed:false,status:'partial',reasons:'check_missing'}}],
  ['non-string reason',{completion:{allowed:false,status:'partial',reasons:[{code:'check_missing'}]}}],
  ['empty reason',{completion:{allowed:false,status:'partial',reasons:['']}}],
  ['non-boolean allowed',{completion:{allowed:'false',status:'partial',reasons:['check_missing']}}],
  ['contradictory status',{completion:{allowed:false,status:'complete',reasons:['check_missing']}}],
  ['missing reason for disallowed',{completion:{allowed:false,status:'partial',reasons:[]}}],
  ['wrong inner protocol',{protocol:'other.v1'}],
  ['missing inner protocol',{protocol:null}],
  ['contradictory ok',{ok:true}],
];
for(const [label,patch] of badValues)negatives.push([label,response=>({...response,result:JSON.stringify({...delivery(),...patch})})]);
for(const [label,alter] of negatives)test(`remains a generic failure: ${label}`,()=>{
  const step=stepFor(alter(fields())),p=toolPresentation(step,'completed',false);
  assert.equal(p.failed,true);
  assert.equal(Boolean(p.warning),false);
  const html=render(step,'en',true);
  assert.match(html,/data-state="failed"/);
  assert.match(html,/tool-failure-summary/);
  assert.doesNotMatch(html,/data-nerya-icon="check"/);
});

test('arbitrary ok:false and model prose cannot opt into delivery presentation',()=>{
  for(const response of [
    {ok:false,result:{ok:false,summary:'Partial delivery; completion allowed=false'}},
    {ok:true,result:{ok:false,completion:{allowed:false,status:'partial',reasons:['check_missing']}}},
    {...fields(),action:'web_search',skill_id:'mcp'},
    {...fields(),result:'Partial delivery, not complete'},
  ])assert.equal(toolPresentation(stepFor(response),'completed',false).failed,true);
});

test('expanded ToolActivity diagnostics retain the exact transport and producer booleans',()=>{
  const step=stepFor(fields()),before=JSON.stringify(step),original=React.useState;
  React.useState=()=>[true,()=>{}];
  try {
    const html=render(step,'en',true),encoded=html.match(/<pre[^>]*data-testid="agent-debug-json"[^>]*>([\s\S]*?)<\/pre>/)[1];
    const body=encoded.replace(/&quot;/g,'"').replace(/&amp;/g,'&').replace(/&#x27;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>');
    assert.deepEqual(JSON.parse(body),{request:step.event.data,response:step.result.data});
    assert.equal(JSON.stringify(step),before);
  }finally{React.useState=original;}
});

const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),ts=require('typescript');
for(const ext of ['.ts','.tsx'])require.extensions[ext]=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,f);
require.extensions['.css']=m=>{m.exports={};};
const {contentValue}=require('../lib/agentConversation.ts');
const {readJsonPrefix,legacyToolValue,diffLines,interactionReceipt,interactionTitle,reasoningPreview}=require('../lib/toolOutputPresentation.ts');
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),{NextIntlClientProvider}=require('next-intl');
const {ToolResultContent}=require('../components/chat/ToolResultContent.tsx');
const messages=Object.assign({},...fs.readdirSync(__dirname+'/../messages/en').filter(f=>f.endsWith('.json')).map(f=>require('../messages/en/'+f)));
const html=(value,family='tool')=>renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale:'en',messages,timeZone:'UTC'},React.createElement(ToolResultContent,{value,family})));
const diff='--- a/main.py\n+++ b/main.py\n@@ -1,2 +1,3 @@\n-# old\n+# @nerya.title New strategy\n+print("new")\n unchanged\n';

test('task_get reports the query outcome and preserves the observed task in details',()=>{
 const {executionSteps}=require('../lib/executionTimeline.ts');
 const {toolPresentation}=require('../lib/agentConversation.ts');
 const {ToolActivity}=require('../components/chat/ReadableExecution.tsx');
 const make=(result,fields={},action='task_get')=>executionSteps([
  {block:{kind:'tool_use',call_id:'task-read',action,payload:{task_id:'observed-task'}}},
  {block:{kind:'tool_result',call_id:'task-read',action,ok:true,error:null,semantic_success:null,result_protocol:'',result,...fields}},
 ])[0];
 for(const state of ['cancelled','failed','pending','awaiting_approval']){
  const record={task_id:'observed-task',state,error:state==='cancelled'?'stopped':state==='failed'?'worker failed':null,
   error_kind:['cancelled','failed'].includes(state)?state:null,output:{status:state==='cancelled'?'stopped':state,ok:state!=='failed'},completion:{allowed:false}};
  for(const semantic_success of [null,true]){
   const step=make(JSON.stringify(record),{semantic_success}),before=JSON.stringify(step);
   const p=toolPresentation(step,'running',false);
   assert.equal(p.failed,false);assert.equal(p.waiting,false);assert.equal(p.pending,false);assert.equal(p.warning,false);
   assert.deepEqual(p.result,record);assert.equal(p.result.completion.allowed,false);
   for(const open of [false,true]){
    const output=renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale:'en',messages,timeZone:'UTC'},React.createElement(ToolActivity,{step,state:'running',open})));
    assert.match(output,/data-state="settled"/);assert.doesNotMatch(output,/tool-failure-summary|text-danger/);
    if(open){assert.match(output,new RegExp(state));assert.match(output,/data-testid="agent-debug"/);}
   }
   assert.equal(JSON.stringify(step),before);
  }
 }
 for(const fields of [{ok:false,error:'task not found'}, {ok:false,error:'query unavailable'},
  {is_error:true}, {isError:true}, {error_kind:'execution'}, {semantic_success:false}]){
  assert.equal(toolPresentation(make({task_id:'observed-task',state:'pending'},fields),'running',false).failed,true);
 }
 assert.equal(toolPresentation(make({state:'failed',error:'worker failed'},{},'subagent_run'),'running',false).failed,true);
});

test('interaction-blocked unstarted native calls render neutrally without hiding genuine errors',()=>{
 const {executionSteps}=require('../lib/executionTimeline.ts');
 const {toolPresentation}=require('../lib/agentConversation.ts');
 const {ToolActivity}=require('../components/chat/ReadableExecution.tsx');
 const response={kind:'tool_result',call_id:'blocked-skill',skill_id:'native',action:'Skill',ok:false,
  result:null,error:'Not started: waiting for user interaction',error_kind:'aborted',elapsed_ms:0};
 const step=executionSteps([{block:{kind:'tool_use',call_id:response.call_id,action:'Skill',payload:{name:'strategy_author'}}},{block:response}])[0];
 for(const locale of ['en','zh']){
  const zh=locale==='zh',p=toolPresentation(step,'awaiting_input',zh);
  assert.equal(p.failed,false);assert.equal(p.waiting,true);assert.equal(p.pending,false);
  for(const open of [false,true]){
   const output=renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale,messages:{},timeZone:'UTC'},React.createElement(ToolActivity,{step,state:'awaiting_input',open})));
   assert.match(output,/data-state="waiting"/);
   assert.match(output,zh?/等待回答/:/Waiting for answer/);
   assert.match(output,zh?/未执行/:/Not executed/);
   assert.doesNotMatch(output,/text-danger|text-warn|tool-failure-summary|Not started: waiting for user interaction/);
  }
 }
 for(const changed of [{error_kind:'execution'},{elapsed_ms:12},{error:'Permission denied'},
  {error:'Execution aborted after starting'},{result:{exit_code:1}}]){
  const actual=executionSteps([{block:{...response,...changed}}])[0];
  assert.equal(toolPresentation(actual,'awaiting_input',true).failed,true,JSON.stringify(changed));
 }
 assert.equal(step.data.error,response.error,'raw diagnostics remain intact');
});

test('typed native diff is never flattened into Markdown',()=>{
 const value={content:[{type:'diff',text:diff,metadata:{path:'strategies/main.py'}}]};
 assert.deepEqual(contentValue(value),value);
 const output=html(value,'edit');
 assert.match(output,/data-testid="tool-diff-preview"/);assert.match(output,/@nerya.title/);assert.doesNotMatch(output,/<h[1-6]/);
});
test('legacy string diffs get the same line-numbered renderer',()=>{
 const output=html(diff,'edit');assert.match(output,/tool-diff-preview/);assert.doesNotMatch(output,/<h[1-6]/);
 assert.deepEqual(diffLines(diff).filter(row=>row.kind==='added').map(row=>row.newLine),[1,2]);
 assert.equal(diffLines(diff).at(-1).oldLine,2);assert.equal(diffLines(diff).at(-1).newLine,3);
});
test('legacy diff metadata is separated from source code, not appended as a code line',()=>{
 const value=diff+'\n'+JSON.stringify({path:'strategies/main.py',bytes_after:200,lines_after:3});
 const normalized=legacyToolValue(value);assert.equal(normalized.content[0].type,'diff');assert.equal(normalized.content[1].type,'json');
 const output=html(value,'edit');assert.match(output,/tool-diff-preview/);assert.match(output,/File lines/);assert.doesNotMatch(output,/&quot;bytes_after&quot;/);
});
test('typed display payload survives the live-event projection',()=>{
 const {liveEventsToBlocks}=require('../lib/chat.ts');
 const display={content:[{type:'diff',text:diff}]};
 const [row]=liveEventsToBlocks([{kind:'tool.complete',seq:1,call_id:'edit',action:'edit_file',ok:true,result:'old text',display_result:display}]);
 assert.deepEqual(row.block.display_result,display);assert.equal(row.block.result,'old text');
});
test('compacted JSON and native JSON parts render fields, not transport prose',()=>{
 const value='strategy_create: ready\n[compacted_kept]\n'+JSON.stringify({strategy_id:'alpha_trend',main_path:'strategies/alpha_trend/main.py',next_steps:['Edit staged files']});
 assert.equal(legacyToolValue(value).strategy_id,'alpha_trend');
 const output=html(value,'strategy');assert.match(output,/alpha_trend/);assert.match(output,/main.py/);assert.doesNotMatch(output,/compacted_kept|&quot;strategy_id&quot;|<h3/);
 assert.match(html({content:[{type:'json',data:{summary:'Native result',count:3}}]}),/Native result/);
});
test('JSON parser respects quoted braces and rejects partial/trailing malformed content',()=>{
 const source='{"text":"brace } and \\"quote","nested":[{"v":1}]} remainder';
 const parsed=readJsonPrefix(source);assert.equal(parsed.value.nested[0].v,1);assert.equal(source.slice(parsed.end),' remainder');
 assert.equal(readJsonPrefix('{"a":'),null);
 assert.equal(legacyToolValue('Normal prose with {braces}'),'Normal prose with {braces}');
});
test('question receipt only shows recorded answers and never assumes defaults',()=>{
 const response={action:'answer',answers:{timeframe:{selected:['4 hours'],text:''},risk:{selected:[],text:'Use capped risk'}}};
 const questions=[{id:'timeframe',question:'Which timeframe?',options:['1 hour (default)','4 hours']},{id:'risk',question:'Risk budget?'},{id:'validation',question:'Which validation?'}];
 const text='User response to Strategy preferences: '+JSON.stringify(response)+' Questions: '+JSON.stringify(questions)+' Omitted answers are unknown. Continue using best judgment within existing permissions. This is not approval for trading or other gated actions.';
 const receipt=interactionReceipt(text);assert.equal(receipt.title,'Strategy preferences');
 assert.deepEqual(receipt.rows.map(row=>row.answers),[['4 hours'],['Use capped risk'],[]]);
 assert.equal(interactionReceipt('Please explain User response to ...'),null);
 assert.equal(interactionReceipt('User response to broken: {"answers":'),null);
 assert.equal(interactionReceipt('User response to Example: {"action":"answer"} Please keep this important explanation.'),null);
 assert.equal(interactionReceipt('User response to Example: {"action":"answer"} Questions: [{"id":'),null);
});

test('completion-gate continuation receipt renders the recorded answer instead of raw feedback',()=>{
 const response={action:'answer',answers:{network:{selected:['USDC'],text:''}}};
 const questions=[{id:'network',question:'Which network should be used?',options:['USDC','USDT']}];
 const text='[completion gate continuation]\nUser response to Hyperliquid setup: '+JSON.stringify(response)+' Questions: '+JSON.stringify(questions)+' Omitted answers are unknown. Continue using best judgment within existing permissions. This is not approval for trading or other gated actions.';
 const receipt=interactionReceipt(text);
 assert.equal(receipt.title,'Hyperliquid setup');
 assert.deepEqual(receipt.rows.map(row=>row.answers),[['USDC']]);
 assert.equal(receipt.rows[0].question,'Which network should be used?');
 assert.doesNotMatch(receipt.note,/Omitted answers|completion gate|selected|answers/);
});

test('legacy continuation feedback does not become a sidebar title',()=>{
 const title='User response to 确认 BTC 永续计价货币: {action:answer,answers:';
 assert.equal(interactionTitle(title),'确认 BTC 永续计价货币');
 assert.equal(interactionTitle('[completion gate continuation]\n'+title),'确认 BTC 永续计价货币');
 assert.equal(interactionTitle('A normal user title: {draft}'),null);
});
test('thinking preview follows the newest nonempty line rather than a frozen prefix',()=>{
 assert.equal(reasoningPreview('first thought\n\n最新检查🙂\n'),'最新检查🙂');
 assert.equal(reasoningPreview('## Check the data'),'Check the data');
});
test('mixed MCP text, image and resource content stays readable without JSON leaking',()=>{
 const output=html({content:[{type:'text',text:'{"summary":"Actual summary","count":2}'},{type:'resource',resource:{uri:'fixture://source',text:'Source code'}}]},'mcp');
 assert.match(output,/Actual summary/);assert.match(output,/Source code/);assert.doesNotMatch(output,/&quot;summary&quot;/);
});

test('typed shell final snapshot replaces live fragments and avoids repeated transport text',()=>{
 const {ToolActivity}=require('../components/chat/ReadableExecution.tsx');
 const {executionSteps}=require('../lib/executionTimeline.ts');
 const step=executionSteps([
  {block:{kind:'tool_use',call_id:'shell',action:'run_shell',payload:{command:'verify'},stdout:'STALE_PARTIAL_OUTPUT'}},
  {block:{kind:'tool_result',call_id:'shell',ok:true,result:'legacy duplicate',display_result:{content:[{type:'shell',data:{stdout:'FINAL_COMPLETE_OUTPUT',stderr:'',exit_code:0}},{type:'text',text:'duplicate text'}]}}},
  ])[0];
 const render=()=>renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale:'en',messages,timeZone:'UTC'},React.createElement(ToolActivity,{step,state:'succeeded',open:true})));
 assert.match(render(),/FINAL_COMPLETE_OUTPUT/);assert.doesNotMatch(render(),/STALE_PARTIAL_OUTPUT|legacy duplicate|duplicate text/);
 step.data.display_result.content[0].data.stdout='';
 assert.doesNotMatch(render(),/STALE_PARTIAL_OUTPUT|FINAL_COMPLETE_OUTPUT|duplicate text/);
});

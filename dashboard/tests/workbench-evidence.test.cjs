const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const ts=require('typescript');
for(const ext of ['.ts','.tsx'])require.extensions[ext]=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,f);
require.extensions['.css']=m=>{m.exports={};};
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),{NextIntlClientProvider}=require('next-intl');
const {DeliveryEvidence}=require('../components/chat/DeliveryEvidence.tsx');
const {deliveryEvidence}=require('../lib/deliveryEvidence.ts');
const {TaskProvenance}=require('../components/chat/TaskProvenance.tsx');
function html(node){return renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale:'en',messages:{},timeZone:'UTC'},node));}
test('delivery distinguishes test evidence from prose and exposes scoped artifacts',()=>{
 const result={id:'result',title:'Report',text:'Everything is done',ts:1,turnId:'original',evidence:{artifacts:{created:['reports/research.md'],tests_run:[{command:'pytest tests/test_contract.py',exit_code:1,ok:false}],unverified_risks:[{message:'Integration not checked'}]},verifier:{hard_passed:false,hard_status:'failed'}}};
 const output=html(React.createElement(DeliveryEvidence,{result}));
 assert.match(output,/Checks need attention/);assert.match(output,/reports\/research.md/);assert.match(output,/exit 1/);assert.match(output,/Integration not checked/);assert.doesNotMatch(output,/Recorded checks passed/);
});

// Reduced from GET /agent/commands for acmd_253977467c6324cc69afd5f4:
// succeeded command, passed historical verifier, but unfinished delivery v4.
function cexResult() {
 const root='evolution/proposals/prp_11c7d132330d/after/strategies/btc_ema_cross_1h';
 return {id:'cex-result',title:'BTC/USDT 1H EMA20/EMA50 Crossover Trend Strategy',text:'已完成。',ts:1,
  delivery:{protocol:'nerya.task_delivery.v1',command_id:'acmd_253977467c6324cc69afd5f4',delivery_revision:4,
   stages:['scope','author','validate','replay','receipt','deliver'].map(id=>({id,label:id,state:'pending'})),
   artifacts:[root,root+'/backtests/20261021_172222/report.md',root+'/backtests/20261021_172222/run.json'].map(ref=>({artifact_id:ref,ref,status:'recorded'})),
   checks:['account_selection','strategy_validation','backtest_completed'].map(check_id=>({check_id,label:check_id,status:'pending',required:true})),
   limitations:['No live trading or account-order execution requested','No scheduled tasks will be created','Backtest result is negative; not a recommendation to trade'],
   completion:{allowed:false,status:'incomplete',reasons:['required_stage_pending']}},
  evidence:{execution:{state:'succeeded'},artifacts:{created:[],modified:[],tests_run:[],errors:[]},
   verifier:{hard_passed:true,hard_status:'passed',trusted:true,details:{hard:{
    checks:['source_binding','historical_run','data_coverage','historical_data','replay_source','execution_integrity'].map(check_id=>({check_id,status:'passed',required:true,revision:'780889be841f8787d21c36aec64ef54638ac7e5f9325ce58af34a78fe653838c'})),
    unverified:['out_of_sample','forward_paper_run','live_execution']}}}}};
}
test('actual CEX result cannot show recorded checks passed with 0 passed and 3 unresolved',()=>{
 const result=cexResult(),evidence=deliveryEvidence(result);
 assert.equal(evidence.status,'pending');
 assert.equal(evidence.passed,0);assert.equal(evidence.unresolved,3);assert.equal(evidence.files.length,3);
 const output=html(React.createElement(DeliveryEvidence,{result}));
 assert.match(output,/Waiting for check results/);assert.match(output,/3 artifacts/);assert.match(output,/0 passed/);assert.match(output,/3 unresolved/);
 assert.doesNotMatch(output,/Recorded checks passed/);
});
test('even declared completion cannot override unresolved contract checks or stages',()=>{
 for(const state of ['failed','pending','stale','missing','partial']){
  const result=cexResult();result.delivery.completion={allowed:true,status:'complete'};
  result.delivery.stages.forEach(stage=>{stage.state='completed';});
  result.delivery.checks.forEach(check=>{check.status=state;});
  assert.equal(deliveryEvidence(result).status,state==='partial'?'pending':state);
  assert.equal(deliveryEvidence(result).unresolved,3);
 }
 for(const state of ['pending','in_progress','blocked']){
  const result=cexResult();result.delivery.completion={allowed:true,status:'complete'};
  result.delivery.checks.forEach(check=>{check.status='passed';});
  result.delivery.stages.forEach(stage=>{stage.state=state;});
  assert.equal(deliveryEvidence(result).status,'pending');
 }
});
test('passing checks alone never bypass an incomplete contract, while real completion remains green',()=>{
 const result=cexResult();result.delivery.checks.forEach(check=>{check.status='passed';});
 result.delivery.stages.forEach(stage=>{stage.state='completed';});
 assert.equal(deliveryEvidence(result).status,'pending');
 result.delivery.completion={allowed:true,status:'partial'};assert.equal(deliveryEvidence(result).status,'pending');
 result.delivery.completion={allowed:true,status:'complete'};assert.equal(deliveryEvidence(result).status,'passed');
 result.delivery.checks=[];assert.equal(deliveryEvidence(result).status,'missing');
 assert.deepEqual(deliveryEvidence(result).checks,[], 'old verifier checks must not silently replace an empty contract');
});
test('strategy links preserve candidate and source task identity',()=>{
 const output=html(React.createElement(TaskProvenance,{thread:{id:'task-1',strategy_id:'strategy-1',strategy_proposal_id:'proposal-1',messages:[]}}));
 assert.match(output,/strategy_id=strategy-1/);assert.match(output,/proposal_id=proposal-1/);assert.match(output,/session_id=task-1/);assert.match(output,/Candidate version/);
});

const { taskEntryTitle } = require('../lib/workbench.ts');
test('legacy session titles resolve from meta before reaching rename controls',()=>{
 assert.equal(taskEntryTitle({meta:{title:'Existing research task'}}),'Existing research task');
 assert.equal(taskEntryTitle({title:'Current title',meta:{title:'Old title'}}),'Current title');
 assert.equal(taskEntryTitle({title:null,meta:{}}),'');
 assert.equal(taskEntryTitle({title:'User response to 确认 BTC 永续计价货币: {"action":"answe'}),'确认 BTC 永续计价货币');
});

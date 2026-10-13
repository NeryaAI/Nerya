const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const Module=require('node:module');
const ts=require('typescript');
const file=path.resolve(__dirname,'../lib/workflowActivity.ts');
const m=new Module(file);
m.require=name=>name==='./workflowPresentation'?{asObject:v=>v&&typeof v==='object'&&!Array.isArray(v)?v:{}}:{contentValue:v=>v};
m._compile(ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,file);
const {activities,activityState,scriptFinancialEvidence}=m.exports;
const t=k=>k;
test('saved native script confirmed receipt overrides hold No action without changing status',()=>{
 const row={run_id:'run_real',status:'hold',outputs:{result:{metadata:{phase:'watch',action_id:'fund_real',receipt:{state:'confirmed',position_id:'4',transaction_hash:'0x123'}}}}};
 const item=activities([row],[])[0];
 assert.equal(activityState(item,t).label,'copy.lib_workflowActivity.012');
 assert.equal(item.status,'hold');
 assert.equal(scriptFinancialEvidence(row).receipt.position_id,'4');
});
test('ordinary holds and candidate requests cannot claim confirmed execution',()=>{
 for(const metadata of [{},{action_id:'fund'},{action_id:'fund',receipt:{state:'prepared'}}]){
  const row={run_id:'r',status:'hold',outputs:{result:{metadata}}};
  assert.equal(scriptFinancialEvidence(row),null);
  assert.equal(activityState(activities([row],[])[0],t).label,'copy.lib_workflowActivity.006');
 }
});
test('uncertain receipt remains pending; errors retain failure label',()=>{
 const row={run_id:'r',status:'hold',outputs:{result:{metadata:{action_id:'fund',receipt:{state:'unconfirmed'}}}}};
 assert.equal(activityState(activities([row],[])[0],t).label,'copy.lib_workflowActivity.013');
 row.status='error';assert.equal(activityState(activities([row],[])[0],t).label,'copy.lib_workflowActivity.001');
});

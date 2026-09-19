const fs=require('node:fs'),ts=require('typescript'),assert=require('node:assert/strict');
require.extensions['.ts']=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,f);
const {verificationSummary,verificationPrompt,replayLabel}=require('../lib/workflowVerification.ts');
const checks=[],t=(zh,en)=>zh;
const base={target:{strategy_id:'example',proposal_id:'prp_selected',source_revision:'revision_selected'},validation:{ok:true,blockers:[]},replay:{status:'missing'}};
function check(name,fn){fn();checks.push(name);}
check('missing replay is not completion',()=>assert.match(verificationSummary(base,t),/下一步验证/));
check('old code invalidates reported result',()=>assert.match(verificationSummary({...base,replay:{status:'stale'}},t),/旧回放不能证明/));
check('sample is not real data',()=>assert.match(verificationSummary({...base,replay:{status:'sample'}},t),/不代表真实行情/));
check('PASS is not future performance',()=>assert.match(verificationSummary({...base,replay:{status:'verified'}},t),/尚未证明未来/));
check('blockers take precedence over a report',()=>assert.match(verificationSummary({...base,validation:{ok:false},replay:{status:'verified'}},t),/先修复配置/));
check('unknown status remains unknown',()=>assert.equal(replayLabel('unknown_future_status',t),'状态未知'));
check('handoff contains the selected candidate and source',()=>{const s=verificationPrompt(base,t);assert.ok(s.includes('prp_selected')&&s.includes('revision_selected'));});
check('handoff does not remove trading intent to pass',()=>assert.match(verificationPrompt(base,t),/不要为通过验证而改变策略的交易目标/));
check('repair handoff includes actual error evidence',()=>{const v={...base,validation:{ok:false,blockers:[{where:'main.py:2',message:'actual syntax error'}]}};assert.match(verificationPrompt(v,t),/main.py:2: actual syntax error/);});
console.log(JSON.stringify({passed:checks.length,checks}));

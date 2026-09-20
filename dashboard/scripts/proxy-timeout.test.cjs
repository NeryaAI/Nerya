"use strict";
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
// Expose the private function only in this test's in-memory compilation.
const source=fs.readFileSync(path.join(__dirname,'../app/api/proxy/[...path]/route.ts'),'utf8');
const compiled=ts.transpileModule(source+'\nexport const timeoutUnderTest = proxyTimeoutMs;\n',{
 compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}
}).outputText;
const exportsForTest={};
vm.runInNewContext(compiled,{
 exports:exportsForTest,process:{env:{}},
 require(name){if(name==='next/server')return {};if(name.endsWith('/requestLocality'))return {};return require(name);}
});
for(const route of ['agent/run_turn','agent/run_turn_internal','strategy/run','strategies/runtime/backtest','triggers/schedules/run_now','triggers/schedules/tick']){
 test(`long route ${route} outlives maximum supported turn`,()=>assert.equal(exportsForTest.timeoutUnderTest(route),7260000));
}
for(const route of ['health','agent/session','llm/config']){
 test(`ordinary route ${route} remains bounded`,()=>assert.equal(exportsForTest.timeoutUnderTest(route),120000));
}
test('hosted duration matches long transport',()=>assert.equal(exportsForTest.maxDuration*1000,exportsForTest.timeoutUnderTest('agent/run_turn')));

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (m,f) => m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'), {
  compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true},
}).outputText,f);
const {conversationError} = require('../lib/conversationError.ts');
test('billing errors select translated settings guidance instead of RPM retry', () => {
  for (const raw of ['HTTP 429 Insufficient balance or no resource package.',JSON.stringify({code:'model_quota_exhausted',status_code:429})]) {
    const error=conversationError(raw,true);
    assert.equal(error.code,'model_quota_exhausted');
    assert.equal(error.canRerun,false);
    assert.equal(error.needsSettings,true);
    assert.match(error.message,/余额/);
    assert.match(conversationError(raw,false).message,/credit/);
  }
});
test('system interruption copy is translated and does not enable blind replay', () => {
  for (const code of ['model_stream_interrupted','command_control_unavailable','command_lease_lost']) {
    for (const zh of [true,false]) {
      const error=conversationError(JSON.stringify({code}),zh);
      assert.equal(error.canRerun,false);
      assert.ok(!error.message.startsWith('copy.'));
      assert.ok(error.message.length>25);
    }
  }
});

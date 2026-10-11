const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText, f);
const {conversationError} = require('../lib/conversationError.ts');

test('billing 429 has an actionable explanation, not a rate-limit retry', () => {
  for (const raw of ['HTTP 429 Insufficient balance or no resource package. Please recharge.', JSON.stringify({code:'model_quota_exhausted',status_code:429})]) {
    const error=conversationError(raw,true);
    assert.equal(error.code,'model_quota_exhausted');
    assert.equal(error.canRerun,false);
    assert.equal(error.needsSettings,true);
    assert.match(error.message,/余额/);
    assert.match(conversationError(raw,false).message,/credit/);
  }
});
test('system interruptions never invite blind replay of completed effects', () => {
  for (const code of ['model_stream_interrupted','command_control_unavailable','command_lease_lost','model_stream_connection_failed']) {
    for (const zh of [true,false]) {
      const error=conversationError(JSON.stringify({code}),zh);
      assert.equal(error.canRerun,false);
      assert.ok(error.message.length>25);
    }
  }
});
test('real rate limits remain distinct and diagnostics remain redacted', () => {
  const error=conversationError('HTTP 429 rpm exhausted Bearer example-secret',true);
  assert.equal(error.code,'rate_limited');
  assert.ok(!error.diagnostics.includes('example-secret'));
});

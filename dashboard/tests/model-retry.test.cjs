const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText, filename);
const { latestModelRetry, rateLimitError } = require('../lib/modelRetry.ts');
const { liveEventsToBlocks } = require('../lib/chat.ts');
const block = state => ({ block: { kind: 'thinking', text: 'must not be displayed', retry: {
  state, status_code: 429, attempt: 2, max_attempts: 9, retry_at: 1234,
} } });
test('operational retry state waits, requests and clears after recovery', () => {
  assert.equal(latestModelRetry([block('waiting')]).state, 'waiting');
  assert.equal(latestModelRetry([block('waiting'), block('requesting')]).state, 'requesting');
  assert.equal(latestModelRetry([block('waiting'), block('recovered')]), null);
  assert.equal(latestModelRetry([{ block: { kind: 'thinking', text: 'private' } }]), null);
});
test('streaming bus projection retains waiting/requesting/recovery without exposing thinking', () => {
  const event = state => ({ kind: 'turn.step', step: { kind: 'thinking', detail: block(state).block } });
  assert.equal(latestModelRetry(liveEventsToBlocks([event('waiting')])).state, 'waiting');
  assert.equal(latestModelRetry(liveEventsToBlocks([event('waiting'), event('requesting')])).state, 'requesting');
  assert.equal(latestModelRetry(liveEventsToBlocks([event('waiting'), event('recovered')])), null);
});
test('invalid retry metadata does not enter the visible status', () => {
  assert.equal(latestModelRetry([{ block: { retry: { state: 'waiting', retry_at: NaN } } }]), null);
});
test('clean and historical wrapped rate limits display 429 without traceback', () => {
  for (const raw of [
    'HTTP 429 | rate_limited | Provider rate limit reached.',
    'HTTP 429: {"error":"rate_limited"}',
    'HTTP 500 | LLMError: sensenova messages api error (429): rpm exhausted | request_id=test | trace: secret/file.py',
  ]) {
    const result = rateLimitError(raw, true);
    assert.equal(result.kind, 'HTTP 429');
    assert.equal(result.showRawByDefault, false);
    assert.doesNotMatch(result.message, /500|trace|secret|LLMError/);
    assert.match(result.hint, /已结束/);
  }
});
test('permanent quota needs operator action, not repeated RPM retries', () => {
  assert.match(rateLimitError('HTTP 429 | quota_exhausted', true).hint, /计费/);
  assert.equal(rateLimitError('HTTP 403 | unauthorized', true), null);
  assert.equal(rateLimitError('HTTP 500 | bug | trace: api error (429)', false), null);
});

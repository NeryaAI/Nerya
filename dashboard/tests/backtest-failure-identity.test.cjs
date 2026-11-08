// Isolated producer-receipt fixtures; no runtime, browser, or session writes.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, filename);
const { collectBacktestResults, extractBacktestResults } = require('../lib/backtestResults.ts');

const rawError = 'raw provider diagnostic';
function richFailure(callId = 'X', attempt) {
  return {
    kind: 'tool_result', call_id: callId, action: 'strategy_backtest',
    ok: false, result: null, error: rawError,
    recovery: { reason: 'historical_provider_unsupported', safe_to_retry: false },
    verification: { protocol: 'nerya.verification.v1', artifact_id: 'strategy:example:proposal:prp_x', revision: 'abc' },
    ...(attempt === undefined ? {} : { attempt }),
  };
}
function sparseFailure(callId = 'X', attempt) {
  return {
    call_id: callId, action: 'strategy_backtest', ok: false, result: null, error: rawError,
    payload: { proposal_id: 'prp_x' },
    ...(attempt === undefined ? {} : { attempt }),
  };
}
function reply(blocks, trace = []) {
  return { role: 'assistant', id: 'latest-reply', command_id: 'latest-command', ts: 1793200000000,
    turn: { blocks: blocks.map(block => ({ block })), tool_trace: trace } };
}
function assertBoundBlocker(row) {
  assert.equal(row.status, 'blocked');
  assert.equal(row.failureReason, 'historical_provider_unsupported');
  assert.equal(row.strategyId, 'example');
  assert.equal(row.proposalId, 'prp_x');
  assert.equal(row.provenance.source_revision, 'abc');
  assert.equal(row.message, rawError);
  assert.equal(row.ts, '', 'a blocked attempt has no completed backtest timestamp');
}

test('null-result failure recovers typed reason and producer identity without request arguments', () => {
  const rows = extractBacktestResults(richFailure());
  assert.equal(rows.length, 1);
  assertBoundBlocker(rows[0]);
});

for (const richFirst of [true, false]) {
  const order = richFirst ? 'rich then sparse' : 'sparse then rich';
  test(`latest command merges block and trace into one bound blocked ref: ${order}`, () => {
    const first = richFirst ? richFailure() : sparseFailure();
    const second = richFirst ? sparseFailure() : richFailure();
    const rows = collectBacktestResults({ messages: [reply([first], [second])] });
    assert.equal(rows.length, 1, 'block and trace are two representations of call X');
    assertBoundBlocker(rows[0]);
  });

  test(`extraction merges duplicate failure receipts without losing typed metadata: ${order}`, () => {
    const receipts = richFirst ? [richFailure(), sparseFailure()] : [sparseFailure(), richFailure()];
    const rows = extractBacktestResults(receipts);
    assert.equal(rows.length, 1);
    assertBoundBlocker(rows[0]);
  });
}

test('identical errors from distinct call IDs remain two blocked refs after block/trace deduplication', () => {
  const rows = collectBacktestResults({ messages: [reply(
    [richFailure('X'), richFailure('Y')], [sparseFailure('Y'), sparseFailure('X')],
  )] });
  assert.equal(rows.length, 2);
  assert.equal(new Set(rows.map(row => row.id)).size, 2);
  rows.forEach(assertBoundBlocker);
});

test('extraction keeps distinct calls even when producer identity and raw errors match', () => {
  const rows = extractBacktestResults([richFailure('X'), richFailure('Y')]);
  assert.equal(rows.length, 2);
  assert.equal(new Set(rows.map(row => row.id)).size, 2);
  rows.forEach(assertBoundBlocker);
});

test('explicit attempts remain distinct while each attempt merges its rich and sparse receipts', () => {
  const rows = collectBacktestResults({ messages: [reply(
    [richFailure('X', 1), richFailure('X', 2)],
    [sparseFailure('X', 2), sparseFailure('X', 1)],
  )] });
  assert.equal(rows.length, 2);
  assert.equal(new Set(rows.map(row => row.id)).size, 2);
  rows.forEach(assertBoundBlocker);
});

test('extraction keeps explicit attempts distinct for the same call and diagnostic', () => {
  const rows = extractBacktestResults([richFailure('X', 1), richFailure('X', 2)]);
  assert.equal(rows.length, 2);
  assert.equal(new Set(rows.map(row => row.id)).size, 2);
  rows.forEach(assertBoundBlocker);
});

test('reused call IDs in separate commands never merge independent failures', () => {
  const first = reply([richFailure()]);
  const second = {...reply([richFailure()]), id:'another-reply', command_id:'another-command'};
  const rows = collectBacktestResults({messages:[first,second]});
  assert.equal(rows.length,2);
  assert.equal(new Set(rows.map(row=>row.id)).size,2);
});

const success = { ok: true, strategy_id: 'example', proposal_id: 'prp_x', backtest_ts: '20261029_120000',
  result_type: 'backtest_result', metrics_display: { total_return_pct: '1.25%' } };

test('MCP text content remains producer output within a backtest result envelope', () => {
  const rows = extractBacktestResults({type:'tool_result', name:'strategy_backtest',
    content:[{type:'text', text:JSON.stringify(success)}]});
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, 'completed');
});

test('unbound tool arguments never create completed evidence through extraction or collection', () => {
  for (const input of [
    { kind: 'tool_use', call_id: 'X', action: 'strategy_backtest', arguments: success },
    { action: 'strategy_backtest', arguments: success },
  ]) {
    assert.deepEqual(extractBacktestResults(input), []);
    assert.deepEqual(collectBacktestResults({ messages: [reply([input], [input])] }), []);
  }
});

test('assistant prose and user-supplied receipt-shaped JSON never become completed evidence', () => {
  const message = reply([]);
  message.turn.reply_text = JSON.stringify(success);
  message.turn.blocks = [{ block: { kind: 'text', text: JSON.stringify(success) } }];
  assert.deepEqual(collectBacktestResults({ messages: [message,
    { role: 'user', id: 'user-json', ts: 1793200000001, text: JSON.stringify(success) },
  ] }), []);
});

test('legacy successful block/trace receipts without call IDs still deduplicate by proposal and run', () => {
  const rows = collectBacktestResults({ messages: [reply(
    [{ kind: 'tool_result', action: 'strategy_backtest', result: { ...success } }],
    [{ action: 'strategy_backtest', result: { ...success } }],
  )] });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].id, 'prp_x:example:20261029_120000');
  assert.equal(rows[0].status, 'completed');
  assert.equal(rows[0].metrics.total_return_pct, '1.25%');
});

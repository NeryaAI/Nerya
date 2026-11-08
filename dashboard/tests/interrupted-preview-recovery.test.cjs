// Synthetic projection of command 4b2fe5e2's bounded, read-only DB event order:
// seq 50/57/68 text -> 163..166 input previews -> 167 interrupted ->
// 168 same-stream replacement -> 169 turn.complete -> command.outcome.
// There were zero tool.start/tool.complete events and delivery_json was NULL.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, f);
const { projectMessageBlocks } = require('../lib/streamTranscript.ts');
const { partitionTranscript } = require('../lib/executionTimeline.ts');
const identity = { session_id:'session', turn_id:'turn', epoch:'bus', stream_id:'turn:1:1' };
const safe = '模型响应中断，本轮未能完成。已有输出不完整，不能作为最终结论。';
const raw = '</arg_value><5b656597></tool_call>delivery_start returned an error: a delivery contract is already active';
const event = (seq, kind, fields = {}) => ({ ...identity, seq, event_id:`event-${seq}`, kind, ...fields });
const preview = [
  event(1, 'turn.step', { mode:'append', completed:false, step:{ kind:'thinking', detail:{ text:'Uncommitted reasoning' } } }),
  event(50, 'message.delta', { mode:'append', completed:false, text:'开始本轮：先建立 delivery 并' }),
  event(57, 'message.delta', { mode:'append', completed:false, text:raw }),
  ...['Skill', 'list_dir', 'delivery_start', 'delivery_update'].map((name, i) =>
    event(163+i, 'model.tool_input_delta', { name, call_id:`preview-${i}`, partial_json:`{"summary":"${raw}` })),
];
const interrupted = event(167, 'model.model_transport', { status:'interrupted', reason:'stream_interrupted', retryable:false });
const replacement = event(168, 'message.delta', { mode:'replace', completed:true, text:safe });
const complete = event(169, 'turn.complete', { stop_reason:'stream_interrupted', transition_reason:'stream_interrupted', tool_calls:0, final_text:safe });
const row = block => ({ turn_id:'turn', block });
const committed = [
  row({ kind:'thinking_delta', stream_id:identity.stream_id, text:'Uncommitted reasoning' }),
  row({ kind:'text_delta', stream_id:identity.stream_id, text:raw }),
  row({ kind:'model_transport', stream_id:identity.stream_id, status:'interrupted', reason:'stream_interrupted' }),
  row({ kind:'text', stream_id:identity.stream_id, text:safe }),
];
const message = (live_events, fields = {}) => ({ id:'assistant', role:'assistant', ts:1, loading:true,
  turn:{ turn_id:'turn', blocks:[] }, live_events, ...fields });
const bodies = rows => rows.map(env => env.block.text).filter(Boolean);
const assertClean = rows => {
  assert.equal(rows.some(env => env.block.completed === false || env.block.phase === 'input_streaming'), false);
  assert.equal(JSON.stringify(rows).includes(raw), false);
};

test('active previews remain visible until the interruption event arrives', () => {
  const rows = projectMessageBlocks(message(preview));
  assert.equal(rows.some(env => env.block.text?.includes(raw)), true);
  assert.equal(rows.filter(env => env.block.phase === 'input_streaming').length, 4);
});

test('interruption revokes uncommitted previews before the replacement and polling settle', () => {
  const rows = projectMessageBlocks(message([...preview, interrupted]));
  assertClean(rows);
  assert.deepEqual(rows, []);
});

test('same-stream completed replacement wins while loading still lags terminal events', () => {
  for (const tail of [[replacement], [replacement, complete]]) {
    const rows = projectMessageBlocks(message([...preview, interrupted, ...tail]));
    assertClean(rows);
    assert.deepEqual(bodies(rows), [safe]);
    assert.equal(partitionTranscript(rows, '', safe).reply, safe);
  }
});

for (const loading of [true, false]) test(`committed terminal snapshot wins over delayed old live previews, loading=${loading}`, () => {
  const input = message(preview, { loading, execution_status:'interrupted',
    turn:{ turn_id:'turn', stopped_reason:'stream_interrupted', decision:{ text:safe }, blocks:committed, delivery:null } });
  const before = JSON.stringify(input);
  const rows = projectMessageBlocks(input);
  assertClean(rows);
  assert.deepEqual(bodies(rows), [safe]);
  assert.equal(JSON.stringify(input), before);
  assert.deepEqual(projectMessageBlocks({ ...input, live_events:[] }), rows);
});

test('earlier completed progress, literal marker prose/code and real tool receipts survive', () => {
  const progress = `说明这些普通文本与代码：\n\`\`\`xml\n${raw}\n\`\`\``;
  const earlier = [
    event(1, 'message.delta', { stream_id:'earlier', text:progress, mode:'replace', completed:true }),
    event(2, 'turn.step', { stream_id:'earlier', completed:true, step:{ kind:'thinking', detail:{ text:'Completed summary' } } }),
    event(3, 'tool.start', { stream_id:'earlier', call_id:'real', action:'read_file' }),
    event(4, 'tool.complete', { stream_id:'earlier', call_id:'real', action:'read_file', ok:true, result:{ found:true } }),
  ].map(e => ({ ...e, event_id:`earlier-${e.seq}` }));
  const rows = projectMessageBlocks(message([...earlier, ...preview, interrupted, replacement, complete]));
  assert.deepEqual(bodies(rows), [progress, 'Completed summary', safe]);
  assert.deepEqual(rows.map(env => env.block.kind), ['text', 'thinking', 'tool_use', 'tool_result', 'text']);
  assert.deepEqual(rows.find(env => env.block.kind === 'tool_result').block.result, { found:true });
});

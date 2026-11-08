// Offline recovery fixtures; no runtime, provider, browser or user data.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, f);
const { enrichCommittedBlocks, executionEntries, executionSteps, partitionTranscript } = require('../lib/executionTimeline.ts');
const { projectMessageBlocks } = require('../lib/streamTranscript.ts');
const row = block => ({ block });
const fallback = row({ kind:'text', text:'Execution failed.' });
const completed = (kind, stream_id, text) => row({ kind, stream_id, text, completed:true });
const kinds = blocks => blocks.map(env => env.block.kind);

test('terminal fallback retains completed commentary and provider summaries in tool order', () => {
  const streamed = [
    completed('thinking', 'step-1', 'Provider summary one'),
    completed('text', 'step-1', 'Public progress one'),
    row({ kind:'tool_use', call_id:'call-1', action:'read_file' }),
    row({ kind:'tool_result', call_id:'call-1', result:{ ok:true } }),
    completed('text', 'step-2', 'Public progress two'),
  ];
  const merged = enrichCommittedBlocks([fallback], streamed, false);
  assert.deepEqual(kinds(merged), ['thinking', 'text', 'tool_use', 'tool_result', 'text', 'text']);
  assert.deepEqual(merged.map(env => env.block.text).filter(Boolean),
    ['Provider summary one', 'Public progress one', 'Public progress two', 'Execution failed.']);
  const partition = partitionTranscript(merged, 'Execution failed.', 'Execution failed.');
  assert.equal(partition.reply, 'Execution failed.');
  assert.deepEqual(executionEntries(partition.work, executionSteps(partition.work), false).map(entry => entry.kind),
    ['thinking', 'text', 'tool', 'text']);
});

test('durable events recover a terminal fallback without retry previews or a duplicate final', () => {
  const identity = { session_id:'s', turn_id:'t', epoch:'e' };
  const events = [
    { kind:'message.delta', stream_id:'old', text:'Discarded preview', completed:true },
    { kind:'turn.step', stream_id:'old', completed:true, step:{ kind:'thinking', detail:{ text:'Discarded summary' } } },
    { kind:'model.tool_input_delta', stream_id:'old', call_id:'preview', name:'read_file', partial_json:'{' },
    { kind:'model.model_transport', stream_id:'old', status:'discarded', discard_partial:true },
    { kind:'message.delta', stream_id:'progress', text:'Public ', mode:'append' },
    { kind:'message.delta', stream_id:'progress', text:'Public progress', mode:'replace', completed:true },
    { kind:'message.delta', stream_id:'progress', text:' stale fragment', mode:'append' },
    { kind:'turn.step', stream_id:'progress', completed:true, step:{ kind:'thinking', detail:{ text:'Provider summary' } } },
    { kind:'tool.start', call_id:'call', action:'read_file' },
    { kind:'tool.complete', call_id:'call', action:'read_file', ok:true, result:{ ok:true } },
    { kind:'message.delta', stream_id:'unfinished', text:'Unfinished tail', completed:false },
    { kind:'turn.step', stream_id:'retry', completed:true, step:{ kind:'thinking', detail:{ text:'Retry status', retry:{ attempt:2 } } } },
    { kind:'message.delta', stream_id:'final', text:'Execution failed.', mode:'replace', completed:true },
  ].map((event, i) => ({ ...identity, seq:i+1, event_id:`event-${i}`, ...event }));
  const message = { role:'assistant', id:'message', ts:1, loading:false,
    turn:{ execution_status:'failed', reply_text:'Execution failed.', blocks:[fallback], activity_events:events },
    live_events:events.slice(-8),
  };
  const blocks = projectMessageBlocks(message);
  assert.deepEqual(kinds(blocks), ['text', 'thinking', 'tool_use', 'tool_result', 'text']);
  assert.deepEqual(blocks.map(env => env.block.text).filter(Boolean),
    ['Public progress', 'Provider summary', 'Execution failed.']);
  assert.deepEqual(projectMessageBlocks({ ...message, live_events:[] }), blocks);
  assert.deepEqual(enrichCommittedBlocks(blocks, blocks, false), blocks);
});

test('sparse committed tool anchors keep recovered narration between calls and the final last', () => {
  const first = row({ kind:'tool_use', call_id:'first' });
  const last = row({ kind:'tool_use', call_id:'last' });
  const saved = [first, last, row({ kind:'tool_result', call_id:'last', result:null }), fallback];
  const streamed = [
    completed('text', 'one', 'Before first'), first,
    row({ kind:'tool_result', call_id:'first', result:{ found:true } }),
    completed('text', 'two', 'Between calls'), last,
    row({ kind:'tool_result', call_id:'last', result:{ stale:true } }),
    completed('thinking', 'three', 'After last'),
  ];
  const merged = enrichCommittedBlocks(saved, streamed);
  assert.deepEqual(merged.map(env => env.block.text || `${env.block.kind}:${env.block.call_id}`),
    ['Before first', 'tool_use:first', 'tool_result:first', 'Between calls', 'tool_use:last', 'tool_result:last', 'After last', 'Execution failed.']);
  assert.equal(merged.find(env => env.block.kind==='tool_result' && env.block.call_id==='last').block.result, null);
});

test('completed event replaces a partial snapshot but cannot regress a saved completed reply', () => {
  const saved = [row({ kind:'text', stream_id:'progress', text:'Partial', completed:false }),
    completed('text', 'final', 'Saved final')];
  const streamed = [completed('text', 'progress', 'Completed progress'),
    completed('text', 'final', 'Stale final'),
    completed('thinking', 'summary', 'Completed summary')];
  assert.deepEqual(enrichCommittedBlocks(saved, streamed).map(env => env.block.text),
    ['Completed progress', 'Completed summary', 'Saved final']);
});

test('same stream name from different epochs or turns does not overwrite completed narration', () => {
  const make = (epoch, turn_id, text) => row({ kind:'text', stream_id:'shared', epoch, turn_id, text, completed:true });
  const saved = [make('one', 't', 'First'), fallback];
  const streamed = [make('one', 't', 'First'), make('two', 't', 'Second'), make('two', 'other', 'Third')];
  assert.deepEqual(enrichCommittedBlocks(saved, streamed).map(env => env.block.text),
    ['First', 'Second', 'Third', 'Execution failed.']);
});

test('terminal discard removes a saved preview while completed receipts and replacement survive', () => {
  const identity = { stream_id:'old', epoch:'e', turn_id:'t' };
  const saved = [row({ ...identity, kind:'text', text:'Stale preview', completed:false }),
    row({ kind:'tool_result', call_id:'done', result:{ ok:true } }), fallback];
  const streamed = [row({ ...identity, kind:'model_transport', status:'discarded' }),
    row({ ...identity, kind:'thinking', text:'Late discarded summary', completed:true }),
    completed('text', 'replacement', 'Recovered commentary')];
  const merged = enrichCommittedBlocks(saved, streamed);
  assert.deepEqual(kinds(merged), ['tool_result', 'text', 'text']);
  assert.deepEqual(merged.map(env => env.block.text).filter(Boolean), ['Recovered commentary', 'Execution failed.']);
});

test('final text is not duplicated under a different stream ID, but distinct progress prefixes survive', () => {
  const saved = [completed('text', 'saved-final', 'Final answer is ready.')];
  const streamed = [completed('text', 'progress', 'Final answer'),
    row({ kind:'tool_use', call_id:'preview', phase:'input_streaming' }),
    completed('text', 'event-final', 'Final answer is ready.')];
  assert.deepEqual(enrichCommittedBlocks(saved, streamed).map(env => env.block.text),
    ['Final answer', 'Final answer is ready.']);
});

test('running snapshots still accept partial updates and keep the active tail', () => {
  const saved = [row({ kind:'text', stream_id:'live', text:'Before', completed:false })];
  const streamed = [row({ kind:'text', stream_id:'live', text:'Before and after', completed:false }),
    row({ kind:'thinking', stream_id:'tail', text:'Active summary', completed:false })];
  const merged = enrichCommittedBlocks(saved, streamed, true);
  assert.deepEqual(merged.map(env => env.block.text), ['Before and after', 'Active summary']);
  assert.deepEqual(merged.map(env => env.block.presentation_active), [false, true]);
});

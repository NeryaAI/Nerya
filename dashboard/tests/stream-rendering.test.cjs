const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, f);
const { mergeStreamEvents, mergeStreamText } = require('../lib/streamEvents.ts');
const { normalizeTranscriptBlocks } = require('../lib/transcriptProjection.ts');
const { liveEventsToBlocks } = require('../lib/chat.ts');
const { hasUnclosedFence } = require('../components/chat/Markdown.tsx');
const { enrichCommittedBlocks } = require('../lib/executionTimeline.ts');

test('stream text handles snapshot, append, replace and reconnect replay without duplication', () => {
  assert.equal(mergeStreamText('', 'Hello'), 'Hello');
  assert.equal(mergeStreamText('Hello', 'Hello world'), 'Hello world');
  assert.equal(mergeStreamText('Hello world', ' world'), 'Hello world');
  assert.equal(mergeStreamText('Hello', ' world', 'append'), 'Hello world');
  assert.equal(mergeStreamText('old', 'new', 'replace'), 'new');
  const events = mergeStreamEvents(
    [{ session_id:'s', turn_id:'t', epoch:'e1', seq:2, event_id:'two', kind:'message.delta', text:'world' }],
    [{ session_id:'s', turn_id:'t', epoch:'e1', seq:1, event_id:'one', kind:'message.delta', text:'Hello ' },
     { session_id:'s', turn_id:'t', epoch:'e1', seq:2, event_id:'two', kind:'message.delta', text:'world' }],
    [{ session_id:'s', turn_id:'t', epoch:'e2', seq:1, event_id:'one', kind:'message.delta', text:'new turn' }],
  );
  assert.deepEqual(events.map(event => [event.epoch, event.seq]), [['e1',1],['e1',2],['e2',1]]);
});

test('transcript projection replaces stream snapshots and keeps completed state', () => {
  const blocks = normalizeTranscriptBlocks([
    { epoch:'e', turn_id:'t', seq:1, block:{ kind:'text_delta', stream_id:'reply', text:'Hello ' } },
    { epoch:'e', turn_id:'t', seq:2, block:{ kind:'text', stream_id:'reply', stream_mode:'replace', text:'Hello world', completed:true } },
    { epoch:'e', turn_id:'t', seq:2, block:{ kind:'text', stream_id:'reply', stream_mode:'replace', text:'Hello world', completed:true } },
  ]);
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].block.text, 'Hello world');
  assert.equal(blocks[0].block.completed, true);
});

test('completed streams ignore late append replay but accept an explicit replacement', () => {
  const blocks = normalizeTranscriptBlocks([
    { epoch:'e', turn_id:'t', seq:1, block:{ kind:'text', stream_id:'reply', text:'Final', completed:true } },
    { epoch:'e', turn_id:'t', seq:2, block:{ kind:'text_delta', stream_id:'reply', text:' old replay' } },
  ]);
  assert.equal(blocks[0].block.text, 'Final');
  const replaced = normalizeTranscriptBlocks([
    { epoch:'e', turn_id:'t', seq:1, block:{ kind:'text', stream_id:'reply', text:'Final', completed:true } },
    { epoch:'e', turn_id:'t', seq:2, block:{ kind:'text', stream_id:'reply', stream_mode:'replace', text:'Corrected', completed:true } },
  ]);
  assert.equal(replaced[0].block.text, 'Corrected');
});

test('live event projection keeps tool result and chart in the call order', () => {
  const blocks = liveEventsToBlocks([
    { session_id:'s', turn_id:'t', epoch:'e', seq:1, event_id:'start', kind:'tool.start', call_id:'call', action:'market_data', payload:{} },
    { session_id:'s', turn_id:'t', epoch:'e', seq:2, event_id:'complete', kind:'tool.complete', call_id:'call', action:'market_data', ok:true, result:{price:1} },
    { session_id:'s', turn_id:'t', epoch:'e', seq:3, event_id:'chart', kind:'chart.block', call_id:'call', chart_block:{chart_id:'chart-1'} },
  ]);
  assert.deepEqual(blocks.map(block => block.block?.kind || block.kind), ['tool_use','tool_result','chart']);
});

test('committed snapshots retain a live result that arrived immediately before commit', () => {
  const committed = [{ block:{ kind:'tool_use', call_id:'call', action:'market_data', attempt:1 } }, { block:{ kind:'tool_result', call_id:'call', action:'market_data', attempt:1 } }];
  const live = [{ block:{ kind:'tool_result', call_id:'call', action:'market_data', attempt:1, ok:true, result:{price:42} } }];
  const merged = enrichCommittedBlocks(committed, live);
  assert.deepEqual(merged[1].block.result, { price:42 });
});

test('streaming markdown treats an unfinished code fence as raw text until completion', () => {
  assert.equal(hasUnclosedFence('before\n```ts\nconst value ='), true);
  assert.equal(hasUnclosedFence('```ts\nconst value = 1\n```'), false);
});

test('discarded model attempt cannot reappear after reconnect or late preview deltas', () => {
  const identity = { epoch:'e', turn_id:'t', stream_id:'t:2:1' };
  const blocks = normalizeTranscriptBlocks([
    { block:{ ...identity, kind:'text_delta', text:'unfinished' } },
    { block:{ ...identity, kind:'thinking_delta', text:'unfinished reasoning' } },
    { block:{ ...identity, kind:'tool_use', phase:'input_streaming', call_id:'preview' } },
    { block:{ ...identity, kind:'model_transport', status:'discarded' } },
    { block:{ ...identity, kind:'text_delta', text:'late old fragment' } },
    { block:{ ...identity, kind:'tool_result', call_id:'completed-earlier', result:{ok:true} } },
    { block:{ ...identity, epoch:'other', kind:'text', text:'different epoch', completed:true } },
    { block:{ ...identity, stream_id:'t:2:2', kind:'text', text:'recovered', completed:true } },
  ]);
  assert.deepEqual(blocks.map(row => row.block.kind), ['tool_result','text','text']);
  assert.deepEqual(blocks.filter(row => row.block.kind==='text').map(row=>row.block.text), ['different epoch','recovered']);
});

test('live discard revokes a stale committed preview but preserves actual tool receipts', () => {
  const identity = { epoch:'e', session_id:'s', turn_id:'t', stream_id:'t:2:1' };
  const live = liveEventsToBlocks([
    { ...identity, seq:1, kind:'message.delta', text:'unfinished' },
    { ...identity, seq:2, kind:'model.tool_input_delta', call_id:'preview', name:'write_file', partial_json:'{' },
    { ...identity, seq:3, kind:'model.model_transport', status:'discarded', discard_partial:true },
    { ...identity, seq:4, stream_id:'t:2:2', kind:'message.delta', text:'recovered', completed:true },
  ]);
  const saved = [
    { block:{ ...identity, kind:'text', text:'stale preview' } },
    { block:{ kind:'tool_result', call_id:'write-once', action:'write_file', result:{saved:true} } },
  ];
  const blocks = enrichCommittedBlocks(saved, live, true);
  assert.deepEqual(blocks.map(row=>row.block.kind), ['tool_result','text']);
  assert.equal(blocks[1].block.text, 'recovered');
  assert.deepEqual(blocks[0].block.result, {saved:true});
});

// Minimal identity mismatch from the interrupted Aave browser capture.
// Real text and the large capture stay outside the repo. No runtime/network IO.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),ts=require('typescript');
require.extensions['.ts']=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{
  compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},
}).outputText,f);
const {projectMessageBlocks}=require('../lib/streamTranscript.ts');
const {enrichCommittedBlocks}=require('../lib/executionTimeline.ts');
const saved=(kind,text,epoch,turn='turn-1',stream='turn-1:1:1')=>({
  turn_id:turn,block:{kind,text,stream_id:stream,completed:true,...(epoch?{epoch}:{})},
});
const event=(kind,text,epoch='bus-1',turn='turn-1',stream='turn-1:1:1')=>({
  kind:kind==='thinking'?'turn.step':'message.delta',turn_id:turn,stream_id:stream,
  epoch,completed:true,mode:'replace',
  ...(kind==='thinking'?{step:{kind:'thinking',detail:{text}}}:{text}),
});

for(const kind of ['thinking','text'])test(kind+': epoch-less saved row and retained live event are one narrative',()=>{
  const message={loading:false,turn:{blocks:[saved(kind,'Narration'),saved('text','Stopped','', 'turn-1','final')],activity_events:[]},
    live_events:[event(kind,'Narration')]};
  const before=JSON.stringify(message);
  const rows=projectMessageBlocks(message);
  assert.equal(rows.length,2);
  assert.equal(rows[0].block.text,'Narration');
  assert.equal(JSON.stringify(message),before);
});

test('identity matching keeps committed text even when the live text differs',()=>{
  const message={loading:false,turn:{blocks:[saved('thinking','Authoritative saved text')]},
    live_events:[event('thinking','Stale retained text')]};
  const rows=projectMessageBlocks(message);
  assert.equal(rows.length,1);
  assert.equal(rows[0].block.text,'Authoritative saved text');
});

for(const loading of [true,false])test('partial saved stream completes in place, loading='+loading,()=>{
  const partial=saved('text','Before');
  partial.block.completed=false;
  const rows=projectMessageBlocks({loading,turn:{blocks:[partial]},
    live_events:[event('text','Before and after')]});
  assert.equal(rows.length,1);
  assert.equal(rows[0].block.text,'Before and after');
  assert.equal(rows[0].block.completed,true);
});

test('epoch metadata may be on the envelope and absent from the live event',()=>{
  const committed=saved('thinking','Saved');
  committed.epoch='bus-1';
  const rows=projectMessageBlocks({loading:false,turn:{blocks:[committed]},
    live_events:[event('thinking','Stale',undefined)] .map(e=>{delete e.epoch;return e;})});
  assert.equal(rows.length,1);
  assert.equal(rows[0].block.text,'Saved');
});

for(const kind of ['thinking','text'])test(kind+': distinct nonempty epochs survive even with identical text',()=>{
  const rows=projectMessageBlocks({loading:false,turn:{blocks:[saved(kind,'Same text','bus-1')]},
    live_events:[event(kind,'Same text','bus-2')]});
  assert.equal(rows.length,2);
  assert.deepEqual(new Set(rows.map(r=>r.block.epoch)),new Set(['bus-1','bus-2']));
});

for(const kind of ['thinking','text'])test(kind+': missing epoch cannot select between two live epochs',()=>{
  const rows=projectMessageBlocks({loading:false,turn:{blocks:[saved(kind,'Same text')]},
    live_events:[event(kind,'Same text','bus-1'),event(kind,'Same text','bus-2')]});
  assert.equal(rows.length,3);
  assert.deepEqual(new Set(rows.map(r=>r.block.epoch||'')),new Set(['','bus-1','bus-2']));
});

test('multiple committed candidates remain ambiguous for one epoch-less live row',()=>{
  const rows=enrichCommittedBlocks([
    saved('thinking','First','bus-1'),saved('thinking','Second','bus-2'),
  ],[saved('thinking','Unknown')]);
  assert.equal(rows.length,3);
  assert.deepEqual(rows.map(r=>r.block.text),['First','Second','Unknown']);
});

test('different turns and missing turn identity cannot use epoch reconciliation',()=>{
  for(const turn of ['turn-other','']) {
    const rows=projectMessageBlocks({loading:false,turn:{blocks:[saved('thinking','Same text')]},
      live_events:[event('thinking','Same text','bus-1',turn)]});
    assert.equal(rows.length,2);
  }
});

test('conflicting session identities cannot borrow the missing epoch',()=>{
  const committed=saved('thinking','Same text');
  committed.block.session_id='session-a';
  const live=event('thinking','Same text');
  live.session_id='session-b';
  assert.equal(projectMessageBlocks({loading:false,turn:{blocks:[committed]},live_events:[live]}).length,2);
});

test('equal wording in separate narrative streams is retained',()=>{
  const rows=projectMessageBlocks({loading:false,turn:{blocks:[saved('text','Repeated phrase'),saved('text','Final','', 'turn-1','final')]},
    live_events:[event('text','Repeated phrase','bus-1','turn-1','turn-1:2:1')]});
  assert.equal(rows.length,3);
  assert.equal(rows.filter(r=>r.block.text==='Repeated phrase').length,2);
});

test('reprojection is stable and retains tool receipts and the terminal failure',()=>{
  const receipt={turn_id:'turn-1',block:{kind:'tool_result',call_id:'call-1',ok:false,error:'Original failure'}};
  const message={loading:false,turn:{blocks:[saved('thinking','Narration'),receipt,saved('text','Stopped','', 'turn-1','final')]},
    live_events:[event('thinking','Narration')]};
  const rows=projectMessageBlocks(message);
  assert.deepEqual(rows.map(r=>r.block.kind),['thinking','tool_result','text']);
  assert.deepEqual(rows[1],receipt);
  assert.equal(rows[2].block.text,'Stopped');
  assert.deepEqual(projectMessageBlocks({...message,turn:{blocks:rows}}),rows);
});

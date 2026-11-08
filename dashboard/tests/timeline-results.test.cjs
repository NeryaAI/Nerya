// Offline component fixtures; no service, model, MCP, browser or user configuration.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, f);
require.extensions['.css'] = m => { m.exports = {}; };
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
const { finalReplyText, publicReplyText, withoutPublicText, collectChatResults } = require('../lib/chatResults.ts');
const { liveEventsToBlocks } = require('../lib/chat.ts');
const { AssistantBubble, UserBubble } = require('../components/chat/ChatMessage.tsx');
const { ExternalSessionTimeline } = require('../components/chat/ExternalSessionTimeline.tsx');
const { ChatResultsPanel } = require('../components/chat/ChatResultsPanel.tsx');
const { DeliveryEvidence } = require('../components/chat/DeliveryEvidence.tsx');
const { TurnWindow } = require('../components/chat/ConversationTimeline.tsx');
const { conversationMatches, matchRange, useTimelineReveal } = require('../components/chat/timelineReveal.ts');
const messages = require('../messages/index.ts');
function html(element, locale = 'en') {
  const previous = console.error;
  console.error = (...args) => { if (!String(args[0]).includes('useLayoutEffect does nothing on the server')) previous(...args); };
  try { return renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale, messages: messages[locale], timeZone:'UTC' }, element)); }
  finally { console.error = previous; }
}
const msg = { role:'assistant', id:'stable-message', ts:1, loading:true, live_events:[
  {kind:'message.delta',seq:1,text:'Public '}, {kind:'message.delta',seq:2,text:'body'},
  {kind:'turn.step',seq:3,step:{kind:'thinking',detail:{text:'PRIVATE REASONING'}}},
  {kind:'tool.start',seq:4,call_id:'read',tool:'read_file',arguments:{path:'fixture.txt'}},
] };
test('public deltas render outside process disclosure and final replaces the same identity', () => {
  const blocks = liveEventsToBlocks(msg.live_events);
  assert.equal(publicReplyText(msg, blocks),'Public body');
  assert.equal(withoutPublicText(blocks).some(env=>env.block?.kind==='tool_use'),true);
  assert.equal(finalReplyText({...msg,loading:false,turn:{decision:{reasoning:'PRIVATE REASONING'}}}), '');
  const live = html(React.createElement(AssistantBubble,{msg}));
  const section = live.match(/<section[^>]*data-turn-section="reply"[\s\S]*?<\/section>/)[0];
  assert.match(section,/Public body/);assert.doesNotMatch(section,/PRIVATE REASONING/);
  assert.match(live,/data-turn-section="reasoning"/);assert.equal((live.match(/Public body/g)||[]).length,1);
  const final = {...msg,loading:false,turn:{reply_text:'Final body',blocks}};
  const done = html(React.createElement(AssistantBubble,{msg:final}));
  assert.match(done,/data-message-identity="stable-message"/);assert.doesNotMatch(done,/Public body/);
  assert.equal((done.match(/<section[^>]*data-turn-section="reply"/g)||[]).length,1);
  assert.equal(collectChatResults({title:'fixture',messages:[msg]}).length,0);
  assert.equal(collectChatResults({title:'fixture',messages:[final]})[0].id,msg.id);
});
test('pending approval retains native renderer outside folded process',()=>{
  const pending={...msg,live_events:[],turn:{blocks:[{block:{kind:'text',text:'Approval needed'}},{block:{kind:'approval_request',approval_id:'fixture-approval',title:'FIXTURE approval'}}]}};
  const output=html(React.createElement(AssistantBubble,{msg:pending,pendingApprovals:new Map([['fixture-approval',{id:'fixture-approval',title:'FIXTURE approval'}]])}));
  assert.match(output,/Approval needed/);assert.doesNotMatch(output,/<details[^>]+group\/process/);
});
test('find counts each literal occurrence, with Unicode offsets and independent message identity',()=>{
  const hits=conversationMatches([{id:'a',text:'Foo foo FOO'},{id:'b',text:'foo'}],'foo');
  assert.deepEqual(hits.map(h=>[h.id,h.matchOffset,h.occurrence]),[['a',0,0],['a',4,1],['a',8,2],['b',0,0]]);
  assert.equal(conversationMatches([{id:'a',text:'a+b a+b'}],'a+b').length,2);
  assert.equal(conversationMatches([{id:'a',text:'😀İ xx'}],'xx')[0].matchOffset,4);
  assert.deepEqual(conversationMatches([{id:'a',text:'text'}],'  '),[]);
});
test('DOM match ranges select the second occurrence across Markdown spans',()=>{
  const oldDocument=global.document,oldFilter=global.NodeFilter;
  const nodes=['first foo and ','f','oo'].map(data=>({data,length:data.length,parentElement:{closest:()=>null}}));
  const root={querySelectorAll:()=>[],querySelector:()=>null};let start,end;
  global.NodeFilter={SHOW_TEXT:4,FILTER_REJECT:2,FILTER_ACCEPT:1};
  global.document={createTreeWalker:()=>{let i=-1;return {nextNode:()=>++i<nodes.length,get currentNode(){return nodes[i]}};},createRange:()=>({setStart:(n,o)=>start=[nodes.indexOf(n),o],setEnd:(n,o)=>end=[nodes.indexOf(n),o]})};
  try {assert.ok(matchRange(root,{id:'a',query:'foo',occurrence:1}));assert.deepEqual(start,[1,0]);assert.deepEqual(end,[2,2]);}
  finally {global.document=oldDocument;global.NodeFilter=oldFilter;}
});
test('1000 external calls keep measured placeholders and mount only live/forced content before intersection',()=>{
  const traces=Array.from({length:1000},(_,i)=>({call_id:'fixture-'+i,source:'mcp',remote_session_id:'fixture-session',sequence:i,tool:'nerya_read_file',status:i===999?'awaiting_approval':'succeeded',arguments:{path:'fixture-'+i+'.txt'},result:{text:'fixture'}}));
  const output=html(React.createElement(ExternalSessionTimeline,{traces,session:'fixture-session',scrollRef:{current:null}}));
  assert.equal((output.match(/data-timeline-turn=/g)||[]).length,1000);
  assert.equal((output.match(/data-timeline-mounted="true"/g)||[]).length,1);
  assert.equal((output.match(/data-testid="external-call"/g)||[]).length,1);
  assert.match(output,/Awaiting approval/);
  const forced=html(React.createElement(TurnWindow,{unit:{id:'old',live:false},session:'fixture',root:{current:null},forced:true},React.createElement('p',null,'forced content')));
  assert.match(forced,/forced content/);
});
test('history operations and body export state their actual scope; failed evidence wins over pass flag',()=>{
  const user=html(React.createElement(UserBubble,{msg:{role:'user',id:'u',backend_message_id:'u',ts:1,text:'fixture'},onEdit:()=>{},onDelete:()=>{},onFork:()=>{}}));
  assert.match(user,/Edit record \(no rerun\)/);assert.match(user,/Delete record/);assert.match(user,/Edit and branch/);
  const result={id:'r',title:'fixture',text:'Everything passed',ts:1,evidence:{verifier:{hard_passed:true},artifacts:{tests_run:[{command:'fixture test',exit_code:1}],unverified_risks:[{message:'NOT CHECKED'}]}}};
  const evidence=html(React.createElement(DeliveryEvidence,{result}));
  assert.match(evidence,/Checks need attention/);assert.match(evidence,/exit 1/);assert.match(evidence,/NOT CHECKED/);assert.doesNotMatch(evidence,/Recorded checks passed/);
  const panel=html(React.createElement(ChatResultsPanel,{results:[result]}));
  assert.match(panel,/Download Markdown body/);assert.match(panel,/not an evidence bundle/);
});
test('report controls and known diagnostics localize while unknown diagnostics and table content survive',()=>{
  const path='evolution/proposals/fixture/after/strategies/eth_report/strategy.py';
  const result={id:'localized-report',title:'报告',ts:1,text:'| 项目 | 内容 |\n| --- | --- |\n| 候选包 | `'+path+'` |\n| 源修订 | `abc123` |\n| 市场 | ETH/USDT |\n| 周期 | 1h |',evidence:{artifacts:{unverified_risks:[{kind:'edit_without_verification'},{kind:'future_diagnostic_code'}]}}};
  for (const [locale,copyLabel,explanation,details] of [
    ['zh','复制','文件已修改，但尚无对应的验证记录。','技术详情'],
    ['en','Copy','Files were edited, but no verification was recorded.','Technical details'],
  ]) {
    const output=html(React.createElement(ChatResultsPanel,{results:[result]}),locale);
    assert.ok(output.includes('title="'+copyLabel+'" aria-label="'+copyLabel+'"'));
    assert.ok(output.includes(explanation));
    assert.ok(output.includes('<details><summary>'+details+'</summary><code>edit_without_verification</code></details>'));
    assert.match(output,/<p>future_diagnostic_code<\/p>/);
    assert.match(output,/<th[^>]*>项目<\/th>/);assert.match(output,/<td[^>]*>候选包<\/td>/);
    assert.ok(output.includes(path));
  }
});

// Minimal hook scheduler for lifecycle assertions; it performs no browser or network I/O.
function hooks(run) {
  const originals={},slots=[],cleanups=[];let cursor=0;
  for(const name of ['useState','useRef','useCallback','useEffect','useLayoutEffect'])originals[name]=React[name];
  React.useState=initial=>{const i=cursor++;if(!(i in slots))slots[i]=initial;return [slots[i],value=>{slots[i]=typeof value==='function'?value(slots[i]):value;}];};
  React.useRef=initial=>{const i=cursor++;return slots[i]||(slots[i]={current:initial});};
  React.useCallback=fn=>fn;
  React.useEffect=fn=>{cleanups.push(fn());};
  React.useLayoutEffect=React.useEffect;
  const render=()=>{cursor=0;return run();};
  return {render,dispose:()=>{cleanups.reverse().forEach(fn=>fn?.());Object.assign(React,originals);}};
}
test('intersection exit unmounts content while preserving its measured height',()=>{
  const saved={IntersectionObserver:global.IntersectionObserver,ResizeObserver:global.ResizeObserver};
  let intersect,resize;
  global.IntersectionObserver=class {constructor(callback){intersect=callback;}observe(){}disconnect(){}};
  global.ResizeObserver=class {constructor(callback){resize=callback;}observe(){}disconnect(){}};
  const props={unit:{id:'measured',live:false},session:'fixture-measured',root:{current:{}},children:'expensive native card'};
  const harness=hooks(()=>TurnWindow.type(props));
  try {
    let tree=harness.render();tree.ref.current={getBoundingClientRect:()=>({height:480})};tree=harness.render();
    assert.equal(tree.props.children,null);intersect([{isIntersecting:true}]);tree=harness.render();assert.equal(tree.props.children,props.children);
    resize();intersect([{isIntersecting:false}]);tree=harness.render();assert.equal(tree.props.children,null);assert.equal(tree.props.style.height,480);
  } finally {harness.dispose();Object.assign(global,saved);}
});
test('reveal waits for mount and cancels superseded, aborted, and old-session requests',async()=>{
  const saved={window:global.window,requestAnimationFrame:global.requestAnimationFrame,cancelAnimationFrame:global.cancelAnimationFrame};
  const frames=new Map();let next=0,focused=0,scrolled=0,session='first';
  global.window={setTimeout,addEventListener:()=>{},removeEventListener:()=>{}};
  global.requestAnimationFrame=fn=>{frames.set(++next,fn);return next;};global.cancelAnimationFrame=id=>frames.delete(id);
  const tick=()=>{const pending=[...frames.values()];frames.clear();pending.forEach(fn=>fn());};
  const target={dataset:{findEntry:'message'},querySelectorAll:()=>[],getBoundingClientRect:()=>({top:100}),classList:{add:()=>{},remove:()=>{}},focus:()=>focused++};
  const unit={dataset:{timelineTurn:'unit',timelineMounted:'false'},scrollIntoView:()=>scrolled++,querySelectorAll:()=>[target]};
  const scopeRef={current:{querySelectorAll:()=>[unit]}},scrollRef={current:{scrollTop:0,clientHeight:300,getBoundingClientRect:()=>({top:0})}};
  const harness=hooks(()=>useTimelineReveal({session,scopeRef,scrollRef,resolveUnit:id=>id==='message'?'unit':undefined}));
  try {
    let hook=harness.render();const waiting=hook.reveal({session:'first',id:'message'});
    assert.equal(scrolled,1);assert.equal(focused,0);tick();assert.equal(focused,0);
    unit.dataset.timelineMounted='true';tick();assert.equal(await waiting,true);assert.equal(focused,1);
    unit.dataset.timelineMounted='false';const stale=hook.reveal({session:'first',id:'message'});const newer=hook.reveal({session:'first',id:'message'});
    assert.equal(await stale,false);unit.dataset.timelineMounted='true';tick();assert.equal(await newer,true);
    const abort=new AbortController();const aborted=hook.reveal({session:'first',id:'message'},abort.signal);abort.abort();assert.equal(await aborted,false);
    const old=hook.reveal({session:'first',id:'message'});session='second';hook=harness.render();tick();assert.equal(await old,false);assert.equal(focused,2);
  } finally {harness.dispose();Object.assign(global,saved);}
});

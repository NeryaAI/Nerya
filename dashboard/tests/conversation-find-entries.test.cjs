// Offline find/index/reveal regression; no service or browser operations.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),ts=require('typescript');
for(const ext of ['.ts','.tsx'])require.extensions[ext]=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{
  compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true},
}).outputText,f);
require.extensions['.css']=m=>{m.exports={};};
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');
const {NextIntlClientProvider}=require('next-intl');
const messages=require('../messages/index.ts').zh;
const findModule=require('../components/chat/ConversationFind.tsx');
const {ConversationTimeline}=require('../components/chat/ConversationTimeline.tsx');
const {conversationMatches,matchRange,useTimelineReveal}=require('../components/chat/timelineReveal.ts');
const block=(kind,text,stream_id)=>({turn_id:'turn-1',block:{kind,text,stream_id,completed:true}});
const middle='契约齐备，开始起草策略包。';
const final='任务已停止，验证尚未完成。';
const completed={id:'local-message',backend_message_id:'saved-message',role:'assistant',ts:1,loading:false,
  execution_status:'interrupted',turn:{turn_id:'turn-1',reply_text:final,blocks:[
    block('text','先核对请求范围。','start'),block('thinking','PRIVATE_THINKING','middle'),
    block('text',middle,'middle'),{block:{kind:'tool_result',result:{text:'PRIVATE_TOOL'},payload:{secret:'PRIVATE_ARGUMENT'}}},
    block('text',final,'final'),
  ]}};

function entries(message=completed,publicOnly=false){
  const previous=findModule.ConversationFind,oldError=console.error;let captured;
  findModule.ConversationFind=props=>{captured=props.entries;return null;};
  console.error=(...args)=>{if(!String(args[0]).includes('useLayoutEffect does nothing on the server'))oldError(...args);};
  try {renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale:'zh',messages,timeZone:'UTC'},
    React.createElement(ConversationTimeline,{messages:[message],session:'task',scrollRef:{current:null},renderMessage:()=>null,publicOnly})));}
  finally {findModule.ConversationFind=previous;console.error=oldError;}
  return captured;
}

test('saved middle public narration is searchable with its stable message identity',()=>{
  const before=JSON.stringify(completed),index=entries();
  const hits=conversationMatches(index,middle);
  assert.equal(hits.length,1);
  assert.equal(hits[0].id,'saved-message');
  assert.equal(conversationMatches(index,final).length,1);
  assert.doesNotMatch(index[0].text,/PRIVATE_/);
  assert.equal(JSON.stringify(completed),before);
});

test('assistant-home remains final-only and result canvases keep their intentional final-only projection',()=>{
  const {publicReplyText}=require('../lib/chatResults.ts');
  const index=entries(completed,true);
  assert.equal(index[0].text,final);
  assert.equal(conversationMatches(index,middle).length,0);
  assert.equal(publicReplyText(completed,completed.turn.blocks),final);
});

test('retained live events and saved snapshots index each public segment only once',()=>{
  const message={...completed,live_events:[{kind:'message.delta',epoch:'live-bus',turn_id:'turn-1',
    stream_id:'middle',mode:'replace',completed:true,text:middle}]};
  assert.equal(conversationMatches(entries(message),middle).length,1);
  assert.equal(conversationMatches(entries(message),final).length,1);
  assert.equal(conversationMatches(entries({...message,loading:true}),middle).length,1);
});

test('reasoning, tool arguments, retry notices, provider diagnostics and non-assistant blocks stay unsearchable',()=>{
  const message={...completed,turn:{...completed.turn,blocks:[...completed.turn.blocks,
    {block:{kind:'text',text:'PRIVATE_RETRY',retry:{attempt:2}}},
    {role:'tool',block:{kind:'text',text:'PRIVATE_TOOL_ROLE'}},
    {block:{kind:'text',role:'tool',text:'PRIVATE_BLOCK_ROLE'}},
    {block:{kind:'model_transport',detail:'PRIVATE_PROVIDER'}},
  ]}};
  assert.equal(conversationMatches(entries(message),'PRIVATE').length,0);
  assert.equal(conversationMatches(entries(message),middle).length,1);
});

test('legacy host diagnostics stay excluded while the visible failure summary appears once',()=>{
  const host='本轮 Agent 在运行过程中异常退出，未能生成最终回复。\n\n错误类型: LLMError\n错误信息: PRIVATE_PROVIDER';
  const message={...completed,turn:{...completed.turn,reply_text:host,stopped_reason:'runtime_error',blocks:[
    block('text',middle,'middle'),block('text','后续公开进度。','after'),block('text',host,'host'),
  ]}};
  const {finalReplyText}=require('../lib/chatResults.ts');
  const index=entries(message);
  assert.equal(conversationMatches(index,middle).length,1);
  assert.equal(conversationMatches(index,finalReplyText(message)).length,1);
  assert.doesNotMatch(index[0].text,/PRIVATE_PROVIDER|错误类型|错误信息/);
});

test('matches keep message offsets and section-local occurrences for identical wording',()=>{
  const message={...completed,turn:{...completed.turn,reply_text:'😀目标 final',blocks:[
    block('text','😀目标 first 目标 second','middle'),block('text','😀目标 final','final'),
  ]}};
  const hits=conversationMatches(entries(message),'目标');
  assert.deepEqual(hits.map(h=>[h.id,h.section,h.occurrence,h.matchOffset]),[
    ['saved-message','commentary',0,2],['saved-message','commentary',1,11],['saved-message','reply',0,24],
  ]);
});

test('user entries retain literal text and external tools are not indexed as assistant narration',()=>{
  assert.deepEqual(entries({role:'user',id:'user',backend_message_id:'saved-user',ts:1,text:'literal *query*'}),[
    {id:'saved-user',text:'literal *query*'},
  ]);
  assert.equal(entries({...completed,turn:{...completed.turn,external_call:{}}})[0].text,'');
});

test('DOM range stays in the requested public section when both contain the same query',()=>{
  const previousDocument=global.document,previousFilter=global.NodeFilter;
  const roots=['commentary','reply'].map(section=>({section,querySelector:()=>null,
    closest:selector=>selector.includes(section)?{}:null,
    nodes:[{data:'目标',length:2,parentElement:{closest:()=>null}}]}));
  let selected;
  global.NodeFilter={SHOW_TEXT:4,FILTER_REJECT:2,FILTER_ACCEPT:1};
  global.document={createTreeWalker:body=>{let i=-1;return {nextNode:()=>++i<body.nodes.length,get currentNode(){return body.nodes[i];}};},
    createRange:()=>({setStart:node=>{selected=node;},setEnd:()=>{}})};
  try {
    for(const section of ['commentary','reply']) {
      assert.ok(matchRange({querySelectorAll:()=>roots},{id:'saved-message',query:'目标',occurrence:0,section}));
      assert.equal(selected,roots.find(root=>root.section===section).nodes[0]);
    }
  } finally {global.document=previousDocument;global.NodeFilter=previousFilter;}
});

test('commentary hit opens only its execution section even when a final reply is already mounted',async()=>{
  const originals=Object.fromEntries(['useState','useRef','useCallback','useEffect'].map(key=>[key,React[key]]));
  const globals=Object.fromEntries(['window','document','NodeFilter','requestAnimationFrame','cancelAnimationFrame'].map(key=>[key,global[key]]));
  const frames=new Map();let next=0,selected;
  const execution={open:false},debug={open:false},tool={open:false};
  const root=(section,text)=>({querySelector:()=>null,closest:selector=>selector.includes(section)?{}:null,
    nodes:[{data:text,length:text.length,parentElement:{closest:()=>null}}]});
  const commentary=root('commentary',middle),reply=root('reply',final);
  const target={dataset:{findEntry:'saved-message'},
    querySelector:selector=>selector.includes('execution-process')?execution:reply,
    querySelectorAll:selector=>selector==='[data-find-text]'?(execution.open?[commentary,reply]:[reply]):[execution,debug,tool],
    getBoundingClientRect:()=>({top:100}),classList:{add:()=>{},remove:()=>{}},focus:()=>{}};
  const unit={dataset:{timelineTurn:'turn-1',timelineMounted:'true'},scrollIntoView:()=>{},querySelectorAll:()=>[target]};
  React.useState=initial=>[initial,()=>{}];React.useRef=current=>({current});React.useCallback=fn=>fn;React.useEffect=()=>{};
  global.NodeFilter={SHOW_TEXT:4,FILTER_REJECT:2,FILTER_ACCEPT:1};
  global.document={createTreeWalker:body=>{let i=-1;return {nextNode:()=>++i<body.nodes.length,get currentNode(){return body.nodes[i];}};},
    createRange:()=>({setStart:(node,offset)=>{selected={node,offset};},setEnd:()=>{},getBoundingClientRect:()=>({top:120})})};
  global.window={setTimeout,getSelection:()=>null,addEventListener:()=>{},removeEventListener:()=>{}};
  global.requestAnimationFrame=fn=>{frames.set(++next,fn);return next;};global.cancelAnimationFrame=id=>frames.delete(id);
  const tick=()=>{const pending=[...frames.values()];frames.clear();pending.forEach(fn=>fn());};
  let hook;
  try {
    hook=useTimelineReveal({session:'task',scopeRef:{current:{querySelectorAll:()=>[unit]}},
      scrollRef:{current:{scrollTop:0,clientHeight:300,getBoundingClientRect:()=>({top:0})}},resolveUnit:()=> 'turn-1'});
    const pending=hook.reveal({session:'task',id:'saved-message',focus:false,
      match:{id:'saved-message',query:middle,matchOffset:10,length:middle.length,occurrence:0,section:'commentary'}});
    tick();tick();tick();
    assert.equal(await pending,true);
    assert.equal(execution.open,true);
    assert.equal(debug.open,false);assert.equal(tool.open,false);
    assert.equal(selected.node,commentary.nodes[0]);
    execution.open=false;
    const finalHit=hook.reveal({session:'task',id:'saved-message',focus:false,
      match:{id:'saved-message',query:final,matchOffset:50,length:final.length,occurrence:0,section:'reply'}});
    tick();tick();
    assert.equal(await finalHit,true);
    assert.equal(execution.open,false);
    assert.equal(selected.node,reply.nodes[0]);
  } finally {hook?.cancel();Object.assign(React,originals);Object.assign(global,globals);}
});

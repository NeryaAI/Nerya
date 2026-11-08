// Local regression fixtures; no model, network or workspace mutation.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),ts=require('typescript');
for(const ext of ['.ts','.tsx'])require.extensions[ext]=(m,f)=>{
  const result=ts.transpileModule(fs.readFileSync(f,'utf8'),{reportDiagnostics:true,compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}});
  assert.equal((result.diagnostics||[]).filter(d=>d.category===ts.DiagnosticCategory.Error).length,0,f);
  m._compile(result.outputText,f);
};
require.extensions['.css']=m=>{m.exports={};};
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),{NextIntlClientProvider}=require('next-intl');
const messages=require('../messages/index.ts').en;
const html=node=>renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale:'en',messages,timeZone:'UTC'},node));
const {projectMessageBlocks}=require('../lib/streamTranscript.ts');
const {normalizeTranscriptBlocks}=require('../lib/transcriptProjection.ts');
const {projectCommand}=require('../lib/conversationCommands.ts');
const {AssistantReplyBubble}=require('../components/chat/AssistantReplyBubble.tsx');
const {NativeBlocksTrack}=require('../components/chat/TurnBlocks.tsx');
const {Markdown,openWorkspaceFileLink}=require('../components/chat/Markdown.tsx');
const {ChatResultsPanel}=require('../components/chat/ChatResultsPanel.tsx');
const block=(kind,fields={})=>({block:{kind,...fields}});

test('plain assistant task results prioritize prose and retain the source task link',()=>{
 const result={id:'r',title:'Task A',text:'Public conclusion',ts:1,taskSessionId:'taskchat_one'};
 const output=html(React.createElement(ChatResultsPanel,{results:[result]}));
 assert.match(output,/Public conclusion/);assert.match(output,/href="\/chat\/taskchat_one"/);
 assert.doesNotMatch(output,/data-testid="delivery-evidence"/);assert.doesNotMatch(output,/Main conversation result/);
 const checked=html(React.createElement(ChatResultsPanel,{results:[{...result,evidence:{artifacts:{tests_run:[{command:'check',exit_code:1}]}}}]}));
 assert.match(checked,/data-testid="delivery-evidence"/);
});

test('resumed live reply is not hidden behind the saved prior-turn blocks',()=>{
 const message={id:'reply',role:'assistant',ts:1,loading:true,turn:{blocks:[block('tool_use',{call_id:'read',action:'read_file'})]},live_events:[{kind:'message.delta',seq:1,stream_id:'new',mode:'append',text:'New progress'}]};
 assert.equal(projectMessageBlocks(message).at(-1).block.text,'New progress');
});
test('commit retains a missing live result and respects an intentionally empty result',()=>{
 const message={id:'reply',role:'assistant',ts:1,turn:{blocks:[block('tool_use',{call_id:'read'})]},live_events:[{kind:'tool.complete',seq:1,call_id:'read',ok:true,result:{count:0}}]};
 assert.equal(projectMessageBlocks(message)[1].block.result.count,0);
 message.turn.blocks.push(block('tool_result',{call_id:'read',ok:false,result:null}));
 assert.equal(projectMessageBlocks(message)[1].block.result,null);
});
test('equal stream names in different sessions and epochs stay separate after normalization',()=>{
 const message={id:'a',role:'assistant',ts:1,loading:true,live_events:['one','two'].map((epoch,i)=>({kind:'message.delta',seq:1,event_id:epoch,epoch,session_id:'s',turn_id:'t',stream_id:'reply',mode:'append',text:String(i)}))};
 assert.deepEqual(projectMessageBlocks(message).map(e=>e.block.text),['0','1']);
});
test('native final snapshot replaces a changed draft and malformed envelopes are ignored',()=>{
 const rows=normalizeTranscriptBlocks([null,block('text_delta',{stream_id:'s',text:'abandoned'}),block('text',{stream_id:'s',text:'Final'}),block('text_delta',{stream_id:'s',text:'late'})]);
 assert.equal(rows.length,1);assert.equal(rows[0].block.text,'Final');
});
test('assistant chat streams public text without exposing tool output or thinking',()=>{
 const message={id:'a',role:'assistant',ts:1,loading:true,live_events:[{kind:'message.delta',seq:1,mode:'append',text:'Visible stream'},{kind:'turn.step',seq:2,step:{kind:'thinking',detail:{text:'PRIVATE'}}},{kind:'tool.complete',seq:3,call_id:'a',result:{secret:'TOOL_BODY'}}]};
 const output=html(React.createElement(AssistantReplyBubble,{message}));
 assert.match(output,/Visible stream/);assert.doesNotMatch(output,/PRIVATE|TOOL_BODY/);
});
test('old command response cannot regress a newer terminal receipt',()=>{
 const thread={id:'s',messages:[],updated_ts:0};
 const command={command_id:'c',session_id:'s',turn_id:'t',kind:'send',input:'hello',attachments:[],context:{},created_at:1,updated_at:3,revision:3,state:'succeeded',result:{reply_text:'Done'}};
 const done=projectCommand(thread,command);
 assert.equal(projectCommand(done,{...command,state:'running',revision:2,result:null}),done);
 assert.equal(done.messages.at(-1).turn.execution_status,'succeeded');
});
test('a result before its use still hides the duplicate friendly card; attempts stay separate',()=>{
 const envelopes=[block('tool_result',{call_id:'r',attempt:1,action:'read_file',ok:true,result:{text:'content'}}),block('tool_use',{call_id:'r',attempt:1,action:'read_file',payload:{path:'one.txt'}}),block('tool_use',{call_id:'r',attempt:2,action:'read_file',payload:{path:'two.txt'}})];
 const output=html(React.createElement(NativeBlocksTrack,{envelopes,live:true,presentation:'expanded'}));
 assert.match(output,/two.txt/);assert.match(output,/one.txt/);
});
test('native failed results use the shared public summary and keep raw diagnostics collapsed',()=>{
 const output=html(React.createElement(NativeBlocksTrack,{envelopes:[block('tool_result',{call_id:'failed',action:'data_api',ok:false,error:'unexpected field: keyword'})],live:false,presentation:'expanded'}));
 assert.match(output,/native-tool-failure-summary/);
 assert.match(output,/unsupported parameter/);
 assert.doesNotMatch(output,/unexpected field: keyword/);
});
test('incomplete fenced code keeps earlier Markdown rendered, with one safe pre element',()=>{
 const fence=String.fromCharCode(96).repeat(3);
 const output=html(React.createElement(Markdown,{streaming:true},'# Heading\n\n'+fence+'python\nprint("<script>")'));
 assert.match(output,/<h1/);assert.match(output,/<pre/);assert.doesNotMatch(output,/<script>/);
});

test('Markdown workspace artifact links use the file locator without changing web or app links',()=>{
 const root='evolution/proposals/prp_1468_fixture/after/strategies/dex/backtests/20261102_152723';
 const paths=[root+'/report.md',root+'/run.json','./strategies/dex/candidate.py','artifacts/池价 report.csv'];
 const directories=[['evolution/proposals/prp_1468_fixture/after/strategies/dex','/strategies?strategy_id=dex&amp;proposal_id=prp_1468_fixture'],
  ['strategies/dex','/strategies?strategy_id=dex']];
 const content=[...paths.map((p,i)=>'[file'+i+'](<'+p+'>)'),
  ...directories.flatMap(([p])=>['[candidate]('+p+')','[candidate]('+p+'/)'])].join('\n\n');
 for(const streaming of [false,true]){
  const output=html(React.createElement(Markdown,{streaming},content));
  for(const p of paths){
   const clean=p.replace(/^\.\//,'');
   assert.ok(output.includes('href="/api/proxy/workspace/file/raw/'+clean.split('/').map(encodeURIComponent).join('/')+'"'),p);
  }
  for(const [,href] of directories) assert.equal(output.split('href="'+href+'"').length-1,2);
  assert.doesNotMatch(output,/href="evolution\/|href="\.\/strategies/);
 }
 const unchanged=['https://example.com/report.md','//example.com/report.md','/strategies?strategy_id=dex','./settings','../chat/session','docs/guide.html','#section',
  'artifacts/../report.md','artifacts/%2e%2e/report.md','artifacts/%252e%252e/report.md',
  'artifacts/unknown/','evolution/proposals/prp_fixture/after/strategies/dex/subdir','strategies/dex/subdir'];
 for(const href of unchanged){
  const output=html(React.createElement(Markdown,null,'[link]('+href+')'));
  assert.ok(output.includes('href="'+href.replaceAll('&','&amp;')+'"'),href);
  assert.doesNotMatch(output,/\/api\/proxy\/workspace\/file/);
 }
});

test('conversation and result Markdown file clicks use the workspace preview while native navigation stays intact',t=>{
 const previousElement=global.Element;
 class LinkTarget {
  constructor(href,download=false){this.href=href;this.download=download;}
  closest(selector){assert.equal(selector,'.nerya-markdown a[href]');return this;}
  getAttribute(){return this.href;}
  hasAttribute(name){return name==='download'&&this.download;}
 }
 global.Element=LinkTarget;
 t.after(()=>{if(previousElement===undefined)delete global.Element;else global.Element=previousElement;});
 const path='artifacts/conversations/229f_fixture/bnb_7day_report.md';
 const raw='/api/proxy/workspace/file/raw/';
 const opened=[];
 const click=(href,options={},opener=p=>opened.push(p))=>{
  const event={target:new LinkTarget(href,options.download),button:0,defaultPrevented:false,
   preventDefault(){this.defaultPrevented=true;},...options};
  openWorkspaceFileLink(event,opener);return event;
 };
 for(const href of [path,raw+path,raw+'artifacts/%E4%B8%AD%E6%96%87%20report.md']){
  const before=opened.length,event=click(href);
  assert.equal(event.defaultPrevented,true);
  assert.equal(opened.length,before+1);
  assert.equal(opened.at(-1),href.includes('%')?'artifacts/中文 report.md':path);
  openWorkspaceFileLink(event,p=>opened.push(p));
  assert.equal(opened.length,before+1,'bubbling from result to workspace must not open twice');
 }
 const unchanged=['https://example.com/'+path,'https://example.com'+raw+path,'//example.com'+raw+path,
  '/strategies?strategy_id=dex','#section','blob:download',raw+'artifacts/../report.md',raw+'artifacts/%252e%252e/report.md',raw+'bad%ZZ.md',raw+path+'?download=1'];
 for(const href of unchanged)assert.equal(click(href).defaultPrevented,false,href);
 for(const options of [{download:true},{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{defaultPrevented:true}]){
  const before=opened.length;click(raw+path,options);assert.equal(opened.length,before);
 }
 const event={target:new LinkTarget(raw+path),button:0,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};
 openWorkspaceFileLink(event);assert.equal(event.defaultPrevented,false,'no opener retains the raw fallback');
 const output=html(React.createElement(ChatResultsPanel,{results:[{id:'bnb',title:'BNB',text:'[查看更新后的中文报告]('+path+')',ts:1}]}));
 assert.ok(output.includes('href="'+raw+path+'"'));
 for(const [file,marker] of [['ChatView.tsx','chat-workbench'],['ChatResultsPanel.tsx','canvas-result-body']]){
  const source=fs.readFileSync(require('node:path').join(__dirname,'../components/chat',file),'utf8');
  assert.match(source,new RegExp('data-testid="'+marker+'"[^>]*onClick=\\{event => openWorkspaceFileLink\\(event,'));
 }
});

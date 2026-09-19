const fs=require('node:fs'),ts=require('typescript'),assert=require('node:assert/strict');
require.extensions['.ts']=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,f);
const {compactWorkflow}=require('../lib/workflowProjection.ts');
const {sourceDimension,sourceErrors,updateSourceDimension}=require('../lib/workflowSources.ts');
const t=(zh,en)=>en,checks=[];
function check(name,fn){fn();checks.push(name);}
function node(id,kind,config,path){return {id,kind,config,title:id,resource:id,subtitle:'',position:{x:0,y:0},editable:true,binding:{path,file:null}};}
const a=node('source:a','source',{id:'a',provider:'runtime.market',market:'BTC',timeframe:'15m',parameters:{score:0,ready:false}},['data_sources',0]);
const b=node('source:b','source',{id:'b',provider:'runtime.market',markets:['BTC','ETH'],timeframes:['1h','4h']},['data_sources',1]);
const root=node('strategy:s','strategy',{},[]), market=node('source:markets/BTC','source','BTC',['markets',0]), script=node('script:main','script',{},null), role=node('agent:left','agent',{},null);
const graph={id:'s:strategy',nodes:[root,market,a,b,script,role],edges:[{id:'a-read',source:a.id,target:script.id,origin:'static',relation:'data',label:'read'},{id:'b-input',source:b.id,target:role.id,origin:'manifest',relation:'context',label:'input'},{id:'parallel',source:script.id,target:role.id,origin:'manifest',relation:'parallel_dispatch',label:'parallel'},{id:'raw',source:market.id,target:script.id,origin:'static',relation:'data',label:'raw'}]};
check('one source node per shared connection',()=>{const out=compactWorkflow(graph,t);assert.equal(out.graph.nodes.filter(n=>n.kind==='source').length,1);assert.equal(out.groups[0].members.length,2);assert.equal(out.aliases.get(b.id),a.id);});
check('all original IDs and custom fields preserved',()=>{const before=JSON.stringify(graph);const out=compactWorkflow(graph,t);assert.equal(JSON.stringify(graph),before);assert.equal(out.groups[0].members[0].config.parameters.score,0);assert.equal(out.groups[0].members[0].config.parameters.ready,false);});
check('edges retain actual evidence origin and identity',()=>{const out=compactWorkflow(graph,t);assert.deepEqual(out.graph.edges.find(e=>e.id==='b-input'),{...graph.edges[1],source:a.id});assert.ok(out.graph.edges.find(e=>e.id==='parallel'));assert.ok(out.graph.edges.find(e=>e.id==='raw'));});
check('separate account connections stay separate',()=>{const x=structuredClone(graph);x.nodes.find(n=>n.id===b.id).config.account='another';assert.equal(compactWorkflow(x,t).groups.length,2);});
check('mixed legacy news reader is not hidden by market presets',()=>{const news=node('source:news/rss','source','rss',['news_sources',0]);const out=compactWorkflow({...graph,nodes:[...graph.nodes,news],edges:[...graph.edges,{id:'news',source:news.id,target:script.id,origin:'static',relation:'data'}]},t);assert.equal(out.groups.length,2);assert.ok(out.graph.edges.find(e=>e.id==='news'));});
check('strategy and scopes stay reachable outside canvas',()=>{const out=compactWorkflow(graph,t);assert.ok(out.supporting[0].members.find(n=>n.id===root.id));assert.ok(out.supporting[0].members.find(n=>n.id===market.id));});
check('full evolution is unchanged',()=>{const x={...graph,id:'s:evolution'};assert.equal(compactWorkflow(x,t).graph,x);});
check('explicit user layout remains unchanged',()=>{const out=compactWorkflow(graph,t,{nodes:{[a.id]:{position:{x:321,y:444}}},edges:[],version:1});assert.deepEqual(out.graph.nodes[0].position,{x:321,y:444});});
check('legacy one period remains scalar',()=>{assert.deepEqual(updateSourceDimension({timeframe:'15m'},'timeframes','timeframe',['1h']),{timeframe:'1h'});});
check('plural remains stable after reducing to one',()=>{assert.deepEqual(updateSourceDimension({timeframes:['15m','1h']},'timeframes','timeframe',['1h']),{timeframes:['1h']});});
check('new multiple periods remove conflicting scalar',()=>{assert.deepEqual(updateSourceDimension({timeframe:'15m'},'timeframes','timeframe',['15m','1h']),{timeframes:['15m','1h']});});
check('inherit markets without mutating configuration',()=>{assert.deepEqual(sourceDimension({},'markets','market',['BTC','ETH']),['BTC','ETH']);});
check('empty and duplicate arrays are errors',()=>{assert.ok(sourceErrors({markets:[]},t).length);assert.ok(sourceErrors({timeframes:['1h','1h']},t).length);});
check('zero row limit and contradictory scalar are errors',()=>{assert.ok(sourceErrors({limit:0},t).length);assert.ok(sourceErrors({timeframe:'15m',timeframes:['1h']},t).length);});
console.log(JSON.stringify({passed:checks.length,checks}));

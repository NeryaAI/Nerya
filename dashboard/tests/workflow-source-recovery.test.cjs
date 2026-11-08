const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');

function fixture(propose, confirmResult=true) {
  const slots=[], effects=[], drafts=new Map(), calls=[], saved=[];
  let cursor=0, tree;
  const hooks={useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;
    return [slots[i], value=>{slots[i]=typeof value==='function'?value(slots[i]):value;}];},
    useEffect(fn,deps){const i=cursor++,old=slots[i];if(!old||deps.some((x,j)=>x!==old[j])){slots[i]=deps;effects.push(fn);}}};
  const element=(type,props)=>({type,props});
  const file=path.resolve(__dirname,'../components/workflows/WorkflowSourceRecovery.tsx');
  const m=new Module(file,module);m.filename=file;m.paths=module.paths;
  m.require=name=>{
    if(name==='react')return hooks;
    if(name==='react/jsx-runtime')return {jsx:element,jsxs:element};
    if(name==='next-intl')return {useTranslations:()=>key=>key};
    if(name.endsWith('workflowApi'))return {workflowApi:{propose:async payload=>{calls.push(payload);return propose(payload);}}};
    if(name.endsWith('editDrafts'))return {readEditDraft:key=>drafts.get(key),writeEditDraft:(key,value)=>value===null?drafts.delete(key):drafts.set(key,value)};
    if(name.endsWith('dialogs'))return {confirm:async()=>confirmResult};
    throw Error(name);
  };
  m._compile(ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,file);
  const data={strategy_id:'fixture',revision:'rev1',can_edit:true,source:{proposal_id:'prp_original'},recovery:{file:'strategy.yml',content:'id: [broken',diagnostic:'line 1 parser error'}};
  const props={data,onDirtyChange:()=>{},onSaved:value=>saved.push(value)};
  function render(){cursor=0;tree=m.exports.WorkflowSourceRecovery(props);effects.splice(0).forEach(fn=>fn());return tree;}
  function find(type,label,node=tree){if(Array.isArray(node))return node.map(x=>find(type,label,x)).find(Boolean);if(!node||typeof node!=='object')return;
    if(node.type===type&&(label===undefined||node.props.children===label))return node;return find(type,label,node.props.children??null);}
  return {data,render,find,calls,saved,drafts};
}

test('failed save keeps exact draft and source; success submits same revision as a proposal',async()=>{
  let fail=true;const f=fixture(async()=>{if(fail)throw Error('revision_conflict');return {workflow:{strategy_id:'fixture',revision:'rev2'}};});
  f.render();assert.equal(f.find('textarea').props.value,'id: [broken');assert.equal(f.find('button','save').props.disabled,true);
  f.find('textarea').props.onChange({target:{value:'id: fixture'}});f.render();
  await f.find('button','save').props.onClick();f.render();await new Promise(setImmediate);f.render();
  assert.equal(f.find('textarea').props.value,'id: fixture');assert.match(JSON.stringify(f.render()),/revision_conflict/);assert.equal(f.saved.length,0);assert.equal(f.drafts.size,1);
  fail=false;await f.find('button','save').props.onClick();await new Promise(setImmediate);f.render();
  assert.deepEqual(f.calls[1],{strategy_id:'fixture',proposal_id:'prp_original',base_revision:'rev1',manifest_source:'id: fixture'});
  assert.equal(f.saved.length,1);assert.equal(f.drafts.size,0);assert.equal(f.data.recovery.content,'id: [broken');
});

test('cancelled discard retains edits, confirmed discard restores source, readonly cannot save',async()=>{
  for(const accept of [false,true]){
    const f=fixture(()=>assert.fail('must not save'),accept);f.render();f.find('textarea').props.onChange({target:{value:'draft'}});f.render();
    await f.find('button','discard').props.onClick();f.render();assert.equal(f.find('textarea').props.value,accept?'id: [broken':'draft');
    f.data.can_edit=false;f.render();assert.equal(f.find('textarea').props.readOnly,true);assert.equal(f.find('button','save').props.disabled,true);
  }
});

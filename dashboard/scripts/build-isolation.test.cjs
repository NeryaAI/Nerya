const {test}=require('node:test');
const assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const path=require('node:path');
function config(env){
 const result=spawnSync(process.execPath,['--input-type=module','-e','import c from "./next.config.mjs"; console.log(JSON.stringify({distDir:c.distDir||".next",tsconfig:c.typescript?.tsconfigPath||"tsconfig.json"}));'],{cwd:path.join(__dirname,'..'),env:{...process.env,NERYA_UI_DIST_DIR:'',NERYA_UI_TSCONFIG:'',NERYA_E2E:'',...env},encoding:'utf8'});
 assert.equal(result.status,0,result.stderr);return JSON.parse(result.stdout);
}
test('ordinary CLI build retains its standard directory',()=>assert.equal(config({}).distDir,'.next'));
test('local service workers share isolated directory without test mode',()=>assert.equal(config({NERYA_UI_DIST_DIR:'.next-local-18380'}).distDir,'.next-local-18380'));
test('existing isolated test configuration still works',()=>assert.deepEqual(config({NERYA_E2E:'1',NERYA_UI_DIST_DIR:'test-results/isolated-build',NERYA_UI_TSCONFIG:'tsconfig.test.json'}),{distDir:'test-results/isolated-build',tsconfig:'tsconfig.test.json'}));

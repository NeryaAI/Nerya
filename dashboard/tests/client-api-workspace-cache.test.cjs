const { test, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, filename);
const identity = require('../lib/workspaceIdentity.ts');
const { callApi, invalidateReadCache } = require('../lib/clientApi.ts');
const originalFetch = global.fetch;

beforeEach(() => {
  global.window = { localStorage: { getItem: () => null } };
  identity.setWorkspaceIdentity('A'); invalidateReadCache();
});
afterEach(() => { global.fetch = originalFetch; delete global.window; identity.setWorkspaceIdentity(null); invalidateReadCache(); });

test('cached financial reads cannot cross workspaces with the same local credentials', async () => {
  let calls = 0;
  global.fetch = async () => { calls++; return Response.json({ workspace: identity.getWorkspaceIdentity(), calls }); };
  assert.equal((await callApi('/financial/grants')).workspace, 'A');
  assert.equal((await callApi('/financial/grants')).calls, 1);
  identity.setWorkspaceIdentity('B');
  assert.equal((await callApi('/financial/grants')).workspace, 'B');
  identity.setWorkspaceIdentity('A');
  assert.equal((await callApi('/financial/grants')).calls, 3);
});

test('a slow old-workspace request cannot supply the new workspace cached read', async () => {
  let finishOld;
  global.fetch = () => new Promise(resolve => { finishOld = resolve; });
  const old = callApi('/financial/resources/same-account');
  identity.setWorkspaceIdentity('B');
  global.fetch = async () => Response.json({ workspace: 'B' });
  assert.equal((await callApi('/financial/resources/same-account')).workspace, 'B');
  finishOld(Response.json({ workspace: 'A' })); await old;
  assert.equal((await callApi('/financial/resources/same-account')).workspace, 'B');
});

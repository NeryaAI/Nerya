const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, filename);
const { moveModelRoute, explicitRoute } = require('../components/settings/modelSettingsState.ts');

test('moving a backup to first preserves credentials, policy and the original state', () => {
  const routes = ['A', 'B', 'C'].map((model, index) => ({ provider: model, model, base_url: `https://${model}.invalid`, provider_key_ref: `vault://${model}`, reasoning_effort: ['low', 'high', 'none'][index], context_window: 32000 * (index + 1) }));
  const moved = moveModelRoute(routes, 2, 0);
  assert.deepEqual(moved.map(row => row.model), ['C', 'A', 'B']);
  assert.deepEqual(routes.map(row => row.model), ['A', 'B', 'C']);
  assert.equal(moved[0], routes[2]);
  assert.deepEqual(moved.map(explicitRoute), [routes[2], routes[0], routes[1]]);
  assert.deepEqual(moveModelRoute(moved, 0, 2), routes);
  for (const [from, to] of [[0, 0], [-1, 0], [0, 3], [0, 1.5]]) assert.equal(moveModelRoute(routes, from, to), routes);
});

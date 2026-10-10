const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText, filename);
const { editableRoute, explicitRoute, connectionPatch, tierPolicy } = require('../components/settings/modelSettingsState.ts');

test('context editing does not freeze inherited URL, credentials, kind or sibling context', () => {
  const effective = { provider: 'fixture', model: 'primary', base_url: 'https://fixture.invalid', provider_key_ref: 'vault://profile', kind: 'anthropic_messages', context_window: 128000, reasoning_effort: 'high' };
  const loaded = editableRoute({ ...effective, declared: { provider: 'fixture', model: 'primary' }, effective });
  assert.equal(loaded.base_url, '');
  assert.equal(loaded.provider_key_ref, '');
  assert.equal(loaded.kind, '');
  const saved = explicitRoute({ ...loaded, context_window: 64000 });
  assert.equal(saved.context_window, 64000);
  assert.equal(saved.base_url, '');
  assert.equal(saved.provider_key_ref, '');
  assert.equal(saved.reasoning_effort, undefined);
  assert.equal(explicitRoute(loaded).context_window, undefined);
  assert.deepEqual(tierPolicy({ declared: { context_window: 128000 } }), { context_window: 128000 });
});

test('explicit route URL and key survive editing and can be cleared to inherit', () => {
  const declared = { provider: 'fixture', model: 'A', base_url: 'https://explicit.invalid', provider_key_ref: 'vault://route', provider_key_env: 'FIXTURE_KEY' };
  const loaded = editableRoute({ ...declared, declared, effective: declared });
  const saved = explicitRoute({ ...loaded, context_window: 64000 });
  assert.equal(saved.provider_key_ref, 'vault://route');
  assert.equal(saved.provider_key_env, 'FIXTURE_KEY');
  assert.equal(saved.base_url, 'https://explicit.invalid');
  assert.equal(explicitRoute({ ...loaded, base_url: '', provider_key_ref: '', provider_key_refs: [] }).base_url, '');
});

test('connection dirty payload contains only edited fields; no synthetic provider defaults', () => {
  const profile = { provider: 'fixture', base_url: 'https://fixture.invalid', provider_key_ref: 'vault://saved', kind: 'anthropic_messages' };
  assert.deepEqual(connectionPatch(profile, 'fixture', profile.base_url, ''), { provider: 'fixture' });
  assert.deepEqual(connectionPatch(profile, 'fixture', profile.base_url, 'new-key'), { provider: 'fixture', provider_key: 'new-key' });
  assert.deepEqual(connectionPatch(profile, 'fixture', '', ''), { provider: 'fixture', base_url: '' });
  assert.deepEqual(connectionPatch(profile, 'fixture', profile.base_url, 'vault://saved'), { provider: 'fixture' });
});

function probeHarness(response) {
  const states = []; let cursor = 0; const calls = [];
  const jsx = (type, props) => ({ type, props });
  const react = { useState(initial) { const i = cursor++; if (!(i in states)) states[i] = initial; return [states[i], value => { states[i] = value; }]; }, useEffect() {} };
  const mod = { exports: {} };
  const source = ts.transpileModule(fs.readFileSync(require.resolve('../components/SetupReadinessCard.tsx'), 'utf8'), { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
  vm.runInNewContext(source, {
    exports: mod.exports, module: mod,
    require(name) {
      if (name === 'react') return react;
      if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx };
      if (name === 'next-intl') return { useLocale: () => 'en', useTranslations: namespace => key => require('../messages/index.ts').en[namespace]?.[key] || key };
      if (name.endsWith('/i18n')) return require('../lib/i18n.ts');
      if (name.endsWith('/clientApi')) return { clientApi: { llmConfig: async () => ({ revision: 'r1' }) } };
      if (name.endsWith('/auth')) return { authHeaders: v => v, handleAuthFailure() {} };
      if (name === './Page') return { Pill: 'pill', Card: 'card' };
      return {};
    },
    AbortController, setTimeout, clearTimeout,
    fetch: async (path, init) => { calls.push({ path, body: JSON.parse(init.body) }); return { ok: response.ok, status: response.ok ? 200 : 503, json: async () => response }; },
  });
  return { calls, render(props) { cursor = 0; return mod.exports.SavedModelTest(props); } };
}
function nodes(tree) {
  if (tree == null || typeof tree !== 'object') return [tree];
  return [tree, ...[tree.props?.children].flat(Infinity).flatMap(nodes)];
}
test('probe runs only on click, carries saved revision, and expires on new revision', async () => {
  const h = probeHarness({ ok: true, revision: 'r1', provider: 'fixture', model: 'A' });
  let view = h.render({ revision: 'r1' });
  assert.equal(h.calls.length, 0);
  assert.ok(nodes(view).includes('Saved · Untested'));
  await nodes(view).find(n => n?.type === 'button').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(h.calls, [{ path: '/api/proxy/llm/messages/probe', body: { revision: 'r1', caller: 'settings:connection-test' } }]);
  assert.ok(nodes(h.render({ revision: 'r1' })).includes('Test passed'));
  assert.ok(nodes(h.render({ revision: 'r2' })).includes('Saved · Untested'));
  view = h.render({ revision: 'r2', dirty: true });
  const button = nodes(view).find(n => n?.type === 'button');
  assert.equal(button.props.disabled, true);
  await button.props.onClick();
  assert.equal(h.calls.length, 1);
});
test('failed probe keeps the saved revision and allows retry', async () => {
  const h = probeHarness({ ok: false, revision: 'r1', error: 'llm_messages_probe_failed' });
  await nodes(h.render({ revision: 'r1' })).find(n => n?.type === 'button').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  const view = h.render({ revision: 'r1' });
  assert.ok(nodes(view).includes('Test failed'));
  assert.ok(nodes(view).some(n => typeof n === 'string' && n.includes('settings remain saved')));
  assert.equal(nodes(view).find(n => n?.type === 'button').props.disabled, false);
});

test('failed probe displays the safe provider cause and request trace', async () => {
  const h = probeHarness({ ok: false, revision: 'r1', error: 'llm_messages_probe_failed', failure_kind: 'rate_limit', status_code: 429, request_id: 'req-fixture' });
  await nodes(h.render({ revision: 'r1' })).find(n => n?.type === 'button').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  const details = nodes(h.render({ revision: 'r1' })).filter(n => typeof n === 'string').join(' ');
  assert.match(details, /rate limited/);
  assert.match(details, /HTTP 429/);
  assert.match(details, /req-fixture/);
});

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, filename);
const { providerDraftOf, providerPatch } = require('../components/settings/providerManagementState.ts');

test('provider editor starts with declarations and a blank key, preserving inheritance', () => {
  const profile = { provider: 'fixture', base_url: 'https://default.invalid/v1', kind: 'chat_completions', provider_key_ref: 'vault://existing', declared: { provider_key_ref: 'vault://existing' } };
  const draft = providerDraftOf(profile);
  assert.deepEqual(draft, { baseUrl: '', kind: '', key: '' });
  assert.deepEqual(providerPatch(profile, draft), { provider: 'fixture' });
  assert.deepEqual(providerPatch(profile, { ...draft, key: 'fixture-new-key' }), { provider: 'fixture', provider_key: 'fixture-new-key' });
});

test('editing one provider sends only its changed connection fields', () => {
  const profile = { provider: 'fixture', base_url: 'https://old.invalid/v1', kind: 'chat_completions', provider_key_ref: 'vault://old' };
  assert.deepEqual(providerPatch(profile, { baseUrl: ' https://new.invalid/v1 ', kind: 'anthropic_messages', key: '  ' }), { provider: 'fixture', base_url: 'https://new.invalid/v1', kind: 'anthropic_messages' });
  assert.deepEqual(providerPatch(profile, { baseUrl: '', kind: '', key: 'vault://replacement' }), { provider: 'fixture', base_url: '', kind: '', provider_key_ref: 'vault://replacement' });
  assert.equal(profile.provider_key_ref, 'vault://old');
});

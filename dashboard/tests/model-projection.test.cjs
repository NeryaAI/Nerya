const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, file) => module._compile(ts.transpileModule(fs.readFileSync(file, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, file);
const { admittedModel, composerSettings } = require('../lib/modelProjection.ts');
test('composer uses assistant defaults unless this conversation has explicit settings', () => {
  const defaults = { model_provider: 'global', model_id: 'previous', model_tier: 'medium', permission_mode: 'auto' };
  const assistant = { model_provider: 'assistant-provider', model_id: 'assistant-model', model_tier: 'light' };
  assert.deepEqual(composerSettings(defaults, assistant), { ...defaults, ...assistant });
  assert.equal(composerSettings(defaults, assistant, defaults), defaults);
  assert.equal(composerSettings(defaults, null), defaults);
  assert.equal(composerSettings(defaults, { model_provider: '', model_id: '', model_tier: '' }), defaults);
});
test('model labels exist in both chat namespaces', () => {
  for (const locale of ['en', 'zh']) {
    const { chat } = require(`../messages/${locale}/chat.json`);
    assert.ok(chat.admittedModel);
    assert.ok(chat.nextSendModel);
  }
});
const context = { accepted_model: { provider: 'agnes', model: 'flash' }, requested_model: { model: 'draft' } };
const command = { session_id: 's', command_id: 'c', turn_id: 't', kind: 'send', state: 'running', created_at: 20, position: 1, context };
const message = { role: 'assistant', command_id: 'c', ts: 20000, turn: { turn_id: 't', context_snapshot: context } };
const thread = messages => ({ id: 's', messages });
for (const state of ['running', 'succeeded', 'failed', 'awaiting_input', 'awaiting_approval', 'interrupted']) {
  test(`durable ${state} model wins over requested preferences`, () => {
    assert.equal(admittedModel(thread([message]), [{ ...command, state }]), 'agnes · flash');
  });
}
test('history survives without a command page; matching older APIs retain receipt', () => {
  assert.equal(admittedModel(thread([message]), []), 'agnes · flash');
  assert.equal(admittedModel(thread([message]), [{ ...command, context: {} }]), 'agnes · flash');
});
test('new command without admission never borrows the previous model', () => {
  assert.equal(admittedModel(thread([message]), [{ ...command, command_id: 'new', turn_id: 'new', created_at: 30, context: {} }]), null);
});
test('resumed command keeps turn identity but never borrows its previous admission', () => {
  const prior = { ...message, command_id: undefined };
  assert.equal(admittedModel(thread([prior]), [{ ...command, command_id: 'resume', created_at: 30, context: {} }]), null);
});
test('queued, guide and other-session commands cannot replace the actual model', () => {
  for (const extra of [{ state: 'queued' }, { kind: 'guide' }, { session_id: 'other' }]) {
    assert.equal(admittedModel(thread([message]), [{ ...command, created_at: 30, context: {}, ...extra }]), 'agnes · flash');
  }
});
test('old command pages cannot overwrite newer history', () => {
  assert.equal(admittedModel(thread([{ ...message, command_id: 'new', ts: 30000 }]), [{ ...command, context: {} }]), 'agnes · flash');
});
test('requested-only, malformed and legacy records remain unknown', () => {
  for (const value of [null, [], {}, { model: '' }, 'model']) {
    assert.equal(admittedModel(thread([]), [{ ...command, context: { accepted_model: value, requested_model: context.accepted_model } }]), null);
  }
  assert.equal(admittedModel(null, []), null);
});

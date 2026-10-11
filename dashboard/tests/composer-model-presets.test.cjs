const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');

require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, filename);

const { DEFAULT_CHAT_RUN_SETTINGS, applyModelTierPreset, applyReasoningPreset } = require('../lib/chat.ts');

test('model preset sends tier routing without accidentally pinning a concrete model', () => {
  const configured = {
    ...DEFAULT_CHAT_RUN_SETTINGS,
    model_provider: 'openai',
    model_id: 'chosen-model',
    model_tier: 'high',
    model_context_window: 128000,
    reasoning_effort: 'high',
  };
  const selected = applyModelTierPreset(configured, 'light');
  assert.equal(selected.model_tier, 'light');
  assert.equal(selected.model_provider, '');
  assert.equal(selected.model_id, '');
  assert.equal(selected.model_context_window, 0);
  assert.equal(selected.reasoning_effort, 'high');
  const automatic = applyModelTierPreset(selected, '');
  assert.equal(automatic.model_tier, '');
  assert.equal(automatic.reasoning_effort, 'high');
});

test('quick thinking is independent, and active-model overrides stay coherent', () => {
  const withModel = {
    ...DEFAULT_CHAT_RUN_SETTINGS,
    model_provider: 'anthropic',
    model_id: 'selected',
    model_overrides: { 'tier:medium:anthropic:selected': { reasoning_effort: 'xhigh', model_context_window: 262144 } },
  };
  const changed = applyReasoningPreset(withModel, 'low', 'tier:medium:anthropic:selected');
  assert.equal(changed.reasoning_effort, 'low');
  assert.deepEqual(changed.model_overrides['tier:medium:anthropic:selected'], {
    reasoning_effort: 'low', model_context_window: 262144,
  });
  assert.equal(withModel.model_overrides['tier:medium:anthropic:selected'].reasoning_effort, 'xhigh');
  const tierOnly = applyReasoningPreset(withModel, 'medium', '__tier');
  assert.deepEqual(tierOnly.model_overrides, withModel.model_overrides);
});

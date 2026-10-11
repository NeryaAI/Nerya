const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, file) => module._compile(ts.transpileModule(fs.readFileSync(file, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, file);
const { composerDelivery } = require('../lib/composerDelivery.ts');
const command = (state, created_at = 1, kind = 'send') => ({ state, created_at, kind });
const input = (commands = [], paused = false, pause_reason = '') => ({
  commands, queue: { paused, pause_reason }, connection: 'online',
});

test('completed empty queues send; the paused bit alone never means queue', () => {
  assert.deepEqual(composerDelivery(input([command('succeeded')])), {
    mode: 'send', reason: 'idle', canSubmit: true, runOnly: false,
  });
  for (const reason of ['operator', 'approval_continued', 'plan_accepted', 'awaiting_input', 'restarted', 'unknown', 'failed', 'interrupted', '']) {
    const decision = composerDelivery(input([command('succeeded')], 1, reason));
    assert.equal(decision.mode, 'send', reason);
    assert.equal(decision.reason, 'paused_empty', reason);
    assert.equal(decision.canSubmit, true, reason);
    assert.equal(decision.runOnly, true, reason);
  }
});

test('only actual running/stopping commands or retained queue items select queue', () => {
  for (const state of ['running', 'stopping']) {
    assert.equal(composerDelivery(input([command(state)])).reason, 'active');
    assert.equal(composerDelivery(input([command(state)])).mode, 'queue');
  }
  for (const paused of [false, true]) {
    const result = composerDelivery(input([command('succeeded'), command('queued', 2)], paused, 'operator'));
    assert.equal(result.mode, 'queue');
    assert.equal(result.reason, paused ? 'held_queue' : 'queued');
    assert.equal(result.runOnly, false);
  }
  assert.equal(composerDelivery(input([command('delivered', 2, 'guide'), command('succeeded')])).mode, 'send');
});

test('decision gates and uncertain delivery block without inventing queue work', () => {
  for (const state of ['awaiting_input', 'awaiting_approval', 'unconfirmed']) {
    const result = composerDelivery(input([command(state)], true, 'operator'));
    assert.equal(result.canSubmit, false);
    assert.equal(result.mode, 'send');
    assert.equal(result.reason, state === 'unconfirmed' ? 'execution_unconfirmed' : state);
    assert.equal(result.runOnly, false);
  }
  for (const [extra, reason] of [[{ awaitingInput: true }, 'awaiting_input'], [{ awaitingApproval: true }, 'awaiting_approval'],
    [{ pendingCount: 1 }, 'delivery_unconfirmed'], [{ connection: 'connecting' }, 'connection_unavailable'],
    [{ connection: 'offline' }, 'connection_unavailable']]) {
    const result = composerDelivery({ ...input([command('succeeded')]), ...extra });
    assert.equal(result.canSubmit, false);
    assert.equal(result.reason, reason);
    assert.equal(result.runOnly, false);
  }
});

test('latest execution closes historical waits; queued and guide rows do not hide them', () => {
  const history = [command('succeeded', 5), command('awaiting_input', 1), command('injected', 6, 'guide')];
  assert.equal(composerDelivery(input(history)).canSubmit, true);
  const result = composerDelivery(input([command('awaiting_approval'), command('queued', 3), command('injected', 4, 'guide')]));
  assert.equal(result.mode, 'queue');
  assert.equal(result.canSubmit, false);
  assert.equal(result.reason, 'awaiting_approval');
});

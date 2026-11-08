const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, file) => module._compile(ts.transpileModule(fs.readFileSync(file, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText, file);
const { composerDelivery } = require('../lib/composerDelivery.ts');
const command = (state, created_at = 1, kind = 'send') => ({ state, created_at, kind });
const input = (commands = [], paused = false, pause_reason = '') => ({
  commands, queue: { paused, pause_reason }, connection: 'online',
});

// Exercise ChatView's actual decision and prop expressions without mounting the
// workbench or touching a browser, API, or live task.
const source = ts.createSourceFile('ChatView.tsx', fs.readFileSync(require.resolve('../components/chat/ChatView.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let currentDelivery, composerProps;
function visit(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'currentDelivery') currentDelivery = node.getText(source);
  if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'composerProps') composerProps = node.initializer;
  ts.forEachChild(node, visit);
}
visit(source);
const prop = name => composerProps.properties.find(node => node.name?.getText(source) === name).initializer.getText(source);
const { copy: i18nCopy } = require('../lib/i18n.ts');
const { commandErrorText } = require('../lib/commandCopy.ts');
function viewState(snapshot, overrides = {}, zh = false) {
  const messages = require(`../messages/${zh ? 'zh' : 'en'}/chat.json`);
  const history = require(`../messages/${zh ? 'zh' : 'en'}/chat-history.json`).chatHistory;
  const context = {
    composerDelivery, i18nCopy, commandErrorText, zh,
    sessionId: 'taskchat_d5510915cd083870893ca24c', externalView: false, active: {},
    commandEngine: { ...snapshot, data: { queue: snapshot.queue }, pending: Array(snapshot.pendingCount || 0).fill({}) },
    workbench: { connection: 'online', view: { pending_interactions: [] } },
    awaitingApproval: false, history: { pending: false, edit: null },
    t: key => { assert.equal(typeof messages.chat[key], 'string', key); return messages.chat[key]; },
    th: key => history[key], ...overrides,
  };
  return require('node:vm').runInNewContext(`${currentDelivery}; const delivery = currentDelivery(); ({ delivery, locked: ${prop('locked')}, lockMessage: ${prop('lockMessage')} })`, context);
}

test('online unconfirmed execution keeps the composer locked with reconciliation copy', () => {
  const snapshot = input([{ ...command('unconfirmed'), command_id: 'acmd_d5510915cd083870893ca24c' }], true, 'restarted');
  for (const zh of [false, true]) {
    const state = viewState(snapshot, {}, zh);
    assert.equal(state.delivery.reason, 'execution_unconfirmed');
    assert.equal(state.delivery.canSubmit, false);
    assert.equal(state.delivery.runOnly, false);
    assert.equal(state.locked, true);
    assert.match(state.lockMessage, zh ? /执行.*未确认/ : /Execution.*unconfirmed/i);
    assert.match(state.lockMessage, zh ? /核对/ : /reconcile/i);
    assert.doesNotMatch(state.lockMessage, /Connection unavailable|连接暂不可用/);
  }
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

test('composer lock copy follows the same decision and connection source as admission', () => {
  assert.match(viewState({ ...input(), pendingCount: 1 }).lockMessage, /Delivery is unconfirmed/);
  assert.match(viewState(input([command('awaiting_approval')])).lockMessage, /approval/i);
  assert.match(viewState(input([command('awaiting_input')])).lockMessage, /answer|response|question/i);
  assert.match(viewState({ ...input(), connection: 'connecting' }).lockMessage, /Connecting/i);
  assert.match(viewState({ ...input(), connection: 'offline' }).lockMessage, /Connection unavailable/);
  const paused = viewState(input([command('succeeded')], true, 'operator'));
  assert.equal(paused.locked, false);
  assert.equal(paused.delivery.runOnly, true);
  assert.equal(paused.lockMessage, undefined);
  const editing = viewState(input(), { history: { pending: false, edit: {} } });
  assert.equal(editing.locked, true);
  assert.equal(editing.lockMessage, require('../messages/en/chat-history.json').chatHistory.editLabel);
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

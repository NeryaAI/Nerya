const { test, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const compilerOptions = { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true };
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), { compilerOptions }).outputText, f);
require.extensions['.css'] = m => { m.exports = {}; };
const identity = require('../lib/workspaceIdentity.ts');
const drafts = require('../lib/chatDraft.ts');
const edits = require('../lib/editDrafts.ts');
const commands = require('../lib/conversationCommands.ts');
const client = require('../lib/clientApi.ts');
const storage = () => {
  const values = new Map();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key),
    key: index => [...values.keys()][index] ?? null, get length() { return values.size; } };
};
const originalCall = client.callApi;
beforeEach(() => {
  identity.setWorkspaceIdentity(null);
  global.sessionStorage = storage(); global.localStorage = storage();
  global.window = Object.assign(new EventTarget(), { sessionStorage, localStorage });
  global.document = { addEventListener() {}, removeEventListener() {} };
  client.callApi = () => { throw Error('No real network is allowed'); };
});
afterEach(() => { identity.setWorkspaceIdentity(null); client.callApi = originalCall; });
const settings = { model_id: 'fixture-model', work_mode: 'plan', permission_mode: 'default' };
const file = { id: 'file-a', name: 'one.txt', size: 10, mime_type: 'text/plain', kind: 'document', artifact_uri: 'artifact://fixture/one.txt' };

test('unknown workspace reads no legacy clues, and same session is isolated across A/B', () => {
  sessionStorage.setItem('nerya.chat.draft.v1:same', JSON.stringify({ text: 'unattributed', attachments: [file] }));
  sessionStorage.setItem('nerya.edit-draft.v1:queue:same', JSON.stringify({ text: 'unattributed' }));
  assert.equal(drafts.readChatDraft('same').text, '');
  identity.setWorkspaceIdentity('A');
  assert.equal(drafts.readChatDraft('same').text, '');
  assert.equal(edits.readEditDraft('queue:same'), null);
  drafts.writeChatDraft('same', { text: 'A', attachments: [file], settings }); edits.writeEditDraft('queue:same', { text: 'A' });
  identity.setWorkspaceIdentity('B');
  assert.equal(drafts.readChatDraft('same').text, ''); assert.equal(edits.readEditDraft('queue:same'), null);
  drafts.writeChatDraft('same', { text: 'B', attachments: [] });
  identity.setWorkspaceIdentity('A');
  assert.equal(drafts.readChatDraft('same').text, 'A'); assert.equal(edits.readEditDraft('queue:same').text, 'A');
  identity.setWorkspaceIdentity(null);
  assert.equal(drafts.readChatDraft('same').text, ''); assert.equal(edits.readEditDraft('queue:same'), null);
});

test('settings-only drafts persist; revision protects all fields, including ABA and workspace changes', () => {
  identity.setWorkspaceIdentity('A');
  drafts.writeChatDraft('same', { text: '', attachments: [], settings });
  identity.setWorkspaceIdentity('B'); identity.setWorkspaceIdentity('A');
  assert.deepEqual(drafts.readChatDraft('same').settings, settings);
  drafts.writeChatDraft('same', { text: 'send', attachments: [file], settings });
  const sent = drafts.captureChatDraft('same');
  drafts.writeChatDraft('same', { ...drafts.readChatDraft('same'), settings: { ...settings, model_id: 'changed' } });
  assert.equal(drafts.clearChatDraftIfUnchanged(sent), false);
  const later = drafts.captureChatDraft('same');
  drafts.writeChatDraft('same', { ...drafts.readChatDraft('same'), text: 'different' });
  drafts.writeChatDraft('same', { ...drafts.readChatDraft('same'), text: 'send' });
  assert.equal(drafts.clearChatDraftIfUnchanged(later), false);
  const oldWorkspace = drafts.captureChatDraft('same'); identity.setWorkspaceIdentity('B'); identity.setWorkspaceIdentity('A');
  assert.equal(drafts.clearChatDraftIfUnchanged(oldWorkspace), false);
  assert.equal(drafts.clearChatDraftIfUnchanged(drafts.captureChatDraft('same')), true);
  assert.equal(drafts.readChatDraft('same').text, ''); assert.equal(drafts.readChatDraft('same').attachments.length, 0);
  assert.equal(drafts.readChatDraft('same').settings.model_id, 'changed');
});

test('identity switch clears memory fallbacks when browser storage is blocked', () => {
  identity.setWorkspaceIdentity('A');
  sessionStorage.setItem = () => { throw Error('blocked'); };
  drafts.writeChatDraft('same', { text: 'memory only', attachments: [] }); edits.writeEditDraft('edit', 'memory only');
  assert.equal(drafts.readChatDraft('same').text, 'memory only');
  identity.setWorkspaceIdentity('B'); identity.setWorkspaceIdentity('A');
  assert.equal(drafts.readChatDraft('same').text, ''); assert.equal(edits.readEditDraft('edit'), null);
});

const pending = (workspace, id, sid = 'same') => ({ workspace_id: workspace, command_id: id, session_id: sid, command_type: 'send', request: { payload: { text: id } }, created_at: 1 });
test('pending migration requires exact ownership; settlePending preserves other workspaces and sessions', () => {
  for (const row of [pending(undefined, 'legacy'), pending('A', 'ack'), pending('A', 'keep', 'other'), pending('B', 'ack')]) {
    localStorage.setItem('nerya.chat.pending-command.v2:'+(row.workspace_id || 'unknown')+row.command_id, JSON.stringify(row));
  }
  assert.deepEqual(commands.pendingCommands(), []);
  identity.setWorkspaceIdentity('A');
  assert.deepEqual(commands.pendingCommands('same').map(row => row.command_id), ['ack']);
  commands.settlePending('ack');
  assert.deepEqual(commands.pendingCommands().map(row => row.command_id), ['keep']);
  identity.setWorkspaceIdentity('B');
  assert.equal(commands.pendingCommands('same')[0].workspace_id, 'B');
  commands.settlePending('ack', 'A');
  assert.equal(commands.pendingCommands('same').length, 1);
  identity.setWorkspaceIdentity(null); assert.deepEqual(commands.pendingCommands(), []);
});

test('unknown or mismatched pending sends are rejected before the API; changed-runtime responses are not applied', async () => {
  await assert.rejects(commands.submitCommand('same', 'send', {}), error => error.code === 'workspace_unknown');
  identity.setWorkspaceIdentity('B');
  await assert.rejects(commands.submitCommand('same', 'send', {}, pending('A', 'wrong')), error => error.code === 'workspace_mismatch');
  let resolve;
  client.callApi = () => new Promise(done => { resolve = done; });
  const sent = commands.submitCommand('same', 'send', {});
  const receipt = commands.pendingCommands('same')[0];
  identity.setWorkspaceIdentity('A');
  resolve({ ok: true, command: { command_id: receipt.command_id, session_id: 'same' } });
  await assert.rejects(sent, error => error.uncertain);
  assert.deepEqual(commands.pendingCommands(), []);
});

test('durable storage retains settings-only rows and strips attachment bodies', async () => {
  const rows = new Map();
  global.indexedDB = { open() {
    const request = {};
    queueMicrotask(() => {
      request.result = { transaction(_name, mode) {
        const tx = { objectStore() { return {
          put(row) { rows.set(row.key, row); }, delete(key) { rows.delete(key); },
          getAll() { const req = {}; queueMicrotask(() => { req.result = [...rows.values()]; req.onsuccess(); }); return req; },
        }; } };
        if (mode === 'readwrite') queueMicrotask(() => tx.oncomplete());
        return tx;
      } };
      request.onsuccess();
    });
    return request;
  } };
  const durable = require('../lib/durableDrafts.ts');
  identity.setWorkspaceIdentity('A');
  durable.saveDurableDraft('A', 'same', { text: '', attachments: [], settings });
  await new Promise(resolve => setTimeout(resolve, 160));
  assert.equal((await durable.savedDrafts('A', 'same'))[0].draft.settings.model_id, settings.model_id);
  identity.setWorkspaceIdentity('B');
  assert.deepEqual(await durable.savedDrafts('A', 'same'), []);
  assert.equal(durable.safeDraft({ text: '', attachments: [{ ...file, text: 'private', data_url: 'data:body' }] }).attachments[0].data_url, undefined);
});

test('draft recovery offers content only without changing stored drafts or the active draft', async () => {
  identity.setWorkspaceIdentity('A');
  const current = { text: 'active draft', attachments: [file], settings, revision: 1 };
  const row = (key, draft, windowId = key) => ({ key, workspace: 'A', scope: 'same', windowId, updatedAt: 1, draft });
  const rows = [
    row('settings', { text: '', attachments: [], settings }),
    row('empty', { text: '', attachments: [] }),
    row('whitespace', { text: ' \n\t', attachments: [], settings }),
    row('text', { text: '  unfinished prompt  ', attachments: [], settings }),
    row('attachment', { text: '', attachments: [file], settings }),
    row('own', current, 'current-window'),
  ];
  const stored = JSON.stringify({ rows, current });
  let cursor = 0, slots = [], effects = [], cleanups = [];
  const hooks = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = initial;
      return [slots[i], value => { slots[i] = value; }]; },
    useRef(value) { const i = cursor++; return slots[i] ??= { current: value }; },
    useCallback: fn => fn,
    useEffect(fn, deps) { const i = cursor++; const old = slots[i];
      if (!old || deps.some((value, j) => value !== old[j])) { slots[i] = deps; effects.push(fn); } },
  };
  const filename = path.resolve(__dirname, '../components/chat/useChatDraft.ts');
  const m = new Module(filename, module); m.filename = filename; m.paths = module.paths;
  m.require = name => name === 'react' ? hooks : name.endsWith('/workspaceIdentity')
    ? { ...identity, useWorkspaceIdentity: () => identity.getWorkspaceIdentity() }
    : name.endsWith('/chatDraft') ? { ...drafts, readChatDraft: () => current,
      writeChatDraft: () => assert.fail('recovery discovery must not overwrite a draft') }
    : name.endsWith('/durableDrafts') ? { draftWindowId: () => 'current-window', savedDrafts: async () => rows,
      saveDurableDraft: () => assert.fail('recovery discovery must not write storage') }
    : Module.createRequire(filename)(name);
  m._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions }).outputText, filename);
  const render = () => { cursor = 0; const result = m.exports.useChatDraft('same');
    const work = effects; effects = []; for (const effect of work) cleanups.push(effect()); return result; };
  try {
    render();
    await new Promise(resolve => setImmediate(resolve));
    const result = render();
    assert.deepEqual(result.recovery, [rows[3], rows[4]]);
    assert.equal(result.text, current.text);
    assert.deepEqual(result.attachments, current.attachments);
    assert.deepEqual(result.settings, settings);
    assert.equal(JSON.stringify({ rows, current }), stored, 'filtering must preserve every stored field');
  } finally { for (const cleanup of cleanups) cleanup?.(); }
});

test('home handoff carries settings and artifact references; unknown identity cannot consume it', () => {
  const handoff = require('../lib/workspaceComposeDraft.ts');
  identity.setWorkspaceIdentity('A');
  assert.equal(handoff.setWorkspaceComposeDraft({ text: 'home', attachments: [{ ...file, data_url: 'data:body' }], settings, autoSend: true }), true);
  identity.setWorkspaceIdentity(null); assert.equal(handoff.takeWorkspaceComposeDraft(), null);
  identity.setWorkspaceIdentity('B'); assert.equal(handoff.takeWorkspaceComposeDraft(), null);
  identity.setWorkspaceIdentity('A');
  const value = handoff.takeWorkspaceComposeDraft();
  assert.deepEqual(value.settings, settings); assert.equal(value.workspace_id, 'A'); assert.equal(value.attachments[0].data_url, undefined);
  assert.equal(handoff.takeWorkspaceComposeDraft(), null);
});

test('resource tabs use workspace keys and never restore untagged legacy tabs', () => {
  const { loadTaskDockPreferences } = require('../components/chat/useTaskDock.ts');
  const prefs = { same: { open: true, expanded: false, selected: 'file:one', tabs: ['file:one'], dismissed: [] } };
  sessionStorage.setItem('nerya.chat.task-dock.v2', JSON.stringify(prefs));
  assert.deepEqual(loadTaskDockPreferences(null), {});
  identity.setWorkspaceIdentity('A'); assert.deepEqual(loadTaskDockPreferences('A'), {});
  sessionStorage.setItem(identity.workspaceStorageKey('nerya.chat.task-dock.v3:', 'A'), JSON.stringify(prefs));
  assert.deepEqual(loadTaskDockPreferences('A'), prefs);
  identity.setWorkspaceIdentity('B'); assert.deepEqual(loadTaskDockPreferences('B'), {}); assert.deepEqual(loadTaskDockPreferences('A'), {});
});

// Render the actual component function with a tiny hook fixture; child menus are
// inert. This exercises handlers without a browser, server, model or new dependency.
function composerFixture(props) {
  let cursor = 0, slots = [], effects = [];
  const hooks = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useRef(value) { const i = cursor++; return slots[i] ??= { current: value }; },
    useId() { cursor++; return 'fixture'; },
    useEffect(fn, deps) { const i = cursor++; const old = slots[i]; if (!old || deps.some((x, j) => x !== old[j])) { slots[i] = deps; effects.push(fn); } },
  };
  const filename = path.resolve(__dirname, '../components/chat/ChatInput.tsx');
  const m = new Module(filename, module); m.filename = filename; m.paths = module.paths;
  m.require = name => {
    if (name === 'react') return hooks;
    if (name === 'next-intl') return { useLocale: () => 'en', useTranslations: () => key => key };
    if (name.endsWith('/workspaceIdentity')) return { ...identity, useWorkspaceIdentity: () => identity.getWorkspaceIdentity() };
    if (name.endsWith('/clientApi')) return client;
    if (name === './ComposerSuggestions') return { useComposerSuggestions: () => ({ inputProps: {}, onKeyDown: () => false, dismiss() {} }), ComposerSuggestions: 'suggestions', ComposerAddMenu: 'add-menu' };
    if (name === './ComposerRunControls') return { ComposerModelMenu: 'model', ComposerPermissionMenu: 'permissions', ComposerWorkMode: 'mode' };
    if (name === './ReferenceSnapshot') return { ReferenceSnapshot: 'reference' };
    if (name === '../icons') return { FileIcon: 'file', SendIcon: 'send', StopIcon: 'stop', XIcon: 'close' };
    return Module.createRequire(filename)(name);
  };
  m._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions }).outputText, filename);
  const render = () => { cursor = 0; const tree = m.exports.ChatInput(props); const work = effects; effects = []; work.forEach(fn => fn()); return tree; };
  return { render, props };
}
function nodes(tree, predicate) {
  if (!tree || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(item => nodes(item, predicate));
  return [...(predicate(tree) ? [tree] : []), ...nodes(tree.props?.children, predicate)];
}
const baseProps = () => ({ value: 'next message', onChange() {}, onSend() {}, sending: false, settings, onSettingsChange() {}, attachments: [] });
test('busy Enter adds a line; approval locks send but leaves draft controls editable with a reason', () => {
  identity.setWorkspaceIdentity('A');
  const props = { ...baseProps(), submitting: true };
  const fixture = composerFixture(props);
  let tree = fixture.render(), prevented = false;
  nodes(tree, node => node.type === 'textarea')[0].props.onKeyDown({ key: 'Enter', nativeEvent: {}, preventDefault() { prevented = true; } });
  assert.equal(prevented, false);
  assert.ok(nodes(tree, node => node.props?.role === 'status').some(node => String(node.props.children).includes('Waiting for confirmation')));
  props.submitting = false; props.locked = true; props.lockMessage = 'Approval required before sending';
  tree = fixture.render();
  assert.equal(nodes(tree, node => node.type === 'textarea')[0].props.disabled, false);
  assert.equal(nodes(tree, node => node.props?.['data-testid'] === 'native-command-send')[0].props.disabled, true);
  assert.equal(nodes(tree, node => node.type === 'model')[0].props.disabled, false);
  assert.ok(nodes(tree, node => node.props?.role === 'status').some(node => node.props.children === props.lockMessage));
});

test('individual failed upload can be removed while successful attachments remain sendable', async () => {
  identity.setWorkspaceIdentity('A');
  global.FileReader = class { readAsDataURL(file) { this.result = 'data:'+file.name; queueMicrotask(() => this.onload()); } };
  const props = baseProps(); props.onAttachmentsChange = files => { props.attachments = files; };
  const fixture = composerFixture(props);
  client.callApi = async (_path, { body }) => {
    const attachment = body.attachments[0];
    return attachment.name === 'bad.txt' ? { ok: false } : { ok: true, attachments: [{ ...attachment, uploaded: true, artifact_uri: 'artifact://fixture/good' }] };
  };
  let tree = fixture.render();
  nodes(tree, node => node.type === 'input' && node.props.type === 'file')[0].props.onChange({ currentTarget: { value: 'picked', files: [
    { name: 'good.txt', size: 1, type: 'text/plain' }, { name: 'bad.txt', size: 1, type: 'text/plain' },
  ] } });
  await new Promise(resolve => setImmediate(resolve)); tree = fixture.render();
  assert.equal(props.attachments.length, 1); assert.equal(props.attachments[0].name, 'good.txt');
  const failed = nodes(tree, node => node.props?.['data-testid'] === 'attachment-progress');
  assert.equal(failed.length, 1);
  const remove = nodes(failed[0], node => String(node.props?.['aria-label']).includes('bad.txt'))[0];
  assert.ok(remove); remove.props.onClick(); tree = fixture.render();
  assert.equal(nodes(tree, node => node.props?.['data-testid'] === 'attachment-progress').length, 0);
  assert.equal(props.attachments.length, 1);
  assert.equal(nodes(tree, node => node.props?.['data-testid'] === 'native-command-send')[0].props.disabled, false);
});

function observerFixture() {
  let cursor = 0, slots = [], effects = [], cleanups = new Map();
  const hooks = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useRef(value) { const i = cursor++; return slots[i] ??= { current: value }; },
    useEffect(fn, deps) { const i = cursor++; const old = slots[i]; if (!old || deps.some((x, j) => x !== old[j])) {
      slots[i] = deps; effects.push(() => { cleanups.get(i)?.(); cleanups.set(i, fn()); });
    } },
  };
  const filename = path.resolve(__dirname, '../components/chat/useConversationCommands.ts');
  const m = new Module(filename, module); m.filename = filename; m.paths = module.paths;
  m.require = name => name === 'react' ? hooks : name.endsWith('/workspaceIdentity')
    ? { ...identity, useWorkspaceIdentity: () => identity.getWorkspaceIdentity() } : Module.createRequire(filename)(name);
  m._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions }).outputText, filename);
  const projected = [];
  return {
    projected,
    render() { cursor = 0; const result = m.exports.useConversationCommands('same', (...args) => projected.push(args));
      const work = effects; effects = []; work.forEach(fn => fn()); return result; },
    dispose() { for (const cleanup of cleanups.values()) cleanup?.(); },
  };
}

test('a healthy pending POST is not exposed as delivery unconfirmed before its receipt', async () => {
  identity.setWorkspaceIdentity('A');
  const calls = [];
  client.callApi = (path, options) => new Promise((resolve, reject) => calls.push({ path, options, resolve, reject }));
  const hook = observerFixture();
  let sent;
  try {
    hook.render();
    calls[0].resolve({ ok: true, commands: [], queue: { paused: false, revision: 1 } });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(hook.render().connection, 'online');
    sent = commands.submitCommand('same', 'send', { payload: { text: 'BTC/USDT 1h, 30 day backtest, no trading' } });
    const durable = commands.pendingCommands('same')[0];
    assert.ok(durable, 'persist the original key before sending for crash recovery');
    assert.deepEqual(hook.render().pending, [], 'an in-flight POST is submitting, not delivery unconfirmed');
    calls[1].resolve({ ok: true, command: { command_id: durable.command_id, session_id: 'same', state: 'running' } });
    await sent;
    assert.deepEqual(hook.render().pending, []);
    assert.deepEqual(commands.pendingCommands('same'), []);
  } finally {
    if (sent) {
      const request = calls.find(call => call.options.method === 'POST');
      request.resolve({ ok: true, command: { command_id: request.options.body.command_id, session_id: 'same' } });
      await sent;
    }
    hook.dispose();
  }
});

for (const status of [404, 503]) test(`a fresh command snapshot stays online while stream events return ${status}`, async () => {
  identity.setWorkspaceIdentity('A');
  const command = { command_id: 'accepted', session_id: 'same', kind: 'send', state: 'running', revision: 1 };
  let failEvents = true;
  const cursors = [];
  client.callApi = async path => {
    if (path.startsWith('/agent/commands/events?')) {
      cursors.push(new URL(path, 'http://fixture').searchParams.get('after_seq'));
      if (failEvents) throw new client.ApiError('events not ready', status, { error: 'events_unavailable' });
      return { ok: true, events: [{ kind: 'message.delta', seq: 1, text: 'Accepted' }], cursor: 1, has_more: false };
    }
    return { ok: true, commands: [command], queue: { paused: false, revision: 1 } };
  };
  const hook = observerFixture();
  try {
    hook.render();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(hook.render().connection, 'online', 'event hydration failure is not a command API outage');
    assert.ok(hook.projected.some(([row]) => row.command_id === 'accepted' && row.state === 'running'));
    failEvents = false;
    hook.render().refresh();
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(hook.projected.some(([, events]) => events.some(event => event.seq === 1)));
    assert.deepEqual(cursors, ['0', '0'], 'retry from the durable cursor after event hydration fails');
  } finally { hook.dispose(); }
});

for (const failure of ['network', 408, 503, 'invalid receipt']) test(`unknown ${failure} delivery retains recovery and the original key without resending`, async () => {
  identity.setWorkspaceIdentity('A');
  let finishPost, accepted;
  const posts = [];
  client.callApi = async (path, options) => {
    if (options.method === 'POST') {
      posts.push(options.body);
      return new Promise((resolve, reject) => { finishPost = () => {
        if (failure === 'invalid receipt') resolve({ ok: true, command: { command_id: 'different', session_id: 'same' } });
        else reject(typeof failure === 'number' ? new client.ApiError('uncertain delivery', failure, {}) : new TypeError('Failed to fetch'));
      }; });
    }
    return { ok: true, commands: accepted ? [accepted] : [], queue: { paused: false, revision: 1 } };
  };
  const hook = observerFixture();
  try {
    hook.render();
    await new Promise(resolve => setImmediate(resolve));
    const sent = commands.submitCommand('same', 'send', { payload: { text: 'Keep this request' } });
    const durable = commands.pendingCommands('same')[0];
    assert.deepEqual(hook.render().pending, []);
    finishPost();
    await assert.rejects(sent, error => error.code === 'delivery_unconfirmed' && error.uncertain);
    assert.deepEqual(hook.render().pending, [durable]);
    hook.render().refresh();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(hook.render().pending, [durable], 'absence from a snapshot is not a rejection');
    accepted = { command_id: durable.command_id, session_id: 'same', kind: 'send', state: 'queued', revision: 1 };
    hook.render().refresh();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(hook.render().pending, []);
    assert.deepEqual(commands.pendingCommands('same'), []);
    assert.equal(posts.length, 1, 'reconciliation must never auto-resend');
    assert.equal(posts[0].command_id, durable.command_id);
  } finally { hook.dispose(); }
});

test('restored pending records stay visible and an explicit resend preserves the original key', async () => {
  identity.setWorkspaceIdentity('A');
  const durable = pending('A', 'restore');
  localStorage.setItem(identity.workspaceStorageKey('nerya.chat.pending-command.v3:', 'A', durable.command_id), JSON.stringify(durable));
  const posts = [];
  client.callApi = async (path, options) => {
    if (options.method === 'POST') {
      posts.push(options.body);
      return { ok: true, command: { command_id: durable.command_id, session_id: 'same', state: 'queued' } };
    }
    if (path.includes('command_id=')) throw new client.ApiError('not found', 404, {});
    return { ok: true, commands: [], queue: { paused: false, revision: 1 } };
  };
  const hook = observerFixture();
  try {
    hook.render();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(hook.render().pending, [durable]);
    await assert.rejects(hook.render().recover(durable), error => error.status === 404);
    assert.equal(posts.length, 0);
    assert.deepEqual(hook.render().pending, [durable]);
    await hook.render().recover(durable, true);
    assert.deepEqual(posts, [durable]);
    assert.deepEqual(hook.render().pending, []);
  } finally { hook.dispose(); }
});

test('a failed result fetch keeps terminal admission and does not starve another command', async () => {
  identity.setWorkspaceIdentity('A');
  const terminal = { command_id: 'done', session_id: 'same', kind: 'send', state: 'succeeded', revision: 2, has_result: true };
  const running = { ...terminal, command_id: 'next', state: 'running', has_result: false };
  let failResult = true;
  client.callApi = async path => {
    if (path.includes('/events?')) return { ok: true, events: [], cursor: 0, has_more: false };
    if (path.includes('command_id=')) {
      if (failResult) throw new TypeError('Failed to fetch');
      return { ok: true, command: { ...terminal, result: { turn_id: 'turn', final_text: 'Done' } } };
    }
    return { ok: true, commands: [terminal, running], queue: { paused: false, revision: 1 } };
  };
  const hook = observerFixture();
  try {
    hook.render();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(hook.render().connection, 'online');
    assert.deepEqual(hook.projected.map(([row]) => row.command_id), ['done', 'next']);
    failResult = false;
    hook.render().refresh();
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(hook.projected.some(([row]) => row.result?.final_text === 'Done'));
  } finally { hook.dispose(); }
});

test('a failed later event page never advances past unprojected events', async () => {
  identity.setWorkspaceIdentity('A');
  const command = { command_id: 'paged', session_id: 'same', kind: 'send', state: 'running', revision: 1 };
  const cursors = [];
  let failPage = true;
  client.callApi = async path => {
    if (!path.includes('/events?')) return { ok: true, commands: [command], queue: { paused: false, revision: 1 } };
    const after = Number(new URL(path, 'http://fixture').searchParams.get('after_seq'));
    cursors.push(after);
    if (after === 1 && failPage) throw new TypeError('Failed to fetch');
    return { ok: true, events: [{ kind: 'message.delta', seq: after + 1, text: String(after + 1) }], cursor: after + 1, has_more: after === 0 };
  };
  const hook = observerFixture();
  try {
    hook.render();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(hook.render().connection, 'online');
    failPage = false;
    hook.render().refresh();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(hook.projected.flatMap(([, events]) => events.map(event => event.seq)), [1, 2]);
    assert.deepEqual(cursors, [0, 1, 0, 1]);
  } finally { hook.dispose(); }
});

for (const status of [404, 503]) test(`actual snapshot failure ${status} still reports connection unavailability`, async () => {
  identity.setWorkspaceIdentity('A');
  client.callApi = async () => { throw new client.ApiError('snapshot unavailable', status, {}); };
  const hook = observerFixture();
  try {
    hook.render();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(hook.render().connection, status === 404 ? 'incompatible' : 'offline');
    assert.equal(hook.render().data, null);
  } finally { hook.dispose(); }
});

test('command observer waits for identity, discards old async snapshot, and refuses foreign recovery', async () => {
  const calls = [];
  client.callApi = (path, options) => new Promise((resolve, reject) => calls.push({ path, options, resolve, reject }));
  const hook = observerFixture();
  try {
    hook.render(); assert.equal(calls.length, 0);
    identity.setWorkspaceIdentity('A'); hook.render(); assert.equal(calls.length, 1);
    const old = calls[0];
    identity.setWorkspaceIdentity('B'); let state = hook.render(); assert.equal(calls.length, 2);
    assert.equal(old.options.signal.aborted, true);
    old.resolve({ ok: true, commands: [{ session_id: 'same', command_id: 'old', state: 'succeeded', kind: 'send', revision: 1 }], queue: {} });
    calls[1].resolve({ ok: true, commands: [], queue: { paused: false, revision: 1 } });
    await new Promise(resolve => setImmediate(resolve));
    state = hook.render(); assert.equal(state.connection, 'online'); assert.equal(hook.projected.length, 0);
    await assert.rejects(state.recover(pending('A', 'foreign'), true), error => error.code === 'workspace_mismatch');
    assert.equal(calls.length, 2);
    identity.setWorkspaceIdentity(null); state = hook.render();
    assert.equal(state.data, null); assert.deepEqual(state.pending, []); assert.equal(calls.length, 2);
  } finally { hook.dispose(); }
});

test('recovery cannot resend after a workspace switch even when the old request returns 404', async () => {
  const calls = [];
  client.callApi = (path, options) => new Promise((resolve, reject) => calls.push({ path, options, resolve, reject }));
  identity.setWorkspaceIdentity('A');
  const hook = observerFixture();
  try {
    const state = hook.render();
    const recovery = state.recover(pending('A', 'recover'), true);
    assert.equal(calls.length, 2);
    identity.setWorkspaceIdentity('B'); hook.render();
    calls[1].reject(new client.ApiError(404, { error: 'not_found' }));
    await assert.rejects(recovery);
    assert.equal(calls.length, 3); assert.equal(hook.projected.length, 0);
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 0);
    calls[0].resolve({ ok: true, commands: [], queue: {} }); calls[2].resolve({ ok: true, commands: [], queue: {} });
    await new Promise(resolve => setImmediate(resolve));
  } finally { hook.dispose(); }
});


test('history switches clear revisions and pending; an old finally cannot unlock a new workspace write', async () => {
  const history = require('../lib/historyClient.ts');
  await assert.rejects(history.writeHistory('same', '/fixture', {}), error => error.code === 'workspace_unknown');
  const calls = [];
  client.callApi = () => new Promise(resolve => calls.push(resolve));
  identity.setWorkspaceIdentity('A');
  const old = history.writeHistory('same', '/fixture', {});
  assert.equal(history.historyPending('same'), true); assert.equal(history.historyRevision('same'), 1);
  identity.setWorkspaceIdentity('B');
  assert.equal(history.historyPending('same'), false); assert.equal(history.historyRevision('same'), 0);
  const fresh = history.writeHistory('same', '/fixture', {});
  calls[0]({ ok: true, session_id: 'same' });
  await assert.rejects(old, error => error.code === 'workspace_changed');
  assert.equal(history.historyPending('same'), true); assert.equal(history.historyRevision('same'), 1);
  calls[1]({ ok: true, session_id: 'same' }); await fresh;
  assert.equal(history.historyPending('same'), false); assert.equal(history.historyRevision('same'), 2);
});

test('old rename ACK cannot alter same-id conversation in the new workspace', async () => {
  const history = require('../lib/historyClient.ts');
  const chat = require('../lib/chat.ts');
  const oldLoad = chat.loadThreads, oldSave = chat.saveThreads;
  let writes = 0, resolve;
  chat.loadThreads = () => [{ id: 'same', title: 'B title', messages: [] }]; chat.saveThreads = () => { writes++; };
  client.callApi = () => new Promise(done => { resolve = done; });
  try {
    identity.setWorkspaceIdentity('A'); const rename = history.renameConversation('same', 'A title');
    identity.setWorkspaceIdentity('B'); resolve({ ok: true, title: 'A title', session_id: 'same' });
    await assert.rejects(rename, error => error.code === 'workspace_changed'); assert.equal(writes, 0);
  } finally { chat.loadThreads = oldLoad; chat.saveThreads = oldSave; }
});

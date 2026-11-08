const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const compilerOptions = { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true };
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), { compilerOptions }).outputText, f);
const identity = require('../lib/workspaceIdentity.ts');
const chat = require('../lib/chat.ts');
const commands = require('../lib/conversationCommands.ts');
const client = require('../lib/clientApi.ts');
const drafts = require('../lib/chatDraft.ts');
const storage = () => {
  const entries = new Map();
  return { getItem: key => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, value),
    removeItem: key => entries.delete(key), key: index => [...entries.keys()][index] ?? null, get length() { return entries.size; } };
};
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const idle = () => new Promise(resolve => setImmediate(resolve));
function nodes(tree, predicate, at = 'root') {
  if (!tree || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap((item, i) => nodes(item, predicate, at + '/' + i));
  return [...(predicate(tree) ? [{ node: tree, at }] : []), ...nodes(tree.props?.children, predicate, at + '/' + String(tree.type))];
}

// Run the actual route component, its effects, draft hook and send handlers.
// Unrelated panels stay inert; no browser, service, model or dependency is needed.
function fixture(t, sessionId = 'assistant_empty', source = 'assistant', reportNavigation = false) {
  global.localStorage = storage(); global.sessionStorage = storage();
  global.window = Object.assign(new EventTarget(), { localStorage, sessionStorage, location: { search: '', href: 'http://fixture/chat/' + (sessionId || '') } });
  global.document = Object.assign(new EventTarget(), { visibilityState: 'visible', hidden: false });
  if (reportNavigation) {
    const previous = {HTMLElement: global.HTMLElement, requestAnimationFrame: global.requestAnimationFrame};
    global.HTMLElement = class {};
    global.requestAnimationFrame = callback => { callback(); return 0; };
    document.getElementById = () => null;
    t.after(() => Object.assign(global, previous));
  }
  global.CustomEvent = class extends Event { constructor(type, options) { super(type); this.detail = options?.detail; } };
  identity.setWorkspaceIdentity('empty-home-test');
  let cursor = 0, dirty = false, tree;
  const slots = [], effects = [], cleanups = new Map();
  const changed = (before, after) => !before || !after || after.some((value, i) => !Object.is(value, before[i]));
  const hooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { const next = typeof value === 'function' ? value(slots[i]) : value; if (!Object.is(next, slots[i])) { slots[i] = next; dirty = true; } }];
    },
    useRef(value) { const i = cursor++; return slots[i] ??= { current: value }; },
    useMemo(fn, deps) { const i = cursor++; if (!slots[i] || changed(slots[i].deps, deps)) slots[i] = { deps, value: fn() }; return slots[i].value; },
    useCallback(fn, deps) { return hooks.useMemo(() => fn, deps); },
    useEffect(fn, deps) { const i = cursor++; if (changed(slots[i], deps)) { slots[i] = deps; effects.push(() => { cleanups.get(i)?.(); cleanups.set(i, fn()); }); } },
  };
  const metadata = deferred(), transcript = deferred(), posts = [], routes = [], transcriptCalls = [];
  const session = { session_id: sessionId, source, created_at: '2026-10-22T00:00:00Z', updated_at: '2026-10-22T00:00:00Z', message_count: 0 };
  let nextTranscript = transcript.promise;
  const api = {
    ...client.clientApi, sessionList: (_source, limit) => limit === 100 ? Promise.resolve({ sessions: [] }) : metadata.promise,
    sessionTranscript: sid => { transcriptCalls.push(sid); return nextTranscript; },
    sessionGet: async () => session,
    llmTiers: async () => ({}), llmModels: async () => ({}), llmConfig: async () => ({}), approvalsPending: async () => ({ approvals: [] }),
  };
  const originalCall = client.callApi;
  client.callApi = (url, options) => {
    assert.equal(options.method, 'POST', 'unexpected network boundary');
    const response = deferred(); posts.push({ url, options, ...response }); return response.promise;
  };
  const workspace = { ...identity, useWorkspaceIdentity: () => identity.getWorkspaceIdentity() };
  function load(relative, overrides, suffix = '') {
    const filename = path.resolve(__dirname, relative);
    const source = fs.readFileSync(filename, 'utf8');
    const inert = {};
    const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    for (const statement of ast.statements) if (ts.isImportDeclaration(statement)) {
      const names = statement.importClause?.namedBindings?.elements || [];
      inert[statement.moduleSpecifier.text] = { ...inert[statement.moduleSpecifier.text], ...Object.fromEntries(names.map(name => [name.propertyName?.text || name.name.text, name.name.text.endsWith('Context') ? { Provider: name.name.text } : name.name.text])) };
    }
    const m = new Module(filename, module); m.filename = filename; m.paths = module.paths;
    m.require = name => name === 'react' ? hooks : name.endsWith('/workspaceIdentity') ? workspace
      : name in overrides ? overrides[name] : name === 'react/jsx-runtime' ? require(name)
      : name.endsWith('.css') ? {} : inert[name] || Module.createRequire(filename)(name);
    m._compile(ts.transpileModule(source + suffix, { compilerOptions }).outputText, filename);
    return m.exports;
  }
  const draftHook = load('../components/chat/useChatDraft.ts', {
    '../../lib/chatDraft': drafts, '../../lib/durableDrafts': { savedDrafts: async () => [], saveDurableDraft() {}, draftWindowId: () => 'fixture' },
  });
  const engine = { data: { commands: [], queue: { paused: false } }, commands: [], pending: [], connection: 'online', refresh() {} };
  let project;
  const workbench = { connection: 'online', view: { pending_interactions: [] }, refresh() {} };
  const { WorkspaceChatView } = load('../components/chat/ChatView.tsx', {
    'next/navigation': { useRouter: () => ({ replace: url => routes.push(url) }) },
    'next-intl': { useTranslations: () => Object.assign(key => key, { rich: key => key }), useLocale: () => 'en' },
    '../../lib/clientApi': { ...client, clientApi: api }, '../../lib/chat': chat,
    '../../lib/conversationCommands': commands, '../../lib/composerDelivery': require('../lib/composerDelivery.ts'),
    '../../lib/modelProjection': require('../lib/modelProjection.ts'),
    '../../lib/historyClient': require('../lib/historyClient.ts'),
    '../../lib/externalCalls': require('../lib/externalCalls.ts'),
    '../../lib/externalNative': { projectExternalThread: thread => thread },
    '../../lib/assistantTaskDelivery': { projectAssistantTaskMessage: message => message },
    '../../lib/i18n': { copy: (_zh, key) => key }, '../../lib/commandCopy': { commandErrorText: code => code },
    '../../lib/dialogs': { toast() {}, confirm: async () => true },
    '../../lib/workspaceComposeDraft': { takeWorkspaceComposeDraft: () => null },
    './useChatDraft': draftHook, './useConversationCommands': { useConversationCommands: (_sid, sink) => { project = sink; return engine; } },
    './useWorkbench': { useWorkbench: () => workbench }, './useAssistantTasks': { useAssistantTasks: () => ({ tasks: [] }) },
    './useMessageHistory': { useMessageHistory: () => ({ pending: false, edit: null }) },
    './useAgentWork': { useAgentWork: () => ({ rows: [] }) }, './useStrategyReports': { useStrategyReports: () => ({ runs: [] }) },
    './useCanvasLayout': { useCanvasLayout: () => ({ layoutRef: {}, width: 48, compact: false, separatorProps: {} }) },
    './useTaskDock': { useTaskDock: () => {
      const [dock, setDock] = hooks.useState({tabs: [], open: false, expanded: false});
      return {...dock, select: id => setDock({...dock, tabs: [...new Set([...dock.tabs, id])], selected: id, open: true}),
        close: () => setDock({...dock, open: false})};
    } },
    './ConversationTimeline': { ConversationTimeline: 'ConversationTimeline', hasReadingAnchor: () => false },
    '../../lib/chatResults': reportNavigation ? require('../lib/chatResults.ts') : { collectChatResults: () => [] },
    '../../lib/strategyDetail': require('../lib/strategyDetail.ts'),
    './WorkspaceResourcePreview': { collectThreadItems: () => [] },
    '../../lib/portfolioArtifacts': { collectPortfolioArtifacts: () => [] },
    '../../lib/backtestResults': { collectBacktestResults: () => [] },
    '../../lib/researchVisuals': { collectResearchVisuals: () => ({ studies: [], instruments: [], charts: [] }) },
    '../../lib/workbench': require('../lib/workbench.ts'), '../../lib/browserTrace': { browserCalls: () => [] },
    '../../lib/toolOutputPresentation': require('../lib/toolOutputPresentation.ts'),
  }, '\nexport { WorkspaceChatView };');
  t.after(() => {
    for (const cleanup of cleanups.values()) cleanup?.();
    client.callApi = originalCall; identity.setWorkspaceIdentity(null);
  });
  return { metadata, transcript, session, posts, routes, transcriptCalls, workbench, engine,
    snapshot(values, emit = true) {
      engine.commands = values; engine.data.commands = values; engine.active = values.find(commands.runningCommand);
      if (emit) values.forEach(value => project(value, []));
    },
    header() { return nodes(tree, node => node.type === 'ChatTaskHeader')[0]?.node.props; },
    setTranscript(value) { nextTranscript = Promise.resolve(value); },
    render() {
      let iterations = 0;
      do {
        assert.ok(iterations++ < 30, 'component must settle');
        cursor = 0; dirty = false; tree = WorkspaceChatView({ sessionId, preferredAssistantId: 'fixture-assistant' });
        effects.splice(0).forEach(effect => effect());
      } while (dirty);
      return tree;
    },
    composer() { return nodes(tree, node => node.type === 'ChatInput')[0]; },
    has(id) { return nodes(tree, node => node.props?.['data-testid'] === id).length > 0; },
    text(key) { return nodes(tree, node => node.props?.children === key).length > 0; },
  };
}

const taskSession = 'taskchat_dcfaccfd5d654ad296110141';

test('approval terminal arriving before hydration cannot seed a title and durable Chinese title replaces stale cache', async t => {
  const sid = 'da9afa29-f829-4075-b93c-ab99c1b3ad08';
  const title = '创建一个 Jupiter SOL/USDC 趋势策略，回测最近7天';
  const request = {source: 'approval_continue', kind: 'approval.continue', payload: {
    text: 'The requested permission was approved. Continue the original task within existing permissions.'}};
  const approval = {command_id: 'approval_fixture', session_id: sid, turn_id: 'approval-turn',
    kind: 'send', state: 'interrupted', revision: 2, created_at: 1792627210, updated_at: 1792627220,
    input: request.payload.text, attachments: [], context: {approval_id: 'fixture'},
    show_user: request.source !== 'approval_continue'};
  const f = fixture(t, sid, 'web');
  f.render();
  f.snapshot([approval]); f.render();
  assert.notEqual(f.header()?.thread?.title, chat.deriveTitle(request.payload.text),
    'hidden approval input must never become the title while session hydration is pending');
  f.transcript.resolve({ok: true, ...f.session, title, messages: [
    {role: 'user', message_id: 'original:user', turn_id: 'original', content: title, ts: f.session.created_at},
  ]});
  await idle(); f.render();
  assert.equal(f.header().thread.title, title);
  assert.equal(f.header().thread.messages.filter(m => m.role === 'user').length, 1);
  // Reproduce the persisted local fallback from the old approval bootstrap.
  const stale = {...f.header().thread, title: chat.deriveTitle(request.payload.text), imported: false};
  chat.cacheThreadTranscript(stale);
  chat.saveThreads([stale]);
  window.dispatchEvent(new CustomEvent('nerya:threads-changed'));
  f.render();
  assert.equal(f.header().thread.title, stale.title, 'fixture installs the old cached title');
  document.dispatchEvent(new Event('visibilitychange'));
  f.metadata.resolve({sessions: [{...f.session, meta: {title}}]});
  await idle(); f.render();
  assert.equal(f.header().thread.title, title, 'hydrated session title wins over the old local fallback');
  assert.equal(f.posts.length, 0);
});

for (const metadataFirst of [true, false]) test('first send keeps its meaningful title with untitled session metadata ' + (metadataFirst ? 'before' : 'after') + ' the command', async t => {
  const sid = 'f58b1b37-e0ad-423b-9e37-d7dd10bc8ceb';
  const input = '创建一个 SOL/USDC 趋势策略，不交易';
  const f = fixture(t, sid, 'web');
  if (metadataFirst) f.session.meta = {title: 'Session ' + sid.slice(0, 8)};
  f.render();
  if (metadataFirst) { f.metadata.resolve({sessions: [f.session]}); await idle(); f.render(); }
  f.snapshot([{command_id: 'first-send', session_id: sid, turn_id: 'first-turn', kind: 'send',
    state: 'running', revision: 1, created_at: 1792627200, updated_at: 1792627210,
    input, attachments: [], context: {}, show_user: true}]);
  f.render();
  assert.equal(f.header().thread.title, input, 'a first user send replaces only a generated placeholder');
  if (!metadataFirst) { f.metadata.resolve({sessions: [f.session]}); await idle(); f.render(); }
  assert.equal(f.header().thread.title, input, 'untitled metadata cannot replace a meaningful command title');
  f.transcript.resolve({ok: true, ...f.session, title: 'Session ' + sid.slice(0, 8), messages: [
    {role: 'user', message_id: 'first-turn:user', turn_id: 'first-turn', content: input, ts: f.session.created_at},
  ]});
  await idle(); f.render();
  assert.equal(f.header().thread.title, input);
  assert.equal(f.posts.length, 0);
});

test('ordinary task transcript preserves source_command_id and binds late snapshot delivery to the saved report', async t => {
  const sid = '79b35790-89be-4483-8d1e-f1a6d48aa526';
  const f = fixture(t, sid, 'web', true);
  f.render();
  f.transcript.resolve({ ok: true, ...f.session, messages: [
    { role: 'assistant', message_id: 'saved-aave-answer', turn_id: 'aave-turn',
      content: 'Historical rate-policy report', ts: f.session.updated_at,
      meta: { source_command_id: 'aac90af3-fixture', execution_status: 'succeeded' } },
  ] });
  await idle();
  let tree = f.render();
  let saved = f.header().thread.messages.find(message => message.id === 'saved-aave-answer');
  assert.equal(saved.command_id, 'aac90af3-fixture');
  const receipt = { protocol: 'nerya.task_delivery.v1', command_id: saved.command_id,
    delivery_id: 'aave-delivery', delivery_revision: 8,
    stages: [{ id: 'deliver', state: 'completed' }], artifacts: [],
    checks: [{ check_id: 'source', status: 'passed', required: true }],
    completion: { allowed: true, status: 'partial', reasons: [] }, limitations: ['Modeled yield only'] };
  f.snapshot([{ command_id: saved.command_id, session_id: sid, turn_id: 'aave-turn',
    kind: 'send', state: 'succeeded', revision: 9, created_at: 1792627200, updated_at: 1792627210,
    input: 'Create Aave strategy', attachments: [], context: {}, delivery: receipt }], false);
  tree = f.render();
  const replies = f.header().thread.messages.filter(message => message.role === 'assistant');
  assert.equal(replies.length, 1, 'snapshot must merge into the durable reply, not create a duplicate');
  assert.equal(replies[0].turn.delivery, receipt);
  f.header().onToggleReports(); tree = f.render();
  const results = nodes(tree, node => node.type === 'ChatResultsPanel')[0].node.props.results;
  assert.equal(results.length, 1);
  assert.equal(results[0].delivery, receipt);
  assert.equal(require('../lib/deliveryEvidence.ts').deliveryEvidence(results[0]).partialDelivery, true);
  assert.equal(f.posts.length, 0);
});

for (const home of [false, true]) test('Reports header opens ' + (home ? 'assistant home global results' : 'direct task local results and preserves backtest details'), async t => {
  // Direct CEX task has no assistant_work row. This is a reduced routing fixture,
  // not a claim about its trading performance or delivery acceptance.
  const sid = home ? 'assistant_report_home' : '297beb00-b7d5-4a52-802d-89dcda82e609';
  const f = fixture(t, sid, home ? 'assistant' : 'web', true);
  f.render();
  f.transcript.resolve({ok: true, ...f.session, messages: [
    {role: 'user', message_id: 'cex-turn:user', turn_id: 'cex-turn', content: 'CEX report', ts: f.session.created_at},
    {role: 'assistant', message_id: 'cex-turn:assistant', turn_id: 'cex-turn', content: '# Current CEX report\nOriginal authored result.',
      ts: f.session.updated_at, turn: {turn_id: 'cex-turn', command_id: '844cde61-fixture'}, meta: {execution_status: 'succeeded'}},
  ]});
  await idle();
  let tree = f.render();
  const header = nodes(tree, node => node.type === (home ? 'AssistantChatHeader' : 'ChatTaskHeader'))[0].node.props;
  header.onToggleReports(); tree = f.render();
  const panel = nodes(tree, node => node.props?.id === 'task-dock-panel-reports')[0].node;
  assert.equal(panel.props.hidden, false);
  assert.equal(nodes(panel, node => node.type === 'AssistantResults').length, home ? 1 : 0);
  const readers = nodes(panel, node => node.type === 'ChatResultsPanel');
  assert.equal(readers.length, home ? 0 : 1);
  if (home) {
    nodes(tree, node => node.type === 'AssistantChatHeader')[0].node.props.onOpenResult('delegated-command');
    tree = f.render();
    assert.deepEqual(nodes(tree, node => node.type === 'AssistantResults')[0].node.props.requestedResult, {commandId: 'delegated-command'});
    return;
  }
  const [report] = readers[0].node.props.results;
  assert.equal(report.text, '# Current CEX report\nOriginal authored result.');
  assert.equal(report.turnId, 'cex-turn');
  assert.equal(typeof readers[0].node.props.onReveal, 'function');
  f.header().onOpenResult(report.id); tree = f.render();
  assert.equal(nodes(tree, node => node.type === 'ChatResultsPanel')[0].node.props.selectedId, report.id);
  const target = {kind: 'backtest', strategyId: 'cex_fixture', proposalId: 'prp_fixture', ts: '20261025_120000'};
  nodes(tree, node => node.type === 'StrategyDetailContext')[0].node.props.value.open(target);
  tree = f.render();
  const chart = nodes(tree, node => node.type === 'BacktestChart')[0].node;
  assert.equal(chart.props.strategyId, target.strategyId);
  assert.equal(chart.props.proposalId, target.proposalId);
  assert.equal(chart.props.ts, target.ts);
  assert.equal(f.posts.length, 0, 'report and detail navigation never submit commands');
});

const childId = 'acmd_dcfaccfd5d654ad296110141';
const followupId = '6d43b7e3-e678-49f0-a669-2e02c9cbddee';
const delivery = {
  protocol: 'nerya.task_delivery.v1', command_id: childId, delivery_id: 'delivery-fixture',
  skill_id: 'strategy_author', workflow_id: 'strategy', delivery_revision: 6,
  stages: ['scope', 'author', 'validate', 'replay', 'receipt', 'deliver'].map((id, i) => ({ id, label: id, state: i < 2 ? 'completed' : 'pending' })),
  artifacts: [], checks: [], completion: { allowed: false, status: 'incomplete' },
};
const child = {
  command_id: childId, session_id: taskSession, turn_id: 'child-turn', kind: 'send',
  state: 'blocked', revision: 7, created_at: 1792627200, updated_at: 1792627210, position: 0,
  input: 'Run strategy', attachments: [], context: {}, error: { code: 'model_length_limit' }, delivery,
};
const savedTranscript = session => ({ ok: true, ...session, updated_at: '2026-10-22T00:00:10Z', messages: [
  { role: 'user', message_id: 'child-turn:user', turn_id: 'child-turn', content: child.input, ts: session.created_at },
  { role: 'assistant', message_id: 'child-turn:assistant', turn_id: 'child-turn', content: 'Token limit reached', ts: session.updated_at,
    turn: { turn_id: 'child-turn', command_id: childId, stopped_reason: 'model_length_limit' },
    meta: { execution_status: 'blocked', command_revision: 7 } },
] });

function headerMarkup(props) {
  // Render in server mode while the route fixture keeps its separate DOM shim.
  const savedWindow = global.window, savedDocument = global.document;
  delete global.window; delete global.document;
  try {
    const React = require('react');
    const { renderToStaticMarkup } = require('react-dom/server');
    const { NextIntlClientProvider } = require('next-intl');
    require.extensions['.css'] = m => { m.exports = {}; };
    const filename = path.resolve(__dirname, '../components/chat/ChatTaskHeader.tsx');
    const m = new Module(filename, module); m.filename = filename; m.paths = module.paths;
    const localRequire = Module.createRequire(filename);
    m.require = name => ['../shell/ShellNavigationTrigger', '../shell/ShellNotifications'].includes(name)
      ? { [name.split('/').at(-1)]: () => null } : localRequire(name);
    require.extensions['.tsx'](m, filename);
    return renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale: 'en', timeZone: 'UTC', messages: require('../messages/index.ts').en },
      React.createElement(m.exports.ChatTaskHeader, props)));
  } finally { global.window = savedWindow; global.document = savedDocument; }
}

for (const snapshotFirst of [true, false]) test('terminal reload preserves command delivery when ' + (snapshotFirst ? 'snapshot' : 'transcript') + ' arrives first', async t => {
  const f = fixture(t, taskSession, 'web');
  f.render();
  if (snapshotFirst) { f.snapshot([child]); f.render(); }
  f.transcript.resolve(savedTranscript(f.session));
  await idle(); f.render();
  if (!snapshotFirst) { f.snapshot([child]); f.render(); }
  assert.deepEqual(f.header().delivery, delivery);
  assert.equal(f.header().thread.messages.filter(m => m.role === 'assistant').length, 1);
  const html = headerMarkup(f.header());
  assert.match(html, /data-testid="delivery-stage-rail"/);
  assert.match(html, /Delivery incomplete/);
  assert.equal((html.match(/data-current=/g) || []).length, 6);
});

test('live to terminal hydration keeps persisted delivery after the observer has drained the command', async t => {
  const f = fixture(t, taskSession, 'web');
  f.render();
  const initial = savedTranscript(f.session);
  initial.messages[1].meta = { execution_status: 'running', command_revision: 6 };
  f.transcript.resolve(initial);
  await idle(); f.render();
  f.snapshot([{ ...child, state: 'running', revision: 6, error: null }]); f.render();
  assert.deepEqual(f.header().delivery, delivery);
  f.snapshot([child]); f.render();
  const next = savedTranscript(f.session);
  next.messages[1].content = 'Final persisted answer';
  f.setTranscript(next);
  window.dispatchEvent(new CustomEvent('nerya:assistant-result', { detail: { sessionId: taskSession } }));
  await idle(); f.render();
  assert.equal(f.header().thread.messages.at(-1).turn.reply_text, 'Final persisted answer');
  assert.deepEqual(f.header().delivery, delivery, 'unchanged drained command must still enrich refreshed transcript');
});

for (const sameTurn of [false, true]) test('newer ' + (sameTurn ? 'resume' : 'send') + ' cannot inherit the old delivery before its projection callback', async t => {
  const f = fixture(t, taskSession, 'web');
  f.render(); f.transcript.resolve(savedTranscript(f.session));
  await idle(); f.render(); f.snapshot([child]); f.render();
  const newer = { ...child, command_id: followupId, turn_id: sameTurn ? child.turn_id : 'followup-turn',
    kind: sameTurn ? 'resume' : 'send', state: 'running', revision: 1, created_at: child.created_at + 20, updated_at: child.updated_at + 20,
    error: null, delivery: undefined };
  f.snapshot([newer, child], false); f.render();
  assert.equal(f.header().delivery, null);
  assert.doesNotMatch(headerMarkup(f.header()), /delivery-stage-rail/);
  const nextDelivery = { ...delivery, command_id: followupId, delivery_id: 'followup-delivery', delivery_revision: 1 };
  const running = { ...newer, revision: 2, delivery: nextDelivery };
  f.snapshot([child, running]); f.render();
  assert.deepEqual(f.header().delivery, nextDelivery);
  f.snapshot([child, newer]); f.render();
  assert.deepEqual(f.header().delivery, nextDelivery, 'late older snapshot cannot roll back the new receipt');
});

test('legacy no-delivery transcript stays usable and foreign or queued commands do not supply a rail', async t => {
  const f = fixture(t, taskSession, 'web');
  f.render(); f.transcript.resolve(savedTranscript(f.session));
  await idle(); f.render();
  f.snapshot([{ ...child, delivery: undefined }, { ...child, command_id: 'foreign', session_id: 'other' },
    { ...child, command_id: 'queued', state: 'queued' }, { ...child, command_id: 'guide', kind: 'guide' }]); f.render();
  assert.equal(f.header().delivery, null);
  assert.equal(f.header().thread.messages.at(-1).turn.reply_text, 'Token limit reached');
  assert.doesNotMatch(headerMarkup(f.header()), /delivery-stage-rail/);
});

test('an older command missing from a paged transcript cannot become the latest header delivery', async t => {
  const f = fixture(t, taskSession, 'web');
  f.render();
  const recent = savedTranscript(f.session);
  recent.messages = recent.messages.map(m => ({ ...m, message_id: m.message_id.replace('child-turn', 'followup-turn'),
    turn_id: 'followup-turn', ts: '2026-10-22T00:00:20Z',
    ...(m.role === 'assistant' ? { turn: { command_id: followupId, turn_id: 'followup-turn' }, meta: { execution_status: 'running', command_revision: 1 } } : {}) }));
  f.transcript.resolve(recent); await idle(); f.render();
  const newer = { ...child, command_id: followupId, turn_id: 'followup-turn', state: 'running', revision: 1,
    created_at: child.created_at + 20, updated_at: child.updated_at + 20, delivery: null };
  f.snapshot([child, newer]); f.render();
  assert.equal(f.header().delivery, null);
  assert.equal(f.header().thread.messages.at(-1).command_id, followupId);
});

test('projection preserves same-command delivery across result replacement and rejects other-command evidence', () => {
  const thread = { id: taskSession, messages: [], updated_ts: 0 };
  const live = commands.projectCommand(thread, { ...child, state: 'running', revision: 6 });
  const terminal = commands.projectCommand(live, { ...child, delivery: undefined, result: { reply_text: 'Final answer' } });
  assert.deepEqual(terminal.messages.at(-1).turn.delivery, delivery);
  assert.equal(terminal.messages.at(-1).turn.reply_text, 'Final answer');
  const cleared = commands.projectCommand(terminal, { ...child, revision: 8, delivery: null });
  assert.equal(cleared.messages.at(-1).turn.delivery, null, 'explicit absence remains authoritative');
  const legacyResult = commands.projectCommand(thread, { ...child, delivery: undefined, result: { delivery } });
  assert.deepEqual(legacyResult.messages.at(-1).turn.delivery, delivery, 'matching result-only evidence remains supported');
  for (const fields of [{ delivery }, { delivery: undefined, result: { command_id: childId, delivery } }, { delivery: undefined }]) {
    const newer = commands.projectCommand(live, { ...child, kind: 'resume', command_id: followupId, state: 'running',
      created_at: child.created_at + 20, revision: 1, ...fields });
    assert.equal(newer.messages.at(-1).turn.delivery, null);
  }
});

for (const code of ['model_length_limit', 'model_max_tokens', 'model_not_configured']) test('header separates ' + code + ' from missing setup without rewriting the source error', async t => {
  const f = fixture(t, taskSession, 'web');
  f.workbench.view = { session_id: taskSession, pending_interactions: [], status: {
    execution: 'blocked', waiting_for: 'configuration', completion: null, needs_attention: true,
    external: false, validation: 'unknown', turn_id: child.turn_id,
  } };
  f.render(); f.transcript.resolve(savedTranscript(f.session)); await idle(); f.render();
  f.snapshot([{ ...child, error: { code }, delivery: null }]); f.render();
  const setup = code === 'model_not_configured';
  assert.equal(f.header().workStatus.waiting_for, setup ? 'configuration' : null);
  assert.equal(f.header().workStatus.execution, 'blocked');
  assert.equal(f.header().thread.messages.at(-1).error, JSON.stringify({ code }));
  const html = headerMarkup(f.header());
  assert.match(html, setup ? /Setup needed/ : /Needs attention/);
  if (!setup) assert.doesNotMatch(html, /Setup needed/);
});

test('real max_tokens wire result keeps the header out of setup across hydration and refresh without an error code', async t => {
  // Main live evidence: error_json is null (public error: null), result.error
  // is absent. The truncation is expressed only by these two result fields.
  const result = { command_id: followupId, turn_id: 'followup-turn',
    stopped_reason: 'max_tokens', transition_reason: 'model_output_truncated' };
  const wire = { ...child, command_id: followupId, turn_id: result.turn_id, error: null, result, delivery: null };
  const f = fixture(t, taskSession, 'web');
  f.workbench.view = { session_id: taskSession, pending_interactions: [], status: {
    execution: 'blocked', waiting_for: 'configuration', completion: null, needs_attention: true,
    external: false, validation: 'unknown', turn_id: result.turn_id,
  } };
  const saved = { ok: true, ...f.session, messages: [{ role: 'assistant',
    message_id: result.turn_id + ':assistant', turn_id: result.turn_id, content: '',
    ts: f.session.updated_at, turn: result, meta: { execution_status: 'blocked', command_revision: wire.revision } }] };
  const assertHeader = () => {
    assert.equal(f.header().workStatus.waiting_for, null);
    assert.equal(f.header().workStatus.execution, 'blocked');
    const message = f.header().thread.messages.findLast(m => m.role === 'assistant');
    assert.equal(message.error, undefined);
    assert.equal(message.turn.error, undefined);
    assert.equal(message.turn.stopped_reason, 'max_tokens');
    assert.equal(message.turn.transition_reason, 'model_output_truncated');
    const html = headerMarkup(f.header());
    assert.match(html, /Needs attention/);
    assert.doesNotMatch(html, /Setup needed/);
  };
  f.render(); f.transcript.resolve(saved); await idle(); f.render();
  assertHeader();
  f.snapshot([wire]); f.render(); assertHeader();
  // Command summaries omit result; a refresh must still use the matching turn.
  const { result: _result, ...summary } = wire;
  f.snapshot([summary]); f.render();
  f.setTranscript(saved);
  window.dispatchEvent(new CustomEvent('nerya:assistant-result', { detail: { sessionId: taskSession } }));
  await idle(); f.render(); assertHeader();
  assert.equal(wire.error, null);
  assert.equal(Object.hasOwn(result, 'error'), false);
});

test('max_tokens truncation mapping preserves explicit setup errors and requires matching transition evidence', () => {
  const { taskStatus } = require('../lib/workbench.ts');
  const thread = { id: taskSession, messages: [], updated_ts: 0 };
  const view = { session_id: taskSession, status: { execution: 'blocked', waiting_for: 'configuration', turn_id: child.turn_id } };
  const wire = { ...child, error: null, result: { stopped_reason: 'max_tokens', transition_reason: 'model_output_truncated' } };
  assert.equal(taskStatus(thread, [wire], view).waiting_for, null);
  for (const command of [
    { ...wire, error: { code: 'model_not_configured' } },
    { ...wire, result: { ...wire.result, error: JSON.stringify({ code: 'model_not_configured' }) } },
    { ...wire, result: { ...wire.result, transition_reason: 'configuration_required' } },
    { ...wire, turn_id: 'other-turn' },
  ]) assert.equal(taskStatus(thread, [command], view), view.status);
});

test('model-limit status uses only matching turn and command evidence and preserves other waiting owners', () => {
  const { taskStatus } = require('../lib/workbench.ts');
  const { shortTaskStatus } = require('../components/chat/taskStatusCopy.ts');
  const thread = commands.projectCommand({ id: taskSession, messages: [], updated_ts: 0 }, child);
  const view = { session_id: taskSession, status: { execution: 'blocked', waiting_for: 'configuration', turn_id: child.turn_id } };
  const status = taskStatus(thread, [child], view);
  assert.equal(shortTaskStatus(status, false), 'Needs attention');
  assert.equal(shortTaskStatus(status, true), '需要处理');
  assert.equal(taskStatus(thread, [], view).waiting_for, null, 'saved matching error survives reload without command API');
  const resultOnly = { ...child, error: null, result: { stopped_reason: 'model_max_tokens' } };
  assert.equal(taskStatus({ ...thread, messages: [] }, [resultOnly], view).waiting_for, null);
  for (const waiting_for of ['approval', 'user']) {
    const waiting = { ...view, status: { ...view.status, waiting_for } };
    assert.equal(taskStatus(thread, [child], waiting), waiting.status);
  }
  const otherTurn = { ...view, status: { ...view.status, turn_id: 'other-turn' } };
  assert.equal(taskStatus(thread, [child], otherTurn), otherTurn.status);
  const newer = { ...child, command_id: followupId, created_at: child.created_at + 20, error: { code: 'model_not_configured' } };
  assert.equal(taskStatus(thread, [child, newer], view), view.status, 'same-turn resume must not reuse an older model error');
  const newerThread = commands.projectCommand(thread, newer);
  assert.equal(taskStatus(newerThread, [child], view), view.status, 'stale snapshot cannot classify a newer command with the same turn ID');
  assert.equal(taskStatus({ ...thread, messages: [] }, [{ ...child, session_id: 'foreign' }], view), view.status);
  assert.equal(view.status.waiting_for, 'configuration', 'server snapshot is not mutated');
});

for (const newer of [false, true]) test('late ' + (newer ? 'newer' : 'same-version') + ' metadata cannot unresolve an empty home while typing', async t => {
  const f = fixture(t);
  f.render();
  assert.equal(f.text('loadingConversation'), true);
  assert.equal(f.has('assistant-empty-conversation'), false, 'unresolved sessions are not empty homes');
  f.transcript.resolve({ ok: true, ...f.session, messages: [] });
  await idle(); f.render();
  assert.equal(f.has('assistant-empty-conversation'), true);
  const composerPath = f.composer().at;
  f.composer().node.props.onChange('First prompt'); f.render();
  f.metadata.resolve({ sessions: [{ ...f.session, ...(newer ? { updated_at: '2026-10-22T00:00:01Z' } : {}) }] });
  await idle(); f.render();
  assert.equal(f.composer().node.props.value, 'First prompt');
  assert.equal(f.posts.length, 0, 'typing never submits a command');
  assert.equal(f.text('loadingConversation'), false, 'a successful empty transcript stays loaded');
  assert.equal(f.has('assistant-empty-conversation'), true);
  assert.equal(f.composer().at, composerPath, 'the composer retains its position in the element tree');
  assert.equal(f.transcriptCalls.length, 1);
  document.dispatchEvent(new Event('visibilitychange'));
  await idle(); f.render();
  f.composer().node.props.onChange('First prompt continued'); f.render();
  assert.equal(f.composer().at, composerPath, 'focus refresh does not relocate the composer either');
  assert.equal(f.composer().node.props.value, 'First prompt continued');
  assert.equal(f.text('loadingConversation'), false);
});

test('actual transcript errors remain retryable and missing sessions do not become empty homes', async t => {
  const f = fixture(t);
  f.render(); f.metadata.resolve({ sessions: [f.session] });
  f.transcript.reject(new Error('transcript unavailable'));
  await idle(); let tree = f.render();
  assert.equal(f.text('transcriptLoadFailed'), true);
  assert.equal(f.has('assistant-empty-conversation'), false);
  f.setTranscript({ ok: false, error: 'session not found' });
  nodes(tree, node => node.type === 'button' && node.props.children === 'retry')[0].node.props.onClick();
  f.render(); await idle(); f.render();
  assert.equal(f.text('sessionNotFound'), true);
  assert.equal(f.has('assistant-empty-conversation'), false);
});

test('first effective send waits for both connections and submits once after readiness', async t => {
  const f = fixture(t);
  f.workbench.connection = 'connecting';
  f.render();
  f.transcript.resolve({ ok: true, ...f.session, messages: [] });
  f.metadata.resolve({ sessions: [f.session] });
  await idle(); f.render();
  f.composer().node.props.onChange('First ready prompt'); f.render();
  for (const [workbench, engine] of [['connecting', 'online'], ['offline', 'online'], ['online', 'connecting']]) {
    f.workbench.connection = workbench; f.engine.connection = engine; f.render();
    assert.equal(f.composer().node.props.locked, true, `${workbench}/${engine} must not advertise readiness`);
    assert.equal(f.composer().node.props.lockMessage, 'copy.components_chat_ChatView.' + (workbench === 'connecting' || engine === 'connecting' ? '036' : '037'));
    f.composer().node.props.onSend();
    assert.equal(f.posts.length, 0);
    assert.deepEqual(commands.pendingCommands(f.session.session_id), []);
    assert.equal(f.composer().node.props.value, 'First ready prompt');
  }
  f.workbench.connection = 'online'; f.engine.connection = 'online'; f.render();
  assert.equal(f.composer().node.props.locked, false);
  assert.equal(f.posts.length, 0, 'readiness never auto-submits the retained draft');
  const send = f.composer().node.props.onSend;
  send();
  assert.equal(f.posts.length, 1, 'the first effective click submits immediately');
  send(); f.render();
  assert.equal(f.posts.length, 1, 'an in-flight click cannot create another command');
  const post = f.posts[0], pending = post.options.body;
  assert.equal(post.url, '/agent/commands');
  assert.equal(pending.request.payload.text, 'First ready prompt');
  assert.equal(commands.pendingCommands(f.session.session_id)[0].command_id, pending.command_id);
  assert.equal(f.composer().node.props.value, 'First ready prompt', 'draft stays until ACK');
  post.resolve({ ok: true, command: { command_id: pending.command_id, session_id: pending.session_id, kind: 'send',
    state: 'running', revision: 1, turn_id: 'ready-turn', input: 'First ready prompt', attachments: [], context: {}, created_at: 1, updated_at: 1 } });
  await idle(); f.render();
  assert.equal(f.posts.length, 1);
  assert.deepEqual(commands.pendingCommands(pending.session_id), []);
  assert.equal(f.composer().node.props.value, '');
});

for (const sessionId of ['assistant_empty', null]) test((sessionId ? 'empty assistant home' : 'new task') + ' sends once and retains edits made before ACK', async t => {
  const f = fixture(t, sessionId);
  f.render();
  f.transcript.resolve({ ok: true, ...f.session, messages: [] });
  await idle(); let tree = f.render();
  f.metadata.resolve({ sessions: sessionId ? [f.session] : [] });
  await idle(); tree = f.render();
  nodes(tree, node => node.type === 'AssistantChatHeader')[0].node.props.onIdentity({ id: 'fixture-assistant', name: 'Fixture' }, sessionId ? 'assistant' : 'task');
  f.render();
  f.composer().node.props.onChange('First prompt'); f.render();
  const sid = f.composer().node.props.sessionId;
  assert.equal(f.posts.length, 0);
  const send = f.composer().node.props.onSend;
  send(); send(); f.render();
  assert.equal(f.posts.length, 1, 'two Send callbacks before admission share the in-flight guard');
  const post = f.posts[0], pending = post.options.body;
  assert.equal(post.url, '/agent/commands');
  assert.equal(pending.session_id, sid);
  assert.equal(pending.request.assistant_id, 'fixture-assistant');
  assert.equal(pending.request.conversation_kind, sessionId ? 'assistant' : 'task');
  assert.equal(commands.pendingCommands(sid)[0].command_id, pending.command_id);
  f.composer().node.props.onChange('Draft written during send'); f.render();
  post.resolve({ ok: true, command: { command_id: pending.command_id, session_id: sid, kind: 'send',
    state: 'running', revision: 1, turn_id: 'first-turn', input: 'First prompt', attachments: [], context: {}, created_at: 1, updated_at: 1 } });
  await idle(); f.render();
  assert.equal(f.posts.length, 1);
  assert.deepEqual(commands.pendingCommands(sid), []);
  assert.equal(f.composer().node.props.value, 'Draft written during send');
  assert.deepEqual(f.routes, sessionId ? [] : ['/chat/' + sid]);
});

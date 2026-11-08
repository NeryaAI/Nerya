// Real hooks and projections, fake API only. No runtime/browser/network.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), Module = require('node:module');
const ts = require('typescript');
const compilerOptions = { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true };
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), { compilerOptions }).outputText, f);
require.extensions['.css'] = m => { m.exports = {}; };
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
const { AssistantBubble } = require('../components/chat/ChatMessage.tsx');
const localeMessages = require('../messages/index.ts');
const api = require('../lib/clientApi.ts');
const identity = require('../lib/workspaceIdentity.ts');
const { projectCommand } = require('../lib/conversationCommands.ts');
const { taskStatus } = require('../lib/workbench.ts');
const { projectMessageBlocks } = require('../lib/streamTranscript.ts');
const { executionSteps } = require('../lib/executionTimeline.ts');
const { toolPresentation } = require('../lib/agentConversation.ts');
const flush = () => new Promise(resolve => setImmediate(resolve));
const sid = 'taskchat_4ac11951e2a3f42047a2cbb1';
const cid = '332b134c-0e8d-42c9-ba08-12450ea8d7c6';
const tid = 'turn_fc7de3d0c580460d8ca1d5467ad010e1';
const running = { command_id: cid, session_id: sid, turn_id: tid, kind: 'send', state: 'running',
  revision: 2, created_at: 1793244900, updated_at: 1793244901, input: 'LP', attachments: [], context: {} };
const terminal = { ...running, state: 'succeeded', revision: 3, updated_at: 1793244988.661582, has_result: true };
const toolStart = { block: { kind: 'tool_use', call_id: 'validate-lp', action: 'strategy_validate' } };
const result = { turn_id: tid, stopped_reason: 'end_turn', reply_text: 'LP validation is incomplete.',
  delivery: { command_id: cid, completion: { allowed: false, status: 'incomplete', reasons: ['request_scope_unverified'] } },
  verifier_outcome: { hard_status: 'failed' }, blocks: [toolStart, { block: { kind: 'tool_result',
    call_id: 'validate-lp', action: 'strategy_validate', ok: false, error: 'incomplete_response' } }] };
const snapshot = command => ({ ok: true, commands: [command], queue: { paused: true, pause_reason: '', revision: 1 } });
const view = execution => ({ ok: true, session_id: sid, revision: 'unchanged-view-revision',
  status: { execution, completion: execution === 'succeeded' ? 'turn_finished' : null,
    validation: execution === 'succeeded' ? 'failed' : 'unknown', turn_id: tid, needs_attention: false, waiting_for: null },
  queue: { count: 0, paused: true }, available_actions: { send: true, stop: execution === 'running', guide: false } });

function fixture(transport) {
  const saved = { callApi: api.callApi, window: global.window, document: global.document, fetch: global.fetch };
  global.window = new EventTarget();
  global.document = Object.assign(new EventTarget(), { hidden: false });
  global.fetch = () => { throw new Error('network forbidden'); };
  identity.setWorkspaceIdentity('terminal-render-fixture');
  const timers = new Map(), requests = [], observers = [];
  let nextTimer = 0;
  api.callApi = (url, options) => { const request = { url, options }; requests.push(request); return transport(request); };
  function observer(file, exported, project = () => {}) {
    const slots = [], effects = [];
    let cursor = 0;
    const hooks = {
      useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = initial;
        return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
      useRef(initial) { return slots[cursor++] ||= { current: initial }; },
      useEffect(fn, deps) { const i = cursor++, old = slots[i];
        if (!old || deps.some((value, j) => value !== old.deps[j])) {
          old?.cleanup?.(); slots[i] = { deps }; effects.push(() => { slots[i].cleanup = fn(); });
        }
      },
    };
    const filename = path.resolve(__dirname, '../components/chat/' + file + '.ts');
    const m = { exports: {} }, realRequire = Module.createRequire(filename);
    vm.runInNewContext(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions }).outputText, {
      module: m, exports: m.exports, require: name => name === 'react' ? hooks : name.endsWith('/workspaceIdentity')
        ? { ...identity, useWorkspaceIdentity: identity.getWorkspaceIdentity } : realRequire(name),
      AbortController, window, document,
      setTimeout(fn, delay) { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
      clearTimeout(id) { timers.delete(id); },
    }, { filename });
    const handle = { render(session = sid) { cursor = 0; const state = m.exports[exported](session, project);
      effects.splice(0).forEach(fn => fn()); return state; },
    dispose() { slots.forEach(value => value?.cleanup?.()); } };
    observers.push(handle); return handle;
  }
  return { requests, observer,
    async poll() { const batch = [...timers.entries()]; assert.ok(batch.length, 'poll remains scheduled');
      for (const [id, timer] of batch) { timers.delete(id); timer.fn(); } await flush(); },
    dispose() { observers.forEach(observer => observer.dispose()); api.callApi = saved.callApi;
      identity.setWorkspaceIdentity(null); Object.assign(global, { window: saved.window, document: saved.document, fetch: saved.fetch }); },
  };
}
const isResult = request => request.url.startsWith('/agent/commands?') && request.url.includes('command_id=');
const targetAssistant = thread => thread.messages.find(message => message.role === 'assistant' && message.command_id === cid);

test('terminal replay projects the producer final before result or transcript hydration', () => {
  const { finalReplyText } = require('../lib/chatResults.ts');
  const finalText = 'Full producer final paragraph. Validation remains incomplete.';
  const receipt = result.blocks[1];
  const events = [
    { kind: 'message.delta', seq: 10, session_id: sid, turn_id: tid, stream_id: 'final-stream', text: finalText, completed: true },
    { kind: 'turn.complete', seq: 11, session_id: sid, turn_id: tid, stop_reason: 'end_turn', final_text: finalText },
  ];
  const source = { id: sid, draft: 'keep draft', messages: [{ id: 'stream-anchor', role: 'assistant', ts: 1234,
    command_id: cid, backend_message_id: tid + ':assistant', loading: true,
    turn: { turn_id: tid, blocks: [receipt] } }] };
  const before = JSON.stringify(source);
  const active = projectCommand(source, running, events);
  assert.equal(finalReplyText(targetAssistant(active)), '', 'terminal event must not end an active command');
  const projected = projectCommand(active, { ...terminal, has_result: false });
  const assistant = targetAssistant(projected);
  assert.equal(assistant.loading, false);
  assert.equal(finalReplyText(assistant), finalText, 'explicit producer final must not wait for a transcript reload');
  assert.equal(assistant.id, 'stream-anchor');
  assert.equal(assistant.ts, 1234);
  assert.equal(projected.draft, source.draft);
  assert.deepEqual(assistant.live_events, events);
  assert.deepEqual(assistant.turn.blocks, [receipt]);
  assert.equal(JSON.stringify(source), before);
  const html = renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale: 'zh', messages: localeMessages.zh, timeZone: 'UTC' },
    React.createElement(AssistantBubble, { msg: assistant })));
  assert.ok(html.includes('data-turn-loading="false"'));
  assert.ok(html.includes('data-testid="result-actions"'));
  assert.equal(finalReplyText(targetAssistant(projectCommand(projected, terminal))), finalText, 'summary replay retains the final');
  const hydrated = projectCommand(projected, { ...terminal, result });
  assert.equal(finalReplyText(targetAssistant(hydrated)), result.reply_text, 'hydrated result remains authoritative');
  assert.deepEqual(targetAssistant(hydrated).turn.delivery, result.delivery);
  for (const invalid of [events.slice(0, 1), [ { ...events[1], turn_id: 'other-turn' } ],
    [ { ...events[1], session_id: 'other-session' } ], [ { ...events[1], command_id: 'other-command' } ]]) {
    assert.equal(finalReplyText(targetAssistant(projectCommand(source, terminal, invalid))), '', 'narration and foreign terminal events are not finals');
  }
  const resumed = { ...terminal, command_id: 'resumed-command', kind: 'resume', created_at: terminal.created_at + 1 };
  assert.equal(finalReplyText(projectCommand(active, resumed).messages.find(message => message.command_id === resumed.command_id)), '',
    'a resumed command cannot inherit the earlier command terminal event');
  const outcome = { kind: 'command.outcome', seq: 12, session_id: sid, turn_id: tid, command_id: cid,
    execution_status: 'succeeded', result };
  assert.equal(finalReplyText(targetAssistant(projectCommand(source, terminal, [outcome]))), result.reply_text,
    'durable command outcome is also authoritative before the result GET');
});

test('session view adopts terminal status and actions even when its revision is unchanged', async () => {
  let current = view('running');
  const runtime = { protocol_version: 1, workspace_id: 'terminal-render-fixture', capabilities: ['conversation_commands', 'session_view'] };
  const f = fixture(request => Promise.resolve(request.url === '/runtime/info' ? runtime : current));
  try {
    const workbench = f.observer('useWorkbench', 'useWorkbench');
    workbench.render(); await flush();
    assert.equal(taskStatus({ id: sid, messages: [] }, [], workbench.render().view).execution, 'running');
    current = view('succeeded'); await f.poll();
    const state = workbench.render();
    assert.equal(taskStatus({ id: sid, messages: [] }, [], state.view).execution, 'succeeded');
    assert.equal(state.view.status.validation, 'failed');
    assert.equal(state.view.available_actions.stop, false);
    assert.equal(state.view.available_actions.send, true);
  } finally { f.dispose(); }
});

test('terminal result stays authoritative after late transcript hydration and subsequent summary polls', async () => {
  let current = running, resolveResult;
  const history = Array.from({ length: 16 }, (_, index) => ({ ...terminal,
    command_id: 'history-' + index, turn_id: 'history-turn-' + index,
    created_at: running.created_at - index - 1, has_result: false }));
  const f = fixture(request => {
    if (isResult(request)) return new Promise(resolve => { resolveResult = resolve; });
    if (request.url.startsWith('/agent/commands/events?')) return Promise.resolve({ ok: true, events: [], cursor: 0, has_more: false });
    return Promise.resolve({ ...snapshot(current), commands: [current, ...history],
      queue: { paused: 1, pause_reason: 'restarted', revision: 36 } });
  });
  const staleThread = { id: sid, messages: [{ id: 'saved-assistant', role: 'assistant', ts: running.created_at * 1000,
    backend_message_id: tid + ':assistant', loading: true, turn: { turn_id: tid, blocks: [toolStart] } }], updated_ts: 0 };
  let thread = staleThread;
  try {
    const engine = f.observer('useConversationCommands', 'useConversationCommands', (command, events) => { thread = projectCommand(thread, command, events); });
    engine.render(); await flush();
    const pending = executionSteps(projectMessageBlocks(targetAssistant(thread)))[0];
    assert.equal(toolPresentation(pending, 'running', true).pending, true, 'live tool is retained until authoritative terminal evidence');
    current = terminal; await f.poll();
    assert.equal(engine.render().active, undefined);
    assert.equal(targetAssistant(thread).loading, false);
    resolveResult({ ok: true, command: { ...terminal, result } }); await flush();
    assert.equal(targetAssistant(thread).turn.reply_text, result.reply_text);
    // ChatView reapplies these commands when a late transcript replaces its callback-owned thread.
    const render = () => engine.render().commands.reduce((value, command) => projectCommand(value, command), staleThread);
    const check = () => {
      const projected = render(), assistant = targetAssistant(projected);
      assert.equal(assistant.loading, false);
      assert.equal(assistant.turn.reply_text, result.reply_text, 'rendered commands must retain the hydrated terminal result');
      assert.equal(assistant.turn.verifier_outcome.hard_status, 'failed');
      assert.equal(assistant.turn.delivery.completion.allowed, false);
      assert.equal(assistant.turn.delivery.completion.status, 'incomplete');
      assert.equal(toolPresentation(executionSteps(projectMessageBlocks(assistant))[0], assistant.execution_status, true).failed, true);
      assert.equal(projected.messages.filter(message => message.role === 'assistant' && message.command_id === cid).length, 1);
      assert.equal(assistant.id, 'saved-assistant', 'read anchor is preserved');
      assert.equal(assistant.ts, staleThread.messages[0].ts);
    };
    check(); await f.poll(); check();
    assert.equal(engine.render().commands.length, 17);
    assert.equal(engine.render().data.queue.revision, 36);
    assert.equal(f.requests.filter(isResult).length, 1, 'summary polling must not redownload a drained result');
    assert.ok(f.requests.every(request => request.options.method === 'GET'), 'hydration never submits a fresh command');
    engine.dispose();
    const freshRoute = f.observer('useConversationCommands', 'useConversationCommands');
    freshRoute.render(); await flush();
    assert.equal(freshRoute.render().active, undefined);
    assert.equal(freshRoute.render().commands.find(command => command.command_id === cid).state, 'succeeded');
    assert.ok(f.requests.every(request => request.options.method === 'GET'), 'fresh route observes the existing command without resubmission');
  } finally { f.dispose(); await flush(); }
});

test('revision churn, late event pages and a missing terminal result cannot drain final hydration', async () => {
  let current = running, resolveOldPage, resultReads = 0;
  const actions = ['read_file', 'write_file', 'strategy_validate', 'delivery_finalize', 'delivery_finalize', 'delivery_finalize', 'delivery_finalize'];
  const blocks = actions.flatMap((action, index) => [
    { block: { kind: 'tool_use', call_id: 'call-' + index, action } },
    { block: { kind: 'tool_result', call_id: 'call-' + index, action, ok: index < 2,
      result: index < 2 ? { saved: true } : { error: index === 2 ? 'request_scope_unverified' : 'revision_conflict' } } },
  ]);
  const finalResult = { ...result, blocks, reply_text: 'Validation failed the requested scope. Delivery remains incomplete. Full producer paragraph.' };
  const staleThread = { id: sid, messages: [{ id: 'read-anchor', role: 'assistant', ts: 1234,
    backend_message_id: tid + ':assistant', loading: true,
    live_events: [{ kind: 'message.delta', seq: 20, stream_id: 'final-preview', text: 'Validation failed the request', completed: false },
      ...actions.map((action, index) => ({ kind: 'tool.start', seq: index + 1, call_id: 'call-' + index, action }))],
    turn: { turn_id: tid, reply_text: 'Validation failed the request', blocks: [toolStart] } }],
    updated_ts: 0, draft: 'keep my current draft' };
  let thread = staleThread;
  const f = fixture(request => {
    if (isResult(request)) return Promise.resolve({ ok: true, command: { ...current, result: ++resultReads === 1 ? null : finalResult } });
    if (request.url.startsWith('/agent/commands/events?')) {
      if (current.state === 'running') {
        const after = new URL(request.url, 'http://fixture').searchParams.get('after_seq');
        if (after === '0') return Promise.resolve({ ok: true, events: [{ kind: 'tool.start', seq: 1, call_id: 'call-2', action: 'strategy_validate' }], cursor: 1, has_more: true });
        return new Promise(resolve => { resolveOldPage = resolve; });
      }
      return Promise.resolve({ ok: true, events: [], cursor: 7, has_more: false });
    }
    return Promise.resolve(snapshot(current));
  });
  try {
    const engine = f.observer('useConversationCommands', 'useConversationCommands', (command, events) => { thread = projectCommand(thread, command, events); });
    engine.render(); await flush();
    current = { ...running, revision: 6 }; await f.poll();
    assert.equal(engine.render().active.state, 'running', 'delivery updates cannot conceal genuinely active execution');
    current = { ...terminal, revision: 7 }; await f.poll();
    resolveOldPage({ ok: true, events: [{ kind: 'message.delta', seq: 2, text: ' stale page' }], cursor: 2, has_more: false });
    await flush();
    assert.equal(engine.render().active, undefined);
    assert.equal(targetAssistant(thread).loading, false);
    assert.equal(targetAssistant(thread).turn.reply_text, staleThread.messages[0].turn.reply_text, 'no synthetic final while producer result is missing');
    await f.poll();
    assert.equal(resultReads, 2, 'has_result without a hydrated result must be retried even after events end at the same revision');
    const rendered = engine.render().commands.reduce((value, command) => projectCommand(value, command), staleThread);
    const assistant = targetAssistant(rendered);
    assert.equal(assistant.turn.reply_text, finalResult.reply_text);
    assert.equal(assistant.turn.delivery.completion.status, 'incomplete');
    assert.equal(assistant.turn.verifier_outcome.hard_status, 'failed');
    const steps = executionSteps(projectMessageBlocks(assistant));
    assert.equal(steps.length, 7);
    assert.ok(steps.every(step => !toolPresentation(step, assistant.execution_status, true).pending));
    assert.equal(assistant.id, 'read-anchor');
    assert.equal(assistant.ts, 1234);
    assert.equal(rendered.draft, 'keep my current draft');
    const html = renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale: 'zh', messages: localeMessages.zh, timeZone: 'UTC' },
      React.createElement(AssistantBubble, { msg: assistant })));
    assert.ok(html.includes(finalResult.reply_text), 'actual reply renderer must show the full producer paragraph over a retained live preview');
    assert.ok(html.includes('data-turn-loading="false"'));
    assert.ok(html.includes('aria-busy="false"'));
    assert.ok(html.includes('data-testid="result-actions"'));
    assert.ok(f.requests.every(request => request.options.method === 'GET'));
  } finally { f.dispose(); await flush(); }
});

test('result availability can advance after terminal events drain without changing command revision', async () => {
  let current = { ...terminal, has_result: false };
  const f = fixture(request => Promise.resolve(isResult(request) ? { ok: true, command: { ...terminal, result } }
    : request.url.startsWith('/agent/commands/events?') ? { ok: true, events: [], cursor: 30, has_more: false } : snapshot(current)));
  try {
    const engine = f.observer('useConversationCommands', 'useConversationCommands');
    engine.render(); await flush();
    assert.equal(engine.render().active, undefined);
    current = terminal; await f.poll();
    assert.equal(f.requests.filter(isResult).length, 1, 'a newly advertised result needs hydration even if execution revision is already seen');
    assert.equal(engine.render().commands[0].result.reply_text, result.reply_text);
  } finally { f.dispose(); await flush(); }
});

test('running event hydration survives late transcript replacement and subsequent cursor polls', async () => {
  let current = running;
  const event = { kind: 'tool.complete', seq: 9, call_id: 'completed-backtest', action: 'strategy_backtest', ok: true,
    result: { kind: 'freeform_backtest', strategy_id: 'fixture', backtest_ts: 'fixture' } };
  const f = fixture(request => request.url.startsWith('/agent/commands/events?')
    ? Promise.resolve({ ok: true, events: request.url.includes('after_seq=0&') ? [event] : [], cursor: 9, has_more: false })
    : Promise.resolve(snapshot(current)));
  let thread = { id: sid, messages: [] };
  try {
    const engine = f.observer('useConversationCommands', 'useConversationCommands', (command, events) => {
      thread = projectCommand(thread, command, events);
    });
    engine.render(); await flush();
    assert.deepEqual(targetAssistant(thread).live_events, [event]);
    const transcript = { id: sid, messages: [{ id: 'history-anchor', role: 'assistant', ts: 1234,
      backend_message_id: tid + ':assistant', turn: { turn_id: tid, blocks: [] } }] };
    const reproject = () => engine.render().commands.reduce((value, command) => projectCommand(value, command), transcript);
    assert.deepEqual(targetAssistant(reproject()).live_events, [event], 'late transcript must not discard events already consumed by the observer cursor');
    current = { ...running, revision: running.revision + 1 };
    await f.poll();
    assert.deepEqual(targetAssistant(reproject()).live_events, [event]);
    assert.equal(targetAssistant(reproject()).loading, true);
    assert.equal(targetAssistant(reproject()).id, 'history-anchor');
  } finally { f.dispose(); await flush(); }
});

test('fresh route rehydrates terminal tool events when the result is already present', async () => {
  const event = { kind: 'tool.complete', seq: 9, call_id: 'backtest-2', action: 'strategy_backtest', ok: false };
  const current = { ...terminal, result: { ...result, blocks: [] } };
  const f = fixture(request => request.url.startsWith('/agent/commands/events?')
    ? Promise.resolve({ ok: true, events: [event], cursor: 9, has_more: false })
    : Promise.resolve(snapshot(current)));
  let thread = { id: sid, messages: [] };
  try {
    const engine = f.observer('useConversationCommands', 'useConversationCommands', (command, events) => {
      thread = projectCommand(thread, command, events);
    });
    engine.render(); await flush();
    const command = engine.render().commands[0];
    assert.equal(command.result.reply_text, result.reply_text);
    assert.deepEqual(targetAssistant(thread).live_events, [event]);
    assert.equal(f.requests.filter(request => request.url.startsWith('/agent/commands/events?')).length, 1);
  } finally { f.dispose(); await flush(); }
});

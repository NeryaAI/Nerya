// Real observer + command projection; deferred transport and clock, no network/browser.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const vm = require('node:vm');
const ts = require('typescript');
const compilerOptions = { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true };
require.extensions['.ts'] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), { compilerOptions }).outputText, f);
const api = require('../lib/clientApi.ts');
const identity = require('../lib/workspaceIdentity.ts');
const { projectCommand } = require('../lib/conversationCommands.ts');
const flush = () => new Promise(resolve => setImmediate(resolve));
const command = { command_id: '71cd376e-ce33-4884-8dc1-5b663ab1e3e9', session_id: 'lp-session',
  turn_id: 'turn_09e990053de6485e937ce31877784fb8', kind: 'send', state: 'running', revision: 7,
  created_at: 1793229900, updated_at: 1793229901, input: 'LP guided command', attachments: [], context: {} };
const terminal = { ...command, state: 'succeeded', revision: 8, updated_at: 1793229966, has_result: true,
  delivery: { command_id: command.command_id, completion: { allowed: false }, status: 'incomplete', revision: 4 } };
const snapshot = commands => ({ ok: true, commands, queue: { paused: 1, pause_reason: 'restarted', revision: 16 } });

function fixture(transport) {
  const slots = [], effects = [], timers = new Map(), requests = [];
  let cursor = 0, session = command.session_id, nextTimer = 0;
  let thread = { id: session, messages: [], updated_ts: 0 };
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
  const saved = { callApi: api.callApi, window: global.window, document: global.document, fetch: global.fetch };
  global.window = new EventTarget(); global.document = Object.assign(new EventTarget(), { hidden: false });
  global.fetch = () => { throw new Error('network forbidden'); };
  identity.setWorkspaceIdentity('terminal-sync-workspace');
  api.callApi = (url, options) => {
    const request = { url, options }; requests.push(request);
    return transport(request);
  };
  const filename = path.resolve(__dirname, '../components/chat/useConversationCommands.ts');
  const m = { exports: {} }, realRequire = Module.createRequire(filename);
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions }).outputText, {
    module: m, exports: m.exports, require: name => name === 'react' ? hooks : name.endsWith('/workspaceIdentity')
      ? { ...identity, useWorkspaceIdentity: identity.getWorkspaceIdentity } : realRequire(name),
    AbortController, window, document,
    setTimeout(fn, delay) { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  }, { filename });
  return {
    requests,
    render(nextSession = session) { session = nextSession; cursor = 0;
      const result = m.exports.useConversationCommands(session, (row, events) => { thread = projectCommand(thread, row, events); });
      effects.splice(0).forEach(fn => fn()); return result; },
    assistant: () => thread.messages.find(message => message.role === 'assistant' && message.command_id === command.command_id),
    async poll() {
      const timer = [...timers.entries()].find(([, value]) => value.delay <= 2000);
      assert.ok(timer, 'summary poll must be scheduled independently of slow event/result hydration');
      timers.delete(timer[0]); timer[1].fn(); await flush();
    },
    dispose() { slots.forEach(value => value?.cleanup?.()); api.callApi = saved.callApi;
      identity.setWorkspaceIdentity(null); Object.assign(global, { window: saved.window, document: saved.document, fetch: saved.fetch }); },
  };
}
function hold(request) {
  return new Promise((resolve, reject) => {
    request.resolve = resolve;
    request.options.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
  });
}
const isEvents = request => request.url.startsWith('/agent/commands/events?');
const isResult = request => new URL(request.url, 'http://fixture').searchParams.has('command_id') && !isEvents(request);

test('a terminal summary clears loading while the previous running event page and large result are held', async () => {
  let current = command;
  const f = fixture(request => isEvents(request) || isResult(request) ? hold(request) : Promise.resolve(snapshot([current])));
  try {
    f.render(); await flush();
    assert.equal(f.assistant().loading, true);
    const slowEvents = f.requests.find(isEvents);
    assert.ok(slowEvents);
    current = terminal;
    await f.poll();
    assert.equal(f.render().active, undefined, 'terminal snapshot releases the stop/composer running state');
    assert.equal(f.assistant().loading, false, 'terminal execution must not await event draining or result hydration');
    assert.equal(f.assistant().execution_status, 'succeeded');
    assert.equal(f.assistant().turn.delivery.completion.allowed, false);
    assert.equal(f.assistant().turn.delivery.status, 'incomplete');
    assert.equal(f.assistant().turn.reply_text, undefined, 'summary cannot synthesize a final answer');
    assert.equal(slowEvents.options.signal.aborted, true, 'new revision aborts old event hydration');
    const slowResult = f.requests.find(isResult);
    assert.ok(slowResult);
    slowEvents.resolve({ ok: true, events: [{ kind: 'message.delta', seq: 1044, text: 'stale running fragment' }], cursor: 1044, has_more: true });
    await flush();
    assert.equal(f.assistant().loading, false);
    assert.equal(f.assistant().live_events.length, 0, 'superseded event request cannot project');
    slowResult.resolve({ ok: true, command: { ...terminal, result: { reply_text: 'Saved final response', diagnostic: 'x'.repeat(1044841) } } });
    await flush();
    assert.equal(f.assistant().turn.reply_text, 'Saved final response');
    assert.equal(f.assistant().turn.delivery.completion.allowed, false);
  } finally { f.dispose(); await flush(); }
});

test('all terminal summaries progress with many held historical results and bounded hydration', async () => {
  const history = Array.from({ length: 8 }, (_, i) => ({ ...terminal, command_id: 'history-' + i,
    turn_id: 'history-turn-' + i, created_at: command.created_at - 100 - i, state: 'interrupted' }));
  const f = fixture(request => isResult(request) ? hold(request) : Promise.resolve(snapshot([...history, terminal])));
  try {
    f.render(); await flush();
    assert.equal(f.assistant().loading, false);
    assert.equal(f.assistant().turn.delivery.completion.allowed, false);
    assert.equal(f.requests.filter(isResult).length, 2, 'history cannot saturate transport and starve summary polls');
    assert.ok(f.requests.some(request => isResult(request) && request.url.includes(command.command_id)), 'latest result has priority over held historical results');
    await f.poll();
    assert.equal(f.requests.filter(isResult).length, 2, 'same-revision hydration must not multiply on each poll');
    assert.equal(f.render().data.queue.pause_reason, 'restarted');
  } finally { f.dispose(); await flush(); }
});

test('a new running command preempts two held older results and later adopts its terminal summary', async () => {
  const history = [0, 1].map(i => ({ ...terminal, command_id: 'held-' + i, turn_id: 'held-turn-' + i,
    created_at: command.created_at - 100 - i, state: 'interrupted' }));
  let rows = history;
  const f = fixture(request => isEvents(request) || isResult(request) ? hold(request) : Promise.resolve(snapshot(rows)));
  try {
    f.render(); await flush();
    const older = f.requests.filter(isResult);
    assert.equal(older.length, 2);
    rows = [...history, command];
    await f.poll();
    assert.equal(f.assistant().loading, true);
    assert.ok(f.requests.some(isEvents), 'running command obtains hydration capacity despite both older slots being held');
    assert.equal(older.filter(request => request.options.signal.aborted).length, 1);
    rows = [...history, terminal];
    await f.poll();
    assert.equal(f.assistant().loading, false);
    assert.equal(f.assistant().turn.delivery.completion.allowed, false);
    assert.ok(f.requests.some(request => isResult(request) && request.url.includes(command.command_id)));
    assert.equal(f.requests.filter(request => (isEvents(request) || isResult(request)) && !request.options.signal.aborted).length, 2);
  } finally { f.dispose(); await flush(); }
});

test('older detail is retried, newer terminal detail survives stale summary replay', async () => {
  const f = fixture(request => isResult(request) ? hold(request) : Promise.resolve(snapshot([terminal])));
  try {
    f.render(); await flush();
    f.requests.find(isResult).resolve({ ok: true, command: { ...command, result: { reply_text: 'stale reply' } } });
    await flush();
    assert.equal(f.assistant().execution_status, 'succeeded');
    assert.equal(f.assistant().turn.reply_text, undefined);
    await f.poll();
    assert.equal(f.requests.filter(isResult).length, 2, 'revision mismatch cannot mark result hydration drained');
    const advanced = { ...terminal, revision: terminal.revision + 1, updated_at: terminal.updated_at + 1,
      result: { reply_text: 'confirmed reply' } };
    f.requests.filter(isResult).at(-1).resolve({ ok: true, command: advanced });
    await flush();
    assert.equal(f.assistant().turn.reply_text, 'confirmed reply');
    assert.equal(f.render().commands[0].revision, advanced.revision, 'GET may advance beyond its initiating summary');
    await f.poll();
    const state = f.render();
    assert.equal(state.commands[0].revision, advanced.revision, 'older summary cannot erase accepted detail');
    assert.equal(state.commands[0].result.reply_text, 'confirmed reply');
    assert.equal(state.active, undefined);
    assert.equal(f.assistant().loading, false);
    assert.equal(f.requests.filter(isResult).length, 2, 'accepted detail does not need another result request');
  } finally { f.dispose(); await flush(); }
  const outcome = { kind: 'command.outcome', seq: 500, session_id: command.session_id,
    command_id: command.command_id, turn_id: command.turn_id, execution_status: 'succeeded' };
  const live = fixture(request => Promise.resolve(isResult(request)
    ? { ok: true, command: { ...terminal, result: { reply_text: 'durable terminal reply' } } }
    : isEvents(request) ? { ok: true, events: [outcome], cursor: 500, has_more: false }
    : snapshot([command])));
  try {
    live.render(); await flush();
    assert.equal(live.assistant().turn.reply_text, 'durable terminal reply', 'terminal replay hydrates top-level GET result despite a running summary');
    assert.equal(live.assistant().loading, false);
    assert.equal(live.render().active, undefined);
    await live.poll();
    assert.equal(live.render().active, undefined, 'stale running summary cannot re-enable stop');
    assert.equal(live.render().commands[0].result.reply_text, 'durable terminal reply');
    assert.equal(live.requests.filter(isResult).length, 1, 'terminal event triggers one detail hydration, without extra polling');
  } finally { live.dispose(); await flush(); }
});

for (const change of ['session', 'workspace ABA']) test('late hydration cannot cross ' + change, async () => {
  const f = fixture(request => isResult(request) ? hold(request) : Promise.resolve(snapshot([terminal])));
  try {
    f.render(); await flush();
    const request = f.requests.find(isResult), before = f.assistant();
    if (change === 'session') f.render('another-session');
    else { identity.setWorkspaceIdentity('other-workspace'); identity.setWorkspaceIdentity('terminal-sync-workspace'); f.render(); }
    assert.equal(request.options.signal.aborted, true);
    request.resolve({ ok: true, command: { ...terminal, result: { reply_text: 'late reply' } } });
    await flush();
    assert.equal(f.assistant().turn.reply_text, undefined);
    if (change === 'session') assert.equal(f.assistant(), before);
  } finally { f.dispose(); await flush(); }
});

for (const state of ['unconfirmed', 'awaiting_approval', 'awaiting_input']) test(state + ' summary preserves its exact state while result is held', async () => {
  const f = fixture(request => isResult(request) ? hold(request) : Promise.resolve(snapshot([{ ...terminal, state }])));
  try {
    f.render(); await flush();
    assert.equal(f.assistant().loading, false);
    assert.equal(f.assistant().execution_status, state);
    assert.equal(f.assistant().turn.delivery.completion.allowed, false);
    assert.equal(f.assistant().turn.reply_text, undefined);
    if (state === 'unconfirmed') assert.match(f.assistant().error, /execution_unconfirmed/);
    assert.ok(f.requests.every(request => request.options.method === 'GET'), 'observer cannot approve, resend or resume');
  } finally { f.dispose(); await flush(); }
});

test('an older command result cannot delay another command terminal summary', async () => {
  const older = { ...terminal, command_id: 'older-command', turn_id: 'older-turn', created_at: command.created_at - 100 };
  const f = fixture(request => isResult(request) ? hold(request) : Promise.resolve(snapshot([older, terminal])));
  try {
    f.render(); await flush();
    assert.ok(f.assistant(), 'current terminal summary must project before an older result hydrates');
    assert.ok(f.requests.some(request => isResult(request) && request.url.includes(command.command_id)),
      'current command hydration must start despite an older held result');
    assert.equal(f.assistant().loading, false);
    assert.equal(f.render().active, undefined);
  } finally { f.dispose(); await flush(); }
});

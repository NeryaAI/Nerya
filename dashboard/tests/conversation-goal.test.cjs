// Offline protocol, real component rendering/actions, and command-observer tests.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, f);
require.extensions['.css'] = m => { m.exports = new Proxy({}, { get: (_, key) => String(key) }); };
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const api = require('../lib/clientApi.ts');
const identity = require('../lib/workspaceIdentity.ts');
const goals = require('../lib/conversationGoal.ts');
const { useConversationCommands } = require('../components/chat/useConversationCommands.ts');
let locale = 'en';
const load = Module._load;
Module._load = function (name, ...args) {
  if (name === 'next-intl') return { useLocale: () => locale, useTranslations: namespace => (key, values = {}) => {
    const text = [namespace, ...key.split('.')].reduce((value, part) => value?.[part], require('../messages/' + locale + '/chat.json'));
    assert.equal(typeof text, 'string', 'missing locale: ' + locale + '.' + key);
    return text.replace(/\{(\w+)\}/g, (_, key) => String(values[key] ?? ''));
  } };
  return load.call(this, name, ...args);
};
const { GoalStatus } = require('../components/chat/GoalStatus.tsx');
Module._load = load;

const fixture = (extra = {}) => ({ protocol: 'nerya.goal.v1', session_id: 's1', goal_id: 'g1',
  objective: 'Verify the result and deliver the evidence', status: 'active', revision: 7,
  token_budget: null, tokens_used: 0, time_used_seconds: 0, remaining_tokens: null,
  stop_reason: '', completion_requested: false, ...extra });
const snapshot = goal => ({ ok: true, commands: [], queue: { paused: false, pause_reason: '', revision: 1 }, goal });
const response = (body, status = 200) => new Response(JSON.stringify(body), { status });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const flush = () => new Promise(resolve => setImmediate(resolve));
const html = (goal, connection = 'online') => renderToStaticMarkup(React.createElement(GoalStatus, { goal, connection, onControl: async () => {} }));

// Same small hook harness used by existing lifecycle tests; host elements are
// rendered by ReactDOMServer and actions invoke the component's real handlers.
function hooks(run) {
  const slots = [], effects = []; let cursor = 0;
  const implementations = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useRef(initial) { const i = cursor++; return slots[i] ||= { current: initial }; },
    useId() { return 'goal-help'; },
    useSyncExternalStore(_subscribe, getSnapshot) { return getSnapshot(); },
    useEffect(fn, deps) { const i = cursor++, old = slots[i];
      if (!old || deps.some((value, index) => value !== old.deps[index])) {
        old?.cleanup?.(); slots[i] = { deps }; effects.push(() => { slots[i].cleanup = fn(); });
      }
    },
  };
  return {
    render() { cursor = 0; const original = Object.fromEntries(Object.keys(implementations).map(key => [key, React[key]]));
      Object.assign(React, implementations);
      try { const result = run(); effects.splice(0).forEach(fn => fn()); return result; }
      finally { Object.assign(React, original); }
    },
    dispose() { slots.forEach(value => value?.cleanup?.()); },
  };
}
function find(tree, type) {
  if (!tree || typeof tree !== 'object') return null;
  if (Array.isArray(tree)) return tree.map(child => find(child, type)).find(Boolean);
  return tree.type === type ? tree : find(tree.props?.children, type);
}
function environment(fetch) {
  const saved = { fetch: global.fetch, window: global.window, document: global.document };
  const storageDescriptor = Object.getOwnPropertyDescriptor(global, 'localStorage');
  const storage = { length: 0, getItem: () => null, setItem() {}, removeItem() {}, key: () => null };
  global.window = Object.assign(new EventTarget(), { localStorage: storage, location: { pathname: '/login' } });
  global.document = Object.assign(new EventTarget(), { hidden: false });
  Object.defineProperty(global, 'localStorage', { value: storage, configurable: true, writable: true }); global.fetch = fetch;
  identity.setWorkspaceIdentity('fixture-workspace');
  return () => { Object.assign(global, saved);
    if (storageDescriptor) Object.defineProperty(global, 'localStorage', storageDescriptor); else delete global.localStorage;
    identity.setWorkspaceIdentity(null); };
}

test('only a matching, valid server goal is projected; null/old backend/text never infer completion', () => {
  for (const value of [null, {}, { goal: null }, { goal: 'done' }, { result: { reply_text: 'Goal complete' } }]) {
    assert.equal(goals.goalFromSnapshot(value, 's1'), null);
  }
  const goal = fixture({ completion_requested: true });
  assert.equal(goals.goalFromSnapshot({ goal }, 's1'), goal);
  assert.equal(goal.status, 'active');
  assert.equal(goals.goalFromSnapshot({ goal }, 'another-session'), null);
  for (const invalid of [{ protocol: 'unknown' }, { status: 'succeeded' }, { revision: -1 }, { revision: '7' },
    { goal_id: '' }, { objective: null }, { tokens_used: NaN }, { token_budget: undefined }, { remaining_tokens: -2 },
    { time_used_seconds: Infinity }, { completion_requested: 1 }, { stop_reason: {} }]) {
    assert.equal(goals.readConversationGoal(fixture(invalid), 's1'), null);
  }
  const newer = fixture({ revision: 9, status: 'paused' });
  assert.equal(goals.goalFromSnapshot({ goal }, 's1', newer), newer);
  assert.equal(goals.goalFromSnapshot({ goal: null }, 's1', newer), null);
  assert.equal(goals.goalFromSnapshot({ goal: fixture({ goal_id: 'g2', revision: 0 }) }, 's1', newer).goal_id, 'g2');
});

test('real SSR renders each state with only its allowed goal control in both locales', () => {
  try {
    for (locale of ['en', 'zh']) {
      const copy = require('../messages/' + locale + '/chat.json').conversationGoal;
      for (const [status, action] of Object.entries({ active: 'pause', paused: 'resume', blocked: 'resume', usage_limited: 'resume', budget_limited: null, complete: null })) {
        const rendered = html(fixture({ status, stop_reason: status === 'complete' ? 'completion_verified' : '' }));
        assert.ok(rendered.includes(copy.states[status]));
        assert.equal((rendered.match(/<button/g) || []).length, action ? 1 : 0);
        if (action) assert.ok(rendered.includes(copy[action]));
        assert.match(rendered, /<details.*<summary/);
        assert.match(rendered, /aria-live="polite"/);
      }
    }
  } finally { locale = 'en'; }
});

test('budget is optional, completion requests stay active, and reasons/objectives are safely rendered', () => {
  assert.doesNotMatch(html(fixture()), /Tokens:|Tokens used:|tokens remaining/);
  const rendered = html(fixture({ completion_requested: true, token_budget: 1000, tokens_used: 125, remaining_tokens: 875, time_used_seconds: 12, stop_reason: 'awaiting_approval' }));
  assert.match(rendered, /data-state="active"/);
  assert.match(rendered, /Tokens: 125 \/ 1,000/);
  assert.match(rendered, /875 tokens remaining/);
  assert.match(rendered, /Time used: 12s/);
  assert.match(rendered, /Completion requested; awaiting server verification/);
  assert.match(rendered, /Waiting for approval/);
  assert.match(rendered, /does not cancel the current turn/);
  assert.match(html(fixture({ tokens_used: 3 })), /Tokens used: 3/);
  assert.match(html(fixture({ stop_reason: 'provider_unavailable' })), /provider unavailable/);
  assert.match(html(fixture({ objective: '<script>bad</script>' })), /&lt;script&gt;/);
  assert.doesNotMatch(html(fixture({ stop_reason: 'constructor' })), /function Object/);
  for (const reason of Object.keys(goals.goalReasonKeys)) {
    assert.doesNotMatch(html(fixture({ stop_reason: reason })), new RegExp('>' + reason + '<'));
  }
});

test('pause action uses the proxy and exact CAS revision; duplicate clicks wait for confirmation', async () => {
  const pending = deferred(), requests = [];
  const restore = environment(async (url, init) => { requests.push({ url, init }); return pending.promise; });
  const goal = fixture();
  let confirmed;
  const harness = hooks(() => GoalStatus({ goal, connection: 'online', onControl: async (action, expected) => {
    confirmed = await goals.controlConversationGoal(expected, action);
  } }));
  try {
    const button = find(harness.render(), 'button');
    const done = button.props.onClick(); await button.props.onClick();
    const waiting = harness.render();
    assert.equal(requests.length, 1);
    assert.equal(find(waiting, 'button').props.disabled, true);
    assert.match(renderToStaticMarkup(waiting), /data-state="active"/);
    assert.equal(requests[0].url, '/api/proxy/agent/goals/control');
    assert.equal(requests[0].init.method, 'POST');
    assert.deepEqual(JSON.parse(requests[0].init.body), { session_id: 's1', action: 'pause', expected_revision: 7 });
    pending.resolve(response({ ok: true, goal: fixture({ status: 'paused', revision: 8 }) })); await done;
    assert.equal(confirmed.status, 'paused');
    assert.equal(find(harness.render(), 'button').props.disabled, false);
    assert.equal(goal.status, 'active', 'component must wait for the parent server projection');
  } finally { harness.dispose(); restore(); }
});

test('waiting child goals only project existing work and new-input pauses have a readable reason', async () => {
  let writes = 0;
  const goal = fixture({ stop_reason: 'awaiting_child_task' });
  const restore = environment(async (url, init) => {
    assert.equal(init.method, 'GET'); return response(snapshot(goal));
  });
  const observer = hooks(() => useConversationCommands('s1', () => {}));
  const component = hooks(() => GoalStatus({ goal, connection: 'online', onControl: async () => { writes++; } }));
  try {
    observer.render(); await flush();
    assert.equal(observer.render().goal.status, 'active');
    const rendered = renderToStaticMarkup(component.render());
    assert.match(rendered, /Waiting for child task/);
    assert.match(rendered, /Waiting for the existing child task/);
    assert.match(rendered, /Pause goal/);
    assert.equal(writes, 0);
    assert.match(html(fixture({ status: 'paused', stop_reason: 'new_user_input' })), /Your new message paused the previous goal/);
  } finally { component.dispose(); observer.dispose(); restore(); }
});

test('resume sends the displayed CAS revision for each resumable state', async () => {
  const requests = [];
  const restore = environment(async (url, init) => { requests.push(JSON.parse(init.body));
    return response({ ok: true, goal: fixture({ status: 'active', revision: 12 }) });
  });
  try {
    for (const status of ['paused', 'blocked', 'usage_limited']) {
      const goal = fixture({ status, revision: 11 });
      const component = hooks(() => GoalStatus({ goal, connection: 'online', onControl: (action, expected) => goals.controlConversationGoal(expected, action) }));
      try { await find(component.render(), 'button').props.onClick();
        assert.doesNotMatch(renderToStaticMarkup(component.render()), /role="alert"/);
      } finally { component.dispose(); }
    }
    assert.deepEqual(requests, Array(3).fill({ session_id: 's1', action: 'resume', expected_revision: 11 }));
  } finally { restore(); }
});

test('resume errors render actionable alerts, retain server state, and never retry writes', async () => {
  for (const [status, code, expected] of [[409, 'goal_revision_conflict', /Goal state changed/], [409, 'decision_pending', /pending input or approval/],
    [409, 'execution_unconfirmed', /Reconcile the unconfirmed/], [403, 'goal_owner_mismatch', /permission/], [404, 'not_found', /unavailable/], [500, 'secret diagnostic', /could not be confirmed/]]) {
    let calls = 0;
    const restore = environment(async () => { calls++; return response({ error: code }, status); });
    const goal = fixture({ status: 'paused' });
    const harness = hooks(() => GoalStatus({ goal, connection: 'online', onControl: (action, expected) => goals.controlConversationGoal(expected, action) }));
    try {
      await find(harness.render(), 'button').props.onClick();
      const rendered = renderToStaticMarkup(harness.render());
      assert.match(rendered, /role="alert"/); assert.match(rendered, expected);
      assert.match(rendered, /data-state="paused"/); assert.doesNotMatch(rendered, /secret diagnostic/);
      assert.equal(calls, 1);
    } finally { harness.dispose(); restore(); }
  }
});

test('offline and terminal goals cannot send controls, including stale handlers', async () => {
  let calls = 0;
  for (const connection of ['offline', 'connecting', 'incompatible']) {
    const harness = hooks(() => GoalStatus({ goal: fixture(), connection, onControl: async () => { calls++; } }));
    try { const tree = harness.render(); assert.equal(find(tree, 'button').props.disabled, true);
      await find(tree, 'button').props.onClick(); assert.match(renderToStaticMarkup(tree), /last confirmed goal/);
    } finally { harness.dispose(); }
  }
  const restore = environment(async () => { calls++; throw Error('must not send'); });
  try {
    for (const status of ['complete', 'budget_limited']) await assert.rejects(goals.controlConversationGoal(fixture({ status }), 'resume'));
    assert.equal(calls, 0);
  } finally { restore(); }
});

test('malformed or stale receipts and workspace switches cannot confirm a control', async () => {
  let receipt;
  const restore = environment(async () => response(receipt));
  try {
    for (receipt of [{ ok: false, goal: fixture({ revision: 8 }) }, { ok: true, goal: null },
      { ok: true, goal: fixture() }, { ok: true, goal: fixture({ revision: 8, session_id: 's2' }) },
      { ok: true, goal: fixture({ revision: 8, goal_id: 'other' }) }]) {
      await assert.rejects(goals.controlConversationGoal(fixture(), 'pause'), /unconfirmed/);
    }
    global.fetch = async () => { identity.setWorkspaceIdentity('another-workspace'); return response({ ok: true, goal: fixture({ revision: 8 }) }); };
    await assert.rejects(goals.controlConversationGoal(fixture(), 'pause'), /workspaceChanged/);
    assert.equal(goals.goalControlErrorKey(new TypeError('network failure')), 'failed');
  } finally { restore(); }
});

test('command observer adopts confirmed goals, rejects older polls, refreshes 409, and clears on navigation', async () => {
  let serverGoal = fixture(), sid = 's1', reads = 0, writes = 0, fail = false;
  const restore = environment(async (url, init) => {
    if (url === '/api/proxy/agent/goals/control') {
      writes++; if (fail) return response({ error: 'decision_pending' }, 409);
      return response({ ok: true, goal: fixture({ status: 'paused', revision: 8 }) });
    }
    assert.match(url, /^\/api\/proxy\/agent\/commands\?/); reads++;
    return response(snapshot(serverGoal));
  });
  const observer = hooks(() => useConversationCommands(sid, () => {}));
  try {
    assert.equal(observer.render().goal, null); await flush();
    const initial = observer.render(); assert.equal(initial.goal.status, 'active');
    await initial.controlGoal('pause', initial.goal); await flush();
    assert.equal(observer.render().goal.status, 'paused', 'older revision 7 poll cannot undo control revision 8');
    fail = true;
    const paused = observer.render(), before = reads;
    await assert.rejects(paused.controlGoal('resume', paused.goal), api.ApiError); await flush();
    assert.ok(reads > before, 'a rejected CAS control refreshes the server snapshot');
    assert.equal(observer.render().goal.status, 'paused');
    serverGoal = fixture({ status: 'complete', revision: 9 }); observer.render().refresh(); await flush();
    assert.equal(observer.render().goal.status, 'complete');
    assert.equal(writes, 2);
    sid = 's2'; assert.equal(observer.render().goal, null); await flush();
    assert.equal(observer.render().goal, null, 'foreign-session goal is hidden');
    await assert.rejects(initial.controlGoal('pause', initial.goal));
    assert.equal(writes, 2);
  } finally { observer.dispose(); restore(); }
});

test('a late control reply cannot populate a different conversation', async () => {
  let sid = 's1'; const pending = deferred();
  const restore = environment(async url => url === '/api/proxy/agent/goals/control' ? pending.promise : response(snapshot(sid === 's1' ? fixture() : null)));
  const observer = hooks(() => useConversationCommands(sid, () => {}));
  try {
    observer.render(); await flush(); const engine = observer.render();
    const action = engine.controlGoal('pause', engine.goal);
    sid = 's2'; observer.render(); await flush();
    pending.resolve(response({ ok: true, goal: fixture({ status: 'paused', revision: 8 }) }));
    await assert.rejects(action, /workspaceChanged/);
    assert.equal(observer.render().goal, null);
  } finally { observer.dispose(); restore(); }
});

test('320px CSS contract wraps controls/metrics and constrains expanded unbroken objectives', () => {
  const postcss = require('postcss');
  const css = postcss.parse(fs.readFileSync(require.resolve('../components/chat/GoalStatus.module.css'), 'utf8'));
  function declarations(selector) {
    const result = {};
    css.walkRules(selector, rule => { if (rule.parent.type === 'root') rule.walkDecls(decl => { result[decl.prop] = decl.value; }); });
    return result;
  }
  const root = declarations('.root');
  assert.equal(root['box-sizing'], 'border-box'); assert.equal(root['max-width'], '100%');
  assert.equal(root['min-width'], '0'); assert.equal(root['overflow-wrap'], 'anywhere');
  for (const selector of ['.heading', '.metrics']) { assert.equal(declarations(selector)['flex-wrap'], 'wrap'); assert.equal(declarations(selector)['min-width'], '0'); }
  assert.equal(declarations('.objective summary')['text-overflow'], 'ellipsis');
  assert.equal(declarations('.objective p')['overflow-wrap'], 'anywhere');
  assert.equal(declarations('.control')['max-width'], '100%');
  assert.equal(declarations('.control')['white-space'], 'normal');
  assert.ok(css.nodes.some(node => node.type === 'atrule' && node.params === '(max-width:480px)' && node.toString().includes('min-height:44px')));
  assert.match(html(fixture({ objective: 'x'.repeat(500), token_budget: 999999999, tokens_used: 999999998, remaining_tokens: 1 })), /x{500}/);
  const view = fs.readFileSync(require.resolve('../components/chat/ChatView.tsx'), 'utf8');
  assert.match(view, /commandEngine\.goal && <GoalStatus/);
  assert.match(view, /onControl=\{commandEngine\.controlGoal\}/);
});

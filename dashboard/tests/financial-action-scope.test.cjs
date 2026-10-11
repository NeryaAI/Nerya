const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');

const compile = filename => ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText;
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const drain = () => new Promise(setImmediate);

function fixture(file, name, initialProps = {}) {
  let workspace = 'A', generation = 1, cursor = 0, slots = [], effects = [], cleanups = [];
  const writes = [], reads = [];
  const dialog = deferred(), descriptor = deferred();
  const jsx = (type, props, key) => ({ type, props: props || {}, key });
  const translation = key => key; translation.has = () => true;
  const hooks = {
    useRef(value) { const i = cursor++; return slots[i] ??= { current: value }; },
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useEffect(fn, deps) { const i = cursor++; const old = slots[i]; if (!old || deps.some((value, n) => value !== old[n])) {
      slots[i] = deps; effects.push(fn);
    } },
  };
  const identity = { useWorkspaceIdentity: () => workspace, getWorkspaceIdentity: () => workspace, workspaceGeneration: () => generation };
  const task = { task_kind: 'scheduled_agent', task_id: 'daily', security_revision: 'v1', source_revision: 'v1' };
  const grant = { grant_id: 'grant-same', state: 'draft', expires_at: 9999999999, revision: 1, security_revision: 'v1',
    policy: { actions: ['wallet_transfer'], resources: { wallets: ['wallet'] }, limits: { asset_amounts: {}, single_usd: '1', rolling_24h_usd: '2', total_usd: '3' } } };
  const run = { run_id: 'same-run', task_kind: 'scheduled_agent', task_id: 'daily', session_id: 'same-session',
    source_revision: 'v1', execution_status: 'failed', business_status: 'failed', delivery_status: 'not_requested',
    created_at: 1, snapshot: {}, result: { effects: [] }, revision: 1 };
  let scopeModule;
  function load(relative) {
    const filename = path.resolve(__dirname, '..', relative);
    const module = new Module(filename); module.filename = filename; module.paths = Module._nodeModulePaths(path.dirname(filename));
    module.require = request => {
      if (request === 'react') return hooks;
      if (request === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
      if (request === 'next-intl') return { useLocale: () => 'en', useTranslations: () => translation };
      if (request === 'next/link') return { default: 'a' };
      if (request.endsWith('/workspaceIdentity')) return identity;
      if (request.endsWith('/useActionScope')) return scopeModule;
      if (request.endsWith('/dialogs')) return { confirm: () => dialog.promise };
      if (request.endsWith('/financial')) return { financialApi: {
        task: async () => { reads.push('task'); return file.includes('TaskRunDetail') ? descriptor.promise : { ok: true, task }; },
        grants: async () => ({ ok: true, grants: [grant] }),
        decision: async () => { writes.push('grant'); return { ok: true }; },
        create: async () => { writes.push('create'); return { ok: true }; },
        capabilities: async () => ({ enabled: true, capabilities: [] }),
        resource: async () => ({ revision: 'v1', permissions: {}, limits: {} }),
        updateResource: async () => { writes.push('permissions'); return { ok: true }; },
      } };
      if (request.endsWith('/taskRuns')) return { pendingRun: () => false, runLabel: s => s,
        taskRunApi: { control: async () => { writes.push('control'); return { ok: true, run_id: 'next' }; } } };
      if (request.endsWith('/usePolledResource')) return { usePolledResource: () => ({ data: { run, events: [] }, refresh() {} }) };
      if (request.endsWith('/runEvidence')) return { loadRunEvidence() {} };
      if (request.endsWith('/ChatMessage')) return { AssistantBubble: 'bubble' };
      if (request.endsWith('/ResourceNotice')) return { ResourceNotice: 'notice' };
      if (request.endsWith('/RunWorkspace')) return { RunWorkspace: 'workspace' };
      if (request.endsWith('/RunAttention')) return { RunAttention: 'attention' };
      throw Error('Unexpected dependency: ' + request);
    };
    module._compile(compile(filename), filename);
    return module.exports;
  }
  scopeModule = load('lib/useActionScope.ts');
  const exports = load(file);
  let props = initialProps;
  return {
    writes, reads, dialog, descriptor, task,
    setWorkspace(next) { if (workspace !== next) { workspace = next; generation++; } },
    render(nextProps = props) {
      props = nextProps; cursor = 0;
      let tree = exports[name](props);
      if (typeof tree.type === 'function') tree = tree.type(tree.props);
      for (const effect of effects.splice(0)) { const cleanup = effect(); if (cleanup) cleanups.push(cleanup); }
      return tree;
    },
    dispose() { for (const fn of cleanups) fn(); },
  };
}

function nodes(value) {
  if (!value || typeof value !== 'object') return [];
  if (Array.isArray(value)) return value.flatMap(nodes);
  return [value, ...Object.values(value.props || {}).flatMap(nodes)];
}
function button(tree, label) {
  const found = nodes(tree).find(node => node.type === 'button' && node.props.children === label);
  assert.ok(found, 'missing button: ' + label);
  return found;
}

test('a grant confirmation cannot mutate a changed workspace, even A -> B -> A', async () => {
  const f = fixture('components/TaskFinancialGrants.tsx', 'TaskFinancialGrants', { taskKind: 'scheduled_agent', taskId: 'daily' });
  f.render(); await drain();
  button(f.render(), 'approveGrant').props.onClick();
  f.setWorkspace('B'); f.setWorkspace('A'); f.dialog.resolve(true); await drain();
  assert.deepEqual(f.writes, []);
  f.dispose();
});

test('a grant confirmation cannot mutate a different task after navigation', async () => {
  const f = fixture('components/TaskFinancialGrants.tsx', 'TaskFinancialGrants', { taskKind: 'scheduled_agent', taskId: 'daily' });
  f.render(); await drain();
  button(f.render(), 'approveGrant').props.onClick();
  f.render({ taskKind: 'scheduled_agent', taskId: 'other' });
  f.dialog.resolve(true); await drain();
  assert.deepEqual(f.writes, []);
  f.dispose();
});

test('grant confirmation still submits once in its original scope', async () => {
  const f = fixture('components/TaskFinancialGrants.tsx', 'TaskFinancialGrants', { taskKind: 'scheduled_agent', taskId: 'daily' });
  f.render(); await drain();
  const approve = button(f.render(), 'approveGrant');
  approve.props.onClick(); approve.props.onClick();
  f.dialog.resolve(true); await drain();
  assert.deepEqual(f.writes, ['grant']);
  f.dispose();
});

test('rerun waits for confirmation and rejects a workspace switch during descriptor load', async () => {
  const f = fixture('components/chat/TaskRunDetail.tsx', 'TaskRunDetail', { runId: 'same-run' });
  button(f.render(), 'startNewRun').props.onClick();
  assert.deepEqual(f.reads, []);
  f.dialog.resolve(true); await drain();
  assert.deepEqual(f.reads, ['task']);
  f.setWorkspace('B'); f.descriptor.resolve({ ok: true, task: f.task }); await drain();
  assert.deepEqual(f.writes, []);
  f.dispose();
});

test('unmounting a pending confirmation prevents later run control', async () => {
  const f = fixture('components/chat/TaskRunDetail.tsx', 'TaskRunDetail', { runId: 'same-run' });
  button(f.render(), 'startNewRun').props.onClick(); f.dispose();
  f.dialog.resolve(true); await drain();
  assert.deepEqual(f.reads, []); assert.deepEqual(f.writes, []);
});

test('financial actions do not fetch or submit before runtime identity is known', async () => {
  const f = fixture('components/TaskFinancialGrants.tsx', 'TaskFinancialGrants', { taskKind: 'scheduled_agent', taskId: 'daily' });
  f.setWorkspace(null); f.render(); await drain();
  assert.deepEqual(f.reads, []); assert.deepEqual(f.writes, []);
  f.dispose();
});

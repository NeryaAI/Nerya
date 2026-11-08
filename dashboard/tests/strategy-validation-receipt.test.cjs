// Offline lifecycle test of the current candidate card; no services or network.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, f);
require.extensions['.css'] = m => { m.exports = {}; };
const load = Module._load;
let testLocale = 'en';
Module._load = function (name, ...args) {
  if (name === 'next-intl') return { useLocale: () => testLocale, useTranslations: namespace => key => {
    const messages = require('../messages/'+testLocale+'/strategies.json')[namespace];
    return messages?.[key] || key;
  } };
  return load.call(this, name, ...args);
};
const React = require('react');
const { StrategyProposalApprovalCard } = require('../components/strategies/StrategyProposalApprovalCard.tsx');
const { workflowApi } = require('../lib/workflowApi.ts');
const { clientApi } = require('../lib/clientApi.ts');
Module._load = load;

function hooks(run) {
  const originals = {}, slots = [], effects = []; let cursor = 0;
  for (const name of ['useState', 'useEffect', 'useMemo', 'useContext']) originals[name] = React[name];
  React.useState = initial => { const i = cursor++; if (!(i in slots)) slots[i] = initial; return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; };
  React.useMemo = fn => fn();
  React.useContext = () => null;
  React.useEffect = (fn, deps) => {
    const i = cursor++, old = slots[i];
    if (!old || deps.some((value, index) => value !== old.deps[index])) {
      old?.cleanup?.(); slots[i] = { deps }; effects.push(() => { slots[i].cleanup = fn(); });
    }
  };
  return { render() { cursor = 0; const tree = run(); effects.splice(0).forEach(fn => fn()); return tree; },
    dispose() { slots.forEach(value => value?.cleanup?.()); Object.assign(React, originals); } };
}
function text(tree) {
  if (!tree) return '';
  if (typeof tree === 'string') return tree;
  if (Array.isArray(tree)) return tree.map(text).join(' ');
  return tree.type?.name === 'IssueList' ? tree.props.items.join(' ') : text(tree.props?.children);
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test('script-driven Agent classification follows projected role calls and retains the declared role', async () => {
  const role = 'confirmation_reviewer';
  const manifest = { strategy_id: 'confirmation_fixture', title: 'Agent words are not invocation evidence',
    execution_mode: 'script', entrypoint: 'main.py:run', agent_task: { enabled: false },
    subagents: [role], params: { reviewer: role } };
  const cases = [
    { code: "def run(ctx):\n    params = ctx.config.params\n    return ctx.subagents.run(params['reviewer'], payload={})\n", called: true },
    { code: "def run(ctx):\n    return ctx.subagents.run(name=ctx.config.params['reviewer'], payload={})\n", called: true },
    { code: "def run(ctx):\n    return ctx.subagents.run('confirmation_reviewer', payload={})\n", called: true },
    { code: 'def run(ctx):\n    return None\n', called: false },
    { code: "def run(ctx):\n    params = ctx.config.params\n    return ctx.subagents.run(params['missing'], payload={})\n", called: false },
    { code: "def run(ctx):\n    params = ctx.config.params\n    params = {}\n    return ctx.subagents.run(params['reviewer'], payload={})\n", called: false },
    { code: "def other(ctx):\n    params = ctx.config.params\ndef run(ctx, params):\n    return ctx.subagents.run(params['reviewer'], payload={})\n", called: false },
  ];
  const { execFileSync } = require('node:child_process');
  const path = require('node:path'), root = path.resolve(__dirname, '../..');
  const venv = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const python = process.env.NERYA_PYTHON || (fs.existsSync(venv) ? venv : 'python');
  const views = JSON.parse(execFileSync(python, ['-B', '-c',
    'import json, sys\nfrom nerya.core import yaml_io\nfrom nerya.strategies.workflow_graph import build_workflows\nprint(json.dumps([build_workflows({"strategy.yml":yaml_io.dumps(case["manifest"]),"main.py":case["code"]}) for case in json.load(sys.stdin)]))'],
    { cwd: root, input: JSON.stringify(cases.map(row => ({ manifest, code: row.code }))), encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } }));
  const { compactWorkflow } = require('../lib/workflowProjection.ts');
  const saved = { get: workflowApi.get, validate: clientApi.strategyRuntimeValidate, window: global.window, document: global.document };
  global.window = { setInterval: () => 1, clearInterval() {} };
  global.document = { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} };
  clientApi.strategyRuntimeValidate = () => { throw Error('Classification must not run validation'); };
  try {
    for (const [index, view] of views.entries()) {
      const original = JSON.stringify(view), calls = view.strategy.edges.filter(edge => edge.relation === 'calls');
      assert.equal(calls.length, cases[index].called ? 1 : 0);
      if (cases[index].called) assert.equal(calls[0].target, 'agent:role/'+role);
      const projected = compactWorkflow(view.strategy, key => key).graph;
      assert.ok(projected.nodes.some(node => node.id === 'agent:role/'+role));
      if (cases[index].called) assert.ok(projected.edges.some(edge => edge.source === 'script:main.py' && edge.target === 'agent:role/'+role));
      for (const locale of ['en', 'zh']) {
        testLocale = locale;
        const snapshot = { ...view, source: { proposal_id: 'prp_classification', state: 'draft' } };
        workflowApi.get = async () => snapshot;
        const proposal = { id: 'prp_classification', strategy_id: manifest.strategy_id, kind: 'strategy_package_proposal', state: 'draft', validation: { ok: true } };
        const harness = hooks(() => StrategyProposalApprovalCard({ proposal }));
        try {
          harness.render(); await flush();
          const rendered = text(harness.render());
          assert.ok(rendered.includes(cases[index].called ? locale === 'zh' ? '脚本驱动 Agent' : 'Script-gated Agent' : locale === 'zh' ? '纯脚本策略' : 'Script strategy'), rendered);
          assert.equal(snapshot.manifest.execution_mode, 'script');
        } finally { harness.dispose(); }
      }
      assert.equal(JSON.stringify(view), original);
    }
  } finally {
    testLocale = 'en'; workflowApi.get = saved.get; clientApi.strategyRuntimeValidate = saved.validate;
    global.window = saved.window; global.document = saved.document;
  }
});

test('mounted card refreshes latest failed receipt, rejects stale success, and preserves transcript', async () => {
  const saved = { get: workflowApi.get, validate: clientApi.strategyRuntimeValidate, window: global.window, document: global.document };
  const timers = new Map(); let nextTimer = 0, reads = 0;
  global.window = { setInterval: fn => { timers.set(++nextTimer, fn); return nextTimer; }, clearInterval: id => timers.delete(id) };
  global.document = { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} };
  const original = { id: 'prp_fixture', strategy_id: 'receipt_fixture', kind: 'strategy_package_proposal', state: 'draft', validation: { ok: false, blockers: [{ message: 'ORIGINAL syntax line 40' }] } };
  const historical = JSON.stringify(original);
  let receipt = { status: 'current', report: original.validation };
  workflowApi.get = async () => { reads++; return { source: { proposal_id: 'prp_fixture', state: 'draft' }, strategy: { nodes: [] }, manifest: {}, validation_receipt: receipt }; };
  clientApi.strategyRuntimeValidate = () => { throw Error('Read-only card must not run validation'); };
  const harness = hooks(() => StrategyProposalApprovalCard({ proposal: original }));
  try {
    harness.render(); await flush();
    assert.match(text(harness.render()), /ORIGINAL syntax line 40/);
    receipt = { status: 'current', report: { ok: false, blockers: [{ message: '2 failed, 3 passed' }] } };
    for (const tick of timers.values()) await tick();
    const updated = text(harness.render());
    assert.match(updated, /2 failed, 3 passed/);
    assert.doesNotMatch(updated, /ORIGINAL syntax/);
    receipt = { status: 'stale', report: { ok: true, blockers: [] } };
    for (const tick of timers.values()) await tick();
    const stale = text(harness.render());
    assert.match(stale, /Current source needs validation/);
    assert.doesNotMatch(stale, /2 failed|ORIGINAL syntax/);
    assert.equal(JSON.stringify(original), historical);
    assert.equal(reads, 3);
  } finally {
    harness.dispose(); assert.equal(timers.size, 0);
    workflowApi.get = saved.get; clientApi.strategyRuntimeValidate = saved.validate;
    global.window = saved.window; global.document = saved.document;
  }
});

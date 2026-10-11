const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const workflowCopy = require('../messages/zh/workflows.json').copy;

function fixture({ initial, syncFails = false, saved } = {}) {
  const slots = []; let index = 0; const calls = [];
  const jsx = (type, props) => ({ type, props: props || {} });
  const module = { exports: {} };
  const code = ts.transpileModule(fs.readFileSync(require.resolve('../components/workflows/WorkflowPromotionReceipt.tsx'), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  vm.runInNewContext(code, { module, exports: module.exports, require(name) {
    if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
    if (name === 'react') return { useEffect() {}, useRef(value) {
      const slot = index++; if (!(slot in slots)) slots[slot] = { current: value }; return slots[slot];
    }, useState(value) {
      const slot = index++; if (!(slot in slots)) slots[slot] = value;
      return [slots[slot], next => { slots[slot] = typeof next === 'function' ? next(slots[slot]) : next; }];
    }};
    if (name === 'next-intl') return { useLocale: () => 'zh' };
    if (name === 'next/link') return { default: 'a' };
    if (name === '../../lib/i18n') return { copy(_zh, key) {
      const [, group, entry] = key.split('.');
      return workflowCopy[group]?.[entry] ?? key;
    } };
    if (name.endsWith('clientApi')) return { clientApi: {
      async strategyRuntimeSchedule(id) { calls.push(['sync', id]); if (syncFails) throw new Error('fixture');
        return { ok: true, trading_id: 'timer', tuning_id: null }; },
      async strategyRuntimeScheduleStatus(id) { calls.push(['status', id]);
        return { ok: true, promotion_receipt: saved, service: { state: 'stopped' } }; },
    }};
    throw new Error(`Unexpected dependency ${name}`);
  }});
  return { calls, facts: module.exports.promotionReceiptFacts, render() {
    index = 0; return module.exports.WorkflowPromotionReceipt({ strategyId: 'demo', proposalId: 'proposal', receipt: initial });
  }};
}
function nodes(node) {
  if (Array.isArray(node)) return node.flatMap(nodes);
  if (!node || typeof node !== 'object') return [];
  return [node, ...nodes(node.props?.children)];
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join(' ');
  if (node && typeof node === 'object') return text(node.props?.children);
  return node == null || typeof node === 'boolean' ? '' : String(node);
}
const failed = { ok: true, proposal_id: 'proposal', validation: { ok: true },
  application: { status: 'applied' }, promotion: { warnings: ['schedule_sync_failed'] },
  schedule_sync: { status: 'failed' }, service: { state: 'stopped', start_requested: false } };

test('legacy sync warning is consumed while an explicit successful retry wins', () => {
  const { facts } = fixture();
  assert.equal(facts({ ok: true, promotion: failed.promotion }).syncFailed, true);
  assert.equal(facts({ ...failed, schedule_sync: { status: 'synced' } }).syncFailed, false);
  assert.equal(facts({ ...failed, schedule_sync: { status: 'synced' } }).warnings.length, 0);
  assert.equal(facts({}).application, 'unrecorded');
});

test('failed sync action invokes only schedule sync and clears obsolete warning', async () => {
  const f = fixture({ initial: failed });
  assert.match(text(f.render()), /版本已应用，调度待同步/);
  const retry = nodes(f.render()).find(node => node.type === 'button' && text(node).includes('仅重试调度同步'));
  retry.props.onClick();
  await new Promise(setImmediate);
  assert.deepEqual(f.calls, [['sync', 'demo']]);
  assert.doesNotMatch(text(f.render()), /仅重试调度同步|版本已应用，调度待同步/);
  assert.match(text(f.render()), /配置已同步/);
});

test('retry failure preserves applied receipt and actionable sync warning', async () => {
  const f = fixture({ initial: failed, syncFails: true });
  nodes(f.render()).find(node => node.type === 'button' && text(node).includes('仅重试调度同步')).props.onClick();
  await new Promise(setImmediate);
  assert.match(text(f.render()), /调度仍未同步；版本已应用/);
  assert.match(text(f.render()), /仅重试调度同步/);
});

test('unknown old records never offer synchronization or invent timer success', () => {
  const f = fixture();
  assert.match(text(f.render()), /未记录/);
  assert.doesNotMatch(text(f.render()), /仅重试调度同步|配置已同步/);
  assert.equal(f.calls.length, 0);
});

test('continuous receipt has no fabricated timer or service-start action', () => {
  const f = fixture({ initial: { ...failed, promotion: {}, schedule_sync: { status: 'synced', trading_id: '', tuning_id: null } } });
  assert.match(text(f.render()), /未配置/);
  assert.match(text(f.render()), /未运行/);
  assert.deepEqual(nodes(f.render()).filter(node => node.type === 'button').map(text), ['重新核对']);
});

test('refresh restores the persisted receipt for this proposal', async () => {
  const f = fixture({ saved: failed });
  nodes(f.render()).find(node => node.type === 'button').props.onClick();
  await new Promise(setImmediate);
  assert.match(text(f.render()), /版本已应用，调度待同步/);
  assert.deepEqual(f.calls, [['status', 'demo']]);
});

test('an older proposal receipt does not offer retry against a newer applied version', async () => {
  const f = fixture({ initial: failed, saved: { ...failed, proposal_id: 'newer' } });
  nodes(f.render()).find(node => node.type === 'button' && text(node) === '重新核对').props.onClick();
  await new Promise(setImmediate);
  assert.match(text(f.render()), /已有新的应用记录/);
  assert.doesNotMatch(text(f.render()), /仅重试调度同步/);
});

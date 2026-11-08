const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(
  ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
    jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  } }).outputText, filename);
require.extensions['.css'] = module => { module.exports = {}; };
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
const { DeliveryEvidence } = require('../components/chat/DeliveryEvidence.tsx');
const { deliveryEvidence } = require('../lib/deliveryEvidence.ts');
const { collectChatResults } = require('../lib/chatResults.ts');
const { projectCommand } = require('../lib/conversationCommands.ts');

test('historical command snapshot supersedes embedded delivery at result collection and rendering', () => {
  const command = { command_id: 'rate-command', session_id: 'rate-session', turn_id: 'rate-turn',
    kind: 'send', state: 'succeeded', revision: 9, created_at: 1, updated_at: 2,
    input: 'Create a yield strategy', context: {}, attachments: [] };
  const saved = { protocol: 'nerya.task_delivery.v1', command_id: command.command_id,
    delivery_id: 'rate-delivery', delivery_revision: 2,
    stages: [{ id: 'deliver', state: 'completed' }],
    checks: [{ check_id: 'coverage', status: 'passed', required: true }],
    artifacts: [{ artifact_id: 'report', ref: 'reports/rate.md' }],
    limitations: ['Rate-policy simulation, not realized yield'],
    completion: { allowed: true, status: 'partial', reasons: [] } };
  const old = { ...saved, delivery_revision: 1, checks: [],
    completion: { allowed: false, status: 'incomplete', reasons: ['producer_evidence_missing'] } };
  const history = { id: command.session_id, title: command.input, messages: [{
    id: 'saved-reply', role: 'assistant', ts: 1000, command_id: command.command_id,
    backend_message_id: command.turn_id + ':assistant',
    turn: { turn_id: command.turn_id, reply_text: 'Saved rate report', delivery: old } }] };
  const snapshot = { ...command, delivery: saved };
  // The snapshot can arrive after transcript hydration, before the active-thread merge.
  const result = collectChatResults(history, [snapshot])[0];
  assert.equal(result.delivery, saved);
  const active = projectCommand(history, snapshot);
  assert.deepEqual(collectChatResults(active, [snapshot])[0].delivery, saved);
  assert.match(render(result, 'en', true), /Partial delivery/);
  assert.match(render(result, 'zh', true), /部分交付/);
  assert.ok(render(result, 'en', true).includes('reports/rate.md'));
  const cleared = collectChatResults(history, [{ ...snapshot, delivery: null }])[0];
  assert.equal(cleared.delivery, null);
  assert.equal(collectChatResults(history, [{ ...snapshot, delivery: undefined }])[0].delivery, old);
  const newerHistory = { ...history, messages: [{ ...history.messages[0], command_revision: 10 }] };
  assert.equal(collectChatResults(newerHistory, [snapshot])[0].delivery, old);
  assert.equal(collectChatResults(history, [{ ...snapshot, session_id: 'other-session' }])[0].delivery, old);
  const followup = { ...snapshot, command_id: 'followup-command', delivery: saved };
  const followupHistory = { ...history, messages: [{ ...history.messages[0],
    command_id: followup.command_id, turn: { ...history.messages[0].turn, delivery: null } }] };
  assert.equal(collectChatResults(followupHistory, [followup])[0].delivery, null);
  assert.equal(history.messages[0].turn.delivery, old);
});

function result(reasons = [], checks = []) {
  return { id: 'delivery', title: 'Report', text: 'All done', ts: 1,
    delivery: { stages: [{ id: 'deliver', state: 'completed' }], artifacts: [], checks,
      completion: { allowed: false, status: 'incomplete', reasons } },
    evidence: { verifier: { hard_status: 'passed', hard_passed: true, trusted: true,
      details: { hard: { checks: [{ check_id: 'old-pass', status: 'passed' }] } } } },
  };
}
function render(value, locale, expanded = false) {
  return renderToStaticMarkup(React.createElement(NextIntlClientProvider,
    { locale, messages: {}, timeZone: 'UTC' },
    React.createElement(DeliveryEvidence, { result: value, expanded })));
}

test('producer partial with completion disallowed keeps the partial label and its coverage gaps', () => {
  const value = result(['check_missing:data_coverage'], Array.from({ length: 12 }, (_, index) => ({
    check_id: 'check-' + index, status: index < 10 ? 'passed' : 'missing', required: true,
  })));
  value.delivery.stages = Array.from({ length: 6 }, (_, index) => ({
    id: 'stage-' + index, state: 'completed',
  }));
  value.delivery.completion = { allowed: false, status: 'partial', reasons: ['check_missing:data_coverage'] };
  value.evidence.verifier.hard_status = 'missing';
  value.evidence.verifier.hard_passed = false;
  value.evidence.verifier.trusted = false;
  const evidence = deliveryEvidence(value);
  assert.equal(evidence.partialDelivery, true);
  assert.equal(evidence.status, 'pending');
  assert.equal(evidence.delivery.completion.allowed, false);
  const output = render(value, 'en', true);
  assert.match(output, /Partial delivery/);
  assert.match(output, /1 unmet completion conditions/);
  assert.match(output, /2 unresolved recorded checks/);
  assert.match(output, /Historical data coverage is incomplete/);
  assert.match(output, /data-state="missing"/);
  assert.doesNotMatch(output, /Recorded checks passed/);
  for (const status of ['failed', 'stale', 'unknown']) {
    value.delivery.checks[0].status = status;
    const unsafe = deliveryEvidence(value);
    assert.equal(unsafe.partialDelivery, false, status);
    assert.notEqual(unsafe.status, 'passed', status);
  }
});
const reasonLabels = [
  ['required_checks_missing', 'Required checks are missing', '缺少必需检查'],
  ['required_artifacts_missing', 'Required artifacts are missing', '缺少必需产物'],
  ['completed_stage_missing', 'No completed stage is recorded', '尚无已完成阶段的记录'],
  ['producer_evidence_missing', 'Producer evidence is missing', '缺少产物生成方证据'],
  ['verified_artifact_missing', 'No verified artifact is recorded', '尚无已核验产物的记录'],
  ['required_stage_pending', 'A required stage is incomplete', '必需阶段尚未完成'],
  ['stage_blocked', 'A stage is blocked', '阶段受阻'],
  ['check_missing', 'Required check is missing', '缺少必需检查'],
  ['check_failed', 'Check failed', '检查失败'],
  ['check_stale', 'Check evidence is out of date', '检查证据已过期'],
  ['check_unknown', 'Check status is unknown', '检查状态未知'],
  ['check_pending', 'Check result is pending', '等待检查结果'],
  ['check_unverified', 'Check evidence is unverified', '检查证据尚未核验'],
  ['artifact_missing', 'Required artifact is missing', '缺少必需产物'],
  ['artifact_unverified', 'Artifact is unverified', '产物尚未核验'],
];

test("known producer labels are localized without replacing custom labels", () => {
  const value = result([], [
    { check_id: "source_binding", label: "Source version unchanged", status: "passed" },
    { check_id: "custom", label: "Custom source audit", status: "missing" },
  ]);
  assert.match(render(value, "zh", true), /源码版本绑定/);
  assert.match(render(value, "zh", true), /Custom source audit/);
  assert.match(render(value, "en", true), /Source version unchanged/);
});

for (const locale of ['en', 'zh']) {
  test(`${locale}: tool errors without delivery or checks have distinct copy and retain diagnostics`, () => {
    const value = { id: 'research-report', title: 'Research report', text: 'Research finished', ts: 1,
      evidence: { artifacts: { errors: [
        { tool: 'data_api', message: 'data_api request failed' },
        { tool: 'research_run', message: 'research_run failed' },
      ] } } };
    const before = JSON.stringify(value);
    const evidence = deliveryEvidence(value);
    assert.equal(evidence.status, 'failed');
    assert.equal(evidence.executionErrors, true);
    assert.equal(evidence.checks.length, 0);
    assert.equal(evidence.unresolved, 0);
    const output = render(value, locale, true);
    assert.match(output, locale === 'en' ? /Tool errors recorded/ : /有工具执行报错/);
    assert.match(output, locale === 'en' ? /Delivery completion still depends on producer evidence/ : /是否完成交付仍以产物生成方证据为准/);
    assert.match(output, /data_api request failed/);
    assert.match(output, /research_run failed/);
    assert.match(output, /data-testid="delivery-evidence" data-state="failed"/);
    assert.doesNotMatch(output, /Checks need attention|有检查需要处理|At least one check failed|至少一项检查未通过|Recorded checks passed|已记录的检查通过|copy.deliveryEvidence/);
    assert.equal(JSON.stringify(value), before);

    for (const failure of [
      { delivery: { checks: [{ check_id: 'validation', status: 'failed' }] } },
      { evidence: { ...value.evidence, verifier: { hard_status: 'failed' } } },
      { evidence: { artifacts: { ...value.evidence.artifacts, tests_run: [{ command: 'verify', exit_code: 1 }] } } },
      { delivery: { checks: [], completion: { allowed: false, reasons: ['check_failed:validation'] } } },
    ]) {
      const failedCheck = { ...value, ...failure };
      assert.equal(deliveryEvidence(failedCheck).status, 'failed');
      assert.equal(deliveryEvidence(failedCheck).executionErrors, false);
      const failedOutput = render(failedCheck, locale, true);
      assert.match(failedOutput, locale === 'en' ? /Checks need attention/ : /有检查需要处理/);
      assert.match(failedOutput, locale === 'en' ? /At least one check failed/ : /至少一项检查未通过/);
      assert.doesNotMatch(failedOutput, /Tool errors recorded|有工具执行报错/);
      assert.match(failedOutput, /data_api request failed/);
    }
  });
  test(`${locale}: admitted partial delivery is not relabelled failed by a recovered tool error`, () => {
    const value = result([], [{ check_id: 'source_binding', status: 'passed', required: true }]);
    value.delivery.completion = { allowed: true, status: 'partial', reasons: [] };
    value.delivery.limitations = ['Retrospective Agent decisions not verified'];
    value.evidence.artifacts = { errors: [{ tool: 'Skill', message: 'Repeated tool call suppressed' }] };
    const before = JSON.stringify(value);
    const output = render(value, locale, true);
    assert.equal(deliveryEvidence(value).partialDelivery, true);
    assert.match(output, locale === 'en' ? /Partial delivery/ : /部分交付/);
    assert.doesNotMatch(output, /Checks need attention|有检查需要处理|Recorded checks passed|已记录的检查通过/);
    assert.match(output, /Repeated tool call suppressed/);
    assert.match(output, /Retrospective Agent decisions not verified/);
    assert.equal(JSON.stringify(value), before);
    for (const state of ['failed', 'stale', 'unknown', 'pending', 'unverified', 'missing']) {
      value.evidence.verifier.hard_status = state;
      assert.equal(deliveryEvidence(value).partialDelivery, false);
      assert.notEqual(deliveryEvidence(value).status, 'passed');
    }
    value.evidence.verifier.hard_status = 'passed';
    value.delivery.checks[0].status = 'failed';
    assert.equal(deliveryEvidence(value).status, 'failed');
    assert.equal(deliveryEvidence(value).partialDelivery, false);
    delete value.delivery;
    assert.equal(deliveryEvidence(value).status, 'failed');
  });
  test(`${locale}: allowed partial delivery retains optional evidence gaps without claiming full verification`, () => {
    const value = result([], [{ check_id: 'required-check', status: 'passed', required: true },
      { check_id: 'historical_performance', status: 'missing', required: false, label: 'Performance evidence' }]);
    value.delivery.completion = { allowed: true, status: 'partial', reasons: [] };
    const output = render(value, locale, true);
    assert.match(output, locale === 'en' ? /Partial delivery/ : /部分交付/);
    assert.match(output, locale === 'en' ? /1 unresolved recorded checks/ : /1 项已记录检查待处理/);
    assert.doesNotMatch(output, /Recorded checks passed|已记录的检查通过|data-state="passed" class/);
    assert.equal(deliveryEvidence(value).partialDelivery, true);
    value.delivery.checks[0].status = 'missing';
    assert.equal(deliveryEvidence(value).partialDelivery, false);
    value.delivery.checks[0].status = 'passed';
    value.delivery.completion.allowed = false;
    assert.equal(deliveryEvidence(value).partialDelivery, false);
  });
  test(`${locale}: trusted workflow failures remain explicit and localized`, () => {
    const reasons = ['trusted_delivery_contract_missing', 'trusted_delivery_contract_invalid',
      'trusted_delivery_workflow_missing', 'untrusted_delivery_contract_source',
      'delivery_contract_binding_mismatch', 'delivery_contract_stale'];
    const output = render(result(reasons), locale, true);
    for (const reason of reasons) assert.ok(!output.includes(reason));
    assert.match(output, locale === 'en' ? /6 unmet completion conditions/ : /6 项完成条件未满足/);
    assert.match(output, locale === 'en' ? /a new command is needed/ : /需要新建命令/);
    assert.doesNotMatch(output, /data-state="passed"|copy.deliveryEvidence/);
  });
  test(`${locale}: empty producer checks expose localized conditions separately in existing details`, () => {
    const reasons = reasonLabels.map(([code], index) => index < 7 ? code : `${code}:id:revision`);
    const value = result(reasons);
    const snapshot = JSON.stringify(value);
    const output = render(value, locale);
    assert.match(output, /data-state="pending"/);
    assert.match(output, locale === 'en' ? /Delivery is incomplete/ : /交付尚未完成/);
    assert.match(output, locale === 'en' ? /0 unresolved recorded checks/ : /0 项已记录检查待处理/);
    assert.match(output, locale === 'en' ? /15 unmet completion conditions/ : /15 项完成条件未满足/);
    assert.match(output, locale === 'en' ? /0 passed/ : /0 项通过/);
    for (const [code, en, zh] of reasonLabels) {
      assert.ok(output.includes(locale === 'en' ? en : zh), code);
      assert.ok(!output.includes(code), `raw known code leaked: ${code}`);
    }
    assert.ok(output.includes('<code>id:revision</code>'));
    assert.equal((output.match(/data-testid="delivery-evidence"/g) || []).length, 1);
    assert.equal((output.match(/<details/g) || []).length, 1);
    assert.doesNotMatch(output, /<details[^>]*open|old-pass|Recorded checks passed|已记录的检查通过|copy.deliveryEvidence/);
    assert.ok(output.indexOf(locale === 'en' ? '<h3>Unmet completion conditions' : '<h3>未满足的完成条件') > output.indexOf('<details'));
    assert.equal(JSON.stringify(value), snapshot, 'rendering must not change authoritative state');
  });

  test(`${locale}: stale, unknown, missing and incomplete checks remain distinct`, () => {
    for (const [state, projected, en, zh] of [
      ['stale', 'stale', 'Outdated', '已过期'],
      ['unknown', 'unknown', 'Unknown', '未知'],
      ['future-status', 'unknown', 'Unknown', '未知'],
      ['missing', 'missing', 'Missing', '缺少检查'],
      ['partial', 'partial', 'Incomplete', '未完成'],
      ['unverified', 'unverified', 'Unverified', '尚未核验'],
    ]) {
      const value = result([`check_${state}:validation`], [{ check_id: 'validation', status: state }]);
      const output = render(value, locale, true);
      assert.match(output, /<details[^>]*open/);
      assert.ok(output.includes(`<li data-state="${projected}">`));
      assert.ok(output.includes(`>${locale === 'en' ? en : zh}</span>`));
      assert.match(output, locale === 'en' ? /1 unresolved recorded checks/ : /1 项已记录检查待处理/);
      assert.match(output, locale === 'en' ? /1 unmet completion conditions/ : /1 项完成条件未满足/);
      assert.doesNotMatch(output, /data-state="passed"|copy.deliveryEvidence/);
    }
  });

  test(`${locale}: unknown reasons and producer labels are escaped plain text`, () => {
    const value = result(['future_reason:<script>alert(1)</script>:v2', 'check_failed:<b>producer</b>'],
      [{ check_id: 'check', status: 'failed', label: '<img src=x onerror=alert(1)>', message: '<iframe>unsafe</iframe>' }]);
    const output = render(value, locale);
    assert.ok(output.includes('future_reason:&lt;script&gt;alert(1)&lt;/script&gt;:v2'));
    assert.ok(output.includes('&lt;b&gt;producer&lt;/b&gt;'));
    assert.ok(output.includes('&lt;img src=x onerror=alert(1)&gt;'));
    assert.ok(output.includes('&lt;iframe&gt;unsafe&lt;/iframe&gt;'));
    assert.doesNotMatch(output, /<script|<img|<iframe|<b>|copy.deliveryEvidence/);
    assert.equal(deliveryEvidence(value).unresolved, 1);
    assert.equal(deliveryEvidence(value).completionReasons.length, 2);
  });

  test(`${locale}: unmet conditions cannot be green even with contradictory declared completion`, () => {
    const value = result(['producer_evidence_missing'], [{ check_id: 'check', status: 'passed' }]);
    value.delivery.completion.allowed = true;
    value.delivery.completion.status = 'complete';
    const output = render(value, locale);
    assert.match(output, /data-testid="delivery-evidence" data-state="pending"/);
    assert.match(output, locale === 'en' ? /1 passed/ : /1 项通过/);
    assert.doesNotMatch(output, /Recorded checks passed|已记录的检查通过/);
  });

  test(`${locale}: absent diagnostics never fabricate a check or success`, () => {
    const value = result();
    delete value.delivery.completion.reasons;
    const output = render(value, locale);
    assert.match(output, /data-state="pending"/);
    assert.doesNotMatch(output, /old-pass|Recorded checks passed|已记录的检查通过|unmet completion conditions|项完成条件未满足/);
    assert.equal(deliveryEvidence(value).checks.length, 0);
  });

  test(`${locale}: legacy history preserves its receipts without synthetic contract conditions`, () => {
    const value = result();
    delete value.delivery;
    const output = render(value, locale);
    assert.match(output, /data-state="passed"/);
    assert.match(output, /old-pass/);
    assert.doesNotMatch(output, /unmet completion conditions|项完成条件未满足/);
    value.evidence = { artifacts: { tests_run: [{ command: 'legacy-test', exit_code: 1 }] } };
    assert.match(render(value, locale), /data-state="failed"/);
    value.evidence = {};
    assert.match(render(value, locale), /data-state="missing"/);
  });
}
